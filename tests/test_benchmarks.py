"""Tests for the benchmark harness (:mod:`benchmarks`, WP-16).

Everything runs **without Postgres and without network**: the query engine is a
deterministic fake, datasets are the built-in temporal fixtures or a temporary
JSONL file, and ``STRATUM_BENCHMARK_OFFLINE`` forbids downloads.

Covered:

* metric functions against known values,
* the LoCoMo/LongMemEval loaders and the built-in fixture,
* the temporal suite carrying corrected (versioned) facts,
* harness determinism (two runs produce identical metrics/predictions),
* the ``run.py`` artifact structure written via the fixture,
* the presence of the vector-baseline configuration.
"""

from __future__ import annotations

import json
import math
from types import SimpleNamespace
from typing import Any

import pytest
from benchmarks import metrics
from benchmarks.datasets import (
    TEMPORAL_FACTS,
    load_dataset,
    load_locomo,
    load_longmemeval,
    seed_temporal_facts,
    temporal_suite,
)
from benchmarks.harness import VECTOR_BASELINE, BenchmarkHarness
from benchmarks.run import main, run_benchmark

# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class FakeEngine:
    """Deterministic engine answering the temporal suite's probes correctly.

    Emulates the bitemporal contract of the built-in suite: it returns the value
    valid on the requested axis, so a correct engine scores ``1.0`` temporal
    correctness and ``0.0`` contradiction. A ``vector_mode_always_latest`` flag
    makes the vector baseline ignore the time axes (the classic vector-store
    failure the suite is designed to catch).
    """

    def __init__(self, *, always_latest: bool = False) -> None:
        self.always_latest = always_latest
        self.calls: list[dict[str, Any]] = []

    def query(
        self,
        tenant: str,
        query: str,
        *,
        mode: str = "hybrid",
        k: int = 8,
        level: str = "l0",
        as_of_valid: str | None = None,
        as_of_tx: str | None = None,
    ) -> list[dict[str, Any]]:
        self.calls.append(
            {
                "tenant": tenant,
                "query": query,
                "mode": mode,
                "k": k,
                "level": level,
                "as_of_valid": as_of_valid,
                "as_of_tx": as_of_tx,
            }
        )
        return [{"content": self._answer(query, as_of_valid, as_of_tx)}]

    def _answer(self, query: str, as_of_valid: str | None, as_of_tx: str | None) -> str:
        if "ip_address" in query:
            if not self.always_latest:
                if as_of_valid == "2024-03-01T00:00:00Z":
                    return "backend ip_address 10.0.0.5"
                if as_of_tx == "2024-04-01T00:00:00Z":
                    return "backend ip_address 10.0.0.5"
            return "backend ip_address 10.0.0.9"
        if "port" in query:
            return "backend port 8080"
        return ""


class DeadEngine:
    """An engine whose ``query`` always raises (B2 regression).

    A swallowed exception must never be scored as a correct absence: the harness
    has to flag the error and score the case as a miss.
    """

    def query(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        raise RuntimeError("engine is dead")


class RecordingCurator:
    """Minimal curator surface recording validate/promote calls (B1)."""

    def __init__(self, *, approve: bool = True) -> None:
        self.approve = approve
        self.validated: list[dict[str, Any]] = []
        self.promoted: list[dict[str, Any]] = []

    def validate(self, tenant: str, candidate: Any, **kwargs: Any) -> Any:
        self.validated.append({"tenant": tenant, "candidate": candidate})
        reason = None if self.approve else "duplicate"
        return SimpleNamespace(approved=self.approve, reason=reason, action="ADD")

    def promote(self, tenant: str, candidate: Any, **kwargs: Any) -> dict[str, Any]:
        self.promoted.append({"tenant": tenant, "candidate": candidate})
        return {"status": "approved"}


class FakeGraphTarget:
    """Duck-typed graph recording seeded entities/facts (B3)."""

    def __init__(self) -> None:
        self.entities: dict[str, str] = {}
        self.facts: list[dict[str, Any]] = []

    def upsert_entity(self, tenant: str, *, name: str, canonical_uri: str, **kwargs: Any) -> str:
        self.entities[canonical_uri] = name
        return f"entity-{name}"

    def add_fact(self, tenant: str, **kwargs: Any) -> str:
        self.facts.append({"tenant": tenant, **kwargs})
        return f"fact-{len(self.facts)}"


# --------------------------------------------------------------------------- #
# metrics
# --------------------------------------------------------------------------- #


def test_f1_known_values() -> None:
    assert metrics.f1("the quick brown", "the quick brown") == pytest.approx(1.0)
    assert metrics.f1("a b", "a b c") == pytest.approx(0.8)
    assert metrics.f1("", "") == pytest.approx(1.0)
    assert metrics.f1("x", "") == pytest.approx(0.0)
    assert metrics.f1("hello world", "goodbye moon") == pytest.approx(0.0)


def test_exact_match_and_accuracy() -> None:
    assert metrics.exact_match("10.0.0.9", "10.0.0.9") == 1.0
    assert metrics.exact_match("10.0.0.9", "10.0.0.5") == 0.0
    assert metrics.exact_match(" Backend IP ", "backend ip") == 1.0
    assert metrics.accuracy(["a", "b", "c"], ["a", "x", "c"]) == pytest.approx(2 / 3)
    assert metrics.accuracy([], []) == pytest.approx(0.0)
    with pytest.raises(ValueError):
        metrics.accuracy(["a"], ["a", "b"])


def test_temporal_correctness_known_values() -> None:
    preds = ["backend ip_address 10.0.0.9", "backend ip_address 10.0.0.5"]
    golds = ["10.0.0.9", "10.0.0.9"]
    assert metrics.temporal_correctness(preds, golds) == pytest.approx(0.5)
    assert metrics.temporal_correctness(
        ["backend ip_address 10.0.0.9"], ["10.0.0.9"]
    ) == pytest.approx(1.0)
    assert metrics.temporal_correctness([], []) == pytest.approx(0.0)
    with pytest.raises(ValueError):
        metrics.temporal_correctness(["a"], [])


def test_contradiction_rate_known_value() -> None:
    assert metrics.contradiction_rate([True, False, False, False]) == pytest.approx(0.25)
    assert metrics.contradiction_rate([]) == pytest.approx(0.0)
    assert metrics.contradiction_rate([False, False]) == pytest.approx(0.0)


def test_ndcg_at_k_known_values() -> None:
    assert metrics.ndcg_at_k(["a", "b", "c"], {"a": 1.0}, k=3) == pytest.approx(1.0)
    # "a" retrieved at rank 2: dcg = 1/log2(3); idcg = 1/log2(2) = 1.
    expected = (1.0) / math.log2(3.0)
    assert metrics.ndcg_at_k(["b", "a"], {"a": 1.0}, k=2) == pytest.approx(expected)
    assert metrics.ndcg_at_k(["a"], {}, k=1) == pytest.approx(0.0)
    with pytest.raises(ValueError):
        metrics.ndcg_at_k(["a"], {"a": 1.0}, k=0)


def test_recall_at_k_known_values() -> None:
    assert metrics.recall_at_k(["a", "b", "c"], ["a", "c", "d"], k=2) == pytest.approx(1 / 3)
    assert metrics.recall_at_k(["a", "c"], ["a", "c"], k=5) == pytest.approx(1.0)
    assert metrics.recall_at_k(["a"], [], k=1) == pytest.approx(0.0)


# --------------------------------------------------------------------------- #
# datasets / loaders
# --------------------------------------------------------------------------- #


def test_temporal_suite_has_corrected_facts() -> None:
    objects = {fact["object_text"] for fact in TEMPORAL_FACTS}
    assert "10.0.0.5" in objects  # the original value
    assert "10.0.0.9" in objects  # the correction

    ip_facts = [f for f in TEMPORAL_FACTS if f["predicate"] == "ip_address"]
    assert len(ip_facts) == 2
    # Exactly one open version, the corrected value; the old one is closed.
    open_facts = [f for f in ip_facts if f["valid_to"] is None]
    assert len(open_facts) == 1
    assert open_facts[0]["object_text"] == "10.0.0.9"
    closed = [f for f in ip_facts if f["valid_to"] is not None]
    assert closed[0]["object_text"] == "10.0.0.5"


def test_temporal_suite_cases_cover_both_axes() -> None:
    cases = temporal_suite()
    assert {c.category for c in cases} >= {"temporal_valid", "temporal_tx"}
    by_valid = {c.as_of_valid for c in cases if c.as_of_valid}
    by_tx = {c.as_of_tx for c in cases if c.as_of_tx}
    assert "2024-03-01T00:00:00Z" in by_valid
    assert "2024-04-01T00:00:00Z" in by_tx
    # Every temporal probe names at least one stale value that must not surface.
    assert all(c.stale_values for c in cases if c.category.startswith("temporal"))


def test_load_dataset_temporal_is_builtin() -> None:
    assert load_dataset("temporal") == temporal_suite()


def test_load_dataset_unknown_raises() -> None:
    with pytest.raises(ValueError):
        load_dataset("nope")


def test_load_locomo_from_jsonl(tmp_path) -> None:
    path = tmp_path / "locomo.jsonl"
    path.write_text(
        json.dumps(
            {
                "conversation": "Bob moved to Berlin in March.",
                "qa": [
                    {"question": "Where does Bob live?", "answer": "Berlin", "category": "spatial"},
                    {"question": "ignored", "answer": "", "category": "spatial"},
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    cases = load_locomo(path)
    assert len(cases) == 1  # the empty answer is skipped
    assert cases[0].question == "Where does Bob live?"
    assert cases[0].gold == "Berlin"
    assert cases[0].category == "spatial"


def test_load_longmemeval_from_json(tmp_path) -> None:
    path = tmp_path / "lme.json"
    path.write_text(
        json.dumps(
            [
                {
                    "question": "What is the new wifi password?",
                    "answer": "hunter2",
                    "question_type": "knowledge_update",
                    "question_date": "2024-07-01",
                }
            ]
        ),
        encoding="utf-8",
    )
    cases = load_longmemeval(path)
    assert len(cases) == 1
    assert cases[0].category == "knowledge_update"
    assert cases[0].as_of_valid == "2024-07-01"


def test_loaders_missing_path_without_download_raises(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("STRATUM_BENCHMARK_OFFLINE", "1")
    with pytest.raises(ValueError):
        load_locomo(None, allow_download=True)
    with pytest.raises(ValueError):
        load_longmemeval(None, allow_download=True)


def test_load_missing_file_raises(tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        load_locomo(tmp_path / "does-not-exist.json")


# --------------------------------------------------------------------------- #
# harness
# --------------------------------------------------------------------------- #


def test_harness_perfect_engine_scores_one() -> None:
    harness = BenchmarkHarness(FakeEngine())
    result = harness.run(temporal_suite(), dataset="temporal", n_runs=3, seed=0)
    assert result.metrics["temporal_correctness"] == pytest.approx(1.0)
    assert result.metrics["contradiction_rate"] == pytest.approx(0.0)
    assert result.metrics["accuracy"] == pytest.approx(1.0)


def test_harness_is_deterministic() -> None:
    first = BenchmarkHarness(FakeEngine()).run(
        temporal_suite(), dataset="temporal", n_runs=2, seed=7
    )
    second = BenchmarkHarness(FakeEngine()).run(
        temporal_suite(), dataset="temporal", n_runs=2, seed=7
    )
    assert first.metrics == second.metrics
    assert first.per_category == second.per_category
    assert first.predictions == second.predictions
    assert first.runs == second.runs


def test_harness_median_over_runs() -> None:
    result = BenchmarkHarness(FakeEngine()).run(
        temporal_suite(), dataset="temporal", n_runs=3, seed=0
    )
    assert result.n_runs == 3
    assert len(result.runs) == 3
    # Median of three identical perfect runs is 1.0.
    assert result.metrics["temporal_correctness"] == pytest.approx(1.0)


def test_harness_records_seed_and_models() -> None:
    result = BenchmarkHarness(FakeEngine()).run(
        temporal_suite(), dataset="temporal", n_runs=1, seed=42
    )
    assert result.seed == 42
    assert result.model
    assert result.embedding_model
    assert result.timestamp  # ISO-8601 UTC, non-empty


def test_harness_detects_vector_regression() -> None:
    """A time-blind vector engine must lose on the temporal categories."""
    temporal = BenchmarkHarness(FakeEngine(always_latest=False)).run(
        temporal_suite(), dataset="temporal", n_runs=1, seed=0
    )
    vector = BenchmarkHarness(FakeEngine(always_latest=True)).run(
        temporal_suite(), dataset="temporal", n_runs=1, seed=0
    )
    # README §14: the engine must *strictly* beat the vector baseline on at
    # least one temporal category (`>`), not merely match it (`>=`).
    beat = False
    for category, metrics_dict in temporal.per_category.items():
        if category.startswith("temporal"):
            baseline = vector.per_category[category]
            assert (
                metrics_dict["temporal_correctness"]
                >= baseline["temporal_correctness"]
            )
            if metrics_dict["temporal_correctness"] > baseline["temporal_correctness"]:
                beat = True
    assert beat, "temporal engine must strictly beat the vector baseline somewhere"


def test_harness_rejects_bad_runs() -> None:
    with pytest.raises(ValueError):
        BenchmarkHarness(FakeEngine()).run(temporal_suite(), n_runs=0)


def test_harness_flags_engine_errors_not_false_absence() -> None:
    """B2: a dead engine must NOT score on the empty-gold absence case."""
    result = BenchmarkHarness(DeadEngine()).run(
        temporal_suite(), dataset="temporal", n_runs=1, seed=0
    )
    # Every case errored → every prediction carries the error flag.
    assert result.predictions
    assert all(record["error"] is True for record in result.predictions)
    # Never a false success: correctness/F1 are zero, not inflated by the
    # empty-gold "correctly removed" shortcut.
    assert result.metrics["temporal_correctness"] == pytest.approx(0.0)
    assert result.metrics["f1"] == pytest.approx(0.0)
    assert result.metrics["accuracy"] == pytest.approx(0.0)
    # The removed-fact (contradiction) category must also be zero, not perfect.
    assert result.per_category["contradiction"]["temporal_correctness"] == pytest.approx(0.0)


def test_harness_empty_gold_without_error_counts_as_removed() -> None:
    """B2: an empty-gold case only counts when NO error occurred."""
    case = next(c for c in temporal_suite() if not c.gold)
    record = BenchmarkHarness(FakeEngine())._score_case(case, "", error=False)
    assert record["temporal_ok"] is True
    assert record["error"] is False
    errored = BenchmarkHarness(FakeEngine())._score_case(case, "", error=True)
    assert errored["temporal_ok"] is False
    assert errored["f1"] == pytest.approx(0.0)
    assert errored["error"] is True


def test_harness_post_curator_flag_and_curator_invocation() -> None:
    """B1: an injected curator runs before the probes and flips post_curator."""
    curator = RecordingCurator()
    result = BenchmarkHarness(FakeEngine(), curator=curator).run(
        temporal_suite(), dataset="temporal", n_runs=1, seed=0
    )
    assert result.post_curator is True
    # Each versioned fact was validated and promoted through the curator.
    assert len(curator.validated) == len(TEMPORAL_FACTS)
    assert len(curator.promoted) == len(TEMPORAL_FACTS)
    # And the measurement is still clean after curation.
    assert result.metrics["contradiction_rate"] == pytest.approx(0.0)


def test_harness_without_curator_is_pre_curator() -> None:
    """B1: no curator injected → the artifact says the numbers are pre-curator."""
    result = BenchmarkHarness(FakeEngine()).run(
        temporal_suite(), dataset="temporal", n_runs=1, seed=0
    )
    assert result.post_curator is False


def test_seed_temporal_facts_writes_expected_windows() -> None:
    """B3: seeding writes every versioned fact with the right valid/tx window."""
    target = FakeGraphTarget()
    written = seed_temporal_facts(target, tenant="acme")
    assert written == len(TEMPORAL_FACTS)
    assert len(target.facts) == len(TEMPORAL_FACTS)

    by_key = {(f["predicate"], f["object_text"]): f for f in target.facts}
    old_ip = by_key[("ip_address", "10.0.0.5")]
    new_ip = by_key[("ip_address", "10.0.0.9")]
    assert old_ip["valid_from"] == "2024-01-01T00:00:00Z"
    assert old_ip["valid_to"] == "2024-06-01T00:00:00Z"
    assert new_ip["valid_from"] == "2024-06-01T00:00:00Z"
    assert new_ip["valid_to"] is None
    # Two distinct subjects become two entities.
    assert len(target.entities) == 2

    # The batch-hook surface is preferred when present and carries the full rows
    # (including the transaction-time window, which the graph surface cannot).
    class BatchTarget:
        def __init__(self) -> None:
            self.rows: list[Any] = []
            self.tenants: list[str] = []

        def seed_facts(self, facts: Any, *, tenant: str = "default") -> None:
            self.rows = list(facts)
            self.tenants.append(tenant)

    batch = BatchTarget()
    assert seed_temporal_facts(batch, tenant="t") == len(TEMPORAL_FACTS)
    assert len(batch.rows) == len(TEMPORAL_FACTS)
    assert batch.tenants == ["t"]
    # The tx windows of the corrected fact are seeded exactly.
    tx_by_key = {(r["predicate"], r["object_text"]): r for r in batch.rows}
    assert tx_by_key[("ip_address", "10.0.0.5")]["tx_from"] == "2024-01-01T00:00:00Z"
    assert tx_by_key[("ip_address", "10.0.0.5")]["tx_to"] == "2024-06-01T00:00:00Z"
    assert tx_by_key[("ip_address", "10.0.0.9")]["tx_from"] == "2024-06-01T00:00:00Z"
    assert tx_by_key[("ip_address", "10.0.0.9")]["tx_to"] is None


def test_vector_baseline_config_present_and_computed() -> None:
    assert VECTOR_BASELINE["mode"] == "vector"
    harness = BenchmarkHarness(FakeEngine(always_latest=True))
    result = harness.run(temporal_suite(), dataset="temporal", n_runs=1, seed=0, want_baseline=True)
    assert result.baseline is not None
    assert result.baseline["config"] == VECTOR_BASELINE
    assert "temporal_correctness" in result.baseline["metrics"]


# --------------------------------------------------------------------------- #
# run.py
# --------------------------------------------------------------------------- #


def test_run_benchmark_returns_result() -> None:
    result = run_benchmark("temporal", runs=2, engine=FakeEngine(), settings=_settings())
    assert result.dataset == "temporal"
    assert result.n_runs == 2
    assert "temporal_correctness" in result.metrics


def test_main_writes_artifact_structure(tmp_path, capsys) -> None:
    out = tmp_path / "report.json"
    code = main(
        ["--dataset", "temporal", "--runs", "2", "--seed", "1", "--out", str(out)],
        engine=FakeEngine(),
    )
    assert code == 0
    assert out.exists()
    data = json.loads(out.read_text(encoding="utf-8"))
    for key in (
        "dataset",
        "seed",
        "model",
        "embedding_model",
        "n_runs",
        "metrics",
        "per_category",
        "predictions",
        "runs",
        "timestamp",
        "settings",
    ):
        assert key in data
    assert data["seed"] == 1
    assert data["n_runs"] == 2
    assert set(data["metrics"]) >= {
        "f1",
        "exact_match",
        "accuracy",
        "temporal_correctness",
        "contradiction_rate",
    }
    # The predictions carry gold vs. predicted content for audit.
    assert data["predictions"]
    assert "gold" in data["predictions"][0]
    assert "prediction" in data["predictions"][0]


def test_main_writes_baseline_when_requested(tmp_path) -> None:
    out = tmp_path / "report-baseline.json"
    code = main(
        ["--dataset", "temporal", "--runs", "1", "--baseline", "--out", str(out)],
        engine=FakeEngine(always_latest=True),
    )
    assert code == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["baseline"]["config"] == VECTOR_BASELINE


def _settings() -> Any:
    from stratum.config import Settings

    return Settings()
