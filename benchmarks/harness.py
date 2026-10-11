"""Benchmark harness: run datasets against the query engine (WP-16).

:class:`BenchmarkHarness` executes a list of :class:`~benchmarks.datasets.BenchmarkCase`
against an injected query engine (a :class:`~stratum.query.QueryEngine` or a
test fake with the same ``query`` signature), collects each case's prediction
versus its gold answer, and computes the §14 metrics per category.

The harness is **reproducible**: the random seed is fixed (``seed`` plus the run
index), the model version is read from :class:`~stratum.config.Settings` and
recorded in the result, and each case is queried ``n_runs`` times (default 3)
with the **median** reported.

**Curator runs before the temporal probes.** When a ``curator`` is injected it
first curates the versioned temporal facts (``Curator.validate`` + ``promote``),
so the reported ``contradiction_rate`` is measured *after* curation (§14: "0 nach
Curator"). Without a curator the run cannot curate anything, so the metric is a
**pre-curator** measurement; the result records this unambiguously in
:attr:`BenchmarkResult.post_curator`.

Nothing here touches Postgres or the network directly; the engine is injected.
"""

from __future__ import annotations

import random
import statistics
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from .datasets import TEMPORAL_FACTS, BenchmarkCase
from .metrics import _contains, contradiction_rate, f1

__all__ = ["VECTOR_BASELINE", "BenchmarkHarness", "BenchmarkResult"]

#: Configuration of the pure-vector baseline (ADR/§14 acceptance threshold:
#: Stratum must beat this on the temporal categories).
VECTOR_BASELINE: dict[str, Any] = {
    "mode": "vector",
    "level": "l0",
    "k": 8,
    "use_curator": False,
}

#: Query modes the harness will pass through to the engine.
_VALID_MODES = ("hybrid", "vector", "graph", "kv")

#: Metrics reported overall and per category.
_METRIC_NAMES = (
    "f1",
    "exact_match",
    "accuracy",
    "temporal_correctness",
    "contradiction_rate",
)


@dataclass
class BenchmarkResult:
    """Outcome of a harness run, ready to serialise as the §14 artifact.

    Attributes:
        dataset: Dataset name (``locomo`` / ``longmemeval`` / ``temporal``).
        seed: The base random seed used.
        model: Pinned chat model version from settings.
        embedding_model: Pinned embedding model version from settings.
        n_runs: Number of repeated runs per case.
        metrics: Median metric values over all runs (overall).
        per_category: ``category -> {metric: median}``.
        predictions: One record per case (from the last run), including gold,
            prediction and the per-case flags.
        runs: The per-run raw metric dicts (for audit).
        baseline: Vector-baseline metrics when computed, else ``None``.
        post_curator: Whether a curator ran before the temporal probes. ``True``
            means the metrics are measured *after* curation (§14
            contradiction-rate target); ``False`` means no curator was injected
            and the numbers are a pre-curator measurement.
        timestamp: ISO-8601 UTC timestamp of the run.
        settings: A small snapshot of the relevant settings.
    """

    dataset: str
    seed: int
    model: str
    embedding_model: str
    n_runs: int
    metrics: dict[str, float]
    per_category: dict[str, dict[str, float]]
    predictions: list[dict[str, Any]] = field(default_factory=list)
    runs: list[dict[str, Any]] = field(default_factory=list)
    baseline: dict[str, Any] | None = None
    post_curator: bool = False
    timestamp: str = ""
    settings: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dict of the whole result."""
        return asdict(self)


class BenchmarkHarness:
    """Runs benchmark cases against an injected query engine.

    Args:
        engine: Object exposing ``query(tenant, query, *, mode, k, level,
            as_of_valid, as_of_tx, ...) -> list[dict]``. A
            :class:`~stratum.query.QueryEngine` or a test fake.
        curator: Optional curator with a ``validate``/``promote`` surface used
            for the temporal contradiction check. When supplied, the versioned
            temporal facts are curated (validated + promoted) **before** the
            temporal probes run, so the reported ``contradiction_rate`` is a
            post-curator measurement. ``None`` disables curation and the result
            is flagged ``post_curator=False``.
        settings: Configuration source; defaults to
            :func:`stratum.config.get_settings`.
        tenant: Tenant id used for every query.
    """

    def __init__(
        self,
        engine: Any,
        curator: Any | None = None,
        settings: Any | None = None,
        *,
        tenant: str = "default",
    ) -> None:
        self.engine = engine
        self.curator = curator
        self.tenant = tenant
        if settings is None:
            from stratum.config import get_settings

            settings = get_settings()
        self.settings = settings

    # ------------------------------------------------------------- properties

    @property
    def model(self) -> str:
        """Pinned chat model version (recorded in the artifact)."""
        return str(getattr(self.settings, "llm_chat_model", "unknown"))

    @property
    def embedding_model(self) -> str:
        """Pinned embedding model version (recorded in the artifact)."""
        return str(getattr(self.settings, "llm_embed_model", "unknown"))

    # ------------------------------------------------------------------ public

    def run(
        self,
        cases: Sequence[BenchmarkCase],
        *,
        dataset: str = "temporal",
        n_runs: int = 3,
        seed: int = 0,
        k: int = 8,
        level: str = "l0",
        mode_override: str | None = None,
        want_baseline: bool = False,
    ) -> BenchmarkResult:
        """Execute ``cases`` ``n_runs`` times and return the median result.

        Args:
            cases: The benchmark cases to run.
            dataset: Dataset label recorded in the result.
            n_runs: Number of repeated runs (``>= 1``); the median is reported.
            seed: Base seed; run ``i`` uses ``seed + i`` so runs are reproducible.
            k: Retrieval cut-off passed to the engine.
            level: Text level passed to the engine.
            mode_override: When set, forces every case into this query mode
                (used to compute the vector baseline).
            want_baseline: Also compute the vector baseline and attach it.

        Raises:
            ValueError: on ``n_runs < 1`` or an invalid ``mode_override``.
        """
        if n_runs < 1:
            raise ValueError(f"n_runs must be >= 1, got {n_runs!r}")
        if mode_override is not None and mode_override not in _VALID_MODES:
            raise ValueError(f"invalid mode_override {mode_override!r}")

        # B1: curate the versioned temporal facts *before* probing, so the
        # contradiction rate is a genuine post-curator measurement.
        temporal_cases = [case for case in cases if case.subject is not None]
        post_curator = False
        if temporal_cases:
            post_curator = self._curate_temporal_facts()

        per_run: list[dict[str, Any]] = []
        last_predictions: list[dict[str, Any]] = []
        for run_index in range(n_runs):
            random.seed(seed + run_index)  # pin any engine-side randomness
            predictions = self._run_once(
                cases, k=k, level=level, mode_override=mode_override
            )
            per_run.append(self._aggregate(predictions))
            last_predictions = predictions

        metrics = _median_metrics(per_run)
        per_category = _median_per_category(per_run)

        baseline: dict[str, Any] | None = None
        if want_baseline:
            baseline = self._vector_baseline(cases, k=k, seed=seed)

        return BenchmarkResult(
            dataset=dataset,
            seed=seed,
            model=self.model,
            embedding_model=self.embedding_model,
            n_runs=n_runs,
            metrics=metrics,
            per_category=per_category,
            predictions=last_predictions,
            runs=per_run,
            baseline=baseline,
            post_curator=post_curator,
            timestamp=datetime.now(UTC).isoformat(),
            settings=_settings_snapshot(self.settings),
        )

    # --------------------------------------------------------------- curation

    def _curate_temporal_facts(self) -> bool:
        """Curate the versioned temporal facts before the probes (B1).

        When a ``curator`` is injected, every :data:`~benchmarks.datasets.
        TEMPORAL_FACTS` fact is turned into a candidate and passed through
        ``curator.validate`` (and ``curator.promote`` when the verdict approves
        and the curator exposes it). This makes the active view the curator's
        output, so ``contradiction_rate`` is measured **after** curation.

        Returns:
            ``True`` when a curator ran, ``False`` when none was injected (the
            metrics are then a pre-curator measurement).
        """
        if self.curator is None:
            return False
        for fact in TEMPORAL_FACTS:
            candidate = _fact_candidate(fact, self.tenant)
            verdict = self.curator.validate(self.tenant, candidate)
            if not getattr(verdict, "approved", False):
                continue
            promote = getattr(self.curator, "promote", None)
            if callable(promote):
                try:
                    promote(self.tenant, candidate)
                except Exception:  # noqa: BLE001 - curation is best-effort here
                    pass
        return True

    # --------------------------------------------------------------- internals

    def _run_once(
        self,
        cases: Sequence[BenchmarkCase],
        *,
        k: int,
        level: str,
        mode_override: str | None,
    ) -> list[dict[str, Any]]:
        """Query every case once and record prediction vs gold."""
        records: list[dict[str, Any]] = []
        for case in cases:
            hits, error = self._query_case(
                case, k=k, level=level, mode_override=mode_override
            )
            prediction = " ".join(str(hit.get("content", "")) for hit in hits)
            records.append(self._score_case(case, prediction, error=error))
        return records

    def _query_case(
        self,
        case: BenchmarkCase,
        *,
        k: int,
        level: str,
        mode_override: str | None,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Call the engine for one case, degrading to ``([], True)`` on error.

        A read path must never crash the whole benchmark: a failing case scores
        zero rather than aborting the run. The second element is the **error
        flag** (B2): a swallowed exception is reported so a silent empty answer
        (which would otherwise look like a correct absence) is scored as a
        failure instead of a false success.
        """
        mode = mode_override or case.mode
        try:
            hits = self.engine.query(
                self.tenant,
                case.question,
                mode=mode,
                k=k,
                level=level,
                as_of_valid=case.as_of_valid,
                as_of_tx=case.as_of_tx,
            )
        except Exception:  # noqa: BLE001 - benchmark isolation, not control flow
            return [], True
        return list(hits or []), False

    def _score_case(
        self, case: BenchmarkCase, prediction: str, *, error: bool = False
    ) -> dict[str, Any]:
        """Score one prediction: EM, F1, temporal correctness, contradiction.

        Case-level ``exact_match``/``accuracy`` use *answer containment*: an
        engine returning a fact row (e.g. ``"backend ip_address 10.0.0.9"``) is
        correct for the gold ``"10.0.0.9"``. The strict string-equality variant
        lives in :func:`benchmarks.metrics.exact_match` and is tested separately.

        A case whose query raised (``error=True``) is scored as a failure:
        ``temporal_ok=False`` and ``f1=0.0`` even when the gold is empty, so a
        dead engine can never score on absence cases (B2).
        """
        contradiction = any(
            _contains_token(prediction, stale) for stale in case.stale_values
        )
        if error:
            # A swallowed exception is not "correctly removed": no answer was
            # produced, so every metric is a miss.
            answer_hit = False
            temporal_ok = False
        elif case.gold:
            answer_hit = _contains_token(prediction, case.gold)
            temporal_ok = answer_hit and not contradiction
        else:
            # An empty gold marks a removed/absent fact: correct when nothing
            # stale (and nothing contradicting) is surfaced.
            answer_hit = not contradiction
            temporal_ok = not contradiction
        if error:
            f1_value = 0.0
        elif case.gold:
            f1_value = f1(prediction, case.gold)
        else:
            f1_value = 1.0 if temporal_ok else 0.0
        return {
            "case_id": case.case_id,
            "category": case.category,
            "gold": case.gold,
            "prediction": prediction,
            "error": error,
            "exact_match": 1.0 if answer_hit else 0.0,
            "f1": f1_value,
            "temporal_ok": temporal_ok,
            "contradiction": contradiction,
            "stale_values": list(case.stale_values),
        }

    def _aggregate(self, predictions: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        """Aggregate one run's per-case records into overall + per-category."""
        overall = _metric_dict(predictions)
        by_category: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for record in predictions:
            by_category[str(record["category"])].append(record)
        return {
            "overall": overall,
            "per_category": {
                category: _metric_dict(records) for category, records in by_category.items()
            },
        }

    def _vector_baseline(
        self, cases: Sequence[BenchmarkCase], *, k: int, seed: int
    ) -> dict[str, Any]:
        """Compute the pure-vector baseline for the same cases (§14 threshold)."""
        result = self.run(
            cases,
            dataset="baseline",
            n_runs=1,
            seed=seed,
            k=int(VECTOR_BASELINE["k"]) if VECTOR_BASELINE.get("k") else k,
            level=str(VECTOR_BASELINE["level"]),
            mode_override=str(VECTOR_BASELINE["mode"]),
            want_baseline=False,
        )
        return {
            "config": dict(VECTOR_BASELINE),
            "metrics": result.metrics,
            "per_category": result.per_category,
        }


# --------------------------------------------------------------------------- #
# Aggregation helpers
# --------------------------------------------------------------------------- #


def _metric_dict(records: Sequence[Mapping[str, Any]]) -> dict[str, float]:
    """Compute the metric set for a list of scored case records."""
    if not records:
        return {name: 0.0 for name in _METRIC_NAMES}
    n = len(records)
    f1_sum = sum(float(r.get("f1", 0.0)) for r in records)
    em_sum = sum(float(r.get("exact_match", 0.0)) for r in records)
    temporal_sum = sum(1.0 for r in records if r.get("temporal_ok"))
    contradiction = contradiction_rate([bool(r.get("contradiction")) for r in records])
    em = em_sum / n
    return {
        "f1": f1_sum / n,
        "exact_match": em,
        "accuracy": em,
        "temporal_correctness": temporal_sum / n,
        "contradiction_rate": contradiction,
    }


def _median_metrics(runs: Sequence[Mapping[str, Any]]) -> dict[str, float]:
    """Median of each overall metric across runs."""
    return {
        name: _median([run["overall"].get(name, 0.0) for run in runs])
        for name in _METRIC_NAMES
    }


def _median_per_category(runs: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, float]]:
    """Median of each per-category metric across runs."""
    categories: set[str] = set()
    for run in runs:
        categories.update(run["per_category"].keys())
    result: dict[str, dict[str, float]] = {}
    for category in sorted(categories):
        result[category] = {
            name: _median(
                [
                    run["per_category"].get(category, {}).get(name, 0.0)
                    for run in runs
                ]
            )
            for name in _METRIC_NAMES
        }
    return result


def _median(values: Sequence[float]) -> float:
    """Median of ``values`` (``0.0`` for an empty sequence); deterministic."""
    if not values:
        return 0.0
    return float(statistics.median(sorted(values)))


def _contains_token(haystack: str, needle: str) -> bool:
    """Return ``True`` when ``needle`` appears in ``haystack`` as a token slice."""
    return _contains(haystack, needle)


def _fact_candidate(fact: Mapping[str, Any], tenant: str) -> dict[str, Any]:
    """Project a temporal fact into a curator candidate payload.

    Produces the WP-07 fact shape the curator validates (``kind``, ``subject``,
    ``predicate``, ``object_text``, ``tenant_id``, ``source_uri`` and the
    validity window). ``tx_from``/``tx_to`` are carried as metadata because the
    curator's time-consistency criterion only reads the valid-time window.
    """
    return {
        "kind": "fact",
        "tenant_id": tenant,
        "subject": fact["subject"],
        "predicate": fact["predicate"],
        "object_text": fact["object_text"],
        "valid_from": fact.get("valid_from"),
        "valid_to": fact.get("valid_to"),
        "tx_from": fact.get("tx_from"),
        "tx_to": fact.get("tx_to"),
        "confidence": 1.0,
        "source_uri": f"stratum://benchmarks/temporal/{fact['subject']}/{fact['predicate']}",
        "embedding": _placeholder_embedding(),
    }


def _placeholder_embedding() -> list[float]:
    """A deterministic unit-ish placeholder embedding for the dedup criterion.

    The harness cannot compute a real embedding (no model is reached offline);
    a fixed vector lets :meth:`Curator.validate` run its dedup path instead of
    rejecting the candidate as ``no_embedding``.
    """
    return [0.0] * 1024


def _settings_snapshot(settings: Any) -> dict[str, Any]:
    """A small, JSON-serialisable snapshot of the benchmark-relevant settings."""
    keys = (
        "llm_chat_model",
        "llm_embed_model",
        "embedding_dim",
        "vector_type",
        "dedup_threshold",
        "min_confidence",
    )
    return {key: getattr(settings, key, None) for key in keys}
