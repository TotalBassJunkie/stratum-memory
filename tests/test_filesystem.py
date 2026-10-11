"""Tests for the filesystem layer + Git-Vault (WP-04).

These tests need neither Postgres nor a YAML library:

* the DB is replaced by :class:`FakeConnFactory`, a dict-backed ``files`` table
  implementing just the operations the store uses (set-based ``LIKE`` for
  :meth:`FilesystemStore.list`, equality lookup for ``_get_file``, and an
  upsert on ``(tenant_id, uri)``);
* the vault root is ``tmp_path`` (``Settings(kb_path=str(tmp_path))``);
* the Git-Vault tests drive a real ``git init``/``commit`` inside ``tmp_path``.
"""

from __future__ import annotations

import re
import subprocess
from contextlib import contextmanager
from pathlib import Path

import pytest

from stratum.config import Settings
from stratum.filesystem import (
    FilesystemStore,
    git_commit,
    git_init,
    make_l0,
    make_l1,
)

pytestmark = pytest.mark.skipif(
    subprocess.run(["git", "--version"], capture_output=True).returncode != 0,
    reason="git is not available on PATH",
)


# ------------------------------------------------------------------ fake database


def _like_match(text: str, pattern: str) -> bool:
    r"""Emulate SQL ``LIKE ... ESCAPE '\'`` (``%`` = any run, ``_`` = one char).

    The fake must honour the escape clause so a literal ``_`` that reaches the
    database *unescaped* really does act as the single-character wildcard — that
    is exactly the failure :meth:`FilesystemStore.list` must avoid.
    """
    regex = ["^"]
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "\\" and index + 1 < len(pattern):
            regex.append(re.escape(pattern[index + 1]))
            index += 2
            continue
        if char == "%":
            regex.append(".*")
        elif char == "_":
            regex.append(".")
        else:
            regex.append(re.escape(char))
        index += 1
    regex.append("$")
    return re.match("".join(regex), text, re.DOTALL) is not None


class _FakeCursor:
    """Minimal cursor emulating the ``files`` upsert/lookup/list statements."""

    def __init__(self, table: dict[tuple[str, str], dict[str, object]]) -> None:
        self._table = table
        self._result: list[dict[str, object]] = []

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute(self, sql: str, params: tuple = ()) -> None:
        normalized = re.sub(r"\s+", " ", sql).strip()
        if normalized.startswith("INSERT INTO files"):
            tenant, uri, path, content_hash, l0, l1, size_bytes, git_commit_hash, updated = (
                params
            )
            self._table[(tenant, uri)] = {
                "uri": uri,
                "path": path,
                "content_hash": content_hash,
                "l0": l0,
                "l1": l1,
                "size_bytes": size_bytes,
                "git_commit": git_commit_hash,
                "updated_at": updated,
            }
        elif normalized.startswith("DELETE FROM files"):
            (tenant, uri) = params
            self._table.pop((tenant, uri), None)
        elif normalized.startswith("SELECT") and "FROM files" in normalized:
            tenant, uri_or_prefix = params
            if " AND uri = " in normalized:
                row = self._table.get((tenant, uri_or_prefix))
                self._result = [dict(row)] if row is not None else []
            else:
                pattern = str(uri_or_prefix)
                rows = [
                    dict(row)
                    for (t, u), row in self._table.items()
                    if t == tenant and _like_match(u, pattern)
                ]
                self._result = sorted(rows, key=lambda r: r["uri"])
        else:  # pragma: no cover - guards against an unexpected statement shape
            raise AssertionError(f"unsupported SQL: {normalized}")

    def fetchone(self) -> dict[str, object] | None:
        return self._result[0] if self._result else None

    def fetchall(self) -> list[dict[str, object]]:
        return list(self._result)


class _FakeConn:
    def __init__(self, table: dict[tuple[str, str], dict[str, object]]) -> None:
        self._table = table

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self._table)

    def commit(self) -> None:  # no-op for the in-memory table
        return None


class FakeConnFactory:
    """In-memory stand-in for :func:`stratum.db.connection`."""

    def __init__(self) -> None:
        self.table: dict[tuple[str, str], dict[str, object]] = {}
        self.tenants: list[str | None] = []

    @contextmanager
    def __call__(self, tenant: str | None = None, **_kwargs: object):
        self.tenants.append(tenant)
        yield _FakeConn(self.table)


# ------------------------------------------------------------------- helpers


def _make_store(tmp_path: Path) -> tuple[FilesystemStore, FakeConnFactory]:
    fake = FakeConnFactory()
    settings = Settings(kb_path=str(tmp_path), db_password=None, db_password_file=None)
    return FilesystemStore(settings, conn_factory=fake), fake


REPO_ROOT = Path(__file__).resolve().parents[1]
V1_URI = "stratum://docs/default/projects/stratum/architecture"
V2_URI = "stratum://docs/default/projects/stratum/roadmap"

# Multi-paragraph body: L0 must be the first meaningful line, L1 a summary.
V1_BODY = (
    "# Stratum-Architektur\n"
    "\n"
    "Stratum-Memory vereint vier Speicherebenen hinter einer URI.\n"
    "Die Ingestion laeuft als Extract-Cognify-Load-Pipeline.\n"
    "\n"
    "## Details\n"
    "\n"
    "Der Curator prueft jeden Kandidaten vor der Promotion.\n"
)


# ------------------------------------------------------------------- (d) path_for


def test_path_for_maps_uri_one_to_one(tmp_path):
    store, _ = _make_store(tmp_path)
    assert store.path_for(V1_URI) == tmp_path / "docs" / "default" / "projects" / "stratum" / "architecture.md"
    assert store.path_for("stratum://facts/acme/customer/42/plan-pro") == (
        tmp_path / "facts" / "acme" / "customer" / "42" / "plan-pro.md"
    )


def test_path_for_keeps_dots_in_segments(tmp_path):
    # `.md` is appended, not substituted, so dotted segments stay 1:1 and
    # distinct (report.v1 vs report.v2 must not collide).
    store, _ = _make_store(tmp_path)
    assert store.path_for("stratum://docs/default/report.v1") != store.path_for(
        "stratum://docs/default/report.v2"
    )
    assert store.path_for("stratum://docs/default/report.v1").name == "report.v1.md"


def test_path_for_rejects_malformed_uri(tmp_path):
    store, _ = _make_store(tmp_path)
    from stratum.uri import UriError

    with pytest.raises(UriError):
        store.path_for("stratum://docs/default/../escape")


# ------------------------------------------------------------- (a) write + frontmatter


def test_write_creates_file_with_frontmatter(tmp_path):
    store, _ = _make_store(tmp_path)
    result = store.write(V1_URI, V1_BODY, tenant="default", metadata={"tags": ["memory"]})

    path = store.path_for(V1_URI)
    assert path.is_file(), "write must materialise the markdown file"
    assert result["uri"] == V1_URI
    assert result["path"] == "docs/default/projects/stratum/architecture.md"

    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    assert f'uri: "{V1_URI}"' in text
    assert 'tenant: "default"' in text
    assert 'bereich: "docs"' in text
    assert "l0: " in text and "l1: " in text
    assert 'tags: ["memory"]' in text
    # Regenerated frontmatter must not duplicate the title heading in the body.
    assert "title: " in text

    # The `files` index row must carry the same hash as the on-disk body.
    row = store._get_file("default", V1_URI)
    assert row is not None
    assert row["content_hash"] == result["content_hash"]
    assert row["path"] == result["path"]
    assert row["git_commit"] == result["git_commit"]


# ------------------------------------------------------------------- (b) read levels


def test_read_returns_distinct_l0_l1_l2(tmp_path):
    store, _ = _make_store(tmp_path)
    store.write(V1_URI, V1_BODY, tenant="default")

    l0 = store.read(V1_URI, level="l0", tenant="default")
    l1 = store.read(V1_URI, level="l1", tenant="default")
    l2 = store.read(V1_URI, level="l2", tenant="default")

    assert l0["content"] == "Stratum-Architektur"
    assert "Stratum-Memory vereint vier Speicherebenen" in l1["content"]
    # L2 is the full body and is strictly longer than either summary.
    assert "Der Curator prueft jeden Kandidaten" in l2["content"]
    assert len(l2["content"]) > len(l1["content"]) > len(l0["content"])
    assert l2["metadata"]["uri"] == V1_URI

    for level, out in (("l0", l0), ("l1", l1), ("l2", l2)):
        assert out["level"] == level


def test_read_missing_file_raises(tmp_path):
    store, _ = _make_store(tmp_path)
    with pytest.raises(FileNotFoundError):
        store.read(V1_URI, tenant="default")


def test_read_rejects_unknown_level(tmp_path):
    store, _ = _make_store(tmp_path)
    store.write(V1_URI, V1_BODY, tenant="default")
    with pytest.raises(ValueError):
        store.read(V1_URI, level="l3", tenant="default")


# ------------------------------------------------------------------ (c) stable hash


def test_content_hash_is_stable_for_identical_body(tmp_path):
    store, _ = _make_store(tmp_path)
    first = store.write(V1_URI, V1_BODY, tenant="default")
    second = store.write(V1_URI, V1_BODY, tenant="default")
    assert first["content_hash"] == second["content_hash"]

    # A different body yields a different hash.
    third = store.write(V1_URI, V1_BODY + "\nExtra paragraph.\n", tenant="default")
    assert third["content_hash"] != first["content_hash"]


def test_content_hash_normalised_body_same_file(tmp_path):
    # B4: a body with and without a trailing newline normalises to the same
    # document (``_render_document`` appends one), so the hash must also be
    # computed over the normalised body and the stored file must be identical.
    store, _ = _make_store(tmp_path)
    plain = store.write(V1_URI, "same text", tenant="default")
    with_newline = store.write(V1_URI, "same text\n", tenant="default")

    assert plain["content_hash"] == with_newline["content_hash"]

    # Body (the part hashed) is identical for both spellings; frontmatter carries
    # a fresh ``updated_at`` so compare the body rather than the whole file.
    body_a = store.read(V1_URI, level="l2", tenant="default")["content"]
    store.write(V1_URI, "same text\n", tenant="default")
    body_b = store.read(V1_URI, level="l2", tenant="default")["content"]
    assert body_a == body_b == "same text\n"


def test_write_twice_identical_body_keeps_git_commit(tmp_path, monkeypatch):
    # B1: the second, identical write produces no new git commit (clean tree).
    # The previously recorded hash must NOT be overwritten with NULL, and the
    # stored hash must equal HEAD. Freeze the clock so both documents are
    # byte-identical (only ``updated_at`` would otherwise differ).
    monkeypatch.setattr(
        "stratum.filesystem._now_iso", lambda: "2026-01-01T00:00:00Z"
    )
    store, _ = _make_store(tmp_path)
    first = store.write(V1_URI, V1_BODY, tenant="default")
    assert first["git_commit"] is not None

    second = store.write(V1_URI, V1_BODY, tenant="default")
    # Clean tree on the second write: no new commit, so the fallback HEAD is used.
    assert second["git_commit"] == first["git_commit"]

    row = store._get_file("default", V1_URI)
    assert row is not None
    assert row["git_commit"] is not None
    assert row["git_commit"] == first["git_commit"]

    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert row["git_commit"] == head


def test_list_escapes_like_wildcard_in_prefix(tmp_path):
    # B3: a literal ``_`` in the prefix must not be treated as the SQL LIKE
    # single-character wildcard. The fake cursor honours ESCAPE, so an
    # unescaped ``_`` would wrongly match sibling URIs and fail this test.
    store, _ = _make_store(tmp_path)
    underscore_uri = "stratum://docs/default/projects/stratum_a/note"
    sibling_uri = "stratum://docs/default/projects/stratum-a/note"
    store.write(underscore_uri, V1_BODY, tenant="default")
    store.write(sibling_uri, V1_BODY, tenant="default")

    listed = store.list("stratum://docs/default/projects/stratum_a", tenant="default")
    assert listed == [underscore_uri]


def test_content_hash_ignores_supplied_frontmatter(tmp_path):
    store, _ = _make_store(tmp_path)
    plain = store.write(V1_URI, V1_BODY, tenant="default")
    with_fm = store.write(
        V1_URI, "---\ntitle: x\n---\n" + V1_BODY, tenant="default"
    )
    # The hash covers the body only, so an existing frontmatter block is ignored.
    assert plain["content_hash"] == with_fm["content_hash"]


# ------------------------------------------------------------------- (f) L0 bound


def test_l0_within_280_chars(tmp_path):
    long_line = "Wort " * 200  # ~1000 chars, far beyond the L0 budget
    assert len(make_l0(long_line)) <= 280

    store, _ = _make_store(tmp_path)
    result = store.write(V1_URI, "# " + long_line, tenant="default")
    assert len(result["l0"]) <= 280


def test_l1_within_200_words():
    body = "word " * 500
    summary = make_l1(body)
    assert len(summary.split()) <= 200
    assert summary  # non-empty


def test_make_l0_skips_decorations():
    body = "```\ncode\n```\n\n---\n\n## Real heading\n\ntext\n"
    assert make_l0(body) == "Real heading"


# ------------------------------------------------------------------- extensions


def test_exists_indexed_and_missing(tmp_path):
    store, _ = _make_store(tmp_path)
    assert store.exists(V1_URI, tenant="default") is False
    store.write(V1_URI, V1_BODY, tenant="default")
    assert store.exists(V1_URI, tenant="default") is True


def test_list_prefix_recursive_and_shallow(tmp_path):
    store, _ = _make_store(tmp_path)
    store.write(V1_URI, V1_BODY, tenant="default")
    store.write(V2_URI, V1_BODY, tenant="default")
    store.write(
        "stratum://docs/default/projects/stratum/sub/deep", V1_BODY, tenant="default"
    )
    store.write("stratum://docs/default/other", V1_BODY, tenant="default")

    base = "stratum://docs/default/projects/stratum"
    recursive = store.list(base, tenant="default")
    assert V1_URI in recursive and V2_URI in recursive
    assert "stratum://docs/default/projects/stratum/sub/deep" in recursive

    shallow = store.list(base, tenant="default", recursive=False)
    assert set(shallow) == {V1_URI, V2_URI}

    # Prefix scoping excludes siblings outside the prefix.
    assert "stratum://docs/default/other" not in recursive


def test_write_tenant_mismatch_raises(tmp_path):
    store, _ = _make_store(tmp_path)
    with pytest.raises(ValueError):
        store.write(V1_URI, V1_BODY, tenant="other")


# ------------------------------------------------------------------- (e) Git-Vault


def test_git_init_and_commit_produce_real_commit(tmp_path):
    # Fresh vault: initialise, write, commit.
    git_init(tmp_path)
    assert (tmp_path / ".git").is_dir()

    (tmp_path / "note.md").write_text("hello vault\n", encoding="utf-8")
    commit_hash = git_commit(tmp_path, "wp-04: initial", author="stratum")

    assert commit_hash is not None
    assert re.fullmatch(r"[0-9a-f]{40}", commit_hash)

    # The hash is real: git recognises it and it is HEAD.
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert head == commit_hash

    # With a clean tree there is nothing to commit.
    assert git_commit(tmp_path, "no-op") is None

    # A follow-up change produces a new distinct commit.
    (tmp_path / "note.md").write_text("hello again\n", encoding="utf-8")
    next_hash = git_commit(tmp_path, "update")
    assert next_hash is not None and next_hash != commit_hash


def test_write_commits_by_default_and_records_hash(tmp_path):
    result = _make_store(tmp_path)[0].write(V1_URI, V1_BODY, tenant="default")
    assert result["git_commit"] is not None

    log = subprocess.run(
        ["git", "log", "--oneline"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "write: " + V1_URI in log


def test_write_can_skip_commit(tmp_path):
    store, _ = _make_store(tmp_path)
    result = store.write(V1_URI, V1_BODY, tenant="default", commit=False)
    assert result["git_commit"] is None
    assert store.path_for(V1_URI).is_file()


# -------------------------------------------------------------- (g) delete/forget


def test_delete_removes_file_row_and_commits(tmp_path):
    store, _ = _make_store(tmp_path)
    store.write(V1_URI, V1_BODY, tenant="default")
    assert store.path_for(V1_URI).is_file()
    assert store._get_file("default", V1_URI) is not None

    commit_hash = store.delete(V1_URI, tenant="default")

    # A real Git commit for the removal is produced and returned.
    assert commit_hash is not None
    assert re.fullmatch(r"[0-9a-f]{40}", commit_hash)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert head == commit_hash

    log = subprocess.run(
        ["git", "log", "--oneline"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "forget: " + V1_URI in log

    # The file and its `files` index row are both gone.
    assert not store.path_for(V1_URI).exists()
    assert store._get_file("default", V1_URI) is None


def test_delete_missing_uri_returns_none(tmp_path):
    store, _ = _make_store(tmp_path)
    assert store.delete(V1_URI, tenant="default") is None


def test_delete_is_idempotent(tmp_path):
    store, _ = _make_store(tmp_path)
    store.write(V1_URI, V1_BODY, tenant="default")

    first = store.delete(V1_URI, tenant="default")
    assert first is not None

    # Second delete: the file is already gone, so it is a no-op returning None
    # without raising.
    assert store.delete(V1_URI, tenant="default") is None

