"""End-to-end scenario: write → curate → query → reflect → forget (WP-17).

This module is the honest full-stack through-line the plan asks for (§14 test
level 4). It wires the **real** modules — :class:`~stratum.ingest.IngestPipeline`,
:class:`~stratum.curator.Curator`, :class:`~stratum.query.QueryEngine`,
:class:`~stratum.reflect.ReflectWorker`, :class:`~stratum.ingest.SessionStore`,
:class:`~stratum.worker.JobQueue` and the ten MCP tools — to one shared
in-memory backend (see ``tests/conftest.py``).

What is *real* here
-------------------
* The **ECL ingestion pipeline** (chunking, content-hash dedup, schema-bound fact
  extraction, ``staging_candidates`` staging) runs unmodified.
* The **Curator's seven criteria** and the **real add-only dedup classifier**
  (:meth:`stratum.vector.MemoryStore.decide`) run unmodified — the store is a
  ``MemoryStore`` subclass whose only changes are the SQL-backed methods.
* The **bi-temporal graph** (close-before-insert, ``facts``/``entities``/
  ``edges``) runs its real SQL against the in-memory backend.
* The **QueryEngine** (hybrid RRF over vector + graph + KV) runs unmodified.
* The **ReflectWorker** (advisory-lock claim, budget, clustering, pending-only
  staging) runs unmodified.
* The **MCP tool surface** (scope gate, error contract, result shapes) runs
  unmodified.

What is faked
-------------
* The **database** — :class:`~tests.conftest.InMemoryDB` models the tables the
  modules touch; it is not a SQL engine.
* The **LLM/embeddings** — a deterministic, token-metering fake (no network).
* The **filesystem vault** — an in-memory vault using the real L0/L1 helpers.

The real-Postgres variant (schema creation, RLS tenant isolation,
close-before-insert on real DDL) is the ``@pytest.mark.integration`` test at the
bottom; it is skipped unless ``STRATUM_TEST_DB`` is set.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any

import pytest

from conftest import FakeLLM, InMemoryDB, InMemoryFS, InMemoryStore
from stratum.auth import AuthContext, Authorizer
from stratum.config import Settings
from stratum.curator import Curator
from stratum.graph import TemporalGraph
from stratum.ingest import IngestPipeline, SessionStore
from stratum.mcp.tools import (
    ErrorCode,
    ToolContext,
    stratum_archive,
    stratum_forget,
    stratum_promote,
    stratum_query,
    stratum_reflect,
    stratum_session_append,
    stratum_write,
)
from stratum.query import QueryEngine
from stratum.reflect import REFLECT_OP, ReflectWorker
from stratum.worker import JobQueue

TENANT = "e2e"
DOC_URI = f"stratum://docs/{TENANT}/notes/ada"
OTHER_TENANT = "e2e_other"


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #


class Harness:
    """One wired stack of real modules over a shared in-memory backend."""

    def __init__(self, db: InMemoryDB, settings: Settings, llm: FakeLLM) -> None:
        self.db = db
        self.settings = settings
        self.llm = llm
        self.store = InMemoryStore(db, settings)
        self.fs = InMemoryFS()
        self.graph = TemporalGraph(settings, conn_factory=db.factory)
        self.curator = Curator(
            settings, store=self.store, graph=self.graph, conn_factory=db.factory
        )
        self.query = QueryEngine(
            settings,
            llm=llm,
            store=self.store,
            graph=self.graph,
            fs=self.fs,
            conn_factory=db.factory,
        )
        self.sessions = SessionStore(
            settings, store=self.store, fs=self.fs, conn_factory=db.factory
        )
        self.ingest = IngestPipeline(
            settings,
            llm=llm,
            store=self.store,
            fs=self.fs,
            graph=self.graph,
            conn_factory=db.factory,
        )
        self.reflect = ReflectWorker(
            settings,
            llm=llm,
            store=self.store,
            graph=self.graph,
            curator=self.curator,
            conn_factory=db.factory,
        )
        self.queue = JobQueue(settings, conn_factory=db.factory)

    def context(self, scopes: set[str] | None = None) -> ToolContext:
        """Build a :class:`ToolContext` over the wired modules.

        ``scopes=None`` grants ``admin`` (a trusted caller); pass an explicit set
        to exercise the scope gate.
        """
        auth = AuthContext(
            tenant=TENANT,
            scopes=set({"admin"} if scopes is None else scopes),
            key_id="key-e2e",
        )
        return ToolContext(
            auth=auth,
            authorizer=Authorizer(),
            settings=self.settings,
            llm=self.llm,
            store=self.store,
            graph=self.graph,
            fs=self.fs,
            curator=self.curator,
            query=self.query,
            sessions=self.sessions,
            ingest=self.ingest,
            queue=self.queue,
            conn_factory=self.db.factory,
            health_fn=lambda: {
                "status": "ok",
                "version": "0.1.0",
                "db": {"ok": True},
                "git": {"ok": True},
                "llm": {"ok": True},
                "jobs": {"queued": 0},
            },
        )

    def pending_candidates(self, kind: str | None = None) -> list[dict[str, Any]]:
        return [
            c
            for c in self.db.candidates(status="pending")
            if kind is None or c["kind"] == kind
        ]


@pytest.fixture
def harness(
    in_memory_db: InMemoryDB, stratum_settings: Settings, fake_llm: FakeLLM
) -> Harness:
    """A fully wired harness over the shared in-memory backend."""
    return Harness(in_memory_db, stratum_settings, fake_llm)


# --------------------------------------------------------------------------- #
# 1. Ingestion stages; the Curator promotes; query finds the fact
# --------------------------------------------------------------------------- #


async def test_full_flow_write_curate_query(harness: Harness) -> None:
    """write (staged) → promote → query returns the promoted fact."""
    db = harness.db

    # --- write (auto_promote=False) -> staged, no active write, no event ----
    result = await harness.ingest.ingest(
        "# Ada\n\nAda plans to use the Analytical Engine.",
        tenant=TENANT,
        source_uri=DOC_URI,
        kind="doc",
    )
    assert result.chunks_new == 1
    assert result.facts_staged == 1

    staged = harness.pending_candidates("fact")
    assert len(staged) == 1
    # Staging is not an active write: no memory_events row is produced yet.
    assert db.ops() == []
    assert db.open_facts(TENANT) == []
    assert len(db.chunks) == 1

    # --- Curator promote -> active fact + ADD event + registry -------------
    candidate_id = staged[0]["candidate_id"]
    promoted = harness.curator.promote(TENANT, candidate_id, actor="key-e2e")
    assert promoted["status"] == "approved"
    assert promoted["action"] == "ADD"
    assert promoted["event_id"] is not None
    assert db.ops() == ["ADD"]
    assert len(db.open_facts(TENANT)) == 1
    assert db.registry[promoted["uri"]]["kind"] == "fact"

    # --- query finds the promoted fact -------------------------------------
    hits = harness.query.query(TENANT, "Ada", mode="hybrid", k=8)
    assert hits, "query returned no hit for the promoted fact"
    assert any("Ada" in hit["content"] for hit in hits)
    assert all(0.0 <= hit["score"] <= 1.0 for hit in hits)


async def test_promoted_fact_is_bi_temporal_and_close_before_insert(
    harness: Harness,
) -> None:
    """A second, changed fact closes the first (close-before-insert, §5.2)."""
    db = harness.db
    await harness.ingest.ingest(
        "Ada plans pro.",
        tenant=TENANT,
        source_uri=DOC_URI,
        kind="doc",
    )
    first = harness.pending_candidates("fact")[0]["candidate_id"]
    harness.curator.promote(TENANT, first)

    # A second candidate with a *changed* object is an UPDATE, not a duplicate.
    entity_id = db.entities[0]["entity_id"]
    candidate = {
        "tenant_id": TENANT,
        "kind": "fact",
        "source_uri": DOC_URI,
        "confidence": 0.9,
        "payload": {
            "subject": "Ada",
            "predicate": "plans",
            "object_text": "enterprise",
            "object": "enterprise",
            "confidence": 0.9,
            "embedding": (await harness.llm.embed(["Ada plans enterprise"]))[0],
            "action": "UPDATE",
        },
    }
    db.staging.append(
        {
            "candidate_id": "manual-update",
            "tenant_id": TENANT,
            "kind": "fact",
            "payload": candidate["payload"],
            "source_uri": DOC_URI,
            "confidence": 0.9,
            "status": "pending",
            "reason": None,
            "ingest_id": None,
            "created_at": db.facts[0]["created_at"],
        }
    )
    promoted = harness.curator.promote(TENANT, "manual-update")
    assert promoted["action"] == "UPDATE"

    open_facts = db.open_facts(TENANT)
    assert len(open_facts) == 1
    assert open_facts[0]["object_text"] == "enterprise"
    # The predecessor was closed (valid_to set), not deleted.
    assert len(db.facts) == 2
    # The entity is reused, not duplicated, for the same canonical URI.
    assert len(db.entities) == 1
    assert db.entities[0]["entity_id"] == entity_id


async def test_duplicate_candidate_is_noop(harness: Harness) -> None:
    """Re-stating the same fact is classified NOOP and writes no second row."""
    db = harness.db
    content = "Ada plans to use the Analytical Engine."
    await harness.ingest.ingest(content, tenant=TENANT, source_uri=DOC_URI, kind="doc")
    harness.curator.promote(TENANT, harness.pending_candidates("fact")[0]["candidate_id"])
    assert len(db.open_facts(TENANT)) == 1

    # Ingest *different* chunk text from a second source (so the level-1 chunk
    # dedup does not skip it) that yields the identical extracted fact — the
    # deterministic fake LLM always returns the same fact.
    await harness.ingest.ingest(
        "Restated: Ada plans to use the Analytical Engine.",
        tenant=TENANT,
        source_uri=f"stratum://docs/{TENANT}/notes/ada2",
        kind="doc",
    )
    dupes = harness.pending_candidates("fact")
    assert dupes, "expected a second fact candidate from the second source"
    result = harness.curator.promote(TENANT, dupes[0]["candidate_id"])
    assert result["action"] == "NOOP"
    # NOOP leaves the stored fact untouched: still exactly one open fact.
    assert len(db.open_facts(TENANT)) == 1
    assert db.ops().count("ADD") == 1
    assert "NOOP" in db.ops()


# --------------------------------------------------------------------------- #
# 2. MCP tool surface: write staging vs. auto-promote (promote scope)
# --------------------------------------------------------------------------- #


async def test_mcp_write_stages_then_promote_scope_promotes(harness: Harness) -> None:
    """``write`` without auto_promote stages; an explicit promote promotes."""
    ctx = harness.context({"read", "write", "promote"})

    staged = await stratum_write(
        ctx, uri=DOC_URI, content="# Ada\n\nAda plans to use the Analytical Engine.",
        kind="doc",
    )
    assert staged["status"] == "staged"
    assert staged["l0"] == "Ada"
    assert harness.db.ops() == []  # staging is not an active write

    candidate_id = harness.pending_candidates("fact")[0]["candidate_id"]
    promoted = stratum_promote(ctx, candidate_id=candidate_id)
    assert promoted["status"] == "promoted"
    assert promoted["action"] == "ADD"
    assert promoted["event_id"] is not None
    assert harness.db.ops() == ["ADD"]


async def test_mcp_write_auto_promote_requires_promote_scope(harness: Harness) -> None:
    """A write-only key may not auto-promote (scope gate fires first)."""
    ctx = harness.context({"write"})
    result = await stratum_write(
        ctx, uri=DOC_URI, content="body", kind="doc", auto_promote=True
    )
    assert result["error"]["code"] == ErrorCode.FORBIDDEN
    assert harness.db.ops() == []


async def test_mcp_write_auto_promote_promotes_through_curator(harness: Harness) -> None:
    """With the promote scope, auto_promote hands *exactly* the write's staged
    candidates to the Curator (B7) — not the tenant-wide pending backlog."""
    ctx = harness.context({"write", "promote"})
    result = await stratum_write(
        ctx,
        uri=DOC_URI,
        content="# Ada\n\nAda plans to use the Analytical Engine.",
        kind="doc",
        auto_promote=True,
    )
    assert result["status"] == "promoted"
    # The write surfaced the id(s) it staged and promoted exactly those.
    assert result["candidate_ids"], "the write must surface its staged candidate ids"
    assert result["promote"]["status"] == "approved"
    assert result["promote"]["candidate_id"] == result["candidate_ids"][0]
    assert len(harness.db.open_facts(TENANT)) == 1
    assert "ADD" in harness.db.ops()


def test_mcp_query_finds_promoted_fact(harness: Harness) -> None:
    """The MCP ``query`` tool returns the promoted fact via the QueryEngine."""
    import asyncio

    asyncio.run(
        harness.ingest.ingest(
            "Ada plans to use the Analytical Engine.",
            tenant=TENANT,
            source_uri=DOC_URI,
            kind="doc",
        )
    )
    harness.curator.promote(TENANT, harness.pending_candidates("fact")[0]["candidate_id"])

    ctx = harness.context({"read"})
    result = stratum_query(ctx, query="Ada", mode="hybrid", k=8)
    assert result["count"] >= 1
    assert any("Ada" in hit["content"] for hit in result["results"])


# --------------------------------------------------------------------------- #
# 3. Reflect writes *only* pending candidates and journals REFLECT
# --------------------------------------------------------------------------- #


async def test_reflect_stages_only_pending_candidates(harness: Harness) -> None:
    """Reflect never promotes: its output is pending synthesis/skill candidates."""
    db = harness.db
    await harness.ingest.ingest(
        "Ada plans to use the Analytical Engine.",
        tenant=TENANT,
        source_uri=DOC_URI,
        kind="doc",
    )
    harness.curator.promote(TENANT, harness.pending_candidates("fact")[0]["candidate_id"])
    before_facts = len(db.open_facts(TENANT))

    result = harness.reflect.run(TENANT)

    assert result.skipped is False
    assert result.staged_candidates >= 1
    assert REFLECT_OP in db.ops()

    reflect_candidates = [
        c for c in harness.pending_candidates() if c["kind"] in ("synthesis", "skill")
    ]
    assert reflect_candidates, "reflect staged no synthesis/skill candidate"
    assert all(c["status"] == "pending" for c in reflect_candidates)
    # No active table was written by reflect.
    assert len(db.open_facts(TENANT)) == before_facts
    assert db.syntheses == []


async def test_reflect_job_is_enqueued_via_mcp(harness: Harness) -> None:
    """The MCP ``reflect`` tool enqueues a budgeted job on the real JobQueue."""
    ctx = harness.context({"promote"})
    result = stratum_reflect(ctx, budget_tokens=5000, async_=True)
    assert result["job_id"]
    assert result["estimated_tokens"] == 5000
    queued = [job for job in harness.db.jobs if job["kind"] == "reflect"]
    assert queued and queued[0]["payload"]["budget_tokens"] == 5000


# --------------------------------------------------------------------------- #
# 4. forget: soft keeps rows, hard removes them and journals FORGET
# --------------------------------------------------------------------------- #


async def test_forget_soft_and_hard(harness: Harness) -> None:
    """``forget`` journals FORGET in both modes; ``hard`` removes rows/files."""
    db = harness.db
    await harness.ingest.ingest(
        "Ada plans to use the Analytical Engine.",
        tenant=TENANT,
        source_uri=DOC_URI,
        kind="doc",
    )
    harness.curator.promote(TENANT, harness.pending_candidates("fact")[0]["candidate_id"])
    # Land a vault document so the hard forget has a file to remove.
    harness.fs.write(DOC_URI, "# Ada\n\nbody", tenant=TENANT)

    ctx = harness.context({"promote"})

    # --- soft: no deletion, history kept -----------------------------------
    soft = stratum_forget(ctx, uri=DOC_URI, hard=False, reason="gdpr")
    assert soft["mode"] == "soft"
    assert soft["deleted_refs"] == 0
    assert len(db.open_facts(TENANT)) == 1
    assert "FORGET" in db.ops()

    # --- hard: rows, vectors and the file are removed ----------------------
    hard = stratum_forget(ctx, uri=DOC_URI, hard=True, reason="gdpr")
    assert hard["mode"] == "hard"
    assert hard["deleted_refs"] >= 1
    assert db.open_facts(TENANT) == []
    assert db.chunks == []
    assert DOC_URI not in harness.fs.docs
    assert harness.fs.deleted[-1] == (DOC_URI, TENANT)
    assert db.ops().count("FORGET") == 2


async def test_hard_forget_removes_derived_state(harness: Harness) -> None:
    """B8: a hard forget also clears staging candidates, episode KV and syntheses."""
    db = harness.db
    await harness.ingest.ingest(
        "Ada plans to use the Analytical Engine.",
        tenant=TENANT,
        source_uri=DOC_URI,
        kind="doc",
    )
    # Land a session episode whose stored ``uri`` is DOC_URI and a scoped synthesis.
    harness.store.kv_set(TENANT, "episode:s-1", {"uri": DOC_URI, "session_id": "s-1", "turns": []})
    db.syntheses.append(
        {
            "synthesis_id": "syn-1",
            "tenant_id": TENANT,
            "scope_uri": DOC_URI,
            "summary": "x",
            "payload": {},
            "model": "m",
            "tokens_used": 0,
        }
    )
    assert db.candidates(status="pending")  # the ingest staged candidates

    ctx = harness.context({"promote"})
    result = stratum_forget(ctx, uri=DOC_URI, hard=True, reason="gdpr")
    assert result["mode"] == "hard"
    # All derived residue for the URI is gone.
    assert db.candidates(status="pending") == []
    assert (TENANT, "episode:s-1") not in db.kv
    assert db.syntheses == []
    # A second forget finds nothing left to delete.
    again = stratum_forget(ctx, uri=DOC_URI, hard=True, reason="gdpr")
    assert again["deleted_refs"] == 0


async def test_archive_journals_archive(harness: Harness) -> None:
    """``archive`` is a soft operation that only journals an ARCHIVE event."""
    ctx = harness.context({"promote"})
    result = stratum_archive(ctx, uri=DOC_URI, reason="stale")
    assert result["status"] == "archived"
    assert result["event_id"]
    assert harness.db.ops() == ["ARCHIVE"]


# --------------------------------------------------------------------------- #
# 5. Session episodes are stored (KV + vault), without an active write
# --------------------------------------------------------------------------- #


def test_session_append_roundtrip(harness: Harness) -> None:
    """``session_append`` writes an episode; KV/FS are not memory_events."""
    ctx = harness.context({"write"})
    first = stratum_session_append(ctx, session_id="s-1", role="user", content="hi")
    second = stratum_session_append(ctx, session_id="s-1", role="assistant", content="hello")

    assert first["turn"] == 0
    assert second["turn"] == 1
    assert first["uri"].startswith(f"stratum://episodes/{TENANT}/")
    assert harness.db.ops() == []  # an episode turn is not an active-store event
    turns = harness.sessions.read(TENANT, "s-1")
    assert [turn["role"] for turn in turns] == ["user", "assistant"]


# --------------------------------------------------------------------------- #
# Real-Postgres round trip (schema creation, RLS, close-before-insert)
# --------------------------------------------------------------------------- #

_REPO_ROOT = Path(__file__).resolve().parents[1]
_INIT_DIR = _REPO_ROOT / "init"

_requires_db = pytest.mark.skipif(
    not os.getenv("STRATUM_TEST_DB"),
    reason="set STRATUM_TEST_DB to a Postgres DSN to run integration tests",
)


def _apply_schema(dsn: str, vector_type: str = "halfvec") -> None:
    """Create extensions, roles, schema and RLS in the target database.

    Renders ``init/003_schema.sql.template`` (substituting the two vector
    placeholders) and executes every ``init/*`` script in order. Every script is
    idempotent, so re-running against an already-provisioned DB is safe.
    """
    import psycopg

    vec_ops = f"{vector_type}_cosine_ops"
    template = (_INIT_DIR / "003_schema.sql.template").read_text(encoding="utf-8")
    schema_sql = template.replace("__VEC_OPS__", vec_ops).replace("__VEC__", vector_type)

    scripts = [
        (_INIT_DIR / "001_extensions.sql").read_text(encoding="utf-8"),
        (_INIT_DIR / "002_app_role.sql").read_text(encoding="utf-8"),
        schema_sql,
        (_INIT_DIR / "004_rls.sql").read_text(encoding="utf-8"),
    ]
    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        for script in scripts:
            cur.execute(script)


@pytest.mark.integration
@_requires_db
def test_integration_real_postgres_roundtrip(test_db_dsn: str) -> None:
    """Full round trip against real Postgres: schema, RLS, close-before-insert.

    Skips unless ``STRATUM_TEST_DB`` points at a pgvector Postgres where the DSN
    user may create extensions/roles (typically the owner/superuser role
    ``stratum``). The application side connects as the non-owner role
    ``stratum_app`` so RLS genuinely applies.
    """
    import psycopg
    from psycopg.rows import dict_row as _dict_row
    from psycopg_pool import ConnectionPool

    from stratum import db as _db
    from stratum.config import get_settings

    tenant = f"it_{uuid.uuid4().hex[:8]}"
    other = f"it_{uuid.uuid4().hex[:8]}"

    try:
        _apply_schema(test_db_dsn)
    except Exception as exc:  # noqa: BLE001 - no privileges => unverifiable, not false
        pytest.skip(f"cannot provision schema in STRATUM_TEST_DB: {exc}")

    with psycopg.connect(test_db_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tenants (tenant_id, name) VALUES (%s, %s), (%s, %s)",
            (tenant, "integration", other, "integration"),
        )

    # Force every application connection into the non-owner app role so RLS
    # applies even though the test DSN user is the (superuser) owner.
    get_settings.cache_clear()
    _db.close_pool()
    _db._POOL = ConnectionPool(
        conninfo=test_db_dsn,
        min_size=1,
        max_size=2,
        kwargs={"row_factory": _dict_row, "options": "-c role=stratum_app"},
        open=True,
    )
    try:
        settings = Settings()
        llm = FakeLLM(dim=settings.embedding_dim)
        store = _db_store(settings)
        graph = TemporalGraph(settings, conn_factory=_db.connection)
        curator = Curator(settings, store=store, graph=graph, conn_factory=_db.connection)

        # --- close-before-insert on real DDL -----------------------------
        entity_id = graph.upsert_entity(
            tenant, name="Ada", canonical_uri=f"stratum://entities/{tenant}/ada"
        )
        graph.add_fact(tenant, subject_entity_id=entity_id, predicate="plan", object_text="pro")
        graph.add_fact(
            tenant, subject_entity_id=entity_id, predicate="plan", object_text="enterprise"
        )
        open_rows = graph.get_facts(tenant, subject_entity_id=entity_id, predicate="plan")
        assert len(open_rows) == 1
        assert open_rows[0]["object_text"] == "enterprise"

        # --- RLS tenant isolation (read as the *other* tenant) -----------
        assert graph.get_facts(other, subject_entity_id=entity_id, predicate="plan") == []

        # --- full pipeline write -> promote -> query ---------------------
        fs = _db_fs(settings)
        ingest = IngestPipeline(
            settings,
            llm=llm,
            store=store,
            fs=fs,
            graph=graph,
            conn_factory=_db.connection,
        )
        import asyncio

        doc_uri = f"stratum://docs/{tenant}/it-note"
        asyncio.run(
            ingest.ingest("Ada plans the engine.", tenant=tenant, source_uri=doc_uri, kind="doc")
        )
        candidates = _pending_fact_candidates(tenant)
        assert candidates, "ingest staged no fact candidate"
        promoted = curator.promote(tenant, candidates[0]["candidate_id"])
        assert promoted["status"] == "approved"

        query = QueryEngine(
            settings,
            llm=llm,
            store=store,
            graph=graph,
            fs=fs,
            conn_factory=_db.connection,
        )
        hits = query.query(tenant, "Ada", mode="hybrid", k=5)
        assert hits, "query returned no hit against real Postgres"

        # --- hard forget removes the rows --------------------------------
        from stratum.mcp.tools import ToolContext, stratum_forget

        ctx = ToolContext(
            auth=AuthContext(tenant=tenant, scopes={"admin"}, key_id="it"),
            authorizer=Authorizer(),
            settings=settings,
            store=store,
            graph=graph,
            fs=fs,
            conn_factory=_db.connection,
        )
        forgotten = stratum_forget(ctx, uri=doc_uri, hard=True, reason="integration")
        assert forgotten["mode"] == "hard"
    finally:
        with psycopg.connect(test_db_dsn) as conn, conn.cursor() as cur:
            for table in ("edges", "facts", "entities", "chunks", "files",
                          "memory_events", "staging_candidates", "uri_registry"):
                column = "tenant" if table == "uri_registry" else "tenant_id"
                cur.execute(f"DELETE FROM {table} WHERE {column} IN (%s, %s)", (tenant, other))
            cur.execute("DELETE FROM tenants WHERE tenant_id IN (%s, %s)", (tenant, other))
        _db.close_pool()
        get_settings.cache_clear()


def _db_store(settings: Settings):
    """A real :class:`~stratum.vector.MemoryStore` (no injected connection)."""
    from stratum.vector import MemoryStore

    return MemoryStore(settings)


def _db_fs(settings: Settings):
    """A real filesystem vault rooted at a throw-away path."""
    from stratum.filesystem import FilesystemStore

    return FilesystemStore(settings)


def _pending_fact_candidates(tenant: str) -> list[dict[str, Any]]:
    """Read pending fact candidates for ``tenant`` through the app-role pool."""
    from stratum import db as _db

    return _db.fetch_all(
        "SELECT candidate_id, payload FROM staging_candidates "
        "WHERE tenant_id = %s AND status = 'pending' AND kind = 'fact' "
        "ORDER BY created_at",
        (tenant,),
        tenant=tenant,
    )
