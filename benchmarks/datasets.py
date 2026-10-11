"""Dataset loaders for the benchmark harness (WP-16).

Three sources are supported:

* ``locomo``      — LoCoMo long-conversation QA (local ``.json``/``.jsonl``),
* ``longmemeval`` — LongMemEval long-term-memory tasks (local ``.json``/``.jsonl``),
* ``temporal``    — a **built-in fixture suite** of *versioned facts*: a fact is
  corrected over time, producing real ``as_of_valid`` / ``as_of_tx`` probes and a
  stale-value contradiction check. This suite needs no dataset file and doubles
  as the offline default so the harness is testable without a network.

Network access is **off by default and disabled**: the two public datasets are
read from a local path passed as an argument. When ``allow_download=True`` (and
no path is given) a loader may fetch a known raw URL — but any failure degrades
to :class:`FileNotFoundError` rather than reaching the network in tests. Set
``STRATUM_BENCHMARK_OFFLINE=1`` to forbid downloads outright.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "DATASETS",
    "TEMPORAL_FACTS",
    "BenchmarkCase",
    "dataset_names",
    "load_dataset",
    "load_json_or_jsonl",
    "load_locomo",
    "load_longmemeval",
    "seed_temporal_facts",
    "temporal_suite",
]

#: The dataset registry used by ``benchmarks.run --dataset``.
DATASETS = ("locomo", "longmemeval", "temporal")

#: Environment variable that forbids any network access from the loaders.
OFFLINE_ENV = "STRATUM_BENCHMARK_OFFLINE"

#: Known raw download locations (only used when ``allow_download=True``).
DOWNLOAD_URLS: dict[str, str] = {
    "locomo": "https://raw.githubusercontent.com/snap-research/locomo/main/data/locomo10.json",
    "longmemeval": "https://huggingface.co/datasets/xiaowu0162/longmemeval/resolve/main/longmemeval_s.json",
}


@dataclass(frozen=True)
class BenchmarkCase:
    """One benchmark question with its gold answer and query parameters.

    Attributes:
        case_id: Stable identifier.
        category: Metric grouping (e.g. ``"temporal"``, ``"knowledge_update"``,
            ``"multi_session"``, ``"single_session"``).
        question: Natural-language query sent to the engine.
        gold: The expected answer text (token-compared, case-insensitive).
        mode: Query mode to use (``hybrid``/``vector``/``graph``/``kv``).
        as_of_valid: Valid-time view for the query (ISO string or ``None``).
        as_of_tx: Transaction-time view for the query (ISO string or ``None``).
        stale_values: Values that must **not** be surfaced (stale versions);
            surfacing one is a contradiction.
        subject: Subject the probe concerns (temporal graph scope).
        predicate: Predicate the probe concerns.
        metadata: Free-form extras carried from the source dataset.
    """

    case_id: str
    category: str
    question: str
    gold: str
    mode: str = "hybrid"
    as_of_valid: str | None = None
    as_of_tx: str | None = None
    stale_values: tuple[str, ...] = ()
    subject: str | None = None
    predicate: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


def dataset_names() -> tuple[str, ...]:
    """Return the names accepted by :func:`load_dataset`."""
    return DATASETS


# --------------------------------------------------------------------------- #
# Generic local file loader
# --------------------------------------------------------------------------- #


def load_json_or_jsonl(path: str | os.PathLike[str]) -> list[dict[str, Any]]:
    """Load a ``.json`` array/object or a ``.jsonl`` line-delimited file.

    Args:
        path: Local filesystem path.

    Returns:
        A list of record dicts. A ``.json`` file may hold either a bare array or
        an object with a ``"data"`` / ``"records"`` / ``"samples"`` list; a
        ``.jsonl`` file holds one JSON object per non-empty line.

    Raises:
        FileNotFoundError: when ``path`` does not exist.
        ValueError: when the file parses but yields no records.
    """
    file = Path(path)
    if not file.exists():
        raise FileNotFoundError(f"dataset not found: {file}")
    text = file.read_text(encoding="utf-8")
    if file.suffix.lower() == ".jsonl":
        records = [json.loads(line) for line in text.splitlines() if line.strip()]
    else:
        payload = json.loads(text)
        records = _extract_records(payload)
    if not records:
        raise ValueError(f"dataset {file} yielded no records")
    return records


def _extract_records(payload: Any) -> list[dict[str, Any]]:
    """Pull a list of record dicts out of a parsed JSON payload."""
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for key in ("data", "records", "samples", "questions", "conversations"):
            value = payload.get(key)
            if isinstance(value, list):
                return [r for r in value if isinstance(r, dict)]
        # A single record object.
        return [payload]
    return []


# --------------------------------------------------------------------------- #
# LoCoMo / LongMemEval
# --------------------------------------------------------------------------- #


def load_locomo(path: str | os.PathLike[str] | None = None, *, allow_download: bool = False) -> list[BenchmarkCase]:
    """Load LoCoMo long-conversation QA cases.

    LoCoMo stores conversations with QA pairs; the loader projects each QA pair
    into a :class:`BenchmarkCase` whose ``category`` is the QA's ``category``
    (defaulting to ``"conversation"``) and whose ``gold`` is the reference
    answer. The raw conversation text is preserved under ``metadata`` so a
    caller can ingest it into the store before running.

    Args:
        path: Local ``.json`` / ``.jsonl`` path. Required unless
            ``allow_download`` is set.
        allow_download: Permit fetching the known raw URL when ``path`` is
            ``None``. Forbidden in offline mode.

    Raises:
        ValueError: when neither ``path`` nor an allowed download is available.
    """
    records = _records_for("locomo", path, allow_download)
    cases: list[BenchmarkCase] = []
    for index, record in enumerate(records):
        conv = _first(record, "conversation", "context", "passage", "dialogue")
        qa_pairs = _qa_pairs(record)
        for qa_index, qa in enumerate(qa_pairs):
            question = str(_first(qa, "question", "query", "q") or "").strip()
            gold = str(_first(qa, "answer", "gold", "a", "response") or "").strip()
            if not question or not gold:
                continue
            category = str(_first(qa, "category", "type") or "conversation")
            cases.append(
                BenchmarkCase(
                    case_id=f"locomo-{index}-{qa_index}",
                    category=category,
                    question=question,
                    gold=gold,
                    mode="hybrid",
                    metadata={"conversation": conv} if conv is not None else {},
                )
            )
    return cases


def load_longmemeval(path: str | os.PathLike[str] | None = None, *, allow_download: bool = False) -> list[BenchmarkCase]:
    """Load LongMemEval long-term-memory cases.

    Each record becomes a :class:`BenchmarkCase`; ``category`` maps to the
    LongMemEval task type (``knowledge_update``, ``temporal_reasoning``,
    ``multi_session``, ``single_session``, …). Temporal questions carrying a
    reference date set ``as_of_valid`` so a time-aware engine can be evaluated.

    Args:
        path: Local ``.json`` / ``.jsonl`` path. Required unless
            ``allow_download`` is set.
        allow_download: Permit fetching the known raw URL when ``path`` is
            ``None``. Forbidden in offline mode.
    """
    records = _records_for("longmemeval", path, allow_download)
    cases: list[BenchmarkCase] = []
    for index, record in enumerate(records):
        question = str(_first(record, "question", "query", "input") or "").strip()
        gold = str(_first(record, "answer", "gold", "target", "output") or "").strip()
        if not question or not gold:
            continue
        category = str(
            _first(record, "question_type", "task_type", "category", "type") or "general"
        )
        as_of = _iso_or_none(_first(record, "question_date", "reference_date", "as_of_valid"))
        cases.append(
            BenchmarkCase(
                case_id=f"longmemeval-{index}",
                category=category,
                question=question,
                gold=gold,
                mode="hybrid",
                as_of_valid=as_of,
                metadata={
                    k: record[k]
                    for k in ("haystack_sessions", "sessions", "context")
                    if k in record
                },
            )
        )
    return cases


def _records_for(
    name: str, path: str | os.PathLike[str] | None, allow_download: bool
) -> list[dict[str, Any]]:
    """Return raw records for ``name`` from a path, or a permitted download."""
    if path is not None:
        return load_json_or_jsonl(path)
    if allow_download and not _offline():
        return _download(name)
    raise ValueError(
        f"{name}: no local path given and downloads are disabled "
        f"(set {OFFLINE_ENV}=0 and allow_download=True to fetch)"
    )


def _offline() -> bool:
    """Return ``True`` when the environment forbids downloads."""
    return os.environ.get(OFFLINE_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def _download(name: str) -> list[dict[str, Any]]:
    """Fetch the known raw URL for ``name`` (only reached when allowed)."""
    import urllib.request

    url = DOWNLOAD_URLS.get(name)
    if not url:
        raise ValueError(f"no download URL known for dataset {name!r}")
    with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310 - fixed https URL
        payload = json.loads(response.read().decode("utf-8"))
    return _extract_records(payload)


def _qa_pairs(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return the QA list of a LoCoMo record, accepting common key spellings."""
    for key in ("qa", "qas", "qa_pairs", "questions", "annotations"):
        value = record.get(key)
        if isinstance(value, list):
            return [v for v in value if isinstance(v, dict)]
    # A record that *is* a single QA pair.
    return [record] if ("question" in record or "query" in record) else []


def _first(mapping: Mapping[str, Any], *keys: str) -> Any:
    """Return the first present, non-``None`` value among ``keys``."""
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return None


def _iso_or_none(value: Any) -> str | None:
    """Return ``value`` as a stripped ISO-ish string, or ``None``."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


# --------------------------------------------------------------------------- #
# Built-in temporal suite
# --------------------------------------------------------------------------- #

#: The versioned facts backing the temporal suite. ``backend``'s ``ip_address``
#: is **corrected** (10.0.0.5 → 10.0.0.9); the correction is also recorded on
#: the transaction axis, so a query ``as_of_tx`` before the correction must see
#: the old value and one after must see the new. ``deploy_target``'s value is
#: closed outright to exercise contradiction on a removed fact.
TEMPORAL_FACTS: tuple[dict[str, Any], ...] = (
    {
        "subject": "backend",
        "predicate": "ip_address",
        "object_text": "10.0.0.5",
        "valid_from": "2024-01-01T00:00:00Z",
        "valid_to": "2024-06-01T00:00:00Z",
        "tx_from": "2024-01-01T00:00:00Z",
        "tx_to": "2024-06-01T00:00:00Z",
    },
    {
        "subject": "backend",
        "predicate": "ip_address",
        "object_text": "10.0.0.9",
        "valid_from": "2024-06-01T00:00:00Z",
        "valid_to": None,
        "tx_from": "2024-06-01T00:00:00Z",
        "tx_to": None,
    },
    {
        "subject": "backend",
        "predicate": "port",
        "object_text": "8080",
        "valid_from": "2024-01-01T00:00:00Z",
        "valid_to": None,
        "tx_from": "2024-01-01T00:00:00Z",
        "tx_to": None,
    },
    {
        "subject": "deploy_target",
        "predicate": "hostname",
        "object_text": "legacy.example.com",
        "valid_from": "2024-01-01T00:00:00Z",
        "valid_to": "2024-05-01T00:00:00Z",
        "tx_from": "2024-01-01T00:00:00Z",
        "tx_to": "2024-05-01T00:00:00Z",
    },
)


def temporal_suite() -> list[BenchmarkCase]:
    """Return the built-in temporal versioning fixtures (no file, no network).

    The suite probes the bitemporal contract directly:

    * **valid-time time travel** — at 2024-03 the backend IP was 10.0.0.5, at
      2024-09 it is 10.0.0.9;
    * **transaction-time time travel** — the system only *knew* the correction
      from 2024-06, so a query ``as_of_tx`` 2024-04 must still answer 10.0.0.5;
    * **contradiction** — the current view must surface 10.0.0.9 and must not
      surface the stale 10.0.0.5; the removed ``deploy_target`` fact must be
      absent from the current view.
    """
    return [
        BenchmarkCase(
            case_id="temporal-ip-valid-old",
            category="temporal_valid",
            question="What is the backend ip_address?",
            gold="10.0.0.5",
            mode="graph",
            as_of_valid="2024-03-01T00:00:00Z",
            stale_values=("10.0.0.9",),
            subject="backend",
            predicate="ip_address",
        ),
        BenchmarkCase(
            case_id="temporal-ip-valid-new",
            category="temporal_valid",
            question="What is the backend ip_address?",
            gold="10.0.0.9",
            mode="graph",
            as_of_valid="2024-09-01T00:00:00Z",
            stale_values=("10.0.0.5",),
            subject="backend",
            predicate="ip_address",
        ),
        BenchmarkCase(
            case_id="temporal-ip-tx-before-correction",
            category="temporal_tx",
            question="What is the backend ip_address?",
            gold="10.0.0.5",
            mode="graph",
            as_of_tx="2024-04-01T00:00:00Z",
            stale_values=("10.0.0.9",),
            subject="backend",
            predicate="ip_address",
        ),
        BenchmarkCase(
            case_id="temporal-ip-tx-after-correction",
            category="temporal_tx",
            question="What is the backend ip_address?",
            gold="10.0.0.9",
            mode="graph",
            as_of_tx="2024-07-01T00:00:00Z",
            stale_values=("10.0.0.5",),
            subject="backend",
            predicate="ip_address",
        ),
        BenchmarkCase(
            case_id="temporal-ip-current",
            category="knowledge_update",
            question="What is the backend ip_address?",
            gold="10.0.0.9",
            mode="graph",
            stale_values=("10.0.0.5",),
            subject="backend",
            predicate="ip_address",
        ),
        BenchmarkCase(
            case_id="temporal-port-current",
            category="knowledge_update",
            question="What is the backend port?",
            gold="8080",
            mode="graph",
            subject="backend",
            predicate="port",
        ),
        BenchmarkCase(
            case_id="temporal-deploy-target-removed",
            category="contradiction",
            question="What is the deploy_target hostname?",
            gold="",
            mode="graph",
            stale_values=("legacy.example.com",),
            subject="deploy_target",
            predicate="hostname",
        ),
    ]


def _accepts_keyword(func: Any, name: str) -> bool:
    """Return ``True`` when ``func`` accepts a keyword argument ``name``."""
    import inspect

    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):
        return False
    if name in signature.parameters:
        return True
    return any(
        param.kind is inspect.Parameter.VAR_KEYWORD
        for param in signature.parameters.values()
    )


def seed_temporal_facts(
    target: Any,
    facts: Sequence[Mapping[str, Any]] | None = None,
    *,
    tenant: str = "default",
) -> int:
    """Write the versioned temporal facts into a real graph/store (B3).

    The temporal suite is only meaningful against a *seeded* engine: the
    fixtures in :data:`TEMPORAL_FACTS` must exist in the store with their exact
    valid- and transaction-time windows before the probes run. This helper
    writes them through an injected ``target`` so the suite can be run against a
    live :class:`~stratum.graph.TemporalGraph` (and, in tests, a fake).

    ``target`` may expose any of:

    * ``seed_facts(facts, tenant=...)`` — a batch hook (preferred): receives the
      full fact rows including the **transaction-time** window, so both axes are
      written exactly; or
    * ``upsert_entity`` + ``add_fact`` — the :class:`TemporalGraph` surface. Each
      fact's ``subject`` is upserted as an entity (canonical URI
      ``stratum://entities/<tenant>/<subject>``) and the fact is added with its
      ``valid_from``/``valid_to``. The graph's ``add_fact`` has no
      ``tx_from``/``tx_to`` parameters (it stamps ``tx_from = now()``), so this
      path reproduces the **valid-time** window exactly; a caller needing an
      exact transaction-time window must supply a ``seed_facts`` hook.

    Args:
        target: The graph/store to seed. A duck-typed object with the methods
            above, or a :class:`~stratum.graph.TemporalGraph`.
        facts: The facts to write; defaults to :data:`TEMPORAL_FACTS`.
        tenant: Tenant id every fact is written under.

    Returns:
        The number of facts written.

    Raises:
        TypeError: when ``target`` exposes neither seeding surface.
    """
    rows = list(TEMPORAL_FACTS if facts is None else facts)

    seed_batch = getattr(target, "seed_facts", None)
    if callable(seed_batch):
        if _accepts_keyword(seed_batch, "tenant"):
            seed_batch(rows, tenant=tenant)
        else:
            seed_batch(rows)
        return len(rows)

    upsert_entity = getattr(target, "upsert_entity", None)
    add_fact = getattr(target, "add_fact", None)
    if not (callable(upsert_entity) and callable(add_fact)):
        raise TypeError(
            "target must expose seed_facts(...) or upsert_entity + add_fact"
        )

    entity_ids: dict[str, str] = {}
    for fact in rows:
        subject = str(fact["subject"])
        if subject not in entity_ids:
            entity_ids[subject] = str(
                upsert_entity(
                    tenant,
                    name=subject,
                    canonical_uri=f"stratum://entities/{tenant}/{subject}",
                )
            )
        add_fact(
            tenant,
            subject_entity_id=entity_ids[subject],
            predicate=str(fact["predicate"]),
            object_text=fact.get("object_text"),
            valid_from=fact.get("valid_from"),
            valid_to=fact.get("valid_to"),
            source_uri=f"stratum://benchmarks/temporal/{subject}/{fact['predicate']}",
        )
    return len(rows)


def load_dataset(
    name: str,
    path: str | os.PathLike[str] | None = None,
    *,
    allow_download: bool = False,
) -> list[BenchmarkCase]:
    """Load a named dataset as a list of :class:`BenchmarkCase`.

    Args:
        name: One of :data:`DATASETS` (``locomo`` / ``longmemeval`` /
            ``temporal``).
        path: Local path for ``locomo`` / ``longmemeval``; ignored for
            ``temporal`` (which is built in).
        allow_download: Permit a network fetch when ``path`` is ``None``.

    Raises:
        ValueError: on an unknown dataset name.
    """
    if name == "temporal":
        return temporal_suite()
    if name == "locomo":
        return load_locomo(path, allow_download=allow_download)
    if name == "longmemeval":
        return load_longmemeval(path, allow_download=allow_download)
    raise ValueError(f"unknown dataset {name!r}; allowed: {DATASETS}")
