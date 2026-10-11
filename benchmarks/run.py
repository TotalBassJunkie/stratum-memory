"""CLI entry point for the benchmark harness (WP-16).

Usage::

    python -m benchmarks.run --dataset temporal --runs 3 --out benchmarks/report-<date>.json
    python -m benchmarks.run --dataset locomo --path data/locomo10.json --runs 3

Writes a JSON artifact with the §14 metrics, the seed, the pinned model version
and a timestamp. The engine is built from :class:`stratum.config.Settings`
(:class:`stratum.query.QueryEngine`) unless one is injected programmatically, so
the temporal suite can also be run fully offline with a fake engine in tests.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any

from .datasets import dataset_names, load_dataset
from .harness import BenchmarkHarness, BenchmarkResult

__all__ = ["build_engine", "default_out_path", "main", "run_benchmark"]

#: Category prefixes used by the temporal-suite acceptance comparison.
TEMPORAL_CATEGORY_PREFIXES = ("temporal_", "knowledge_update", "contradiction")


def default_out_path() -> str:
    """Return the default artifact path ``benchmarks/report-<date>.json``."""
    return str(Path(__file__).resolve().parent / f"report-{date.today().isoformat()}.json")


def build_engine(settings: Any) -> Any:
    """Construct the default query engine from settings."""
    from stratum.query import QueryEngine

    return QueryEngine(settings)


def run_benchmark(
    dataset: str,
    *,
    path: str | None = None,
    runs: int = 3,
    seed: int = 0,
    k: int = 8,
    level: str = "l0",
    baseline: bool = False,
    engine: Any | None = None,
    curator: Any | None = None,
    settings: Any | None = None,
    allow_download: bool = False,
) -> BenchmarkResult:
    """Run a named dataset and return the :class:`BenchmarkResult`.

    Args:
        dataset: One of :func:`benchmarks.datasets.dataset_names`.
        path: Local dataset path (required for ``locomo`` / ``longmemeval``).
        runs: Repeated runs per case; the median is reported.
        seed: Base random seed (recorded in the artifact).
        k: Retrieval cut-off.
        level: Text level.
        baseline: Also compute the vector baseline.
        engine: Injected engine (defaults to a fresh :class:`QueryEngine`).
        curator: Optional curator passed to the harness.
        settings: Configuration source (defaults to :func:`get_settings`).
        allow_download: Permit a network fetch when ``path`` is missing.
    """
    if settings is None:
        from stratum.config import get_settings

        settings = get_settings()
    cases = load_dataset(dataset, path, allow_download=allow_download)
    if engine is None:
        engine = build_engine(settings)
    harness = BenchmarkHarness(engine, curator=curator, settings=settings)
    return harness.run(
        cases,
        dataset=dataset,
        n_runs=runs,
        seed=seed,
        k=k,
        level=level,
        want_baseline=baseline,
    )


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """Build and parse the command-line arguments."""
    parser = argparse.ArgumentParser(
        prog="python -m benchmarks.run",
        description="Run the Stratum-Memory benchmark harness (WP-16).",
    )
    parser.add_argument("--dataset", choices=dataset_names(), default="temporal")
    parser.add_argument("--path", default=None, help="local .json/.jsonl dataset path")
    parser.add_argument("--runs", type=int, default=3, help="repeated runs (median reported)")
    parser.add_argument("--seed", type=int, default=0, help="base random seed")
    parser.add_argument("--k", type=int, default=8, help="retrieval cut-off")
    parser.add_argument("--level", default="l0", choices=("l0", "l1", "l2"))
    parser.add_argument("--out", default=None, help="output JSON path")
    parser.add_argument(
        "--baseline", action="store_true", help="also compute the vector baseline"
    )
    parser.add_argument(
        "--allow-download",
        action="store_true",
        help="permit a network fetch when --path is omitted",
    )
    return parser.parse_args(argv)


def _write_artifact(result: BenchmarkResult, out: str) -> str:
    """Write ``result`` as JSON to ``out``; return the resolved path."""
    target = Path(out)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(result.to_dict(), indent=2, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    return str(target)


def main(argv: Sequence[str] | None = None, *, engine: Any | None = None) -> int:
    """CLI entry point: run a dataset and write the JSON artifact.

    Returns the process exit code (``0`` on success).
    """
    args = _parse_args(argv)
    result = run_benchmark(
        args.dataset,
        path=args.path,
        runs=args.runs,
        seed=args.seed,
        k=args.k,
        level=args.level,
        baseline=args.baseline,
        engine=engine,
        allow_download=args.allow_download,
    )
    out = args.out or default_out_path()
    written = _write_artifact(result, out)
    print(json.dumps(result.metrics, indent=2, sort_keys=True))
    print(f"artifact: {written}", file=sys.stderr)
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI invocation
    raise SystemExit(main())
