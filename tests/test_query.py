"""Tests for the query engine (:mod:`stratum.query`, WP-10).

Everything runs without Postgres: ``llm``, ``store``, ``graph`` and ``fs`` are
replaced by small fakes, and the KV / entity / enrichment SQL goes through an
injected fake connection. The tests pin the contract of :meth:`QueryEngine.query`
plus the pure :func:`rrf_fuse` function:

* RRF is deterministic and ranks higher-ranked URIs higher,
* ``mode="vector"`` returns ``k`` results, descending, scores in ``[0, 1]``,
* ``level`` l0/l1/l2 yield increasingly long texts,
* ``as_of_valid`` / ``as_of_tx`` reach ``get_facts`` unchanged,
* ``hybrid`` fuses sources without duplicating a URI,
* ``scope`` restricts results to a URI prefix,
* two identical runs produce identical output (reproducibility).
"""

from __future__ import annotations

from typing import Any

import pytest

from stratum.config import Settings
from stratum.query import QueryEngine, rrf_fuse

# --------------------------------------------------------------------------- #
# Recording fakes
# --------------------------------------------------------------------------- #


class FakeLLM:
    """Embedding client stand-in (synchronous, deterministic)."""

    def __init__(self, vector: list[float] | None = None) -> None:
        self.vector = vector if vector is not None else [0.1, 0.2, 0.3]
        self.calls: list[list[str]] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [list(self.vector) for _ in texts]


class FakeStore:
    """Vector store stand-in returning fixed chunks / facts."""

    def __init__(
        self,
        chunks: list[dict[str, Any]] | None = None,
        facts: list[dict[str, Any]] | None = None,
    ) -> None:
        self.chunks = list(chunks or [])
        self.facts = list(facts or [])
        self.chunk_scope: str | None = None
        self.fact_threshold: float | None = None

    def search_chunks(
        self, tenant: str, embedding: Any, *, k: int = 8, scope: str | None = None
    ) -> list[dict[str, Any]]:
        self.chunk_scope = scope
        return self.chunks[:k]

    def find_similar_facts(
        self, tenant: str, embedding: Any, *, threshold: float, limit: int = 5
    ) -> list[dict[str, Any]]:
        self.fact_threshold = threshold
        return self.facts[:limit]


class FakeGraph:
    """Temporal-graph stand-in that records the ``get_facts`` call."""

    def __init__(
        self,
        facts: list[dict[str, Any]] | None = None,
        edges: list[dict[str, Any]] | None = None,
    ) -> None:
        self.facts = list(facts or [])
        self.edges = list(edges or [])
        self.get_facts_calls: list[dict[str, Any]] = []
        self.traverse_calls: list[dict[str, Any]] = []

    def get_facts(self, tenant: str, **kwargs: Any) -> list[dict[str, Any]]:
        self.get_facts_calls.append({"tenant": tenant, **kwargs})
        return self.facts

    def traverse(self, tenant: str, **kwargs: Any) -> list[dict[str, Any]]:
        self.traverse_calls.append({"tenant": tenant, **kwargs})
        return self.edges


class FakeFS:
    """Filesystem vault stand-in returning level-dependent content."""

    def __init__(self, documents: dict[str, dict[str, str]] | None = None) -> None:
        self.documents = documents or {}
        self.read_calls: list[tuple[str, str]] = []

    def read(self, uri: str, *, level: str = "l1", tenant: str) -> dict[str, Any]:
        self.read_calls.append((uri, level))
        doc = self.documents.get(uri)
        if doc is None:
            raise FileNotFoundError(uri)
        return {
            "uri": uri,
            "level": level,
            "content": doc.get(level, ""),
            "metadata": {"tags": ["demo"]} if level != "l0" else {"tags": []},
        }


class FakeCursor:
    def __init__(self, conn: FakeConnection) -> None:
        self._conn = conn
        self._last: Any = None
        self.rowcount = 0

    def execute(self, sql: str, params: tuple | dict = ()) -> None:
        self._conn.statements.append((" ".join(sql.split()), params))
        self._last = self._conn.router(sql, params)

    def fetchone(self) -> Any:
        return self._last if isinstance(self._last, dict) else None

    def fetchall(self) -> Any:
        return self._last if isinstance(self._last, list) else []

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class FakeConnection:
    def __init__(self, router: Any) -> None:
        self.router = router
        self.statements: list[tuple[str, Any]] = []

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        pass

    def __enter__(self) -> FakeConnection:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def _make_conn_factory(router: Any):
    def factory(tenant: str) -> FakeConnection:
        return FakeConnection(router)

    return factory


def _empty_router(sql: str, params: Any) -> Any:
    return None


def _engine(
    *,
    llm: Any | None = None,
    store: Any | None = None,
    graph: Any | None = None,
    fs: Any | None = None,
    router: Any | None = None,
) -> QueryEngine:
    return QueryEngine(
        Settings(),
        llm=llm or FakeLLM(),
        store=store or FakeStore(),
        graph=graph or FakeGraph(),
        fs=fs or FakeFS(),
        conn_factory=_make_conn_factory(router or _empty_router),
    )


# --------------------------------------------------------------------------- #
# rrf_fuse
# --------------------------------------------------------------------------- #


def test_rrf_fuse_ranks_higher_positions_higher():
    fused = rrf_fuse([["a", "b", "c"]], k=60)
    assert [uri for uri, _ in fused] == ["a", "b", "c"]
    assert fused[0][1] > fused[1][1] > fused[2][1]


def test_rrf_fuse_combines_rankings():
    # b and c appear in both rankings, so they outrank a (only in one).
    fused = dict(rrf_fuse([["a", "b", "c"], ["b", "c", "d"]], k=60))
    assert set(fused) == {"a", "b", "c", "d"}
    assert fused["b"] > fused["a"]
    assert fused["c"] > fused["a"]
    assert fused["b"] == pytest.approx(1 / 62 + 1 / 61)
    assert fused["d"] == pytest.approx(1 / 63)


def test_rrf_fuse_is_deterministic_and_counts_duplicates_once():
    first = rrf_fuse([["a", "a", "b"], ["b", "a"]], k=60)
    second = rrf_fuse([["a", "a", "b"], ["b", "a"]], k=60)
    assert first == second
    # "a" is counted once per ranking: rank 1 in the first, rank 2 in the second.
    assert dict(first)["a"] == pytest.approx(1 / 61 + 1 / 62)


def test_rrf_fuse_rejects_negative_k():
    with pytest.raises(ValueError):
        rrf_fuse([["a"]], k=-1)


# --------------------------------------------------------------------------- #
# vector mode
# --------------------------------------------------------------------------- #


def _chunks() -> list[dict[str, Any]]:
    return [
        {"source_uri": "stratum://docs/default/a", "text": "alpha", "similarity": 0.5},
        {"source_uri": "stratum://docs/default/b", "text": "beta", "similarity": 0.9},
        {"source_uri": "stratum://docs/default/c", "text": "gamma", "similarity": 0.7},
    ]


def test_vector_mode_returns_k_descending_scores_in_unit_interval():
    engine = _engine(store=FakeStore(chunks=_chunks()))
    results = engine.query("default", "hello", mode="vector", k=3, level="l2")
    assert len(results) == 3
    scores = [r["score"] for r in results]
    assert scores == sorted(scores, reverse=True)
    assert all(0.0 <= s <= 1.0 for s in scores)
    assert [r["uri"] for r in results] == [
        "stratum://docs/default/b",
        "stratum://docs/default/c",
        "stratum://docs/default/a",
    ]
    assert all(r["level"] == "l2" for r in results)


def test_vector_mode_respects_k_limit():
    engine = _engine(store=FakeStore(chunks=_chunks()))
    results = engine.query("default", "hello", mode="vector", k=2)
    assert len(results) == 2


def test_vector_mode_passes_scope_and_zero_threshold_to_store():
    store = FakeStore(chunks=_chunks())
    engine = _engine(store=store)
    engine.query("default", "hello", mode="vector", k=3, scope="stratum://docs/default")
    assert store.chunk_scope == "stratum://docs/default"
    assert store.fact_threshold == 0.0


# --------------------------------------------------------------------------- #
# level control
# --------------------------------------------------------------------------- #


def test_levels_produce_different_lengths():
    uri = "stratum://docs/default/a"
    fs = FakeFS(
        {
            uri: {
                "l0": "short",
                "l1": "a much longer level-one summary text",
                "l2": "the complete document body with every detail " * 4,
            }
        }
    )
    engine = _engine(store=FakeStore(chunks=_chunks()), fs=fs)
    l0 = engine.query("default", "q", mode="vector", k=1, level="l0")[0]["content"]
    l1 = engine.query("default", "q", mode="vector", k=1, level="l1")[0]["content"]
    l2 = engine.query("default", "q", mode="vector", k=1, level="l2")[0]["content"]
    assert len(l0) < len(l1) < len(l2)
    assert (uri, "l0") in fs.read_calls
    assert (uri, "l2") in fs.read_calls


def test_fact_text_levels_grow():
    facts = [
        {
            "fact_id": "f1",
            "subject_name": "Ada",
            "predicate": "works_at",
            "object_text": "Analytical Engines",
            "valid_from": "2026-01-01T00:00:00Z",
            "valid_to": None,
            "confidence": 0.9,
            "source_uri": "stratum://docs/default/ada",
            "similarity": 0.8,
        }
    ]
    engine = _engine(store=FakeStore(facts=facts))
    results = {}
    for level in ("l0", "l1", "l2"):
        results[level] = engine.query("default", "ada", mode="vector", k=1, level=level)[0][
            "content"
        ]
    assert "works_at" in results["l0"]
    assert len(results["l0"]) < len(results["l1"]) < len(results["l2"])
    assert "provenance" in results["l2"]
    assert results["l0"] == "Ada works_at Analytical Engines"


# --------------------------------------------------------------------------- #
# graph mode / as_of passthrough
# --------------------------------------------------------------------------- #


def _entity_router(sql: str, params: Any) -> Any:
    if "FROM entities" in sql and "lower(name)" in sql:
        return {
            "entity_id": "11111111-1111-1111-1111-111111111111",
            "name": "Ada",
            "canonical_uri": "stratum://entities/default/ada",
        }
    return None


def test_as_of_and_scope_reach_get_facts():
    graph = FakeGraph(
        facts=[
            {
                "fact_id": "f1",
                "subject_name": "Ada",
                "predicate": "works_at",
                "object_text": "Analytical Engines",
                "confidence": 1.0,
            }
        ]
    )
    engine = _engine(graph=graph, router=_entity_router)
    results = engine.query(
        "default",
        "Ada",
        mode="graph",
        k=5,
        as_of_valid="2026-01-01T00:00:00Z",
        as_of_tx="2026-02-01T00:00:00Z",
    )
    assert results, "graph mode should return the fact"
    assert len(graph.get_facts_calls) == 1
    call = graph.get_facts_calls[0]
    assert call["tenant"] == "default"
    assert call["subject_entity_id"] == "11111111-1111-1111-1111-111111111111"
    assert call["as_of_valid"] == "2026-01-01T00:00:00Z"
    assert call["as_of_tx"] == "2026-02-01T00:00:00Z"


def test_graph_mode_without_entity_returns_empty():
    engine = _engine(graph=FakeGraph(), router=_empty_router)
    assert engine.query("default", "nobody", mode="graph") == []


# --------------------------------------------------------------------------- #
# kv mode
# --------------------------------------------------------------------------- #


def _kv_router(sql: str, params: Any) -> Any:
    if "FROM kv" in sql:
        return [
            {"key": "theme/color", "value": {"name": "blue"}},
            {"key": "theme/font", "value": "inter"},
        ]
    return None


def test_kv_mode_returns_key_hits_sorted():
    engine = _engine(router=_kv_router)
    results = engine.query("default", "theme", mode="kv", k=5)
    uris = [r["uri"] for r in results]
    assert len(results) == 2
    assert uris == sorted(uris)
    assert all(r["score"] in (0.5, 1.0) for r in results)


def test_kv_exact_key_scores_higher_than_prefix():
    engine = _engine(router=_kv_router)
    results = engine.query("default", "theme/color", mode="kv", k=5)
    assert results[0]["score"] >= results[-1]["score"]


# --------------------------------------------------------------------------- #
# hybrid mode
# --------------------------------------------------------------------------- #


def test_hybrid_fuses_sources_and_does_not_duplicate_uri():
    shared = "stratum://facts/default/f1"
    # A vector chunk and a graph fact share the same URI; it must appear once.
    chunks = [{"source_uri": shared, "text": "shared", "similarity": 0.8}]
    facts = [
        {
            "fact_id": "f1",
            "subject_name": "Ada",
            "predicate": "works_at",
            "object_text": "Engines",
            "confidence": 0.9,
        },
        {
            "fact_id": "f2",
            "subject_name": "Ada",
            "predicate": "born_in",
            "object_text": "London",
            "confidence": 0.7,
        },
    ]
    store = FakeStore(chunks=chunks, facts=facts)
    graph = FakeGraph(facts=facts)
    engine = _engine(store=store, graph=graph, router=_entity_router)
    results = engine.query("default", "Ada", mode="hybrid", k=10)
    uris = [r["uri"] for r in results]
    assert len(uris) == len(set(uris)), "hybrid must fuse duplicate URIs"
    assert shared in uris
    scores = [r["score"] for r in results]
    assert scores == sorted(scores, reverse=True)
    assert all(0.0 <= s <= 1.0 for s in scores)


# --------------------------------------------------------------------------- #
# scope filtering
# --------------------------------------------------------------------------- #


def test_scope_filters_to_prefix():
    chunks = [
        {"source_uri": "stratum://docs/default/projects/a", "text": "a", "similarity": 0.9},
        {"source_uri": "stratum://docs/default/other/b", "text": "b", "similarity": 0.8},
    ]
    engine = _engine(store=FakeStore(chunks=chunks))
    results = engine.query(
        "default", "q", mode="vector", k=5, scope="stratum://docs/default/projects"
    )
    assert [r["uri"] for r in results] == ["stratum://docs/default/projects/a"]


# --------------------------------------------------------------------------- #
# reproducibility
# --------------------------------------------------------------------------- #


def test_scores_are_reproducible():
    engine = _engine(store=FakeStore(chunks=_chunks(), facts=[
        {"fact_id": "f1", "subject_name": "Ada", "predicate": "p", "object_text": "o",
         "confidence": 0.6}
    ]), graph=FakeGraph(), router=_entity_router)
    first = engine.query("default", "hello", mode="hybrid", k=5, level="l1")
    second = engine.query("default", "hello", mode="hybrid", k=5, level="l1")
    assert first == second


def test_invalid_mode_and_k_rejected():
    engine = _engine()
    with pytest.raises(ValueError):
        engine.query("default", "q", mode="nope")
    with pytest.raises(ValueError):
        engine.query("default", "q", k=0)
    with pytest.raises(ValueError):
        engine.query("default", "q", level="l9")
