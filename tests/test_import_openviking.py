"""DB-free tests for the OpenViking bulk importer's pure helpers (B4/B5/B13).

Nothing here touches the network, OpenViking or Postgres: only the standalone
helpers of ``scripts/import_openviking.py`` are exercised — the segment
sanitiser, the target-URI builder (incl. its length/charset validation) and the
checkpoint state file. The module is imported by path because ``scripts/`` is
not an installed package.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from stratum.uri import UriError, parse_uri

REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT_PATH = REPO_ROOT / "scripts" / "import_openviking.py"


def _load_module():
    """Import ``scripts/import_openviking.py`` by file path (no package)."""
    spec = importlib.util.spec_from_file_location("import_openviking", _SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["import_openviking"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def importer():
    return _load_module()


# --------------------------------------------------------------------------- #
# _sanitize_segment — Unicode must be transliterated, never leaked (B4)
# --------------------------------------------------------------------------- #


def test_sanitize_segment_folds_unicode_letters(importer):
    """``isalnum()`` is True for Unicode; the slug must stay ASCII anyway."""
    assert importer._sanitize_segment("Über") == "uber"
    assert importer._sanitize_segment("café") == "cafe"
    # A CJK string has no ASCII base letter: it degrades to the fallback.
    assert importer._sanitize_segment("漢字") == "x"


def test_sanitize_segment_replaces_spaces_and_specials(importer):
    assert importer._sanitize_segment("My File Name.md") == "my-file-name.md"
    assert importer._sanitize_segment("a  b") == "a-b"


def test_sanitize_segment_keeps_allowed_chars(importer):
    assert importer._sanitize_segment("note_2026.v1~x-y") == "note_2026.v1~x-y"


def test_sanitize_segment_empty_uses_fallback(importer):
    assert importer._sanitize_segment("   ") == "x"
    assert importer._sanitize_segment("---") == "x"


def test_sanitize_segment_truncates(importer):
    slug = importer._sanitize_segment("a" * 500)
    assert len(slug) == 120
    assert slug == "a" * 120


def test_sanitize_segment_output_matches_uri_grammar(importer):
    """Every produced segment must satisfy the real Stratum segment regex."""
    import re

    seg_re = re.compile(r"^[a-z0-9._~-]+$")
    for raw in ("Über", "漢字", "My File.md", "a/b", "***", "Näme (v2)"):
        assert seg_re.match(importer._sanitize_segment(raw))


# --------------------------------------------------------------------------- #
# _target_uri — must yield a *valid* stratum URI or raise (B4)
# --------------------------------------------------------------------------- #


def test_target_uri_maps_viking_path(importer):
    uri = importer._target_uri("example", "viking://user/example/memories/notes/Ada.md")
    assert uri == "stratum://docs/example/openviking/memories/notes/ada.md"
    # And it round-trips through the real parser.
    assert parse_uri(uri).tenant == "example"


def test_target_uri_sanitizes_unicode_path_segments(importer):
    uri = importer._target_uri("example", "viking://user/example/memories/Über Café.md")
    assert uri == "stratum://docs/example/openviking/memories/uber-cafe.md"
    parse_uri(uri)  # must not raise


def test_target_uri_rejects_over_long_uri(importer):
    """A path so long the URI exceeds the 255-char ceiling must raise (B4)."""
    deep = "/".join(["segment"] * 40)
    with pytest.raises(importer.UriBuildError):
        importer._target_uri("example", f"viking://user/example/{deep}")


def test_target_uri_empty_path_uses_root(importer):
    uri = importer._target_uri("example", "viking://user/example")
    assert uri == "stratum://docs/example/openviking/root"
    parse_uri(uri)


# --------------------------------------------------------------------------- #
# _load_state / _save_state — resumable checkpoint (no network)
# --------------------------------------------------------------------------- #


def test_load_state_missing_file_is_empty(importer, tmp_path: Path):
    assert importer._load_state(tmp_path / "nope.state") == set()


def test_save_then_load_roundtrip(importer, tmp_path: Path):
    path = tmp_path / "var" / "import.state"  # parent must be auto-created
    importer._save_state(path, {"viking://user/example/a", "viking://user/example/b"})
    assert path.is_file()
    assert importer._load_state(path) == {
        "viking://user/example/a",
        "viking://user/example/b",
    }


def test_save_state_is_atomic_no_tmp_left(importer, tmp_path: Path):
    path = tmp_path / "import.state"
    importer._save_state(path, {"x"})
    assert not path.with_suffix(".tmp").exists()


# --------------------------------------------------------------------------- #
# _target_uri error type is exposed for the caller's skip logic
# --------------------------------------------------------------------------- #


def test_uribuilderror_is_valueerror(importer):
    assert issubclass(importer.UriBuildError, ValueError)
    assert issubclass(UriError, ValueError)


# --------------------------------------------------------------------------- #
# _import_one — truncation and bad-URI handling (B4/B5), no network
# --------------------------------------------------------------------------- #


class _FakeOV:
    """Minimal OpenViking client double (no HTTP)."""

    def __init__(self, content: str = "body text") -> None:
        self.content = content
        self.read_calls = 0

    def read(self, uri: str) -> str:
        self.read_calls += 1
        return self.content


class _FakeFS:
    """FilesystemStore double: records write() calls, no I/O."""

    def __init__(self) -> None:
        self.writes: list[dict] = []

    def write(self, uri, content, *, tenant, metadata=None, commit=True):
        self.writes.append({"uri": uri, "tenant": tenant})
        return {"uri": uri, "l0": "", "l1": ""}


class _FakePipeline:
    """IngestPipeline double returning a configurable result object."""

    def __init__(self, *, truncated: bool = False) -> None:
        self.truncated = truncated
        self.calls: list[dict] = []

    async def ingest(self, content, *, tenant, source_uri, kind, skip_facts):
        self.calls.append({"source_uri": source_uri, "kind": kind})
        return type(
            "R",
            (),
            {
                "chunks_new": 1,
                "facts_staged": 0,
                "truncated": self.truncated,
                "ingest_id": "i",
                "candidate_ids": [],
            },
        )()


async def test_import_one_truncated_is_not_marked_done(importer):
    """B5b: a truncated run returns ``False`` so the checkpoint is not advanced."""
    ov = _FakeOV()
    pipeline = _FakePipeline(truncated=True)
    good, info = await importer._import_one(
        pipeline, ov, _FakeFS(), tenant="example", ov_uri="viking://user/example/a/b.md", mode="ecl"
    )
    assert good is False
    assert info.startswith("truncated")


async def test_import_one_success_is_marked_done(importer):
    ov = _FakeOV()
    pipeline = _FakePipeline(truncated=False)
    good, info = await importer._import_one(
        pipeline, ov, _FakeFS(), tenant="example", ov_uri="viking://user/example/a/b.md", mode="ecl"
    )
    assert good is True
    assert "chunks=" in info
    # The target URI is the validated stratum URI.
    assert pipeline.calls[0]["source_uri"] == "stratum://docs/example/openviking/a/b.md"


async def test_import_one_empty_content_is_done_without_ingest(importer):
    ov = _FakeOV(content="   \n  ")
    pipeline = _FakePipeline()
    good, info = await importer._import_one(
        pipeline, ov, _FakeFS(), tenant="example", ov_uri="viking://user/example/a/b.md", mode="structure"
    )
    assert good is True
    assert info == "empty"
    assert pipeline.calls == []


async def test_import_one_bad_uri_returns_failure(importer):
    """B4: an unmappable (over-long) path fails instead of importing wrongly."""
    ov = _FakeOV()
    pipeline = _FakePipeline()
    deep = "/".join(["segment"] * 40)
    good, info = await importer._import_one(
        pipeline, ov, _FakeFS(), tenant="example", ov_uri=f"viking://user/example/{deep}", mode="ecl"
    )
    assert good is False
    assert info.startswith("bad-uri")
    assert pipeline.calls == []
