"""Query engine: hybrid retrieval + L0-L2 level control (WP-10).

This module is the read/serving side of Stratum-Memory. It turns a natural
language ``query`` into a ranked list of memory hits across the four storage
layers (filesystem/docs, temporal graph/facts, vector/chunks, KV):

* ``mode="vector"``  — embed the query, then combine ``MemoryStore.search_chunks``
  (URI-prefix ``scope``) and ``MemoryStore.find_similar_facts``.
* ``mode="graph"``   — resolve a start entity (name / alias / vector), then walk
  ``TemporalGraph.get_facts`` (+ ``traverse``) honouring ``as_of_valid`` /
  ``as_of_tx``.
* ``mode="kv"``      — prefix / exact-key lookup in the ``kv`` table.
* ``mode="hybrid"``  — Reciprocal Rank Fusion (:func:`rrf_fuse`, ``k=60``) over
  the vector, graph and KV candidate rankings; deterministic.

``level`` picks how much text each hit carries: ``l0`` (shortest, the default),
``l1`` and ``l2``. Documents are read through ``FilesystemStore.read(level=...)``;
facts get a graded text form (``subject predicate object`` → +window →
+provenance/confidence).

Scoring is always in ``[0, 1]`` and fully deterministic: ties break on ``uri``
ascending, and no unordered container ever influences the output order.

Design rules (mirrors the rest of the package):

* The engine only *reads*; it owns no tables.
* Database access goes exclusively through :func:`stratum.db.connection`
  (injectable via ``conn_factory``) so unit tests run without Postgres.
* ``llm`` / ``store`` / ``graph`` / ``fs`` are all injectable; missing ones are
  constructed lazily from ``Settings``.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .config import Settings, get_settings
from .db import connection as _default_connection
from .uri import UriError, build_uri, parse_uri

__all__ = ["QueryEngine", "rrf_fuse", "RRF_K"]

#: Default Reciprocal Rank Fusion constant (Cormack et al. use 60).
RRF_K = 60

_MODES = ("hybrid", "vector", "graph", "kv")
_LEVELS = ("l0", "l1", "l2")

#: Fallback L0 length (characters) when no filesystem document is available.
_L0_FALLBACK_CHARS = 280

_SLUG_RE = re.compile(r"[^a-z0-9._~-]+")


# --------------------------------------------------------------------------- helpers


def rrf_fuse(rankings: list[list[str]], *, k: int = RRF_K) -> list[tuple[str, float]]:
    """Fuse several ranked URI lists with Reciprocal Rank Fusion.

    Each URI scores ``sum(1 / (k + rank))`` over the rankings it appears in,
    where ``rank`` is its 1-based position. A URI is counted at most once per
    ranking (at its best, i.e. first, position), so duplicates inside a single
    ranking do not inflate the score.

    Args:
        rankings: One ordered list of URIs per retrieval source.
        k: RRF smoothing constant (default :data:`RRF_K` = 60). Must be ``>= 0``.

    Returns:
        ``[(uri, score), ...]`` sorted by score descending, ties broken by
        ``uri`` ascending. Pure and deterministic.
    """
    if k < 0:
        raise ValueError(f"k must be >= 0, got {k!r}")
    scores: dict[str, float] = {}
    for ranking in rankings:
        seen: set[str] = set()
        rank = 0
        for uri in ranking:
            if uri in seen:
                continue
            seen.add(uri)
            rank += 1
            scores[uri] = scores.get(uri, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))


def _clamp01(value: float) -> float:
    """Clamp ``value`` into ``[0.0, 1.0]``."""
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value


def _slug(value: Any) -> str:
    """Return a URI-segment-safe slug of ``value`` (never empty)."""
    slug = _SLUG_RE.sub("-", str(value).lower()).strip("-.")
    return slug or "x"


def _get(row: Any, key: str, index: int) -> Any:
    """Read a column from a mapping row or a positional sequence row."""
    if isinstance(row, Mapping):
        return row.get(key)
    try:
        return row[index]
    except (IndexError, TypeError):
        return None


def _vector_literal(embedding: Sequence[float]) -> str:
    """Render an embedding as a pgvector literal ``[a,b,c]``."""
    return "[" + ",".join(repr(float(x)) for x in embedding) + "]"


def _resolve_awaitable(value: Any) -> Any:
    """Return ``value``, awaiting it first when it is a coroutine/awaitable.

    ``LLMClient.embed`` is ``async``; the query engine exposes a synchronous
    surface. When called from a running event loop the coroutine is executed on
    a short-lived thread with its own loop, so the sync API works in both a
    plain script and an async MCP handler.
    """
    if not inspect.isawaitable(value):
        return value
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(value)

    result: dict[str, Any] = {}
    error: dict[str, BaseException] = {}

    def _run() -> None:
        try:
            result["value"] = asyncio.run(value)
        except BaseException as exc:  # noqa: BLE001 - re-raised on the caller thread
            error["exc"] = exc

    thread = threading.Thread(target=_run)
    thread.start()
    thread.join()
    if error:
        raise error["exc"]
    return result["value"]


def _fmt_window(valid_from: Any, valid_to: Any) -> str:
    """Render a valid-time window as ``from..to`` (``to`` defaults to ``open``)."""
    start = str(valid_from) if valid_from is not None else "?"
    end = str(valid_to) if valid_to is not None else "open"
    return f"{start}..{end}"


@dataclass
class _Candidate:
    """Internal intermediate hit before it is rendered for the caller."""

    uri: str
    content: str
    score: float
    provenance: str
    valid_from: Any = None
    valid_to: Any = None
    confidence: float | None = None
    tags: tuple[str, ...] = field(default_factory=tuple)
    source: str = ""


def _in_scope(uri: str, scope: str | None) -> bool:
    """Return ``True`` when ``uri`` lies under the ``scope`` path prefix.

    The prefix is matched on a segment boundary, so ``.../projects`` does *not*
    match ``.../projects2``.
    """
    if not scope:
        return True
    prefix = scope.rstrip("/")
    return uri == prefix or uri.startswith(prefix + "/")


# --------------------------------------------------------------------------- engine


class QueryEngine:
    """Hybrid query engine over the four Stratum storage layers.

    Args:
        settings: Configuration source; defaults to :func:`get_settings`.
        llm: Embedding client (``embed`` method, may be sync or async);
            defaults to :class:`stratum.llm.LLMClient`.
        store: Vector/KV store (:class:`stratum.vector.MemoryStore`).
        graph: Temporal graph (:class:`stratum.graph.TemporalGraph`).
        fs: Filesystem vault (:class:`stratum.filesystem.FilesystemStore`).
        conn_factory: Callable ``(tenant) -> contextmanager[Connection]`` used
            for KV lookup and entity resolution. Defaults to
            :func:`stratum.db.connection`.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        llm: Any | None = None,
        store: Any | None = None,
        graph: Any | None = None,
        fs: Any | None = None,
        conn_factory: Any | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._llm = llm
        self._store = store
        self._graph = graph
        self._fs = fs
        self._connect = conn_factory or _default_connection

    # ----------------------------------------------------------- lazy backends

    def _get_llm(self) -> Any:
        if self._llm is None:
            from .llm import LLMClient

            self._llm = LLMClient(self._settings)
        return self._llm

    def _get_store(self) -> Any:
        if self._store is None:
            from .vector import MemoryStore

            self._store = MemoryStore(self._settings)
        return self._store

    def _get_graph(self) -> Any:
        if self._graph is None:
            from .graph import TemporalGraph

            self._graph = TemporalGraph(self._settings, conn_factory=self._connect)
        return self._graph

    def _get_fs(self) -> Any:
        if self._fs is None:
            from .filesystem import FilesystemStore

            self._fs = FilesystemStore(self._settings, conn_factory=self._connect)
        return self._fs

    def _vec_cast(self) -> str:
        """pgvector cast matching ``Settings.vector_type``."""
        return "vector" if self._settings.vector_type == "vector" else "halfvec"

    def _embed_one(self, query: str) -> list[float]:
        """Embed a single query string and return the first vector."""
        llm = self._get_llm()
        vectors = _resolve_awaitable(llm.embed([query]))
        if not vectors:
            raise ValueError("embedding client returned no vector for the query")
        return [float(x) for x in vectors[0]]

    # ------------------------------------------------------------------- public

    def query(
        self,
        tenant: str,
        query: str,
        *,
        scope: str | None = None,
        mode: str = "hybrid",
        k: int = 8,
        level: str = "l0",
        as_of_valid: Any | None = None,
        as_of_tx: Any | None = None,
        filters: Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Run a query and return up to ``k`` ranked hits.

        Args:
            tenant: Tenant id (RLS scope).
            query: Natural-language query.
            scope: Optional URI prefix; only hits at or below it are returned.
            mode: One of ``hybrid`` / ``vector`` / ``graph`` / ``kv``.
            k: Maximum number of results (``>= 1``).
            level: Text level for each hit: ``l0`` (default), ``l1`` or ``l2``.
            as_of_valid: Valid-time view passed through to ``get_facts``.
            as_of_tx: Transaction-time view passed through to ``get_facts``.
            filters: Optional ``{"tags": [...], "bereich": str,
                "confidence_min": float}``.

        Returns:
            A list of ``{"uri", "level", "score", "content", "provenance",
            "valid_from", "valid_to"}`` dicts, score descending, ties by URI.

        Raises:
            ValueError: on an unknown ``mode``/``level`` or ``k < 1``.
        """
        if mode not in _MODES:
            raise ValueError(f"invalid mode {mode!r}; allowed: {_MODES}")
        if level not in _LEVELS:
            raise ValueError(f"invalid level {level!r}; allowed: {_LEVELS}")
        if k < 1:
            raise ValueError(f"k must be >= 1, got {k!r}")

        filters = dict(filters or {})

        embedding: list[float] | None = None
        if mode in ("vector", "hybrid", "graph"):
            embedding = self._embed_one(query)

        if mode == "vector":
            assert embedding is not None
            candidates = self._vector_candidates(
                tenant, embedding, k=k, scope=scope, level=level
            )
        elif mode == "graph":
            candidates = self._graph_candidates(
                tenant,
                query,
                embedding,
                k=k,
                level=level,
                as_of_valid=as_of_valid,
                as_of_tx=as_of_tx,
            )
        elif mode == "kv":
            candidates = self._kv_candidates(tenant, query, k=k, level=level)
        else:
            candidates = self._hybrid_candidates(
                tenant,
                query,
                embedding,  # type: ignore[arg-type]
                k=k,
                scope=scope,
                level=level,
                as_of_valid=as_of_valid,
                as_of_tx=as_of_tx,
            )

        candidates = [c for c in candidates if _in_scope(c.uri, scope)]
        candidates = self._apply_filters(candidates, filters)
        candidates = self._dedupe(candidates)
        candidates.sort(key=lambda c: (-c.score, c.uri))
        return [self._to_result(c, level) for c in candidates[:k]]

    # ------------------------------------------------------------------ vector

    def _vector_candidates(
        self,
        tenant: str,
        embedding: Sequence[float],
        *,
        k: int,
        scope: str | None,
        level: str,
    ) -> list[_Candidate]:
        """Chunk + fact candidates via the vector layer, ranked by similarity."""
        store = self._get_store()
        candidates: list[_Candidate] = []

        chunks = store.search_chunks(tenant, embedding, k=k, scope=scope)
        for chunk in chunks or []:
            uri = chunk.get("source_uri") if isinstance(chunk, Mapping) else None
            if not uri:
                continue
            fallback = str(chunk.get("text") or "")
            content, tags = self._doc_content(tenant, str(uri), level, fallback)
            candidates.append(
                _Candidate(
                    uri=str(uri),
                    content=content,
                    score=_clamp01(float(chunk.get("similarity", 0.0))),
                    provenance=str(uri),
                    tags=tags,
                    source="vector",
                )
            )

        facts = store.find_similar_facts(tenant, embedding, threshold=0.0, limit=k)
        if facts:
            enriched = self._enrich_facts(tenant, facts)
            for fact in facts:
                fact_id = fact.get("fact_id") if isinstance(fact, Mapping) else None
                merged = dict(fact)
                if fact_id is not None:
                    merged.update(enriched.get(str(fact_id), {}))
                candidates.append(self._fact_candidate(tenant, merged, level, "vector"))

        candidates.sort(key=lambda c: (-c.score, c.uri))
        return candidates

    # ------------------------------------------------------------------- graph

    def _graph_candidates(
        self,
        tenant: str,
        query: str,
        embedding: Sequence[float] | None,
        *,
        k: int,
        level: str,
        as_of_valid: Any | None,
        as_of_tx: Any | None,
    ) -> list[_Candidate]:
        """Fact + traversal-edge candidates around the resolved start entity."""
        graph = self._get_graph()
        entity = self._resolve_entity(tenant, query, embedding)
        if not entity:
            return []
        entity_id = entity.get("entity_id")
        if not entity_id:
            return []
        subject_name = entity.get("name") or entity.get("canonical_uri") or str(entity_id)

        candidates: list[_Candidate] = []

        facts = graph.get_facts(
            tenant,
            subject_entity_id=entity_id,
            as_of_valid=as_of_valid,
            as_of_tx=as_of_tx,
        )
        for fact in facts or []:
            row = dict(fact)
            row.setdefault("subject_name", subject_name)
            candidates.append(self._fact_candidate(tenant, row, level, "graph"))

        edges = graph.traverse(
            tenant, start_entity_id=entity_id, as_of_valid=as_of_valid
        )
        for edge in edges or []:
            depth = int(edge.get("depth", 1) or 1)
            rel = edge.get("rel_type") or "related"
            dst = edge.get("dst_entity_id") or edge.get("dst") or ""
            base = f"{subject_name} -[{rel}]-> {dst}"
            if level == "l0":
                content = base
            elif level == "l1":
                content = f"{base} (depth: {depth})"
            else:
                content = f"{base} (depth: {depth}) [provenance: graph]"
            candidates.append(
                _Candidate(
                    uri=self._entity_uri(tenant, dst),
                    content=content,
                    score=_clamp01(1.0 / (1.0 + depth)),
                    provenance="graph",
                    source="graph",
                )
            )

        candidates.sort(key=lambda c: (-c.score, c.uri))
        return candidates

    def _resolve_entity(
        self, tenant: str, name: str, embedding: Sequence[float] | None
    ) -> dict[str, Any] | None:
        """Resolve a start entity by exact name/alias, then by nearest vector.

        Returns ``{"entity_id", "name", "canonical_uri"}`` or ``None``. Any
        storage/connection error degrades to ``None`` rather than raising.
        """
        try:
            with self._connect(tenant) as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT entity_id, name, canonical_uri FROM entities "
                    "WHERE tenant_id = %s "
                    "AND (lower(name) = lower(%s) OR %s = ANY(aliases)) "
                    "ORDER BY entity_id LIMIT 1",
                    (tenant, name, name),
                )
                row = cur.fetchone()
                if row is not None:
                    return dict(row)
                if embedding is not None:
                    cur.execute(
                        "SELECT entity_id, name, canonical_uri FROM entities "
                        f"WHERE tenant_id = %s AND embedding IS NOT NULL "
                        f"ORDER BY embedding <=> %s::{self._vec_cast()} LIMIT 1",
                        (tenant, _vector_literal(embedding)),
                    )
                    row = cur.fetchone()
                    if row is not None:
                        return dict(row)
        except Exception:  # noqa: BLE001 - read path must degrade, not crash
            return None
        return None

    # ---------------------------------------------------------------------- kv

    def _kv_candidates(
        self, tenant: str, query: str, *, k: int, level: str
    ) -> list[_Candidate]:
        """Prefix / exact-key KV candidates, ordered deterministically by key."""
        try:
            with self._connect(tenant) as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT key, value FROM kv "
                    "WHERE tenant_id = %s "
                    "AND (key = %s OR starts_with(key, %s)) "
                    "AND (expires_at IS NULL OR expires_at > now()) "
                    "ORDER BY key LIMIT %s",
                    (tenant, query, query, k),
                )
                rows = cur.fetchall()
        except Exception:  # noqa: BLE001 - read path must degrade, not crash
            return []

        candidates: list[_Candidate] = []
        for row in rows or []:
            key = _get(row, "key", 0)
            value = _get(row, "value", 1)
            if key is None:
                continue
            key_str = str(key)
            if isinstance(value, str):
                content = value
            else:
                content = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
            candidates.append(
                _Candidate(
                    uri=self._kv_uri(tenant, key_str),
                    content=content,
                    score=1.0 if key_str == query else 0.5,
                    provenance=f"kv:{key_str}",
                    source="kv",
                )
            )
        candidates.sort(key=lambda c: (-c.score, c.uri))
        return candidates

    # ------------------------------------------------------------------ hybrid

    def _hybrid_candidates(
        self,
        tenant: str,
        query: str,
        embedding: Sequence[float],
        *,
        k: int,
        scope: str | None,
        level: str,
        as_of_valid: Any | None,
        as_of_tx: Any | None,
    ) -> list[_Candidate]:
        """Fuse vector + graph + KV rankings with RRF and attach content."""
        vector = self._vector_candidates(
            tenant, embedding, k=k, scope=scope, level=level
        )
        graph = self._graph_candidates(
            tenant,
            query,
            embedding,
            k=k,
            level=level,
            as_of_valid=as_of_valid,
            as_of_tx=as_of_tx,
        )
        kv = self._kv_candidates(tenant, query, k=k, level=level)

        rankings = [
            [c.uri for c in sorted(source, key=lambda c: (-c.score, c.uri))]
            for source in (vector, graph, kv)
        ]
        fused = rrf_fuse(rankings, k=RRF_K)

        by_uri = self._merge_by_uri([*vector, *graph, *kv])

        non_empty = sum(1 for ranking in rankings if ranking)
        max_possible = non_empty / (RRF_K + 1) if non_empty else 0.0

        results: list[_Candidate] = []
        for uri, score in fused:
            candidate = by_uri.get(uri)
            if candidate is None:
                continue
            normalized = _clamp01(score / max_possible) if max_possible else 0.0
            results.append(
                _Candidate(
                    uri=candidate.uri,
                    content=candidate.content,
                    score=normalized,
                    provenance=candidate.provenance,
                    valid_from=candidate.valid_from,
                    valid_to=candidate.valid_to,
                    confidence=candidate.confidence,
                    tags=candidate.tags,
                    source="hybrid",
                )
            )
        results.sort(key=lambda c: (-c.score, c.uri))
        return results

    # ---------------------------------------------------------------- rendering

    def _fact_candidate(
        self, tenant: str, row: Mapping[str, Any], level: str, source: str
    ) -> _Candidate:
        """Build a fact candidate with its level-graded text form."""
        confidence = row.get("confidence")
        try:
            confidence = float(confidence) if confidence is not None else None
        except (TypeError, ValueError):
            confidence = None
        provenance = row.get("source_uri") or self._fact_uri(tenant, row)
        return _Candidate(
            uri=self._fact_uri(tenant, row),
            content=self._fact_text(row, level),
            score=_clamp01(confidence if confidence is not None else 1.0),
            provenance=str(provenance),
            valid_from=row.get("valid_from"),
            valid_to=row.get("valid_to"),
            confidence=confidence,
            source=source,
        )

    def _fact_text(self, row: Mapping[str, Any], level: str) -> str:
        """Graded fact text: ``l0`` < ``l1`` (window) < ``l2`` (provenance)."""
        subject = (
            row.get("subject_name")
            or row.get("subject")
            or row.get("subject_entity_id")
            or "?"
        )
        predicate = row.get("predicate") or ""
        obj = row.get("object_text")
        if obj is None:
            obj = row.get("object_entity_id") or ""
        parts = [str(p) for p in (subject, predicate, obj) if p not in (None, "")]
        base = " ".join(parts)
        if level == "l0":
            return base
        window = _fmt_window(row.get("valid_from"), row.get("valid_to"))
        if level == "l1":
            return f"{base} (valid: {window})"
        provenance = row.get("source_uri") or "unknown"
        confidence = row.get("confidence")
        try:
            conf_text = f"{float(confidence):.2f}" if confidence is not None else "?"
        except (TypeError, ValueError):
            conf_text = "?"
        return (
            f"{base} (valid: {window}) "
            f"[provenance: {provenance}, confidence: {conf_text}]"
        )

    def _fact_uri(self, tenant: str, row: Mapping[str, Any]) -> str:
        """Stable URI for a fact row (``stratum://facts/<tenant>/<id>``)."""
        fact_id = row.get("fact_id")
        if fact_id is not None:
            try:
                return build_uri("facts", tenant, _slug(fact_id))
            except UriError:  # pragma: no cover - defensive
                pass
        source = row.get("source_uri")
        if source:
            return str(source)
        return build_uri("facts", tenant, "unknown")

    def _entity_uri(self, tenant: str, entity_id: Any) -> str:
        """Stable URI for an entity id (``stratum://entities/<tenant>/<id>``)."""
        try:
            return build_uri("entities", tenant, _slug(entity_id))
        except UriError:  # pragma: no cover - defensive
            return build_uri("entities", tenant, "unknown")

    def _kv_uri(self, tenant: str, key: str) -> str:
        """Synthetic URI for a KV key (KV has no native ``stratum://`` URI)."""
        return build_uri("facts", tenant, f"kv/{_slug(key)}")

    def _doc_content(
        self, tenant: str, uri: str, level: str, fallback_text: str
    ) -> tuple[str, tuple[str, ...]]:
        """Read a document at ``level`` via the vault, falling back to chunk text."""
        fs = self._get_fs()
        content = ""
        tags: tuple[str, ...] = ()
        if fs is not None:
            try:
                document = fs.read(uri, level=level, tenant=tenant)
                content = str(document.get("content") or "")
                metadata = document.get("metadata") or {}
                raw_tags = metadata.get("tags")
                if isinstance(raw_tags, (list, tuple)):
                    tags = tuple(str(tag) for tag in raw_tags)
            except (FileNotFoundError, ValueError, OSError):
                content = ""
        if not content:
            content = (
                fallback_text[:_L0_FALLBACK_CHARS]
                if level == "l0"
                else fallback_text
            )
        return content, tags

    # ------------------------------------------------------------------ helpers

    def _enrich_facts(
        self, tenant: str, facts: Sequence[Mapping[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        """Best-effort fetch of predicate/subject/confidence for fact ids.

        ``MemoryStore.find_similar_facts`` only yields ``fact_id``,
        ``object_text`` and ``similarity``; joining ``facts`` to ``entities``
        supplies the rest for graded text. A missing connection (or any error)
        degrades to ``{}`` so retrieval still returns the hits it already has.
        """
        ids = [str(f.get("fact_id")) for f in facts if f.get("fact_id") is not None]
        if not ids:
            return {}
        try:
            with self._connect(tenant) as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT f.fact_id, f.predicate, f.object_text, f.valid_from, "
                    "f.valid_to, f.confidence, f.source_uri, e.name AS subject_name "
                    "FROM facts f JOIN entities e "
                    "ON e.entity_id = f.subject_entity_id "
                    "WHERE f.tenant_id = %s AND f.fact_id = ANY(%s)",
                    (tenant, ids),
                )
                rows = cur.fetchall()
        except Exception:  # noqa: BLE001 - enrichment is optional
            return {}
        enriched: dict[str, dict[str, Any]] = {}
        for row in rows or []:
            fact_id = _get(row, "fact_id", 0)
            if fact_id is None:
                continue
            if isinstance(row, Mapping):
                enriched[str(fact_id)] = dict(row)
            else:
                enriched[str(fact_id)] = {
                    "fact_id": fact_id,
                    "predicate": _get(row, "predicate", 1),
                    "object_text": _get(row, "object_text", 2),
                    "valid_from": _get(row, "valid_from", 3),
                    "valid_to": _get(row, "valid_to", 4),
                    "confidence": _get(row, "confidence", 5),
                    "source_uri": _get(row, "source_uri", 6),
                    "subject_name": _get(row, "subject_name", 7),
                }
        return enriched

    def _merge_by_uri(self, candidates: Sequence[_Candidate]) -> dict[str, _Candidate]:
        """Merge candidates sharing a URI, preferring the richest content."""
        merged: dict[str, _Candidate] = {}
        for candidate in candidates:
            existing = merged.get(candidate.uri)
            if existing is None:
                merged[candidate.uri] = candidate
                continue
            if not existing.content and candidate.content:
                existing.content = candidate.content
            if existing.valid_from is None:
                existing.valid_from = candidate.valid_from
            if existing.valid_to is None:
                existing.valid_to = candidate.valid_to
            if existing.confidence is None:
                existing.confidence = candidate.confidence
            if not existing.tags and candidate.tags:
                existing.tags = candidate.tags
            existing.score = max(existing.score, candidate.score)
        return merged

    def _dedupe(self, candidates: Sequence[_Candidate]) -> list[_Candidate]:
        """Collapse duplicate URIs, keeping the best-scoring candidate."""
        best: dict[str, _Candidate] = {}
        for candidate in candidates:
            existing = best.get(candidate.uri)
            if existing is None:
                best[candidate.uri] = candidate
            elif candidate.score > existing.score:
                best[candidate.uri] = candidate
        return list(best.values())

    def _apply_filters(
        self, candidates: Sequence[_Candidate], filters: Mapping[str, Any]
    ) -> list[_Candidate]:
        """Apply ``bereich`` / ``tags`` / ``confidence_min`` filters."""
        bereich = filters.get("bereich")
        raw_tags = filters.get("tags")
        tag_filter = {str(t) for t in raw_tags} if isinstance(raw_tags, (list, tuple, set)) else None
        conf_min = filters.get("confidence_min")
        conf_min = float(conf_min) if isinstance(conf_min, (int, float)) else None

        kept: list[_Candidate] = []
        for candidate in candidates:
            if bereich:
                try:
                    if parse_uri(candidate.uri).bereich != bereich:
                        continue
                except UriError:
                    continue
            if tag_filter is not None and not (set(candidate.tags) & tag_filter):
                continue
            if (
                conf_min is not None
                and candidate.confidence is not None
                and candidate.confidence < conf_min
            ):
                continue
            kept.append(candidate)
        return kept

    def _to_result(self, candidate: _Candidate, level: str) -> dict[str, Any]:
        """Render a candidate as the public result dict (score rounded)."""
        return {
            "uri": candidate.uri,
            "level": level,
            "score": round(candidate.score, 6),
            "content": candidate.content,
            "provenance": candidate.provenance,
            "valid_from": candidate.valid_from,
            "valid_to": candidate.valid_to,
        }
