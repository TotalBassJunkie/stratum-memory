"""Observability & cost metering (WP-13).

This module is the single, lightweight observability surface for Stratum-Memory
(``MASTER-PLAN.md`` §4 — "/health, /metrics (Prometheus text format), strukturierte
JSON-Logs" — and §15, the operating checks):

* :func:`setup_logging` / :func:`log_event` — one JSON object per log record
  (``time``, ``level``, ``logger``, ``msg`` plus extra fields),
* :class:`Metrics` — a thread-safe, in-memory registry of counters, gauges and
  histogram-style observations that renders the Prometheus text exposition
  format deterministically,
* per-tenant / per-model token + cost accounting
  (:meth:`Metrics.add_tokens`, :meth:`Metrics.cost_summary`),
* :func:`health` — a never-raising readiness probe covering DB, Git, LLM and the
  queue depth,
* :func:`metrics_response` — the ``(body, content_type)`` tuple a FastAPI/MCP
  handler returns.

Everything degrades gracefully: :func:`health` is callable without a reachable
database and returns ``status="degraded"`` instead of raising. The module does no
outbound I/O of its own and never touches secrets.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

__all__ = [
    "DEFAULT_DB_TIMEOUT",
    "DEFAULT_MODEL",
    "PROMETHEUS_CONTENT_TYPE",
    "PRICES",
    "JsonFormatter",
    "Metrics",
    "default_metrics",
    "health",
    "log_event",
    "metrics_response",
    "setup_logging",
]

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

#: Content type of the Prometheus text exposition format (version 0.0.4).
PROMETHEUS_CONTENT_TYPE = "text/plain; version=0.0.4"

#: Model label used when :meth:`Metrics.add_tokens` is called without ``model``.
DEFAULT_MODEL = "unknown"

#: Simple price table: model -> (USD per 1000 prompt tokens, per 1000 completion
#: tokens). Models absent from this table are treated as free (cost ``0.0``), so
#: adding an entry is the only change needed to start metering cost.
PRICES: dict[str, tuple[float, float]] = {
    "gpt-4o-mini": (0.00015, 0.0006),
    "gpt-4o": (0.0025, 0.01),
    "text-embedding-3-small": (0.00002, 0.0),
    "text-embedding-3-large": (0.00013, 0.0),
}

#: Default seconds :func:`health` waits for a database connection before it
#: declares the DB unreachable. Overridable via ``STRATUM_HEALTH_DB_TIMEOUT``.
DEFAULT_DB_TIMEOUT = 1.0

# --------------------------------------------------------------------------- #
# Structured JSON logging
# --------------------------------------------------------------------------- #

#: Attribute names that belong to ``logging.LogRecord`` itself; every other
#: attribute attached via ``extra=`` is treated as an "extra field" and emitted
#: at the top level of the JSON object.
_STANDARD_ATTRS: frozenset[str] = frozenset(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__
) | {"message", "asctime", "exc_text", "taskName"}

#: Levels that cannot be used as ``extra=`` keys (``logging`` would raise).
_RESERVED_EXTRA_KEYS: frozenset[str] = _STANDARD_ATTRS


class JsonFormatter(logging.Formatter):
    """Format a :class:`logging.LogRecord` as a single JSON line.

    The object always carries ``time`` (ISO-8601 UTC), ``level``, ``logger`` and
    ``msg``; any additional attribute on the record (e.g. the ``**fields`` of
    :func:`log_event`) is copied to the top level. Exceptions are included under
    ``exc_info``.
    """

    def format(self, record: logging.LogRecord) -> str:  # noqa: A003 - logging API
        payload: dict[str, Any] = {
            "time": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in _STANDARD_ATTRS or key.startswith("_"):
                continue
            payload[key] = _jsonable(value)
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)


def _jsonable(value: Any) -> Any:
    """Return ``value`` unchanged when JSON-serialisable, else its ``str``."""
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return str(value)
    return value


def _coerce_level(level: str | int) -> int:
    """Map a level name or number to its numeric logging level."""
    if isinstance(level, int):
        return level
    mapping = getattr(logging, "getLevelNamesMapping", None)
    if callable(mapping):
        try:
            return int(mapping()[str(level).upper()])
        except (KeyError, TypeError, ValueError):
            pass
    try:
        return int(level)
    except (TypeError, ValueError):
        return logging.INFO


def setup_logging(level: str | int = "INFO") -> logging.Logger:
    """Configure the root logger for one-JSON-object-per-record output.

    Existing root handlers are replaced so repeated calls do not duplicate log
    lines. Returns the configured root logger.

    Args:
        level: Logging level as a name (``"INFO"``) or a numeric value.
    """
    root = logging.getLogger()
    root.setLevel(_coerce_level(level))
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root.handlers[:] = [handler]
    return root


def _sanitize_fields(fields: Mapping[str, Any]) -> dict[str, Any]:
    """Rename keys that collide with ``LogRecord`` attributes.

    ``logging`` raises when ``extra`` would overwrite a reserved attribute, so a
    colliding key ``name`` becomes ``field_name`` instead of failing.
    """
    safe: dict[str, Any] = {}
    for key, value in fields.items():
        name = str(key)
        if name in _RESERVED_EXTRA_KEYS or name.startswith("_"):
            name = f"field_{name}"
        safe[name] = value
    return safe


def log_event(
    logger: logging.Logger | str,
    msg: str,
    *,
    level: str | int = logging.INFO,
    **fields: Any,
) -> None:
    """Emit a structured log record carrying ``fields`` as top-level JSON keys.

    Args:
        logger: A :class:`logging.Logger` or a logger name.
        msg: Human-readable message (the ``msg`` key of the JSON object).
        level: Logging level as a name or number (default ``INFO``).
        **fields: Arbitrary structured data; each becomes a JSON key. Keys that
            clash with reserved record attributes are prefixed with ``field_``.
    """
    if isinstance(logger, str):
        logger = logging.getLogger(logger)
    logger.log(_coerce_level(level), msg, extra=_sanitize_fields(fields))


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #


@dataclass
class _Histogram:
    """Accumulator for :meth:`Metrics.observe` (count / sum / min / max)."""

    count: int = 0
    sum: float = 0.0
    min: float | None = None
    max: float | None = None


def _label_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _label_items(labels: Mapping[str, Any] | None) -> tuple[tuple[str, str], ...]:
    """Return labels as a sorted, hashable ``((key, value), ...)`` tuple."""
    if not labels:
        return ()
    return tuple(sorted((str(k), _label_value(v)) for k, v in labels.items()))


def _merge_labels(
    labels: Mapping[str, Any] | None, extra: Mapping[str, Any]
) -> dict[str, Any] | None:
    if not labels and not extra:
        return None
    merged: dict[str, Any] = dict(labels or {})
    merged.update(extra)
    return merged


def _format_number(value: Any) -> str:
    """Render a numeric metric value without a trailing ``.0`` for integers."""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    number = float(value)
    if number.is_integer():
        return str(int(number))
    return repr(number)


def _escape_label_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _format_labels(items: tuple[tuple[str, str], ...]) -> str:
    if not items:
        return ""
    inner = ",".join(f'{key}="{_escape_label_value(val)}"' for key, val in items)
    return "{" + inner + "}"


class Metrics:
    """Thread-safe, in-memory metrics registry with a Prometheus renderer.

    Three metric kinds are supported:

    * **counters** via :meth:`inc` (monotonic),
    * **gauges** via :meth:`set_gauge` (last-value-wins),
    * **observations** via :meth:`observe` (count / sum / min / max, rendered as a
      Prometheus ``summary`` family).

    Labels are passed as a mapping (``labels={"tenant": "t1"}``) or, as a
    convenience, as keyword arguments (``tenant="t1"``). Token and cost accounting
    lives alongside (:meth:`add_tokens`, :meth:`cost_summary`).

    The registry is intentionally small and dependency-free; a single process
    holds one instance (see :func:`default_metrics`).
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._kinds: dict[str, str] = {}
        self._counters: dict[str, dict[tuple[tuple[str, str], ...], float]] = {}
        self._gauges: dict[str, dict[tuple[tuple[str, str], ...], float]] = {}
        self._observations: dict[
            str, dict[tuple[tuple[str, str], ...], _Histogram]
        ] = {}
        self._tokens: dict[str, dict[str, list[int]]] = {}

    # ------------------------------------------------------------- internals

    def _declare(self, name: str, kind: str) -> None:
        existing = self._kinds.get(name)
        if existing is None:
            self._kinds[name] = kind
        elif existing != kind:
            raise ValueError(
                f"metric {name!r} already registered as {existing!r}, not {kind!r}"
            )

    # ---------------------------------------------------------------- counters

    def inc(
        self,
        name: str,
        value: float = 1,
        *,
        labels: Mapping[str, Any] | None = None,
        **label_kwargs: Any,
    ) -> float:
        """Increment counter ``name`` by ``value`` (default 1); returns the total."""
        key = _label_items(_merge_labels(labels, label_kwargs))
        with self._lock:
            self._declare(name, "counter")
            series = self._counters.setdefault(name, {})
            new_value = series.get(key, 0.0) + value
            series[key] = new_value
            return new_value

    # ------------------------------------------------------------------ gauges

    def set_gauge(
        self,
        name: str,
        value: float,
        *,
        labels: Mapping[str, Any] | None = None,
        **label_kwargs: Any,
    ) -> None:
        """Set gauge ``name`` to ``value`` (last value wins)."""
        key = _label_items(_merge_labels(labels, label_kwargs))
        with self._lock:
            self._declare(name, "gauge")
            self._gauges.setdefault(name, {})[key] = value

    # ------------------------------------------------------------ observations

    def observe(
        self,
        name: str,
        value: float,
        *,
        labels: Mapping[str, Any] | None = None,
        **label_kwargs: Any,
    ) -> None:
        """Record one observation of ``value`` for ``name``.

        The aggregate (count / sum / min / max) is exposed by
        :meth:`render_prometheus` as a Prometheus ``summary`` family.
        """
        key = _label_items(_merge_labels(labels, label_kwargs))
        with self._lock:
            self._declare(name, "summary")
            series = self._observations.setdefault(name, {})
            hist = series.get(key)
            if hist is None:
                hist = _Histogram()
                series[key] = hist
            hist.count += 1
            hist.sum += value
            if hist.min is None or value < hist.min:
                hist.min = value
            if hist.max is None or value > hist.max:
                hist.max = value

    # --------------------------------------------------------------- rendering

    def render_prometheus(self) -> str:
        """Render all metrics in the Prometheus text exposition format.

        The output is deterministic: metric names are sorted alphabetically, and
        label sets are sorted by (key, value). Each family is preceded by its
        ``# TYPE <name> <counter|gauge|summary>`` line. The result is newline
        terminated.
        """
        with self._lock:
            kinds = dict(self._kinds)
            counters = {name: dict(series) for name, series in self._counters.items()}
            gauges = {name: dict(series) for name, series in self._gauges.items()}
            observations = {
                name: {key: hist for key, hist in series.items()}
                for name, series in self._observations.items()
            }

        lines: list[str] = []
        for name in sorted(kinds):
            kind = kinds[name]
            if kind == "counter":
                lines.append(f"# TYPE {name} counter")
                for key, value in sorted(counters.get(name, {}).items()):
                    lines.append(f"{name}{_format_labels(key)} {_format_number(value)}")
            elif kind == "gauge":
                lines.append(f"# TYPE {name} gauge")
                for key, value in sorted(gauges.get(name, {}).items()):
                    lines.append(f"{name}{_format_labels(key)} {_format_number(value)}")
            elif kind == "summary":
                lines.append(f"# TYPE {name} summary")
                for key in sorted(observations.get(name, {})):
                    hist = observations[name][key]
                    suffix = _format_labels(key)
                    lines.append(f"{name}_count{suffix} {_format_number(hist.count)}")
                    lines.append(f"{name}_sum{suffix} {_format_number(hist.sum)}")
                    lines.append(f"{name}_min{suffix} {_format_number(hist.min or 0)}")
                    lines.append(f"{name}_max{suffix} {_format_number(hist.max or 0)}")
        return "".join(line + "\n" for line in lines)

    # ------------------------------------------------------------ tokens / cost

    def add_tokens(
        self,
        tenant: str,
        prompt: int,
        completion: int,
        *,
        model: str | None = None,
    ) -> None:
        """Add prompt/completion token counts for ``tenant`` and ``model``.

        ``model=None`` is recorded under the :data:`DEFAULT_MODEL` label. Counts
        accumulate across calls.
        """
        model_key = model or DEFAULT_MODEL
        with self._lock:
            tenant_map = self._tokens.setdefault(tenant, {})
            entry = tenant_map.setdefault(model_key, [0, 0])
            entry[0] += int(prompt)
            entry[1] += int(completion)

    def cost_summary(self) -> dict[str, dict[str, dict[str, Any]]]:
        """Return accumulated tokens and cost per tenant and model.

        Shape: ``{tenant: {model: {"prompt", "completion", "total",
        "cost_usd"}}}``. Cost uses :data:`PRICES` (USD per 1000 tokens); models
        not listed there cost ``0.0``. Keys are sorted for deterministic output.
        """
        with self._lock:
            snapshot = {
                tenant: {model: list(counts) for model, counts in models.items()}
                for tenant, models in self._tokens.items()
            }

        summary: dict[str, dict[str, dict[str, Any]]] = {}
        for tenant in sorted(snapshot):
            summary[tenant] = {}
            for model in sorted(snapshot[tenant]):
                prompt, completion = snapshot[tenant][model]
                price_in, price_out = PRICES.get(model, (0.0, 0.0))
                cost = prompt / 1000.0 * price_in + completion / 1000.0 * price_out
                summary[tenant][model] = {
                    "prompt": prompt,
                    "completion": completion,
                    "total": prompt + completion,
                    "cost_usd": round(cost, 6),
                }
        return summary


_DEFAULT_METRICS = Metrics()


def default_metrics() -> Metrics:
    """Return the process-wide default :class:`Metrics` registry."""
    return _DEFAULT_METRICS


def metrics_response(metrics: Metrics | None = None) -> tuple[str, str]:
    """Return ``(body, content_type)`` for a ``/metrics`` endpoint.

    Defaults to the process-wide registry (:func:`default_metrics`); pass an
    explicit :class:`Metrics` instance when serving a different registry.
    """
    registry = metrics if metrics is not None else _DEFAULT_METRICS
    return registry.render_prometheus(), PROMETHEUS_CONTENT_TYPE


# --------------------------------------------------------------------------- #
# Health
# --------------------------------------------------------------------------- #


def _db_timeout() -> float:
    raw = os.environ.get("STRATUM_HEALTH_DB_TIMEOUT")
    if raw is None:
        return DEFAULT_DB_TIMEOUT
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_DB_TIMEOUT


def _check_db(timeout: float) -> tuple[bool, str | None]:
    """Run ``SELECT 1`` through :mod:`stratum.db`; never raises.

    Uses the module pool directly so the wait is bounded by ``timeout`` (the
    default :func:`stratum.db.connection` wait is 30 s, far too long for a probe).
    """
    try:
        from . import db

        pool = db.get_pool()
        with pool.connection(timeout=timeout) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        return True, None
    except Exception as exc:  # noqa: BLE001 - a probe must never raise
        return False, f"{type(exc).__name__}: {exc}"


def _check_queued_jobs() -> tuple[int | None, str | None]:
    """Count queued rows in ``jobs``; never raises.

    ``jobs`` is under the tenant RLS policy, so a plain ``fetch_all`` with no
    tenant context would see zero rows (or fail) regardless of the real queue
    depth. The probe is a privileged, cross-tenant read, so it opens a
    ``bypass_rls`` connection (B12).
    """
    try:
        from . import db

        with db.connection(bypass_rls=True) as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM jobs WHERE status = 'queued'")
            row = cur.fetchone()
        if not row:
            return 0, None
        return int(row["n"]), None
    except Exception as exc:  # noqa: BLE001 - a probe must never raise
        return None, f"{type(exc).__name__}: {exc}"


def _check_git(kb_path: str) -> bool:
    """Return whether ``kb_path`` is (the root of) a git repository."""
    try:
        path = Path(kb_path)
        git_marker = path / ".git"
        return git_marker.is_dir() or git_marker.is_file()
    except Exception:  # noqa: BLE001 - a probe must never raise
        return False


def health() -> dict[str, Any]:
    """Readiness probe covering DB, Git, LLM and the queue; never raises.

    Returns a dict with ``status`` (``"ok"`` when the database is reachable,
    otherwise ``"degraded"``), the package ``version``, and sub-results ``db``,
    ``jobs``, ``git`` and ``llm``. When the DB is unreachable the jobs count is
    skipped (reported as ``None``) rather than hanging on a second connection
    attempt. Callable without a reachable database.
    """
    from . import __version__
    from .config import get_settings

    settings = get_settings()
    db_ok, db_error = _check_db(_db_timeout())

    if db_ok:
        queued, jobs_error = _check_queued_jobs()
    else:
        queued, jobs_error = None, "skipped: database unreachable"

    llm_ok = bool(str(getattr(settings, "llm_base_url", "") or "").strip())
    git_ok = _check_git(settings.kb_path)

    return {
        "status": "ok" if db_ok else "degraded",
        "version": __version__,
        "db": {"ok": db_ok, "error": db_error},
        "jobs": {"queued": queued, "error": jobs_error},
        "git": {"ok": git_ok, "path": settings.kb_path},
        "llm": {"ok": llm_ok, "base_url": settings.llm_base_url},
    }
