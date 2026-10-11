"""Shared, **optional** pytest fixtures for the Stratum-Memory test suite.

This file is deliberately additive: every ``tests/test_*.py`` module keeps its own
local fakes, and nothing here overrides or shadows an existing fixture or name.
The fixtures exist for two reasons:

1. **Cross-module end-to-end tests** (``tests/test_e2e.py``) need one *shared*
   in-memory backend that the real modules can be wired to. Instead of inventing
   yet another per-file fake, the E2E test reuses the deterministic fakes below:
   a token-metering :class:`FakeLLM`, an :class:`InMemoryDB` whose cursor routes
   the SQL the real modules emit, an :class:`InMemoryStore` (a
   :class:`~stratum.vector.MemoryStore` subclass, so the *real* dedup classifier
   runs) and an :class:`InMemoryFS` vault.
2. **Integration gating** — the ``test_db_dsn`` fixture skips unless
   ``STRATUM_TEST_DB`` points at a live pgvector Postgres, mirroring the
   per-module ``requires_db`` markers.

None of the fixtures touch Postgres, the network or the real vault: they are
DB-free, deterministic and side-effect-free.
"""

from __future__ import annotations

import hashlib
import os
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from stratum.config import Settings
from stratum.filesystem import make_l0, make_l1
from stratum.vector import VALID_OPS, MemoryStore, cosine_similarity

__all__ = [
    "FakeLLM",
    "InMemoryDB",
    "InMemoryFS",
    "InMemoryStore",
]

#: Default tenant used by the shared fixtures.
DEFAULT_TENANT = "e2e"

_WS_RE = re.compile(r"\s+")


def _norm(sql: str) -> str:
    """Collapse whitespace in ``sql`` (the modules emit multi-line SQL)."""
    return _WS_RE.sub(" ", sql).strip()


def _unwrap(value: Any) -> Any:
    """Unwrap a psycopg ``Jsonb``/adapter wrapper to its plain Python value."""
    return getattr(value, "obj", value)


def _unwrap_params(params: Any) -> list[Any]:
    """Normalise a params tuple/list, unwrapping JSON adapters."""
    if params is None:
        return []
    if isinstance(params, (tuple, list)):
        return [_unwrap(p) for p in params]
    return [_unwrap(params)]


def _new_id() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(UTC)


def _vector_text(vector: Any) -> str | None:
    """Render an embedding as the pgvector text literal ``[a,b,c]`` (or ``None``)."""
    if vector is None:
        return None
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"


def _parse_vector(value: Any) -> list[float] | None:
    """Parse a pgvector text literal (``[0.1,0.2]``) or a sequence into floats."""
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return [float(x) for x in value]
    text = str(value).strip().strip("[]")
    if not text:
        return None
    try:
        return [float(part) for part in text.split(",")]
    except ValueError:
        return None


def _first_list(params: list[Any]) -> set[Any]:
    """Return the first list/tuple/set argument as a set (for ``IN (…)`` params).

    The modules interleave scalar tenant placeholders with URI/entity candidate
    lists, so the position of the list differs per statement; finding the first
    list is the robust way to recover it.
    """
    for value in params:
        if isinstance(value, (list, tuple, set)):
            return set(value)
    return set()


# --------------------------------------------------------------------------- #
# Deterministic fake LLM
# --------------------------------------------------------------------------- #


class _Usage:
    """Minimal ``Usage`` look-alike (only ``total_tokens`` is read)."""

    def __init__(self, total_tokens: int = 0) -> None:
        self.total_tokens = total_tokens

    def as_dict(self) -> dict[str, int]:
        return {"total_tokens": self.total_tokens}


class FakeLLM:
    """Deterministic, offline chat/embed/fetch double with token metering.

    The vectors are a stable function of the input text (SHA-256 seeded), so two
    identical texts always embed identically and cosine similarity is meaningful
    without a real model.

    Args:
        dim: Embedding dimension (small by default to keep tests fast).
        facts_per_chunk: How many bi-temporal facts ``chat`` returns per call.
        embed_cost / chat_cost: Token counters added per call (budget metering).
        skill: Whether the reflect ``chat`` reply suggests a skill.
    """

    def __init__(
        self,
        *,
        dim: int = 8,
        facts_per_chunk: int = 1,
        embed_cost: int = 1,
        chat_cost: int = 5,
        skill: bool = True,
        fetch_body: bytes | None = None,
        fetch_status: int = 200,
    ) -> None:
        self.dim = dim
        self.facts_per_chunk = facts_per_chunk
        self.embed_cost = embed_cost
        self.chat_cost = chat_cost
        self.skill = skill
        self.fetch_body = fetch_body if fetch_body is not None else b""
        self.fetch_status = fetch_status
        self.embed_calls = 0
        self.chat_calls = 0
        self.fetch_calls = 0
        self._total = 0

    # -- metering ----------------------------------------------------------

    def usage(self) -> _Usage:
        return _Usage(self._total)

    # -- deterministic vectors --------------------------------------------

    def _vector(self, text: str) -> list[float]:
        # Expand the 32-byte digest to `dim` bytes deterministically by hashing
        # counter-suffixed blocks (a single SHA-256 digest is only 32 bytes).
        raw: list[float] = []
        block = 0
        while len(raw) < self.dim:
            digest = hashlib.sha256(f"{block}:{text}".encode()).digest()
            raw.extend(((b / 255.0) * 2.0) - 1.0 for b in digest)
            block += 1
        raw = raw[: self.dim]
        norm = sum(x * x for x in raw) ** 0.5 or 1.0
        return [x / norm for x in raw]

    # -- async API (mirrors LLMClient) ------------------------------------

    async def embed(self, texts: Any, *, model: str | None = None) -> list[list[float]]:
        inputs = [texts] if isinstance(texts, str) else list(texts)
        self.embed_calls += 1
        self._total += self.embed_cost * len(inputs)
        return [self._vector(str(text)) for text in inputs]

    async def chat(self, messages: Any, **kwargs: Any) -> Any:
        self.chat_calls += 1
        self._total += self.chat_cost
        # A schema-bound fact extraction is recognised by the caller's schema
        # keyword; otherwise reply with a reflect-style cluster summary.
        schema = kwargs.get("json_schema") or {}
        name = schema.get("name") if isinstance(schema, dict) else None
        if name == "fact_extraction":
            facts = [
                {
                    "subject": "Ada",
                    "predicate": "plans",
                    "object": "to use the Analytical Engine",
                    "object_text": "to use the Analytical Engine",
                    "valid_from": "2024-01-01T00:00:00Z",
                    "valid_to": None,
                    "confidence": 0.9,
                }
                for _ in range(self.facts_per_chunk)
            ]
            content: Any = {"facts": facts}
        else:
            content = {
                "summary": f"summary {self.chat_calls}",
                "patterns": ["pattern-a", "pattern-b"],
                "skill_suggestion": "reuse the plan pattern" if self.skill else None,
            }
        return type("ChatResult", (), {"content": content, "model": "fake-model"})()

    async def fetch(self, url: str, **kwargs: Any) -> tuple[int, bytes]:
        self.fetch_calls += 1
        return self.fetch_status, self.fetch_body


# --------------------------------------------------------------------------- #
# In-memory SQL backend
# --------------------------------------------------------------------------- #


class _Cursor:
    """Cursor routing every statement to the shared :class:`InMemoryDB`."""

    def __init__(self, db: InMemoryDB) -> None:
        self._db = db
        self._last: Any = None
        self._rows: list[Any] = []
        self.rowcount = 0

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, sql: str, params: Any = ()) -> None:
        result = self._db._handle(_norm(sql), params)
        if isinstance(result, int):
            # A DELETE/UPDATE returning a row count.
            self.rowcount = result
            self._last = None
            self._rows = []
        elif isinstance(result, list):
            self.rowcount = len(result)
            self._last = None
            self._rows = result
        else:
            self.rowcount = 0 if result is None else 1
            self._last = result
            self._rows = []

    def fetchone(self) -> Any:
        return self._last

    def fetchall(self) -> list[Any]:
        return list(self._rows)


class _Connection:
    """Connection whose cursors share one :class:`InMemoryDB`; ``commit`` no-op."""

    def __init__(self, db: InMemoryDB) -> None:
        self._db = db

    def __enter__(self) -> _Connection:
        return self

    def __exit__(self, *exc: object) -> bool:
        # The in-memory backend applies writes immediately; no rollback model.
        return False

    def cursor(self) -> _Cursor:
        return _Cursor(self._db)

    def commit(self) -> None:
        return None


class InMemoryDB:
    """An in-memory model of the Stratum tables the real modules touch.

    The class is *not* a SQL engine: :meth:`_handle` recognises the exact
    statement shapes the modules emit and applies the matching mutation to plain
    dicts/lists. It is shared by every collaborator (graph, curator, ingest,
    query, reflect, MCP tools) so a write made through one module is visible to
    the next — which is what makes the end-to-end test a real round trip.
    """

    def __init__(self) -> None:
        self.tenants: dict[str, dict[str, Any]] = {
            DEFAULT_TENANT: {"tenant_id": DEFAULT_TENANT, "name": "e2e"},
        }
        self.staging: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self.registry: dict[str, dict[str, Any]] = {}
        self.entities: list[dict[str, Any]] = []
        self.facts: list[dict[str, Any]] = []
        self.edges: list[dict[str, Any]] = []
        self.files: list[dict[str, Any]] = []
        self.chunks: list[dict[str, Any]] = []
        self.kv: dict[tuple[str, str], dict[str, Any]] = {}
        self.jobs: list[dict[str, Any]] = []
        self.syntheses: list[dict[str, Any]] = []
        self.audit_log: list[dict[str, Any]] = []

    # ---------------------------------------------------------------- factory

    def factory(self, tenant: str | None = None, **_kwargs: Any) -> _Connection:
        """Return a connection context manager (mirrors ``stratum.db.connection``)."""
        return _Connection(self)

    # ------------------------------------------------------------- introspection

    def ops(self) -> list[str]:
        """The ``memory_events.op`` values in insertion order."""
        return [event["op"] for event in self.events]

    def events_for(self, op: str) -> list[dict[str, Any]]:
        return [event for event in self.events if event["op"] == op]

    def candidates(self, *, status: str | None = None) -> list[dict[str, Any]]:
        return [c for c in self.staging if status is None or c["status"] == status]

    def open_facts(self, tenant: str) -> list[dict[str, Any]]:
        return [
            f
            for f in self.facts
            if f["tenant_id"] == tenant and f["valid_to"] is None and f["tx_to"] is None
        ]

    # ----------------------------------------------------------------- routing

    def _handle(self, sql: str, params: Any) -> Any:
        u = sql.upper()
        p = _unwrap_params(params)

        # --- staging_candidates -----------------------------------------
        if u.startswith("INSERT INTO STAGING_CANDIDATES"):
            return self._insert_candidate(u, p)
        if u.startswith("SELECT CANDIDATE_ID, TENANT_ID, KIND"):
            return self._select_candidate(p)
        if u.startswith("SELECT CANDIDATE_ID FROM STAGING_CANDIDATES"):
            return self._pending(p)
        if u.startswith("UPDATE STAGING_CANDIDATES"):
            return self._update_candidate(p)

        # --- entities ---------------------------------------------------
        if u.startswith("INSERT INTO ENTITIES"):
            return self._insert_entity(p)
        if u.startswith("SELECT ENTITY_ID FROM ENTITIES"):
            return self._entity_by_uri(p)
        if u.startswith("SELECT ENTITY_ID, NAME, CANONICAL_URI FROM ENTITIES"):
            return self._resolve_entity(p)

        # --- facts ------------------------------------------------------
        if u.startswith("SELECT VALID_FROM FROM FACTS"):
            return self._open_fact(p)
        if u.startswith("UPDATE FACTS SET VALID_TO"):
            return self._close_fact(p)
        if u.startswith("UPDATE FACTS SET SUPERSEDED_BY = NULL"):
            return self._detach_superseded(p)
        if u.startswith("INSERT INTO FACTS"):
            return self._insert_fact(p)
        if u.startswith("DELETE FROM FACTS"):
            return self._delete_facts(p)
        if u.startswith("SELECT FACT_ID, TENANT_ID, SUBJECT_ENTITY_ID"):
            return self._get_facts(p)
        if u.startswith("SELECT FACT_ID, SUBJECT_ENTITY_ID, PREDICATE"):
            return self._contradiction(sql, p)

        # --- fact joins (query enrich + reflect sample) -----------------
        if "E.NAME AS SUBJECT_NAME, F.SOURCE_URI, F.EMBEDDING::TEXT" in u:
            return self._reflect_sample(p)
        if u.startswith("SELECT F.FACT_ID, F.PREDICATE, F.OBJECT_TEXT, F.VALID_FROM"):
            return self._enrich_facts(p)

        # --- deletions (hard forget) ------------------------------------
        if u.startswith("DELETE FROM EDGES"):
            return self._delete_edges(p)
        if u.startswith("DELETE FROM ENTITIES"):
            return self._delete_entities(p)
        if u.startswith("DELETE FROM CHUNKS"):
            return self._delete_chunks(p)
        if u.startswith("DELETE FROM FILES"):
            return self._delete_files(p)
        if u.startswith("DELETE FROM URI_REGISTRY"):
            return self._delete_registry(p)
        if u.startswith("DELETE FROM STAGING_CANDIDATES") and "SOURCE_URI" in u:
            return self._delete_staging_by_source(p)
        if u.startswith("DELETE FROM KV"):
            return self._delete_kv_by_uri(p)
        if u.startswith("DELETE FROM SYNTHESES") and "SCOPE_URI" in u:
            return self._delete_syntheses_by_scope(p)

        # --- journal / registry / active side tables --------------------
        if u.startswith("INSERT INTO MEMORY_EVENTS"):
            return self._insert_event(u, p)
        if u.startswith("INSERT INTO AUDIT_LOG"):
            return self._insert_audit(p)
        if u.startswith("INSERT INTO URI_REGISTRY"):
            return self._upsert_registry(p)
        if u.startswith("INSERT INTO SYNTHESES"):
            return self._insert_synthesis(p)
        if u.startswith("INSERT INTO FILES"):
            return self._upsert_file(p)

        # --- reflect / jobs ---------------------------------------------
        if u.startswith("SELECT MAX(CREATED_AT) AS LAST_REFLECT"):
            return {"last_reflect": None}
        if u.startswith("SELECT COUNT(*) AS N FROM MEMORY_EVENTS"):
            return {"n": self._count_events(p)}
        if u.startswith("SELECT PG_TRY_ADVISORY_XACT_LOCK"):
            return {"acquired": True}
        if u.startswith("SELECT 1 FROM JOBS"):
            return None
        if u.startswith("INSERT INTO JOBS"):
            return self._insert_job(u, p)
        if u.startswith("UPDATE JOBS"):
            return self._update_job(u, p)

        # --- misc reads -------------------------------------------------
        if u.startswith("SELECT KEY, VALUE FROM KV"):
            return self._kv_scan(p)
        if u.startswith("SELECT 1 FROM CHUNKS"):
            return self._chunk_hash(p)
        if u.startswith("WITH RECURSIVE"):
            return []
        if u.startswith("SELECT 1"):
            return {"?column?": 1}

        raise AssertionError(f"unhandled SQL in InMemoryDB: {sql!r}")

    # ------------------------------------------------- staging_candidates

    def _insert_candidate(self, u: str, p: list[Any]) -> Any:
        if "'PENDING') RETURNING CANDIDATE_ID" in u:
            # Reflect: (tenant, kind, payload, source_uri); confidence/status fixed.
            tenant, kind, payload, source_uri = p
            confidence, status, ingest_id = 1.0, "pending", None
        else:
            # Ingest: (tenant, kind, payload, source_uri, confidence, status, ingest_id).
            tenant, kind, payload, source_uri, confidence, status, ingest_id = p
        row = {
            "candidate_id": _new_id(),
            "tenant_id": tenant,
            "kind": kind,
            "payload": dict(payload or {}),
            "source_uri": source_uri,
            "confidence": float(confidence),
            "status": status,
            "reason": None,
            "ingest_id": ingest_id,
            "created_at": _now(),
        }
        self.staging.append(row)
        if "RETURNING CANDIDATE_ID" in u:
            return {"candidate_id": row["candidate_id"]}
        return None

    def _select_candidate(self, p: list[Any]) -> Any:
        if len(p) == 1:
            candidate_id = p[0]
            tenant = None
        else:
            tenant, candidate_id = p
        for row in self.staging:
            if row["candidate_id"] == str(candidate_id) and (
                tenant is None or row["tenant_id"] == tenant
            ):
                return dict(row)
        return None

    def _pending(self, p: list[Any]) -> list[Any]:
        tenant, limit = p
        pending = [
            {"candidate_id": row["candidate_id"]}
            for row in self.staging
            if row["tenant_id"] == tenant and row["status"] == "pending"
        ]
        return pending[: int(limit)]

    def _update_candidate(self, p: list[Any]) -> Any:
        status, reason, tenant, candidate_id = p
        for row in self.staging:
            if row["tenant_id"] == tenant and row["candidate_id"] == str(candidate_id):
                row["status"] = status
                row["reason"] = reason
        return None

    # ------------------------------------------------------------- entities

    def _insert_entity(self, p: list[Any]) -> dict[str, Any]:
        tenant, name, canonical_uri, type_, aliases, embedding = p
        for row in self.entities:
            if row["tenant_id"] == tenant and row["canonical_uri"] == canonical_uri:
                row["name"] = name
                row["type"] = type_
                row["aliases"] = list(aliases or [])
                if embedding is not None:
                    row["embedding"] = embedding
                return {"entity_id": row["entity_id"]}
        row = {
            "entity_id": _new_id(),
            "tenant_id": tenant,
            "name": name,
            "canonical_uri": canonical_uri,
            "type": type_,
            "aliases": list(aliases or []),
            "embedding": embedding,
            "created_at": _now(),
        }
        self.entities.append(row)
        return {"entity_id": row["entity_id"]}

    def _entity_by_uri(self, p: list[Any]) -> Any:
        tenant, canonical_uri = p
        for row in self.entities:
            if row["tenant_id"] == tenant and row["canonical_uri"] == canonical_uri:
                return {"entity_id": row["entity_id"]}
        return None

    def _resolve_entity(self, p: list[Any]) -> Any:
        if len(p) < 3:
            # Vector fallback: only entities with an embedding would match; none.
            return None
        tenant, name, _name2 = p
        for row in self.entities:
            if row["tenant_id"] != tenant:
                continue
            if row["name"].lower() == str(name).lower() or name in (row.get("aliases") or []):
                return {
                    "entity_id": row["entity_id"],
                    "name": row["name"],
                    "canonical_uri": row["canonical_uri"],
                }
        return None

    # ---------------------------------------------------------------- facts

    def _open_fact(self, p: list[Any]) -> Any:
        tenant, subject_entity_id, predicate = p
        for row in self.facts:
            if (
                row["tenant_id"] == tenant
                and row["subject_entity_id"] == subject_entity_id
                and row["predicate"] == predicate
                and row["valid_to"] is None
                and row["tx_to"] is None
            ):
                return {"valid_from": row["valid_from"]}
        return None

    def _close_fact(self, p: list[Any]) -> None:
        valid_to, tenant, subject_entity_id, predicate = p
        for row in self.facts:
            if (
                row["tenant_id"] == tenant
                and row["subject_entity_id"] == subject_entity_id
                and row["predicate"] == predicate
                and row["valid_to"] is None
                and row["tx_to"] is None
            ):
                row["valid_to"] = valid_to
                row["tx_to"] = _now()
        return None

    def _detach_superseded(self, p: list[Any]) -> int:
        # (tenant, tenant, [candidates], tenant, [candidates], tenant, [candidates])
        tenant = p[0]
        candidates = _first_list(p)
        fact_ids = self._fact_ids_for_candidates(tenant, candidates)
        count = 0
        for row in self.facts:
            if row["tenant_id"] == tenant and row.get("superseded_by") in fact_ids:
                row["superseded_by"] = None
                count += 1
        return count

    def _insert_fact(self, p: list[Any]) -> dict[str, Any]:
        (
            tenant,
            subject_entity_id,
            predicate,
            object_text,
            object_entity_id,
            valid_from,
            valid_to,
            confidence,
            source_uri,
            source_span,
            embedding,
        ) = p
        row = {
            "fact_id": _new_id(),
            "tenant_id": tenant,
            "subject_entity_id": subject_entity_id,
            "predicate": predicate,
            "object_text": object_text,
            "object_entity_id": object_entity_id,
            "valid_from": valid_from,
            "valid_to": valid_to,
            "tx_from": _now(),
            "tx_to": None,
            "confidence": float(confidence),
            "source_uri": source_uri,
            "source_span": source_span,
            "superseded_by": None,
            "embedding": embedding,
            "created_at": _now(),
        }
        self.facts.append(row)
        return {"fact_id": row["fact_id"]}

    def _get_facts(self, p: list[Any]) -> list[Any]:
        tenant = p[0]
        subject_entity_id = p[1] if len(p) > 1 else None
        rows = []
        for row in self.facts:
            if row["tenant_id"] != tenant:
                continue
            if subject_entity_id is not None and row["subject_entity_id"] != subject_entity_id:
                continue
            if row["valid_to"] is not None or row["tx_to"] is not None:
                continue
            rows.append({k: v for k, v in row.items()})
        return rows

    def _contradiction(self, sql: str, p: list[Any]) -> Any:
        tenant, subject_entity_id, predicate, value = p
        # The query compares on the entity axis when the assertion is an entity
        # id, else on the text axis (see TemporalGraph.contradiction_check).
        by_entity = "OBJECT_ENTITY_ID IS NOT NULL" in sql.upper()
        for row in self.facts:
            if (
                row["tenant_id"] != tenant
                or row["subject_entity_id"] != subject_entity_id
                or row["predicate"] != predicate
                or row["valid_to"] is not None
                or row["tx_to"] is not None
            ):
                continue
            if by_entity:
                if row["object_entity_id"] is not None and row["object_entity_id"] != value:
                    return self._fact_row(row)
            else:
                if row["object_text"] is not None and row["object_text"] != value:
                    return self._fact_row(row)
        return None

    @staticmethod
    def _fact_row(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "fact_id": row["fact_id"],
            "subject_entity_id": row["subject_entity_id"],
            "predicate": row["predicate"],
            "object_text": row["object_text"],
            "object_entity_id": row["object_entity_id"],
            "valid_from": row["valid_from"],
            "confidence": row["confidence"],
        }

    def _fact_ids_for_candidates(self, tenant: str, candidates: set[str]) -> set[str]:
        entity_ids = {
            row["entity_id"]
            for row in self.entities
            if row["tenant_id"] == tenant and row["canonical_uri"] in candidates
        }
        ids: set[str] = set()
        for row in self.facts:
            if row["tenant_id"] != tenant:
                continue
            if (
                row["source_uri"] in candidates
                or row["subject_entity_id"] in entity_ids
                or row["object_entity_id"] in entity_ids
            ):
                ids.add(row["fact_id"])
        return ids

    # ---------------------------------------------------- query/reflect joins

    def _enrich_facts(self, p: list[Any]) -> list[Any]:
        tenant, ids = p
        wanted = {str(i) for i in (ids or [])}
        out = []
        for row in self.facts:
            if row["tenant_id"] != tenant or row["fact_id"] not in wanted:
                continue
            subject = next(
                (e["name"] for e in self.entities if e["entity_id"] == row["subject_entity_id"]),
                None,
            )
            out.append(
                {
                    "fact_id": row["fact_id"],
                    "predicate": row["predicate"],
                    "object_text": row["object_text"],
                    "valid_from": row["valid_from"],
                    "valid_to": row["valid_to"],
                    "confidence": row["confidence"],
                    "source_uri": row["source_uri"],
                    "subject_name": subject,
                }
            )
        return out

    def _reflect_sample(self, p: list[Any]) -> list[Any]:
        tenant = p[0]
        out = []
        for row in self.facts:
            if row["tenant_id"] != tenant:
                continue
            subject = next(
                (e["name"] for e in self.entities if e["entity_id"] == row["subject_entity_id"]),
                None,
            )
            out.append(
                {
                    "fact_id": row["fact_id"],
                    "predicate": row["predicate"],
                    "object_text": row["object_text"],
                    "subject_name": subject,
                    "source_uri": row["source_uri"],
                    "embedding_text": row["embedding"],
                    "created_at": row["created_at"],
                }
            )
        return out

    # -------------------------------------------------------------- deletions

    def _delete_edges(self, p: list[Any]) -> int:
        tenant = p[0]
        candidates = _first_list(p)
        fact_ids = self._fact_ids_for_candidates(tenant, candidates)
        entity_ids = {
            row["entity_id"]
            for row in self.entities
            if row["tenant_id"] == tenant and row["canonical_uri"] in candidates
        }
        before = len(self.edges)
        self.edges = [
            e
            for e in self.edges
            if not (
                e["tenant_id"] == tenant
                and (
                    e.get("fact_id") in fact_ids
                    or e.get("src_entity_id") in entity_ids
                    or e.get("dst_entity_id") in entity_ids
                )
            )
        ]
        return before - len(self.edges)

    def _delete_facts(self, p: list[Any]) -> int:
        tenant = p[0]
        candidates = _first_list(p)
        fact_ids = self._fact_ids_for_candidates(tenant, candidates)
        before = len(self.facts)
        self.facts = [
            f
            for f in self.facts
            if not (f["tenant_id"] == tenant and f["fact_id"] in fact_ids)
        ]
        return before - len(self.facts)

    def _delete_entities(self, p: list[Any]) -> int:
        tenant = p[0]
        candidates = _first_list(p)
        before = len(self.entities)
        self.entities = [
            e
            for e in self.entities
            if not (e["tenant_id"] == tenant and e["canonical_uri"] in candidates)
        ]
        return before - len(self.entities)

    def _delete_chunks(self, p: list[Any]) -> int:
        tenant, uri = p
        before = len(self.chunks)
        self.chunks = [
            c for c in self.chunks if not (c["tenant_id"] == tenant and c["source_uri"] == uri)
        ]
        return before - len(self.chunks)

    def _delete_files(self, p: list[Any]) -> int:
        tenant, uri = p
        before = len(self.files)
        self.files = [f for f in self.files if not (f["tenant_id"] == tenant and f["uri"] == uri)]
        return before - len(self.files)

    def _delete_registry(self, p: list[Any]) -> int:
        (uri,) = p
        existed = self.registry.pop(uri, None)
        return 1 if existed is not None else 0

    def _delete_staging_by_source(self, p: list[Any]) -> int:
        tenant, uri = p
        before = len(self.staging)
        self.staging = [
            c for c in self.staging if not (c["tenant_id"] == tenant and c["source_uri"] == uri)
        ]
        return before - len(self.staging)

    def _delete_kv_by_uri(self, p: list[Any]) -> int:
        tenant, uri = p
        keys = [
            key
            for (row_tenant, key), value in self.kv.items()
            if row_tenant == tenant and str(value.get("uri")) == uri
        ]
        for key in keys:
            del self.kv[(tenant, key)]
        return len(keys)

    def _delete_syntheses_by_scope(self, p: list[Any]) -> int:
        tenant, scope_uri = p
        before = len(self.syntheses)
        self.syntheses = [
            s
            for s in self.syntheses
            if not (s["tenant_id"] == tenant and s.get("scope_uri") == scope_uri)
        ]
        return before - len(self.syntheses)

    # ------------------------------------------------------- journal/registry

    def _insert_event(self, u: str, p: list[Any]) -> dict[str, Any]:
        if "NULL, %S, %S) RETURNING" in u:
            tenant, op, target_uri, after, actor = p
            before = None
        else:
            tenant, op, target_uri, before, after, actor = p
        row = {
            "event_id": _new_id(),
            "tenant_id": tenant,
            "op": op,
            "target_uri": target_uri,
            "before": before,
            "after": after,
            "actor": actor,
            "created_at": _now(),
        }
        self.events.append(row)
        return {"event_id": row["event_id"]}

    def _upsert_registry(self, p: list[Any]) -> None:
        uri, bereich, tenant, kind, ref_id = p
        existing = self.registry.get(uri)
        if existing is not None and ref_id is None:
            ref_id = existing.get("ref_id")
        self.registry[uri] = {
            "uri": uri,
            "bereich": bereich,
            "tenant": tenant,
            "kind": kind,
            "ref_id": ref_id,
        }
        return None

    def _insert_synthesis(self, p: list[Any]) -> dict[str, Any]:
        tenant, scope_uri, summary, payload, model, tokens_used = p
        row = {
            "synthesis_id": _new_id(),
            "tenant_id": tenant,
            "scope_uri": scope_uri,
            "summary": summary,
            "payload": payload,
            "model": model,
            "tokens_used": int(tokens_used),
        }
        self.syntheses.append(row)
        return {"synthesis_id": row["synthesis_id"]}

    def _insert_audit(self, p: list[Any]) -> None:
        tenant, actor, action, target_uri, detail = p
        self.audit_log.append(
            {
                "tenant_id": tenant,
                "actor": actor,
                "action": action,
                "target_uri": target_uri,
                "detail": detail,
            }
        )
        return None

    def _upsert_file(self, p: list[Any]) -> dict[str, Any]:
        tenant, uri, path, content_hash, l0, l1, size_bytes, git_commit, embedding = p
        for row in self.files:
            if row["tenant_id"] == tenant and row["uri"] == uri:
                row.update(
                    path=path,
                    content_hash=content_hash,
                    l0=l0,
                    l1=l1,
                    size_bytes=int(size_bytes or 0),
                    git_commit=git_commit,
                )
                return {"file_id": row["file_id"]}
        row = {
            "file_id": _new_id(),
            "tenant_id": tenant,
            "uri": uri,
            "path": path,
            "content_hash": content_hash,
            "l0": l0,
            "l1": l1,
            "size_bytes": int(size_bytes or 0),
            "git_commit": git_commit,
            "embedding": embedding,
        }
        self.files.append(row)
        return {"file_id": row["file_id"]}

    # ---------------------------------------------------------- reflect/jobs

    def _count_events(self, p: list[Any]) -> int:
        tenant = p[0]
        return sum(
            1 for e in self.events if e["tenant_id"] == tenant and e["op"] != "REFLECT"
        )

    def _insert_job(self, u: str, p: list[Any]) -> dict[str, Any]:
        if "'RUNNING'" in u:
            tenant, payload, worker_id = p
            row = {
                "job_id": _new_id(),
                "tenant_id": tenant,
                "kind": "reflect",
                "payload": dict(payload or {}),
                "status": "running",
                "locked_by": worker_id,
                "locked_at": _now(),
                "attempts": 1,
                "max_attempts": 1,
                "last_error": None,
                "created_at": _now(),
            }
        else:
            tenant, kind, payload, max_attempts, run_after = p
            row = {
                "job_id": _new_id(),
                "tenant_id": tenant,
                "kind": kind,
                "payload": dict(payload or {}),
                "status": "queued",
                "locked_by": None,
                "locked_at": None,
                "attempts": 0,
                "max_attempts": int(max_attempts),
                "run_after": run_after,
                "last_error": None,
                "created_at": _now(),
            }
        self.jobs.append(row)
        return {"job_id": row["job_id"]}

    def _update_job(self, u: str, p: list[Any]) -> int:
        if "STATUS = 'DONE'" in u:
            (job_id,) = p
            for row in self.jobs:
                if row["job_id"] == str(job_id):
                    row["status"] = "done"
                    row["locked_by"] = None
                    row["locked_at"] = None
                    return 1
            return 0
        return 0

    # ------------------------------------------------------------------ misc

    def _kv_scan(self, p: list[Any]) -> list[Any]:
        tenant, query, _prefix, limit = p
        rows = []
        for (row_tenant, key), value in self.kv.items():
            if row_tenant != tenant:
                continue
            if key == query or key.startswith(query):
                rows.append({"key": key, "value": value})
        rows.sort(key=lambda row: row["key"])
        return rows[: int(limit)]

    def _chunk_hash(self, p: list[Any]) -> Any:
        tenant, digest = p
        for chunk in self.chunks:
            if chunk["tenant_id"] == tenant and chunk["content_hash"] == digest:
                return {"?column?": 1}
        return None


# --------------------------------------------------------------------------- #
# Vector/KV store: a real MemoryStore whose DB calls are in-memory
# --------------------------------------------------------------------------- #


class InMemoryStore(MemoryStore):
    """A :class:`~stratum.vector.MemoryStore` backed by :class:`InMemoryDB`.

    The DB-touching methods are overridden; :meth:`MemoryStore.decide` (the real
    ADD/UPDATE/DELETE/NOOP classifier, ADR-0005) is **inherited unchanged**, so
    the Curator exercises the production dedup logic against in-memory facts.
    """

    def __init__(self, db: InMemoryDB, settings: Settings) -> None:
        super().__init__(settings)
        self.db = db

    # -- chunks -----------------------------------------------------------

    def has_content_hash(self, tenant: str, digest: str) -> bool:
        return any(
            c["tenant_id"] == tenant and c["content_hash"] == digest for c in self.db.chunks
        )

    def add_chunk(
        self,
        tenant: str,
        *,
        source_uri: str,
        ordinal: int,
        text: str,
        embedding: list[float],
        content_hash: str | None = None,
    ) -> dict[str, Any]:
        digest = content_hash or hashlib.sha256(text.encode("utf-8")).hexdigest()
        if self.has_content_hash(tenant, digest):
            existing = next(
                c for c in self.db.chunks
                if c["tenant_id"] == tenant and c["content_hash"] == digest
            )
            return {"chunk_id": existing["chunk_id"], "skipped": True}
        row = {
            "chunk_id": _new_id(),
            "tenant_id": tenant,
            "source_uri": source_uri,
            "ordinal": ordinal,
            "text": text,
            "content_hash": digest,
            "embedding": list(embedding),
        }
        self.db.chunks.append(row)
        return {"chunk_id": row["chunk_id"], "skipped": False}

    def delete_by_source(self, tenant: str, uri: str) -> int:
        before = len(self.db.chunks)
        self.db.chunks = [
            c for c in self.db.chunks if not (c["tenant_id"] == tenant and c["source_uri"] == uri)
        ]
        return before - len(self.db.chunks)

    # -- KV ---------------------------------------------------------------

    def kv_get(self, tenant: str, key: str) -> dict[str, Any] | None:
        value = self.db.kv.get((tenant, key))
        return None if value is None else dict(value)

    def kv_set(
        self,
        tenant: str,
        key: str,
        value: dict[str, Any],
        expires_at: Any | None = None,
    ) -> None:
        self.db.kv[(tenant, key)] = dict(value)

    # -- events -----------------------------------------------------------

    def log_event(
        self,
        tenant: str,
        *,
        op: str,
        target_uri: str | None = None,
        before: Any | None = None,
        after: Any | None = None,
        actor: str | None = None,
    ) -> str:
        if op not in VALID_OPS:
            raise ValueError(f"invalid op {op!r}")
        row = {
            "event_id": _new_id(),
            "tenant_id": tenant,
            "op": op,
            "target_uri": target_uri,
            "before": before,
            "after": after,
            "actor": actor,
            "created_at": _now(),
        }
        self.db.events.append(row)
        return row["event_id"]

    # -- search -----------------------------------------------------------

    def find_similar_facts(
        self,
        tenant: str,
        embedding: Any,
        *,
        threshold: float,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        matches: list[tuple[float, dict[str, Any]]] = []
        for fact in self.db.facts:
            if (
                fact["tenant_id"] != tenant
                or fact["embedding"] is None
                or fact["valid_to"] is not None
                or fact["tx_to"] is not None
            ):
                continue
            stored = _parse_vector(fact["embedding"])
            if stored is None:
                continue
            similarity = cosine_similarity(embedding, stored)
            if similarity >= threshold:
                matches.append((similarity, fact))
        matches.sort(key=lambda item: (-item[0], item[1]["fact_id"]))
        return [
            {
                "fact_id": fact["fact_id"],
                "object_text": fact["object_text"],
                "similarity": similarity,
            }
            for similarity, fact in matches[:limit]
        ]

    def search_chunks(
        self,
        tenant: str,
        embedding: Any,
        *,
        k: int = 8,
        scope: str | None = None,
    ) -> list[dict[str, Any]]:
        matches: list[tuple[float, dict[str, Any]]] = []
        for chunk in self.db.chunks:
            if chunk["tenant_id"] != tenant or chunk["embedding"] is None:
                continue
            if scope and not chunk["source_uri"].startswith(scope):
                continue
            similarity = cosine_similarity(embedding, chunk["embedding"])
            matches.append((similarity, chunk))
        matches.sort(key=lambda item: (-item[0], item[1]["chunk_id"]))
        return [
            {
                "chunk_id": chunk["chunk_id"],
                "source_uri": chunk["source_uri"],
                "ordinal": chunk["ordinal"],
                "text": chunk["text"],
                "similarity": similarity,
            }
            for similarity, chunk in matches[:k]
        ]


# --------------------------------------------------------------------------- #
# In-memory filesystem vault
# --------------------------------------------------------------------------- #


class InMemoryFS:
    """An in-memory stand-in for :class:`stratum.filesystem.FilesystemStore`.

    It mirrors the public surface the rest of the system uses (``write`` /
    ``read`` / ``exists`` / ``delete``) and derives L0/L1 with the *real*
    :func:`stratum.filesystem.make_l0` / :func:`make_l1` helpers.
    """

    def __init__(self) -> None:
        self.docs: dict[str, dict[str, Any]] = {}
        self.deleted: list[tuple[str, str]] = []

    def write(
        self,
        uri: str,
        content: str,
        *,
        tenant: str,
        metadata: dict[str, Any] | None = None,
        commit: bool = True,
    ) -> dict[str, Any]:
        l0 = make_l0(content)
        l1 = make_l1(content)
        body = content if content.endswith("\n") else content + "\n"
        content_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
        self.docs[uri] = {
            "uri": uri,
            "content": content,
            "l0": l0,
            "l1": l1,
            "metadata": dict(metadata or {}),
        }
        return {
            "uri": uri,
            "path": uri,
            "l0": l0,
            "l1": l1,
            "content_hash": content_hash,
            "git_commit": "e2e-commit",
        }

    def read(self, uri: str, *, level: str = "l1", tenant: str) -> dict[str, Any]:
        doc = self.docs.get(uri)
        if doc is None:
            raise FileNotFoundError(uri)
        if level == "l0":
            content = doc["l0"]
        elif level == "l1":
            content = doc["l1"]
        else:
            content = doc["content"]
        return {
            "uri": uri,
            "level": level,
            "content": content,
            "l0": doc["l0"],
            "l1": doc["l1"],
            "path": uri,
            "metadata": dict(doc["metadata"]),
        }

    def exists(self, uri: str, *, tenant: str) -> bool:
        return uri in self.docs

    def path_for(self, uri: str) -> Path:
        return Path(uri)

    def delete(self, uri: str, *, tenant: str) -> dict[str, Any]:
        existed = self.docs.pop(uri, None) is not None
        self.deleted.append((uri, tenant))
        return {"uri": uri, "git_commit": "forget-e2e" if existed else None}


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def tenant() -> str:
    """A deterministic tenant id for DB-free tests."""
    return DEFAULT_TENANT


@pytest.fixture
def stratum_settings(tmp_path: Path) -> Settings:
    """Deterministic :class:`Settings` with the vault rooted under ``tmp_path``."""
    return Settings(
        kb_path=str(tmp_path / "kb"),
        vector_type="halfvec",
        embedding_dim=8,
        dedup_threshold=0.92,
        min_confidence=0.6,
        reflect_budget_tokens=20000,
        reflect_max_tokens=50000,
    )


@pytest.fixture
def fake_llm() -> FakeLLM:
    """A deterministic, token-metering LLM/embedding double."""
    return FakeLLM()


@pytest.fixture
def in_memory_db() -> InMemoryDB:
    """A fresh shared in-memory backend (one instance per test)."""
    return InMemoryDB()


@pytest.fixture
def in_memory_store(in_memory_db: InMemoryDB, stratum_settings: Settings) -> InMemoryStore:
    """A real :class:`MemoryStore` (dedup logic) over the in-memory backend."""
    return InMemoryStore(in_memory_db, stratum_settings)


@pytest.fixture
def in_memory_fs() -> InMemoryFS:
    """An in-memory filesystem vault (L0/L1 via the real helpers)."""
    return InMemoryFS()


@pytest.fixture
def test_db_dsn() -> str:
    """The live Postgres DSN, or skip the test when ``STRATUM_TEST_DB`` is unset."""
    dsn = os.getenv("STRATUM_TEST_DB")
    if not dsn:
        pytest.skip("set STRATUM_TEST_DB to a Postgres DSN to run integration tests")
    return dsn
