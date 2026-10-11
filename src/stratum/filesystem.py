"""Filesystem layer + Git-Vault (WP-04).

Owns the L0-L2 file vault described in ``MASTER-PLAN.md`` §5.3:

* **URI <-> path** mapping: ``stratum://<bereich>/<tenant>/<pfad>`` becomes
  ``<kb_path>/<bereich>/<tenant>/<pfad>.md`` (1:1, no escaping),
* **frontmatter documents**: each file is Markdown with a YAML frontmatter block
  carrying ``uri``, ``tenant``, ``bereich``, ``title``, ``l0``, ``l1``, ``tags``
  and ``updated_at``,
* **L0/L1 derivation**: :func:`make_l0` (<= 280 chars) and :func:`make_l1`
  (<= 200 words) are pure, LLM-free text reductions of the body,
* **Git-Vault**: :func:`git_init` and :func:`git_commit` version every write;
  the resulting commit hash is recorded in ``files.git_commit``,
* **index tables**: :meth:`FilesystemStore.write` maintains the ``files`` index
  row *only*. ``memory_events`` is deliberately **not** written here; that audit
  / event stream is owned by the Curator / promotion layer described in
  ``MASTER-PLAN.md`` §8 (a ``write`` is a raw file landing, not a memory
  event).

Design rules:

* Every database access goes through an injectable ``conn_factory`` whose
  signature mirrors :func:`stratum.db.connection`; the default is the real,
  tenant-scoped RLS connection. Tests inject an in-memory fake, so this module
  stays exercisable without Postgres.
* No third-party YAML/frontmatter dependency is imported at runtime. Each
  frontmatter value is serialised as a JSON literal, which is also a valid YAML
  1.2 scalar/flow value (YAML 1.2 is a superset of JSON) and therefore remains
  readable by PyYAML in a fully provisioned deployment.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import Settings, get_settings
from .db import connection as _default_connection
from .uri import parse_uri

__all__ = [
    "L0_MAX_CHARS",
    "L1_MAX_WORDS",
    "FilesystemStore",
    "git_commit",
    "git_init",
    "make_l0",
    "make_l1",
]

#: Hard upper bound for a generated L0 abstraction (characters).
L0_MAX_CHARS = 280

#: Hard upper bound for a generated L1 short summary (words).
L1_MAX_WORDS = 200

#: Columns selected by :meth:`FilesystemStore._get_file`, in order.
_FILE_COLUMNS = (
    "uri",
    "path",
    "content_hash",
    "l0",
    "l1",
    "size_bytes",
    "git_commit",
    "updated_at",
)

_VALID_LEVELS = ("l0", "l1", "l2")

_WS_RE = re.compile(r"\s+")
_CODE_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_HR_RE = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s*")
_LIST_RE = re.compile(r"^\s*([-*+]|\d+[.)])\s+")
_BLOCKQUOTE_RE = re.compile(r"^\s*>+\s?")
_IMG_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")


# --------------------------------------------------------------------- pure L0/L1


def _clean_line(line: str) -> str:
    """Reduce one Markdown line to plain prose (headings, lists, links)."""
    line = line.rstrip()
    if not line.strip():
        return ""
    if _HR_RE.match(line):
        return ""
    line = _BLOCKQUOTE_RE.sub("", line)
    line = _HEADING_RE.sub("", line)
    line = _LIST_RE.sub("", line)
    line = _IMG_RE.sub("", line)
    line = _LINK_RE.sub(r"\1", line)
    line = line.replace("`", "")
    line = line.replace("**", "").replace("__", "")
    line = line.replace("*", "")
    return _WS_RE.sub(" ", line).strip()


def _meaningful_lines(text: str) -> list[str]:
    """Return the cleaned, non-empty prose lines of ``text``.

    Fenced code blocks (``` / ~~~) are skipped entirely — code is not prose and
    must never leak into an L0/L1 summary.
    """
    lines: list[str] = []
    in_fence = False
    for raw in text.splitlines():
        if _CODE_FENCE_RE.match(raw):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        cleaned = _clean_line(raw)
        if cleaned:
            lines.append(cleaned)
    return lines


def _paragraphs(text: str) -> list[str]:
    """Return cleaned paragraphs (blank-line separated) with code blocks removed."""
    paragraphs: list[str] = []
    buffer: list[str] = []
    in_fence = False
    for raw in text.splitlines():
        if _CODE_FENCE_RE.match(raw):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if not raw.strip():
            if buffer:
                paragraphs.append(" ".join(buffer))
                buffer = []
            continue
        cleaned = _clean_line(raw)
        if cleaned:
            buffer.append(cleaned)
    if buffer:
        paragraphs.append(" ".join(buffer))
    return paragraphs


def _truncate(text: str, limit: int) -> str:
    """Truncate ``text`` to at most ``limit`` characters (ellipsis included)."""
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "\u2026"


def make_l0(body: str) -> str:
    """Return a one-line L0 abstraction of ``body`` (<= 280 characters).

    The first meaningful line is used: frontmatter, blank lines, code fences and
    horizontal rules are skipped, and Markdown decoration is stripped. The result
    is truncated to :data:`L0_MAX_CHARS` characters.
    """
    _, text = _split_frontmatter(body)
    lines = _meaningful_lines(text)
    if not lines:
        return ""
    return _truncate(lines[0], L0_MAX_CHARS)


def make_l1(body: str) -> str:
    """Return a short L1 summary of ``body`` (<= 200 words).

    The leading paragraphs are extracted without an LLM and truncated to
    :data:`L1_MAX_WORDS` words. Structure (headings, lists, code) is flattened to
    plain prose.
    """
    _, text = _split_frontmatter(body)
    words: list[str] = []
    for paragraph in _paragraphs(text):
        words.extend(paragraph.split())
        if len(words) >= L1_MAX_WORDS:
            break
    return " ".join(words[:L1_MAX_WORDS])


# --------------------------------------------------- frontmatter (dependency-free)


def _render_frontmatter(fields: Mapping[str, Any]) -> str:
    """Serialise ``fields`` as a YAML frontmatter block.

    Every value is emitted as a JSON literal, which is simultaneously a valid
    YAML 1.2 scalar or flow collection, so the block round-trips through both the
    local parser below and a full YAML parser such as PyYAML.
    """
    lines = ["---"]
    for key, value in fields.items():
        lines.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
    lines.append("---")
    return "\n".join(lines)


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split ``text`` into ``(frontmatter, body)``.

    A frontmatter block is delimited by a leading ``---`` line and the next
    ``---``/``...`` line. Values are parsed with :func:`json.loads`; anything
    that is not valid JSON is kept as a raw string. When no block is present the
    frontmatter is empty and the whole input is returned as the body.
    """
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, text
    end: int | None = None
    for index in range(1, len(lines)):
        if lines[index].strip() in ("---", "..."):
            end = index
            break
    if end is None:
        return {}, text

    fields: dict[str, Any] = {}
    for raw in lines[1:end]:
        if not raw.strip():
            continue
        key, sep, value = raw.partition(":")
        if not sep:
            continue
        key = key.strip()
        value = value.strip()
        try:
            fields[key] = json.loads(value)
        except (ValueError, TypeError):
            fields[key] = value
    return fields, "\n".join(lines[end + 1 :])


def _render_document(fields: Mapping[str, Any], body: str) -> str:
    """Render the complete Markdown document (frontmatter + body)."""
    if not body.endswith("\n"):
        body = body + "\n"
    return _render_frontmatter(fields) + "\n" + body


def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string with a ``Z`` suffix."""
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _escape_like(value: str) -> str:
    r"""Escape ``LIKE`` metacharacters (``\``, ``%``, ``_``) in ``value``.

    Used together with a ``LIKE %s ESCAPE '\'`` clause so a literal ``_`` in a
    prefix (e.g. a filename segment) is matched literally instead of acting as
    the single-character wildcard.
    """
    return (
        value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )


def _row_to_dict(row: Any) -> dict[str, Any] | None:
    """Normalise a row (dict_row mapping or positional sequence) to a dict."""
    if row is None:
        return None
    if isinstance(row, Mapping):
        return dict(row)
    return {name: row[index] for index, name in enumerate(_FILE_COLUMNS)}


# ------------------------------------------------------------------------- Git-Vault


def _git(kb_path: Path, *args: str, text: bool = False) -> subprocess.CompletedProcess:
    """Run a git subcommand inside ``kb_path`` (raising on a non-zero exit)."""
    return subprocess.run(
        ["git", *args],
        cwd=str(kb_path),
        check=True,
        capture_output=True,
        text=text,
    )


def git_init(kb_path: str | Path) -> Path:
    """Initialise (or re-initialise) a git repository at ``kb_path``.

    Idempotent: running it on an existing repository is a no-op. The directory is
    created when missing.
    """
    kb = Path(kb_path)
    kb.mkdir(parents=True, exist_ok=True)
    _git(kb, "init")
    return kb


def _ensure_repo(kb_path: Path) -> None:
    """Initialise ``kb_path`` as a git repository if it is not one yet."""
    if not (kb_path / ".git").exists():
        _git(kb_path, "init")


def git_commit(kb_path: str | Path, message: str, author: str = "stratum") -> str | None:
    """Stage everything under ``kb_path`` and create a commit.

    Args:
        kb_path: Git-Vault root (initialised on demand).
        message: Commit message.
        author: Committer identity; also used to synthesise an email so commits
            work without a global git identity.

    Returns:
        The new commit hash, or ``None`` when there was nothing to commit
        (detected via ``git status --porcelain``).
    """
    kb = Path(kb_path)
    kb.mkdir(parents=True, exist_ok=True)
    _ensure_repo(kb)

    _git(kb, "add", "-A")
    status = _git(kb, "status", "--porcelain", text=True).stdout
    if not status.strip():
        return None

    _git(
        kb,
        "-c",
        f"user.name={author}",
        "-c",
        f"user.email={author}@stratum.local",
        "commit",
        "-m",
        message,
    )
    return _git(kb, "rev-parse", "HEAD", text=True).stdout.strip()


# ------------------------------------------------------------------- FilesystemStore


class FilesystemStore:
    """Tenant-scoped Markdown vault with Git versioning and a ``files`` index.

    Args:
        settings: Configuration source; defaults to :func:`get_settings`. The
            vault root is ``settings.kb_path``.
        conn_factory: Callable with the signature of
            :func:`stratum.db.connection` returning a context manager that yields
            a DB connection. Defaults to the real tenant-scoped connection; tests
            inject an in-memory fake.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        conn_factory: Callable[..., AbstractContextManager[Any]] | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.kb_path = Path(self.settings.kb_path)
        self._conn_factory = conn_factory or _default_connection

    # ---------------------------------------------------------------- URI <-> path

    def path_for(self, uri: str) -> Path:
        """Map ``uri`` to its on-disk Markdown path (``1:1`` segment mapping).

        ``stratum://docs/default/projects/stratum`` maps to
        ``<kb_path>/docs/default/projects/stratum.md``. ``.md`` is *appended* to
        the final segment, so a segment that already contains dots (e.g.
        ``report.v2``) keeps them and never collides with ``report.v1``.
        """
        parsed = parse_uri(uri)
        parts = parsed.path.split("/")
        parent = Path(*parts[:-1]) if len(parts) > 1 else Path()
        filename = parts[-1] + ".md"
        return self.kb_path / parsed.bereich / parsed.tenant / parent / filename

    def _relative_path(self, path: Path) -> str:
        """Return ``path`` relative to the vault root in POSIX form."""
        return path.relative_to(self.kb_path).as_posix()

    def _git_head(self) -> str | None:
        """Return the current ``HEAD`` commit hash, or ``None`` on failure."""
        try:
            return _git(self.kb_path, "rev-parse", "HEAD", text=True).stdout.strip() or None
        except subprocess.CalledProcessError:
            return None

    # ----------------------------------------------------------------------- write

    def write(
        self,
        uri: str,
        content: str,
        *,
        tenant: str,
        metadata: Mapping[str, Any] | None = None,
        commit: bool = True,
    ) -> dict[str, Any]:
        """Write a Markdown document with frontmatter and index it in ``files``.

        Args:
            uri: Target ``stratum://`` URI.
            content: Markdown body (an existing frontmatter block is stripped and
                regenerated).
            tenant: Tenant id used for the RLS-scoped DB access; must match the
                tenant encoded in ``uri``.
            metadata: Optional extra frontmatter inputs (``title``, ``tags``).
            commit: When ``True`` (default) the vault is committed and the hash is
                recorded in ``files.git_commit``.

        Returns:
            ``{"uri", "path", "l0", "l1", "content_hash", "git_commit"}`` where
            ``path`` is vault-relative POSIX.
        """
        parsed = parse_uri(uri)
        if parsed.tenant != tenant:
            raise ValueError(
                f"tenant mismatch: uri tenant {parsed.tenant!r} != tenant {tenant!r}"
            )

        _, body = _split_frontmatter(content)
        if not body.endswith("\n"):
            body = body + "\n"
        content_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
        l0 = make_l0(body)
        l1 = make_l1(body)

        meta = dict(metadata or {})
        title = str(meta.get("title") or parsed.path.rsplit("/", 1)[-1])
        raw_tags = meta.get("tags")
        tags = [str(tag) for tag in raw_tags] if isinstance(raw_tags, (list, tuple)) else []
        updated_at = _now_iso()

        fields = {
            "uri": uri,
            "tenant": tenant,
            "bereich": parsed.bereich,
            "title": title,
            "l0": l0,
            "l1": l1,
            "tags": tags,
            "updated_at": updated_at,
        }

        path = self.path_for(uri)
        document = _render_document(fields, body)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(document, encoding="utf-8")

        git_commit_hash: str | None = None
        if commit:
            git_commit_hash = git_commit(self.kb_path, f"write: {uri}", author="stratum")
            if git_commit_hash is None:
                # Clean tree: nothing new to commit (identical body). Fall back to
                # the current HEAD so a valid ``files.git_commit`` is never
                # overwritten with NULL.
                git_commit_hash = self._git_head()

        rel_path = self._relative_path(path)
        self._upsert_file(
            tenant,
            uri=uri,
            rel_path=rel_path,
            content_hash=content_hash,
            l0=l0,
            l1=l1,
            size_bytes=len(document.encode("utf-8")),
            git_commit_hash=git_commit_hash,
            updated_at=updated_at,
        )

        return {
            "uri": uri,
            "path": rel_path,
            "l0": l0,
            "l1": l1,
            "content_hash": content_hash,
            "git_commit": git_commit_hash,
        }

    # ------------------------------------------------------------------------ read

    def read(self, uri: str, *, level: str = "l1", tenant: str) -> dict[str, Any]:
        """Read a document, returning content at the requested ``level``.

        ``level`` selects the returned ``content``:

        * ``"l0"`` — the L0 abstraction,
        * ``"l1"`` — the L1 summary (default),
        * ``"l2"`` — the full Markdown body.

        ``metadata`` merges the file frontmatter with the ``files`` index row.
        Raises :class:`FileNotFoundError` when the file is absent.
        """
        if level not in _VALID_LEVELS:
            raise ValueError(f"invalid level {level!r}; allowed: {_VALID_LEVELS}")
        parse_uri(uri)
        path = self.path_for(uri)
        if not path.is_file():
            raise FileNotFoundError(f"no file for {uri!r} at {path}")

        frontmatter, body = _split_frontmatter(path.read_text(encoding="utf-8"))
        row = self._get_file(tenant, uri)

        l0 = frontmatter.get("l0") or (row or {}).get("l0") or ""
        l1 = frontmatter.get("l1") or (row or {}).get("l1") or ""

        if level == "l0":
            content = l0
        elif level == "l1":
            content = l1
        else:
            content = body

        metadata: dict[str, Any] = dict(frontmatter)
        if row:
            metadata.update(row)

        return {
            "uri": uri,
            "level": level,
            "content": content,
            "l0": l0,
            "l1": l1,
            "path": self._relative_path(path),
            "metadata": metadata,
        }

    # ---------------------------------------------------------------------- delete

    def delete(self, uri: str, *, tenant: str, commit: bool = True) -> str | None:
        """Remove the vault file and its ``files`` index row for ``uri``.

        This is the **hard-forget** removal hook: it unlinks the on-disk Markdown
        file behind :meth:`path_for`, drops the matching ``files`` row, and (by
        default) records the removal in the Git-Vault with a ``forget: <uri>``
        commit. It is the method :func:`stratum.mcp.tools.stratum_forget` calls
        when ``hard=True`` (the soft path leaves the file in place and only drops
        index/derived rows elsewhere).

        The operation is idempotent: when no file exists for ``uri`` it returns
        ``None`` without touching the vault or the database, so calling it twice
        is safe.

        Args:
            uri: Target ``stratum://`` URI.
            tenant: Tenant id used for the RLS-scoped DB access; must match the
                tenant encoded in ``uri``.
            commit: When ``True`` (default) a ``forget: <uri>`` commit is created
                and its hash returned. When ``False`` the file and row are still
                removed, but no commit is made and ``None`` is returned.

        Returns:
            The new commit hash (a ``forget: <uri>`` commit), or the current
            ``HEAD`` when the tree was already clean, or ``None`` when the file
            did not exist or ``commit`` is ``False``.
        """
        parsed = parse_uri(uri)
        if parsed.tenant != tenant:
            raise ValueError(
                f"tenant mismatch: uri tenant {parsed.tenant!r} != tenant {tenant!r}"
            )

        path = self.path_for(uri)
        if not path.is_file():
            return None

        path.unlink()
        self._delete_file(tenant, uri)

        if not commit:
            return None

        commit_hash = git_commit(self.kb_path, f"forget: {uri}", author="stratum")
        if commit_hash is None:
            # Clean tree: the removal produced nothing new to commit. Fall back to
            # the current HEAD so callers still get a valid hash (never NULL), as
            # ``write`` does.
            commit_hash = self._git_head()
        return commit_hash

    # ---------------------------------------------------------------------- exists

    def exists(self, uri: str, *, tenant: str) -> bool:
        """Return ``True`` when the URI is indexed in ``files`` or on disk."""
        if self._get_file(tenant, uri) is not None:
            return True
        return self.path_for(uri).is_file()

    # ------------------------------------------------------------------------ list

    def list(self, prefix_uri: str, *, tenant: str, recursive: bool = True) -> list[str]:
        """List tenant URIs indexed under ``prefix_uri``.

        ``prefix_uri`` is treated as a path-segment prefix. When ``recursive`` is
        ``False`` only direct children (one segment below the prefix) are
        returned. Results are sorted lexicographically.
        """
        prefix = prefix_uri.rstrip("/")
        escaped = _escape_like(prefix)
        pattern = escaped + "/%"
        with self._conn_factory(tenant) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT uri FROM files WHERE tenant_id = %s "
                "AND uri LIKE %s ESCAPE '\\' ORDER BY uri",
                (tenant, pattern),
            )
            rows = cur.fetchall()

        uris: list[str] = []
        for row in rows:
            uri = row["uri"] if isinstance(row, Mapping) else row[0]
            remaining = str(uri)[len(prefix) + 1 :]
            if recursive or "/" not in remaining:
                uris.append(str(uri))
        return sorted(uris)

    # ---------------------------------------------------------------- DB primitives

    def _upsert_file(
        self,
        tenant: str,
        *,
        uri: str,
        rel_path: str,
        content_hash: str,
        l0: str,
        l1: str,
        size_bytes: int,
        git_commit_hash: str | None,
        updated_at: str,
    ) -> None:
        """Insert or update the ``files`` row for ``uri``."""
        with self._conn_factory(tenant) as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO files "
                "(tenant_id, uri, path, content_hash, l0, l1, size_bytes, git_commit, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (tenant_id, uri) DO UPDATE SET "
                "path = EXCLUDED.path, content_hash = EXCLUDED.content_hash, "
                "l0 = EXCLUDED.l0, l1 = EXCLUDED.l1, size_bytes = EXCLUDED.size_bytes, "
                "git_commit = EXCLUDED.git_commit, updated_at = EXCLUDED.updated_at",
                (
                    tenant,
                    uri,
                    rel_path,
                    content_hash,
                    l0,
                    l1,
                    size_bytes,
                    git_commit_hash,
                    updated_at,
                ),
            )
            conn.commit()

    def _get_file(self, tenant: str, uri: str) -> dict[str, Any] | None:
        """Return the ``files`` index row for ``uri``, or ``None``."""
        with self._conn_factory(tenant) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT uri, path, content_hash, l0, l1, size_bytes, git_commit, updated_at "
                "FROM files WHERE tenant_id = %s AND uri = %s",
                (tenant, uri),
            )
            row = cur.fetchone()
        return _row_to_dict(row)

    def _delete_file(self, tenant: str, uri: str) -> None:
        """Delete the ``files`` index row for ``uri`` (idempotent)."""
        with self._conn_factory(tenant) as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM files WHERE tenant_id = %s AND uri = %s",
                (tenant, uri),
            )
            conn.commit()
