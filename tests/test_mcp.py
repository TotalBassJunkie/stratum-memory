"""Tests for the MCP agent tool API (:mod:`stratum.mcp`, WP-11).

Everything runs **without Postgres and without a network**: every collaborator is
a small fake injected through :class:`~stratum.mcp.tools.ToolContext`. The tests
pin:

* the ten tools are registered on the FastMCP server (skipped if ``mcp`` is
  missing),
* importing :mod:`stratum.mcp.tools` never needs the ``mcp`` library,
* scope enforcement (a ``read`` key cannot ``write``/``promote``; a missing scope
  yields the ``FORBIDDEN`` error format),
* ``query``/``get`` delegate with the expected arguments (incl. ``level`` and the
  ``as_of_*`` time views),
* ``write`` stages by default and calls the Curator when ``auto_promote`` is set,
* ``forget`` distinguishes soft vs. hard, ``archive`` journals ``ARCHIVE`` and
  ``forget`` journals ``FORGET``,
* ``health`` surfaces the probe status.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from stratum.auth import AuthContext
from stratum.config import Settings
from stratum.mcp import tools as tools_mod
from stratum.mcp.tools import (
    TOOL_NAMES,
    ErrorCode,
    ToolContext,
    stratum_archive,
    stratum_forget,
    stratum_get,
    stratum_health,
    stratum_link,
    stratum_promote,
    stratum_query,
    stratum_reflect,
    stratum_session_append,
    stratum_write,
)

# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class FakeQuery:
    """Records ``query`` calls and returns a fixed hit list."""

    def __init__(self, results: list[dict[str, Any]] | None = None) -> None:
        self.results = list(results or [])
        self.calls: list[dict[str, Any]] = []

    def query(self, tenant: str, query: str, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append({"tenant": tenant, "query": query, **kwargs})
        return self.results


class FakeFS:
    """Filesystem vault stand-in; records reads and can raise FileNotFoundError."""

    def __init__(self, documents: dict[str, dict[str, Any]] | None = None) -> None:
        self.documents = documents or {}
        self.read_calls: list[tuple[str, str, str]] = []

    def read(self, uri: str, *, level: str = "l1", tenant: str) -> dict[str, Any]:
        self.read_calls.append((uri, level, tenant))
        doc = self.documents.get(uri)
        if doc is None:
            raise FileNotFoundError(uri)
        return doc


class FakeStore:
    """Vector/KV store stand-in that records emitted ``memory_events``."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def log_event(self, tenant: str, **kwargs: Any) -> str:
        self.events.append({"tenant": tenant, **kwargs})
        return f"evt-{len(self.events)}"


class FakeCurator:
    """Curator stand-in recording promote calls."""

    def __init__(self) -> None:
        self.batch_calls: list[str] = []
        self.promote_calls: list[tuple[str, str]] = []

    def promote(
        self, tenant: str, candidate_id: str, *, actor: str | None = None
    ) -> dict[str, Any]:
        self.promote_calls.append((tenant, candidate_id))
        if candidate_id == "missing":
            raise LookupError(f"candidate not found: {candidate_id}")
        return {
            "status": "approved",
            "action": "ADD",
            "uri": f"stratum://facts/{tenant}/x",
            "event_id": "evt-core",
            "reason": None,
        }

    def promote_batch(self, tenant: str) -> dict[str, int]:
        self.batch_calls.append(tenant)
        return {"processed": 1, "approved": 1, "rejected": 0, "limit": 200}


class FakeIngest:
    """Ingestion pipeline stand-in recording the ingest call (incl. metadata)."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def ingest(
        self,
        source: Any,
        *,
        tenant: str,
        source_uri: str,
        kind: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> Any:
        self.calls.append(
            {
                "source": source,
                "tenant": tenant,
                "source_uri": source_uri,
                "kind": kind,
                "metadata": metadata,
            }
        )
        return SimpleNamespace(
            ingest_id="ing-1",
            chunks_new=2,
            facts_staged=1,
            truncated=False,
            candidate_id="cand-1",
        )


class FakeSessions:
    """Session store stand-in returning a synthetic turn."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def append(
        self,
        tenant: str,
        session_id: str,
        role: str,
        content: str,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "tenant": tenant,
                "session_id": session_id,
                "role": role,
                "content": content,
                "metadata": metadata,
            }
        )
        return {"uri": f"stratum://episodes/{tenant}/2026/10/{session_id}", "turn": {"index": 3}}


class FakeQueue:
    """Job queue stand-in recording enqueues."""

    def __init__(self) -> None:
        self.enqueued: list[dict[str, Any]] = []

    def enqueue(self, tenant: str, kind: str, payload: dict[str, Any]) -> str:
        self.enqueued.append({"tenant": tenant, "kind": kind, "payload": payload})
        return "job-9"


class FakeGraph:
    """Temporal graph stand-in recording link writes."""

    def __init__(self) -> None:
        self.entities: list[dict[str, Any]] = []
        self.facts: list[dict[str, Any]] = []
        self.edges: list[dict[str, Any]] = []

    def upsert_entity(self, tenant: str, **kwargs: Any) -> str:
        self.entities.append({"tenant": tenant, **kwargs})
        return f"ent-{len(self.entities)}"

    def add_fact(self, tenant: str, **kwargs: Any) -> str:
        self.facts.append({"tenant": tenant, **kwargs})
        return "fact-1"

    def add_edge(self, tenant: str, **kwargs: Any) -> str:
        self.edges.append({"tenant": tenant, **kwargs})
        return "edge-1"


# --------------------------------------------------------------------------- #
# Contexts
# --------------------------------------------------------------------------- #

TENANT = "t1"
DOC_URI = f"stratum://docs/{TENANT}/notes/a"


def _ctx(
    scopes: set[str] | None = None,
    *,
    tenant: str = TENANT,
    **collaborators: Any,
) -> ToolContext:
    """Build a ToolContext with fake collaborators and the given scopes."""
    auth = AuthContext(
        tenant=tenant,
        scopes=set({"admin"} if scopes is None else scopes),
        key_id="key-x",
    )
    return ToolContext(
        auth=auth,
        settings=Settings(),
        llm=SimpleNamespace(),
        store=collaborators.pop("store", FakeStore()),
        graph=collaborators.pop("graph", FakeGraph()),
        fs=collaborators.pop("fs", FakeFS()),
        curator=collaborators.pop("curator", FakeCurator()),
        query=collaborators.pop("query", FakeQuery()),
        sessions=collaborators.pop("sessions", FakeSessions()),
        ingest=collaborators.pop("ingest", FakeIngest()),
        queue=collaborators.pop("queue", FakeQueue()),
        health_fn=collaborators.pop("health_fn", None),
        hard_delete=collaborators.pop("hard_delete", None),
        fs_delete=collaborators.pop("fs_delete", None),
        vector_delete=collaborators.pop("vector_delete", None),
        conn_factory=collaborators.pop("conn_factory", None),
    )


# --------------------------------------------------------------------------- #
# Module decoupling / registration
# --------------------------------------------------------------------------- #


def test_tools_module_does_not_import_mcp():
    """``stratum.mcp.tools`` must not depend on the ``mcp`` library."""
    source = Path(tools_mod.__file__).read_text(encoding="utf-8")
    assert "from mcp" not in source
    assert "import mcp" not in source


def test_tool_names_are_the_ten_of_the_spec():
    assert TOOL_NAMES == (
        "stratum_write",
        "stratum_query",
        "stratum_get",
        "stratum_promote",
        "stratum_archive",
        "stratum_forget",
        "stratum_link",
        "stratum_session_append",
        "stratum_reflect",
        "stratum_health",
    )


def test_build_server_registers_ten_tools():
    pytest.importorskip("mcp")
    import asyncio

    from stratum.mcp.server import build_server

    server = build_server(_ctx())
    registered = asyncio.run(server.list_tools())
    assert {tool.name for tool in registered} == set(TOOL_NAMES)
    assert len(registered) == 10


# --------------------------------------------------------------------------- #
# Scope enforcement
# --------------------------------------------------------------------------- #


async def test_read_key_cannot_write():
    ctx = _ctx({"read"})
    result = await stratum_write(ctx, uri=DOC_URI, content="hi", kind="doc")
    assert result["error"]["code"] == ErrorCode.FORBIDDEN
    assert "message" in result["error"]


async def test_read_key_cannot_promote():
    ctx = _ctx({"read"})
    result = stratum_promote(ctx, candidate_id="cand-1")
    assert result["error"]["code"] == ErrorCode.FORBIDDEN


def test_missing_scope_yields_forbidden():
    ctx = _ctx(set())
    result = stratum_query(ctx, query="anything")
    assert result["error"]["code"] == "FORBIDDEN"
    assert isinstance(result["error"]["message"], str)
    assert result["error"]["message"]


def test_read_key_cannot_forget():
    ctx = _ctx({"read"})
    result = stratum_forget(ctx, uri=DOC_URI, hard=False, reason="gdpr")
    assert result["error"]["code"] == ErrorCode.FORBIDDEN


def test_admin_key_acts_across_tenants():
    ctx = _ctx({"admin"}, tenant="other")
    store = ctx.store
    result = stratum_archive(ctx, uri=DOC_URI, reason="cleanup")
    assert result["status"] == "archived"
    assert store.events[-1]["tenant"] == TENANT  # tenant taken from the URI


# --------------------------------------------------------------------------- #
# query / get delegation
# --------------------------------------------------------------------------- #


def test_query_delegates_with_all_arguments():
    hits = [{"uri": DOC_URI, "score": 0.9, "content": "x"}]
    query_engine = FakeQuery(hits)
    ctx = _ctx({"read"}, query=query_engine)

    result = stratum_query(
        ctx,
        query="find notes",
        scope=f"stratum://docs/{TENANT}",
        mode="hybrid",
        k=5,
        level="l2",
        as_of_valid="2026-01-01T00:00:00Z",
        as_of_tx="2026-02-01T00:00:00Z",
        filters={"tags": ["a"]},
    )

    assert result["results"] == hits
    assert result["count"] == 1
    call = query_engine.calls[0]
    assert call["tenant"] == TENANT
    assert call["query"] == "find notes"
    assert call["mode"] == "hybrid"
    assert call["k"] == 5
    assert call["level"] == "l2"
    assert call["as_of_valid"] == "2026-01-01T00:00:00Z"
    assert call["as_of_tx"] == "2026-02-01T00:00:00Z"
    assert call["scope"] == f"stratum://docs/{TENANT}"
    assert call["filters"] == {"tags": ["a"]}


def test_query_rejects_bad_mode():
    ctx = _ctx({"read"})
    result = stratum_query(ctx, query="q", mode="nope")
    assert result["error"]["code"] == ErrorCode.VALIDATION


def test_get_reads_requested_level():
    fs = FakeFS(
        {
            DOC_URI: {
                "uri": DOC_URI,
                "level": "l2",
                "content": "full body",
                "metadata": {"valid_from": "2026-01-01"},
            }
        }
    )
    ctx = _ctx({"read"}, fs=fs)
    result = stratum_get(ctx, uri=DOC_URI, level="l2")
    assert result["content"] == "full body"
    assert result["level"] == "l2"
    assert result["valid_from"] == "2026-01-01"
    assert fs.read_calls == [(DOC_URI, "l2", TENANT)]


def test_get_missing_uri_is_not_found():
    ctx = _ctx({"read"}, fs=FakeFS())
    result = stratum_get(ctx, uri=DOC_URI, level="l1")
    assert result["error"]["code"] == ErrorCode.NOT_FOUND


# --------------------------------------------------------------------------- #
# write staging vs. auto_promote
# --------------------------------------------------------------------------- #


async def test_write_stages_by_default():
    curator = FakeCurator()
    ingest = FakeIngest()
    ctx = _ctx({"write"}, curator=curator, ingest=ingest)

    result = await stratum_write(ctx, uri=DOC_URI, content="# Hello\n\nbody", kind="doc")

    assert result["status"] == "staged"
    assert result["ingest_id"] == "ing-1"
    assert result["l0"] == "Hello"
    assert curator.batch_calls == []  # Curator not invoked
    assert curator.promote_calls == []
    assert ingest.calls[0]["source_uri"] == DOC_URI


async def test_write_forwards_metadata_to_ingest():
    ingest = FakeIngest()
    ctx = _ctx({"write"}, ingest=ingest)

    result = await stratum_write(
        ctx,
        uri=DOC_URI,
        content="body",
        kind="doc",
        metadata={"tags": ["a"], "valid_from": "2026-01-01", "source_uri": DOC_URI},
    )

    assert ingest.calls[0]["metadata"] == {
        "tags": ["a"],
        "valid_from": "2026-01-01",
        "source_uri": DOC_URI,
    }
    assert result["metadata"]["tags"] == ["a"]


async def test_write_auto_promote_calls_curator():
    curator = FakeCurator()
    ctx = _ctx({"write", "promote"}, curator=curator, ingest=FakeIngest())

    result = await stratum_write(
        ctx, uri=DOC_URI, content="body", kind="doc", auto_promote=True
    )

    assert result["status"] == "promoted"
    # m2: the write's own candidate id is promoted individually, not a bogus
    # ``promote(tenant, tenant)``.
    assert curator.promote_calls == [(TENANT, "cand-1")]
    assert curator.batch_calls == []
    assert result["promote"]["status"] == "approved"
    assert result["promote"]["candidate_id"] == "cand-1"


async def test_write_auto_promote_requires_promote_scope():
    """B1: a write-only key must not be able to auto-promote."""
    curator = FakeCurator()
    ctx = _ctx({"write"}, curator=curator, ingest=FakeIngest())

    result = await stratum_write(
        ctx, uri=DOC_URI, content="body", kind="doc", auto_promote=True
    )

    assert result["error"]["code"] == ErrorCode.FORBIDDEN
    # The scope check fires before any Curator access.
    assert curator.promote_calls == []
    assert curator.batch_calls == []


async def test_write_auto_promote_without_candidate_id_uses_batch():
    """Without a candidate id the Curator falls back to a tenant-wide batch."""
    curator = FakeCurator()

    class _NoCandidateIngest(FakeIngest):
        async def ingest(self, source, *, tenant, source_uri, kind, metadata=None):
            self.calls.append({"source": source, "tenant": tenant, "source_uri": source_uri, "kind": kind, "metadata": metadata})
            return SimpleNamespace(
                ingest_id="ing-2", chunks_new=0, facts_staged=0,
                truncated=False, candidate_id=None, candidate_ids=[],
            )

    ctx = _ctx({"write", "promote"}, curator=curator, ingest=_NoCandidateIngest())
    result = await stratum_write(
        ctx, uri=DOC_URI, content="body", kind="doc", auto_promote=True
    )

    assert result["status"] == "promoted"
    assert curator.batch_calls == [TENANT]
    assert curator.promote_calls == []


async def test_write_auto_promote_promotes_only_staged_candidate_ids():
    """B7: auto_promote promotes *exactly* the ids the ingest staged, not a batch."""
    curator = FakeCurator()

    class _MultiCandidateIngest(FakeIngest):
        async def ingest(self, source, *, tenant, source_uri, kind, metadata=None):
            self.calls.append({"source": source, "tenant": tenant, "source_uri": source_uri, "kind": kind, "metadata": metadata})
            return SimpleNamespace(
                ingest_id="ing-3", chunks_new=1, facts_staged=2, truncated=False,
                candidate_id=None, candidate_ids=["cand-a", "cand-b"],
            )

    ctx = _ctx({"write", "promote"}, curator=curator, ingest=_MultiCandidateIngest())
    result = await stratum_write(
        ctx, uri=DOC_URI, content="body", kind="doc", auto_promote=True
    )

    assert result["status"] == "promoted"
    # Each staged id promoted individually; no tenant-wide batch sweep.
    assert curator.promote_calls == [(TENANT, "cand-a"), (TENANT, "cand-b")]
    assert curator.batch_calls == []
    assert result["promote"]["approved"] == 2
    assert result["candidate_ids"] == ["cand-a", "cand-b"]


async def test_write_records_audit_log():
    """B20a: write records a tenant-scoped ``audit_log`` row."""
    log: list[tuple[str, tuple]] = []
    ctx = _ctx({"write"}, conn_factory=_recording_factory(log), ingest=FakeIngest())

    await stratum_write(ctx, uri=DOC_URI, content="body", kind="doc")

    audits = [sql for sql, _ in log if "INSERT INTO audit_log" in sql]
    assert audits, "write must record an audit_log entry"
    # And the params carry the acting tenant.
    _, params = next((s, p) for s, p in log if "INSERT INTO audit_log" in s)
    assert params[0] == TENANT
    assert params[2] == "WRITE"


def test_forget_records_audit_log():
    """B20a: forget records an ``audit_log`` row as well."""
    log: list[tuple[str, tuple]] = []
    ctx = _ctx({"promote"}, conn_factory=_recording_factory(log))
    stratum_forget(ctx, uri=DOC_URI, hard=False, reason="gdpr")
    audits = [sql for sql, _ in log if "INSERT INTO audit_log" in sql]
    assert audits


async def test_tool_error_increments_metric():
    """B20c: a failed tool increments ``stratum_tool_errors_total{tool=}``."""
    from stratum.observability import default_metrics

    before = default_metrics().render_prometheus()
    ctx = _ctx({"read"})  # read key cannot write -> FORBIDDEN
    await stratum_write(ctx, uri=DOC_URI, content="x", kind="doc")
    after = default_metrics().render_prometheus()
    assert after != before
    assert 'stratum_tool_errors_total{tool="stratum_write"}' in after


async def test_write_feeds_llm_total_tokens_gauge():
    """B20c: a successful write publishes the cumulative LLM token gauge."""

    class _Usage:
        total_tokens = 123

    class _LLM:
        def usage(self):
            return _Usage()

    from stratum.observability import default_metrics

    ctx = _ctx({"write"}, ingest=FakeIngest())
    ctx.llm = _LLM()
    await stratum_write(ctx, uri=DOC_URI, content="body", kind="doc")
    assert "stratum_llm_total_tokens 123" in default_metrics().render_prometheus()


async def test_write_rejects_unknown_kind():
    ctx = _ctx({"write"})
    result = await stratum_write(ctx, uri=DOC_URI, content="x", kind="bogus")
    assert result["error"]["code"] == ErrorCode.VALIDATION


# --------------------------------------------------------------------------- #
# archive / forget events
# --------------------------------------------------------------------------- #


def test_archive_writes_archive_event():
    store = FakeStore()
    ctx = _ctx({"promote"}, store=store)
    result = stratum_archive(ctx, uri=DOC_URI, reason="stale")
    assert result["status"] == "archived"
    assert store.events[-1]["op"] == "ARCHIVE"
    assert store.events[-1]["target_uri"] == DOC_URI


def test_forget_soft_does_not_delete():
    store = FakeStore()
    called: list[tuple[str, str]] = []

    def hard_delete(tenant: str, uri: str) -> int:
        called.append((tenant, uri))
        return 3

    ctx = _ctx({"promote"}, store=store, hard_delete=hard_delete)
    result = stratum_forget(ctx, uri=DOC_URI, hard=False, reason="gdpr")
    assert result["mode"] == "soft"
    assert result["deleted_refs"] == 0
    assert called == []  # soft never deletes
    assert store.events[-1]["op"] == "FORGET"


def test_forget_hard_deletes_and_counts():
    store = FakeStore()
    called: list[tuple[str, str]] = []

    def hard_delete(tenant: str, uri: str) -> int:
        called.append((tenant, uri))
        return 4

    ctx = _ctx({"promote"}, store=store, hard_delete=hard_delete)
    result = stratum_forget(ctx, uri=DOC_URI, hard=True, reason="gdpr")
    assert result["mode"] == "hard"
    assert result["deleted_refs"] == 4
    assert called == [(TENANT, DOC_URI)]
    assert store.events[-1]["op"] == "FORGET"


def test_forget_requires_reason():
    ctx = _ctx({"promote"})
    result = stratum_forget(ctx, uri=DOC_URI, hard=False, reason="  ")
    assert result["error"]["code"] == ErrorCode.VALIDATION


# --------------------------------------------------------------------------- #
# Hard forget: dependency-safe deletion + Git-committing file removal (B2)
# --------------------------------------------------------------------------- #


class FakeDeleteFS(FakeFS):
    """Filesystem vault that removes a file and reports a Git commit."""

    def __init__(self, commit: str = "deadbeef") -> None:
        super().__init__()
        self.commit = commit
        self.deleted: list[tuple[str, str]] = []

    def delete(self, uri: str, *, tenant: str) -> dict[str, Any]:
        self.deleted.append((uri, tenant))
        return {"uri": uri, "git_commit": self.commit}


class _RecordCursor:
    """Cursor that records the executed SQL and never raises."""

    def __init__(self, log: list[tuple[str, tuple]]) -> None:
        self._log = log

    def __enter__(self) -> _RecordCursor:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def execute(self, sql: str, params: tuple = ()) -> None:
        self._log.append((re.sub(r"\s+", " ", sql).strip(), tuple(params)))

    @property
    def rowcount(self) -> int:
        return 1


class _RecordConn:
    """Connection whose cursor records SQL; commit is a no-op marker."""

    def __init__(self, log: list[tuple[str, tuple]]) -> None:
        self._log = log

    def __enter__(self) -> _RecordConn:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def cursor(self) -> _RecordCursor:
        return _RecordCursor(self._log)

    def commit(self) -> None:
        self._log.append(("COMMIT", ()))


def _recording_factory(log: list[tuple[str, tuple]]):
    def factory(tenant: str, **kwargs: Any) -> _RecordConn:
        return _RecordConn(log)

    return factory


def test_hard_forget_default_deletes_in_dependency_order():
    """B2/B8: edges → facts → entities → chunks/files/uri_registry → derived state."""
    log: list[tuple[str, tuple]] = []
    fs = FakeDeleteFS(commit="cafe1234")
    ctx = _ctx(
        {"promote"},
        conn_factory=_recording_factory(log),
        fs=fs,
    )

    result = stratum_forget(ctx, uri=DOC_URI, hard=True, reason="gdpr")

    statements = [sql for sql, _ in log if sql != "COMMIT"]
    delete_statements = [sql for sql in statements if sql.startswith("DELETE FROM")]
    table_order = [
        next(
            (
                table
                for table in (
                    "edges",
                    "facts",
                    "entities",
                    "chunks",
                    "files",
                    "uri_registry",
                    "staging_candidates",
                    "kv",
                    "syntheses",
                )
                if f"DELETE FROM {table}" in sql
            ),
            "",
        )
        for sql in delete_statements
    ]
    assert table_order == [
        "edges",
        "facts",
        "entities",
        "chunks",
        "files",
        "uri_registry",
        # B8: derived state must be forgotten too, or it resurrects the URI.
        "staging_candidates",
        "kv",
        "syntheses",
    ]
    # The self-referential ``superseded_by`` FK is detached before the fact delete.
    update_statements = [sql for sql in statements if sql.startswith("UPDATE facts")]
    assert update_statements and "superseded_by = NULL" in update_statements[0]
    # The edge delete covers both the fact link and the entity link.
    edges_sql = delete_statements[0]
    assert "fact_id" in edges_sql
    assert "canonical_uri" in edges_sql
    assert ("COMMIT", ()) in log

    # The vault file was removed through Git, and the commit is surfaced.
    assert fs.deleted == [(DOC_URI, TENANT)]
    assert result["git_commit"] == "cafe1234"
    assert result["mode"] == "hard"
    assert result["deleted_refs"] >= 9  # one rowcount per DELETE statement
    assert ctx.store.events[-1]["op"] == "FORGET"
    assert ctx.store.events[-1]["after"]["git_commit"] == "cafe1234"


def test_hard_forget_of_linked_uri_succeeds():
    """B2: a URI linked via ``stratum_link`` forgets cleanly (edges + fact)."""
    log: list[tuple[str, tuple]] = []
    fs = FakeDeleteFS(commit="abc999")
    ctx = _ctx(
        {"promote"},
        conn_factory=_recording_factory(log),
        fs=fs,
    )

    result = stratum_forget(ctx, uri=DOC_URI, hard=True, reason="right to be forgotten")

    assert "error" not in result
    sqls = [sql for sql, _ in log]
    assert any("DELETE FROM edges" in sql for sql in sqls)
    assert any("DELETE FROM facts" in sql for sql in sqls)
    assert any("DELETE FROM entities" in sql for sql in sqls)
    assert result["git_commit"] == "abc999"
    assert ctx.store.events[-1]["after"]["git_commit"] == "abc999"


def test_hard_forget_honours_injected_fs_and_vector_hooks():
    """Injected ``fs_delete``/``vector_delete`` hooks are used when supplied."""
    deleted_vectors: list[tuple[str, str]] = []
    deleted_files: list[tuple[str, str]] = []
    ctx = _ctx(
        {"promote"},
        hard_delete=lambda tenant, uri: 2,
        vector_delete=lambda tenant, uri: (deleted_vectors.append((tenant, uri)) or 0),
        fs_delete=lambda tenant, uri: (
            deleted_files.append((tenant, uri)) or "hookcommit"
        ),
    )

    result = stratum_forget(ctx, uri=DOC_URI, hard=True, reason="gdpr")
    # A custom ``hard_delete`` short-circuits the default path, so the fs/vector
    # hooks are not consulted; the result still carries the normalised shape.
    assert result["deleted_refs"] == 2
    assert result["git_commit"] is None


def test_hard_forget_default_hook_removes_vectors_and_commits():
    """The default path runs ``vector_delete`` (tally) and ``fs_delete`` (commit)."""
    log: list[tuple[str, tuple]] = []
    fs = FakeDeleteFS(commit="vec000")
    ctx = _ctx(
        {"promote"},
        conn_factory=_recording_factory(log),
        fs=fs,
    )
    result = stratum_forget(ctx, uri=DOC_URI, hard=True, reason="gdpr")
    assert result["git_commit"] == "vec000"
    assert ("COMMIT", ()) in log


# --------------------------------------------------------------------------- #
# link: scope + arguments + error paths (M2)
# --------------------------------------------------------------------------- #


OTHER_URI = "stratum://docs/other/notes/b"


def test_link_returns_edge_and_fact_ids():
    graph = FakeGraph()
    ctx = _ctx({"write"}, graph=graph)

    result = stratum_link(
        ctx, src_uri=DOC_URI, dst_uri=f"stratum://docs/{TENANT}/notes/b", rel_type="related_to"
    )

    assert result["edge_id"] == "edge-1"
    assert result["fact_id"] == "fact-1"
    assert result["rel_type"] == "related_to"
    # Two entities were resolved/created (src + dst).
    assert [e["canonical_uri"] for e in graph.entities] == [
        DOC_URI,
        f"stratum://docs/{TENANT}/notes/b",
    ]
    assert graph.facts[0]["source_uri"] == DOC_URI
    assert graph.edges[0]["fact_id"] == "fact-1"


def test_link_requires_write_scope():
    ctx = _ctx({"read"})
    result = stratum_link(
        ctx, src_uri=DOC_URI, dst_uri=f"stratum://docs/{TENANT}/notes/b", rel_type="rel"
    )
    assert result["error"]["code"] == ErrorCode.FORBIDDEN


def test_link_dst_uri_cross_tenant_is_forbidden():
    """The destination URI's tenant is authorised too (not only the source)."""
    graph = FakeGraph()
    ctx = _ctx({"write"}, graph=graph, tenant=TENANT)

    result = stratum_link(ctx, src_uri=DOC_URI, dst_uri=OTHER_URI, rel_type="rel")

    assert result["error"]["code"] == ErrorCode.FORBIDDEN
    assert graph.entities == []  # nothing written before the gate


def test_link_src_uri_cross_tenant_is_forbidden():
    graph = FakeGraph()
    ctx = _ctx({"write"}, graph=graph, tenant=TENANT)

    result = stratum_link(
        ctx, src_uri=OTHER_URI, dst_uri=DOC_URI, rel_type="rel"
    )

    assert result["error"]["code"] == ErrorCode.FORBIDDEN
    assert graph.entities == []


def test_link_missing_dst_is_not_found():
    """A graph that cannot resolve the destination endpoint raises NOT_FOUND."""
    class _Graph(FakeGraph):
        def upsert_entity(self, tenant: str, **kwargs: Any) -> str:
            if kwargs.get("canonical_uri") == "stratum://docs/t1/notes/b":
                raise LookupError("entity backend unavailable")
            return super().upsert_entity(tenant, **kwargs)

    ctx = _ctx({"write"}, graph=_Graph())
    result = stratum_link(
        ctx, src_uri=DOC_URI, dst_uri="stratum://docs/t1/notes/b", rel_type="rel"
    )
    # A LookupError from an unexpected place is normalised: the graph write is
    # guarded, so the tool reports a structured error rather than a traceback.
    assert result["error"]["code"] == ErrorCode.INTERNAL


def test_link_rejects_bad_rel_type():
    ctx = _ctx({"write"})
    result = stratum_link(ctx, src_uri=DOC_URI, dst_uri=DOC_URI, rel_type="  ")
    assert result["error"]["code"] == ErrorCode.VALIDATION


def test_link_rejects_malformed_uri():
    ctx = _ctx({"write"})
    result = stratum_link(ctx, src_uri="not-a-uri", dst_uri=DOC_URI, rel_type="rel")
    assert result["error"]["code"] == ErrorCode.VALIDATION


# --------------------------------------------------------------------------- #
# INTERNAL errors never leak internals (m3)
# --------------------------------------------------------------------------- #


def test_internal_error_is_generic_and_logged():
    class _Boom:
        def query(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("secret-detail-42")

    ctx = _ctx({"read"}, query=_Boom())
    result = stratum_query(ctx, query="x")

    assert result["error"]["code"] == ErrorCode.INTERNAL
    assert "secret-detail-42" not in result["error"]["message"]
    assert "RuntimeError" not in result["error"]["message"]
    assert result["error"]["message"] == "internal error"


# --------------------------------------------------------------------------- #
# Module entry points (M1): __main__ + CLI
# --------------------------------------------------------------------------- #


def test_mcp_package_main_is_runnable_module():
    from stratum.mcp import __main__ as mcp_main

    assert hasattr(mcp_main, "main")


def test_cli_rejects_missing_subcommand():
    import stratum.cli as cli

    with pytest.raises(SystemExit):
        cli.main([])


def test_cli_health_returns_json_and_exit_code(capsys: pytest.CaptureFixture[str]):
    import stratum.cli as cli
    import stratum.observability as obs

    original = obs.health
    obs.health = lambda: {"status": "ok", "db": {"ok": True}}  # type: ignore[assignment]
    try:
        code = cli.main(["health"])
    finally:
        obs.health = original  # type: ignore[assignment]

    assert code == 0
    assert '"status": "ok"' in capsys.readouterr().out


def test_cli_health_degraded_exits_nonzero(capsys: pytest.CaptureFixture[str]):
    import stratum.cli as cli
    import stratum.observability as obs

    original = obs.health
    obs.health = lambda: {"status": "degraded"}  # type: ignore[assignment]
    try:
        code = cli.main(["health"])
    finally:
        obs.health = original  # type: ignore[assignment]

    assert code == 1
    assert '"status": "degraded"' in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# promote / session / reflect / health
# --------------------------------------------------------------------------- #


def test_promote_not_found_maps_to_error():
    class _Curator:
        def promote(self, tenant: str, candidate_id: str, *, actor: str | None = None) -> dict[str, Any]:
            raise LookupError(f"candidate not found: {candidate_id}")

    ctx = _ctx({"promote"}, curator=_Curator())
    result = stratum_promote(ctx, candidate_id="missing")
    assert result["error"]["code"] == ErrorCode.NOT_FOUND


def test_session_append_returns_turn_index():
    sessions = FakeSessions()
    ctx = _ctx({"write"}, sessions=sessions)
    result = stratum_session_append(
        ctx, session_id="s-1", role="user", content="hello", metadata={"x": 1}
    )
    assert result["turn"] == 3
    assert result["uri"].startswith("stratum://episodes/")
    assert sessions.calls[0]["role"] == "user"


def test_reflect_enqueues_budgeted_job():
    queue = FakeQueue()
    ctx = _ctx({"promote"}, queue=queue)
    result = stratum_reflect(ctx, scope=f"stratum://docs/{TENANT}", budget_tokens=500, async_=True)
    assert result["job_id"] == "job-9"
    assert result["estimated_tokens"] == 500
    assert queue.enqueued[0]["kind"] == "reflect"
    assert queue.enqueued[0]["payload"]["budget_tokens"] == 500


def test_reflect_over_budget_is_rejected():
    ctx = _ctx({"promote"}, queue=FakeQueue())
    result = stratum_reflect(ctx, budget_tokens=10_000_000)
    assert result["error"]["code"] == ErrorCode.BUDGET_EXCEEDED


def test_health_reports_probe_status():
    def probe() -> dict[str, Any]:
        return {
            "status": "ok",
            "version": "0.1.0",
            "db": {"ok": True, "error": None},
            "jobs": {"queued": 3, "error": None},
            "git": {"ok": True},
            "llm": {"ok": False},
        }

    ctx = _ctx({"read"}, health_fn=probe)
    result = stratum_health(ctx)
    assert result["status"] == "ok"
    assert result["db"] is True
    assert result["git"] is True
    assert result["llm"] is False
    assert result["queued_jobs"] == 3


def test_health_degraded_without_probe_support():
    def probe() -> dict[str, Any]:
        return {"status": "degraded", "db": {"ok": False}, "jobs": {"queued": None}}

    ctx = _ctx({"read"}, health_fn=probe)
    result = stratum_health(ctx)
    assert result["status"] == "degraded"
    assert result["db"] is False
    assert result["queued_jobs"] is None


# --------------------------------------------------------------------------- #
# Operational HTTP routes /health + /metrics (WP-17, B1)
# --------------------------------------------------------------------------- #


def test_build_server_registers_health_and_metrics_routes():
    """The live server exposes real ``/health`` and ``/metrics`` routes (B1)."""
    pytest.importorskip("mcp")
    from stratum.mcp.server import build_server

    server = build_server(_ctx())
    paths = {getattr(route, "path", None) for route in server.streamable_http_app().routes}
    assert "/health" in paths
    assert "/metrics" in paths


def test_health_route_returns_probe_json(monkeypatch: pytest.MonkeyPatch):
    """``GET /health`` renders the observability probe (status ok → 200)."""
    pytest.importorskip("mcp")
    import asyncio

    import stratum.observability as obs
    from stratum.mcp.server import build_server

    monkeypatch.setattr(obs, "health", lambda: {"status": "ok", "db": {"ok": True}})
    server = build_server(_ctx())

    route = next(
        r for r in server.streamable_http_app().routes if getattr(r, "path", None) == "/health"
    )
    response = asyncio.run(route.endpoint(None))
    assert response.status_code == 200
    assert response.body == b'{"status":"ok","db":{"ok":true}}'


def test_health_route_returns_503_when_degraded(monkeypatch: pytest.MonkeyPatch):
    pytest.importorskip("mcp")
    import asyncio

    import stratum.observability as obs
    from stratum.mcp.server import build_server

    monkeypatch.setattr(obs, "health", lambda: {"status": "degraded"})
    server = build_server(_ctx())
    route = next(
        r for r in server.streamable_http_app().routes if getattr(r, "path", None) == "/health"
    )
    response = asyncio.run(route.endpoint(None))
    assert response.status_code == 503


def test_metrics_route_returns_prometheus_text(monkeypatch: pytest.MonkeyPatch):
    """``GET /metrics`` renders the Prometheus exposition with its exact type."""
    pytest.importorskip("mcp")
    import asyncio

    import stratum.observability as obs
    from stratum.mcp.server import build_server

    monkeypatch.setattr(
        obs,
        "metrics_response",
        lambda: ("# TYPE x counter\nx 1\n", "text/plain; version=0.0.4"),
    )
    server = build_server(_ctx())
    route = next(
        r for r in server.streamable_http_app().routes if getattr(r, "path", None) == "/metrics"
    )
    response = asyncio.run(route.endpoint(None))
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/plain; version=0.0.4"
    assert b"# TYPE x counter" in response.body


# --------------------------------------------------------------------------- #
# Optional key gate (WP-17, B2)
# --------------------------------------------------------------------------- #


def test_require_api_key_defaults_off(monkeypatch: pytest.MonkeyPatch):
    pytest.importorskip("mcp")
    from stratum.mcp.server import _require_api_key

    monkeypatch.delenv("STRATUM_REQUIRE_API_KEY", raising=False)
    assert _require_api_key() is False


@pytest.mark.parametrize(
    ("value", "expected"),
    [("1", True), ("true", True), ("YES", True), ("0", False), ("", False), ("off", False)],
)
def test_require_api_key_parsing(monkeypatch: pytest.MonkeyPatch, value: str, expected: bool):
    pytest.importorskip("mcp")
    from stratum.mcp.server import _require_api_key

    monkeypatch.setenv("STRATUM_REQUIRE_API_KEY", value)
    assert _require_api_key() is expected


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("Bearer stk_t_abc", "stk_t_abc"),
        ("bearer   stk_t_abc ", "stk_t_abc"),
        ("Basic abc", None),
        ("Bearer", None),
        (None, None),
    ],
)
def test_bearer_token_parsing(header: str | None, expected: str | None):
    pytest.importorskip("mcp")
    from stratum.mcp.server import _bearer_token

    assert _bearer_token(header) == expected


def test_effective_ctx_uses_bootstrap_without_override():
    """Outside a gated request the bootstrap context is used unchanged."""
    pytest.importorskip("mcp")
    from stratum.mcp.server import _effective_ctx

    ctx = _ctx({"admin"}, tenant="default")
    assert _effective_ctx(ctx) is ctx


def test_effective_ctx_prefers_bound_request_auth():
    """A per-request AuthContext overrides the bootstrap identity (B2)."""
    pytest.importorskip("mcp")
    from stratum.mcp import server as server_mod

    bootstrap = AuthContext(tenant="default", scopes={"admin"}, key_id="bootstrap")
    ctx = _ctx({"admin"}, tenant="default")
    ctx.auth = bootstrap
    request_auth = AuthContext(tenant="t9", scopes={"read"}, key_id="k9")

    token = server_mod._CURRENT_AUTH.set(request_auth)
    try:
        effective = server_mod._effective_ctx(ctx)
    finally:
        server_mod._CURRENT_AUTH.reset(token)

    assert effective.auth is request_auth
    assert effective.auth.tenant == "t9"
    assert effective is not ctx


def _call_asgi(app, scope):
    """Drive an ASGI app once and return the emitted ``http.response.start``."""
    import asyncio

    captured: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request"}

    async def send(message: dict[str, Any]) -> None:
        captured.append(message)

    asyncio.run(app(scope, receive, send))
    return captured[0]


def test_key_gate_missing_header_is_401():
    """No bearer header → 401 with ``WWW-Authenticate`` (B2)."""
    pytest.importorskip("mcp")
    from stratum.mcp.server import _ApiKeyAuthMiddleware

    async def inner(scope, receive, send):  # pragma: no cover - never reached
        raise AssertionError("gate must reject before the app runs")

    mw = _ApiKeyAuthMiddleware(inner, mcp_path="/mcp", verify=lambda token: None)
    scope = {"type": "http", "path": "/mcp", "headers": []}
    start = _call_asgi(mw, scope)
    assert start["status"] == 401
    assert (b"www-authenticate", b"Bearer") in [(k.lower(), v) for k, v in start["headers"]]


def test_key_gate_invalid_token_is_403():
    pytest.importorskip("mcp")
    from stratum.mcp.server import _ApiKeyAuthMiddleware

    async def inner(scope, receive, send):  # pragma: no cover - never reached
        raise AssertionError("gate must reject before the app runs")

    mw = _ApiKeyAuthMiddleware(inner, mcp_path="/mcp", verify=lambda token: None)
    scope = {
        "type": "http",
        "path": "/mcp",
        "headers": [(b"authorization", b"Bearer stk_t_bad")],
    }
    start = _call_asgi(mw, scope)
    assert start["status"] == 403


def test_key_gate_binds_request_auth_on_success():
    """A valid token binds the resolved identity for the request (B2)."""
    pytest.importorskip("mcp")
    from stratum.mcp import server as server_mod

    resolved = AuthContext(tenant="t9", scopes={"read"}, key_id="k9")
    observed: dict[str, Any] = {}

    async def inner(scope, receive, send):
        observed["auth"] = server_mod._CURRENT_AUTH.get()
        await send({"type": "http.response.start", "status": 200, "headers": []})

    mw = server_mod._ApiKeyAuthMiddleware(inner, mcp_path="/mcp", verify=lambda token: resolved)
    scope = {
        "type": "http",
        "path": "/mcp",
        "headers": [(b"authorization", b"Bearer stk_t9_good")],
    }
    start = _call_asgi(mw, scope)
    assert start["status"] == 200
    assert observed["auth"] is resolved
    # The binding is reset after the request (no leakage into other calls).
    assert server_mod._CURRENT_AUTH.get() is None


def test_key_gate_passes_non_mcp_paths_through():
    """``/health`` and ``/metrics`` stay public even with the gate enabled (B2)."""
    pytest.importorskip("mcp")
    from stratum.mcp.server import _ApiKeyAuthMiddleware

    async def inner(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})

    mw = _ApiKeyAuthMiddleware(inner, mcp_path="/mcp", verify=lambda token: None)
    scope = {"type": "http", "path": "/health", "headers": []}
    start = _call_asgi(mw, scope)
    assert start["status"] == 200
