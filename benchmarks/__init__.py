"""Stratum-Memory benchmark harness (WP-16).

Evaluates the query engine (:class:`stratum.query.QueryEngine`) against the
LoCoMo / LongMemEval long-term-memory benchmarks plus a built-in *temporal
suite* of versioned facts. Everything is deterministic: a fixed seed, pinned
models from :class:`stratum.config.Settings`, and repeated runs whose median is
reported.

The package is import-light and side-effect free; the heavy engine is built
lazily by :mod:`benchmarks.run` and injected into the harness, so unit tests run
without Postgres or a network.
"""

from __future__ import annotations

from .datasets import (
    DATASETS,
    TEMPORAL_FACTS,
    BenchmarkCase,
    dataset_names,
    load_dataset,
    temporal_suite,
)
from .harness import VECTOR_BASELINE, BenchmarkHarness, BenchmarkResult
from .metrics import (
    accuracy,
    contradiction_rate,
    exact_match,
    f1,
    ndcg_at_k,
    recall_at_k,
    temporal_correctness,
)

__all__ = [
    "DATASETS",
    "TEMPORAL_FACTS",
    "VECTOR_BASELINE",
    "BenchmarkCase",
    "BenchmarkHarness",
    "BenchmarkResult",
    "accuracy",
    "contradiction_rate",
    "dataset_names",
    "exact_match",
    "f1",
    "load_dataset",
    "ndcg_at_k",
    "recall_at_k",
    "temporal_correctness",
    "temporal_suite",
]
