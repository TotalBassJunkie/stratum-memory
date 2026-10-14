"""Job-queue runner on the ``jobs`` table (WP-05b).

The worker drives the three batch lanes of ``MASTER-PLAN.md`` (ingestion,
curator, reflect) through a single Postgres-backed queue. Instead of Redis or
RabbitMQ the queue is the ``jobs`` table (ADR-0002) and jobs are claimed with
``SELECT … FOR UPDATE SKIP LOCKED`` — so any number of parallel workers can run
without ever double-claiming a row.

Design rules:

* **No double claim.** :meth:`JobQueue.claim` performs the select-and-lock in a
  *single* ``UPDATE`` whose subquery ends in ``FOR UPDATE SKIP LOCKED``. Two
  workers racing for the same row: one updates it, the other skips to the next
  eligible row (or gets ``None``). The lock also holds if the worker crashes
  mid-job, until :meth:`JobQueue.recover_stale` requeues it.
* **Retry with backoff, bounded attempts.** :meth:`JobQueue.fail` requeues a
  failed job with ``run_after = now() + 2**attempts`` seconds until
  ``attempts >= max_attempts``, then marks it ``failed`` and records
  ``last_error``.
* **RLS-scoped connections.** Every statement runs through
  :func:`stratum.db.connection` with a tenant, so the non-privileged
  ``stratum_app`` role and its RLS context are used. The ``jobs`` table itself
  carries a ``tenant_id`` (schema WP-02) and is part of the tenant RLS policy
  set, so every insert writes ``tenant_id`` explicitly (a ``default`` column
  default would collide with the ``WITH CHECK`` policy for other tenants).
  System jobs use the ``default`` tenant.

The module is import-safe without a live database; tests inject a fake
``conn_factory`` (see :class:`JobQueue`).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import threading
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from psycopg.types.json import Jsonb

from . import db
from .config import Settings, get_settings

__all__ = [
    "BACKOFF_CAP_SECONDS",
    "DEFAULT_POLL_INTERVAL",
    "DEFAULT_SCHEDULER_INTERVAL",
    "STALE_TIMEOUT_SECONDS",
    "JobQueue",
    "backoff_seconds",
    "default_handlers",
    "main",
    "run_forever",
]

logger = logging.getLogger("stratum.worker")

#: Upper bound for the exponential retry backoff (seconds).
BACKOFF_CAP_SECONDS = 3600

#: Default sleep between empty claim polls (seconds).
DEFAULT_POLL_INTERVAL = 1.0

#: Default seconds between scheduler ticks (B2's poll-based cron seam).
DEFAULT_SCHEDULER_INTERVAL = 60.0

#: Default age after which a ``running`` job without a heartbeat is considered
#: stale and requeued (seconds).
STALE_TIMEOUT_SECONDS = 300

JobHandler = Callable[[dict[str, Any]], None]


def _row_get(row: Any, key: str, index: int) -> Any:
    """Read a column from a row that may be a mapping (``dict_row``) or sequence."""
    if isinstance(row, Mapping):
        return row[key]
    return row[index]


def _job_tenant(job: Any, fallback: str) -> str:
    """Return a job row's ``tenant_id`` (B6), falling back when absent/blank."""
    if isinstance(job, Mapping):
        value = job.get("tenant_id")
    else:
        value = None
    text = str(value or "").strip()
    return text or fallback


def _coerce_run_after(value: str | datetime | None) -> datetime | None:
    """Normalise ``value`` to a timezone-aware UTC datetime (``None`` stays ``None``)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        text = value[:-1] + "+00:00" if value.endswith("Z") else value
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    raise TypeError(f"run_after must be str/datetime/None, got {type(value).__name__}")


def backoff_seconds(attempts: int) -> int:
    """Return the retry delay in seconds for an already-attempted job.

    Exponential (``2 ** attempts``) and capped at :data:`BACKOFF_CAP_SECONDS`.
    ``attempts`` is the value stored after the claim incremented it, so the
    first retry waits 2 s, the second 4 s, and so on.
    """
    return int(min(2 ** max(1, int(attempts)), BACKOFF_CAP_SECONDS))


class JobQueue:
    """Postgres-backed job queue using ``SELECT … FOR UPDATE SKIP LOCKED``.

    Args:
        settings: Configuration source; defaults to :func:`get_settings`.
        conn_factory: Optional ``(tenant) -> contextmanager[Connection]`` used to
            obtain a connection. Defaults to :func:`stratum.db.connection`.
            Injecting a fake factory is how the unit tests record the emitted
            SQL without a live Postgres.
        default_tenant: Tenant used by the tenant-less operations
            (``complete``, ``fail``, ``recover_stale``) and by
            :func:`run_forever`; system jobs use ``"default"``.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        conn_factory: Callable[[str], Any] | None = None,
        default_tenant: str = "default",
    ) -> None:
        self.settings = settings or get_settings()
        self.default_tenant = default_tenant
        self._connect: Callable[[str], Any] = conn_factory or db.connection

    # ------------------------------------------------------------------ writes

    def enqueue(
        self,
        tenant: str,
        kind: str,
        payload: Mapping[str, Any] | None = None,
        *,
        run_after: str | datetime | None = None,
        max_attempts: int = 3,
    ) -> str:
        """Insert a ``queued`` job and return its ``job_id``.

        Args:
            tenant: Tenant id (RLS session scope).
            kind: Job kind, matched against the handler registry at run time.
            payload: JSON-serialisable job arguments (defaults to ``{}``).
            run_after: Earliest run time (defaults to ``now()``), so a job can be
                scheduled into the future.
            max_attempts: Attempts before the job is marked ``failed``.
        """
        # ``tenant_id`` must be written explicitly (B4): the column default is
        # ``'default'`` and the RLS ``WITH CHECK`` policy compares it against
        # ``current_setting('stratum.tenant')``. Omitting the column makes the
        # insert collide with the policy for every tenant other than ``default``.
        sql = (
            "INSERT INTO jobs (tenant_id, kind, payload, status, max_attempts, run_after) "
            "VALUES (%s, %s, %s, 'queued', %s, COALESCE(%s, now())) "
            "RETURNING job_id"
        )
        params = (
            tenant,
            kind,
            Jsonb(dict(payload or {})),
            int(max_attempts),
            _coerce_run_after(run_after),
        )
        with self._connect(tenant) as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            conn.commit()
        return str(_row_get(row, "job_id", 0))

    # ------------------------------------------------------------------- claim

    def claim(
        self,
        tenant: str,
        worker_id: str,
        *,
        kinds: list[str] | tuple[str, ...] | None = None,
    ) -> dict[str, Any] | None:
        """Atomically claim the next due job, or ``None`` if none is ready.

        The select-and-lock is a single ``UPDATE``: the subquery picks the
        oldest due ``queued`` row (optionally filtered to ``kinds``) with
        ``ORDER BY run_after LIMIT 1 FOR UPDATE SKIP LOCKED``. ``SKIP LOCKED``
        makes a concurrent worker skip a row another transaction already holds,
        so parallel workers never claim the same job.

        The claim increments ``attempts`` and stamps ``locked_by`` /
        ``locked_at``; the row is returned (``RETURNING *``) as a dict.
        """
        sql = (
            "UPDATE jobs "
            "SET status = 'running', locked_by = %s, locked_at = now(), "
            "    attempts = attempts + 1 "
            "WHERE job_id = ("
            "  SELECT job_id FROM jobs "
            "  WHERE status = 'queued' AND run_after <= now()"
        )
        params: list[Any] = [worker_id]
        if kinds:
            sql += " AND kind = ANY(%s)"
            params.append(list(kinds))
        sql += " ORDER BY run_after LIMIT 1 FOR UPDATE SKIP LOCKED) RETURNING *"

        with self._connect(tenant) as conn, conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            row = cur.fetchone()
            conn.commit()
        return None if row is None else dict(row)

    # -------------------------------------------------------------- completion

    def complete(self, job_id: str, *, tenant: str | None = None) -> None:
        """Mark a job ``done`` and release its claim.

        ``tenant`` (B6) settles the row under the job's own RLS scope; when
        omitted the queue's ``default_tenant`` is used (system jobs).
        """
        sql = (
            "UPDATE jobs SET status = 'done', locked_by = NULL, locked_at = NULL "
            "WHERE job_id = %s"
        )
        with self._connect(tenant or self.default_tenant) as conn, conn.cursor() as cur:
            cur.execute(sql, (job_id,))
            conn.commit()

    def fail(self, job_id: str, error: str, *, tenant: str | None = None) -> None:
        """Record a failure: requeue with backoff, or exhaust to ``failed``.

        If ``attempts < max_attempts`` the job returns to ``queued`` with
        ``run_after = now() + backoff_seconds(attempts)``; otherwise it is
        marked ``failed``. ``last_error`` is always set and the claim released.
        ``tenant`` (B6) settles the row under the job's own RLS scope.
        """
        with self._connect(tenant or self.default_tenant) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT attempts, max_attempts FROM jobs WHERE job_id = %s", (job_id,)
            )
            row = cur.fetchone()
            if row is None:
                conn.commit()
                return
            attempts = int(_row_get(row, "attempts", 0))
            max_attempts = int(_row_get(row, "max_attempts", 1))
            if attempts < max_attempts:
                cur.execute(
                    "UPDATE jobs SET status = 'queued', "
                    "run_after = now() + make_interval(secs => %s), "
                    "last_error = %s, locked_by = NULL, locked_at = NULL "
                    "WHERE job_id = %s",
                    (backoff_seconds(attempts), error, job_id),
                )
            else:
                cur.execute(
                    "UPDATE jobs SET status = 'failed', last_error = %s, "
                    "locked_by = NULL, locked_at = NULL WHERE job_id = %s",
                    (error, job_id),
                )
            conn.commit()

    # --------------------------------------------------------------- recovery

    def recover_stale(self, timeout_seconds: float = STALE_TIMEOUT_SECONDS) -> int:
        """Requeue ``running`` jobs whose claim is older than ``timeout_seconds``.

        Crash recovery: a worker that died mid-job leaves a ``running`` row with
        a stale ``locked_at`` (no heartbeat). Such rows are returned to
        ``queued`` so another worker picks them up. Returns the number of rows
        requeued.
        """
        sql = (
            "UPDATE jobs SET status = 'queued', locked_by = NULL, locked_at = NULL "
            "WHERE status = 'running' "
            "AND locked_at < now() - make_interval(secs => %s)"
        )
        with self._connect(self.default_tenant) as conn, conn.cursor() as cur:
            cur.execute(sql, (int(timeout_seconds),))
            count = cur.rowcount
            conn.commit()
        return int(count or 0)

    # ------------------------------------------------------------------ stats

    def stats(self, tenant: str) -> dict[str, int]:
        """Return the job count per status for the given tenant scope."""
        sql = "SELECT status, count(*) AS n FROM jobs GROUP BY status"
        with self._connect(tenant) as conn, conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()
        return {
            str(_row_get(row, "status", 0)): int(_row_get(row, "n", 1)) for row in rows
        }


def _dispatch(
    queue: JobQueue,
    handlers: Mapping[str, JobHandler],
    job: dict[str, Any],
    *,
    tenant: str | None = None,
) -> None:
    """Run one job's handler and settle the row as ``done`` or failed.

    A missing handler or an exception in the handler routes to
    :meth:`JobQueue.fail` (requeue-with-backoff or ``failed``); success routes to
    :meth:`JobQueue.complete`. ``tenant`` is the job's own RLS scope (B6) used to
    settle the row; it defaults to the queue's ``default_tenant``.
    """
    job_id = str(_row_get(job, "job_id", 0))
    kind = str(_row_get(job, "kind", 1))
    job_tenant = tenant or _job_tenant(job, queue.default_tenant)
    handler = handlers.get(kind)
    try:
        if handler is None:
            raise LookupError(f"no handler registered for kind {kind!r}")
        handler(job)
    except Exception as exc:  # noqa: BLE001 - any handler failure must settle the job
        logger.exception("job %s (kind=%s) failed", job_id, kind)
        queue.fail(job_id, f"{type(exc).__name__}: {exc}", tenant=job_tenant)
    else:
        queue.complete(job_id, tenant=job_tenant)


def run_forever(
    queue: JobQueue,
    handlers: Mapping[str, JobHandler],
    *,
    worker_id: str,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    stop_event: threading.Event | None = None,
    scheduler_tick: Callable[[str], None] | None = None,
    scheduler_interval: float = DEFAULT_SCHEDULER_INTERVAL,
    tenants: list[str] | None = None,
) -> None:
    """Poll the queue until ``stop_event`` is set.

    Each iteration claims one job (scanning ``tenants``, default
    ``[queue.default_tenant]``), runs its handler and settles it
    (``complete`` / ``fail``). When no job is ready the loop waits up to
    ``poll_interval`` seconds — ``stop_event.wait`` wakes immediately on
    shutdown, so the worker exits cleanly without finishing the sleep.

    The job's own ``tenant_id`` is used for the handler work and for settling the
    row (B6): a reflect job enqueued for tenant ``acme`` must run and be completed
    under ``acme``, not the queue's ``default``. ``complete``/``fail`` therefore
    receive that tenant explicitly, and ``tenants`` lets the loop *see* those
    non-default rows at all (the claim is RLS-scoped, so a tenant the worker never
    scans is invisible).

    ``scheduler_tick`` (B2) is an optional poll-based cron seam: when given it is
    invoked once per tenant in ``tenants`` at most once every
    ``scheduler_interval`` seconds (the first time immediately). This is what
    fires the reflect 03:00 schedule without an external crontab — see
    :meth:`stratum.reflect.ReflectWorker.run_scheduler_tick`. A tick that raises
    is logged and swallowed; the job loop keeps running.
    """
    stop = stop_event if stop_event is not None else threading.Event()
    tenant_list = list(tenants) if tenants else [queue.default_tenant]
    next_tick = 0.0
    while not stop.is_set():
        if scheduler_tick is not None:
            now = time.monotonic()
            if now >= next_tick:
                next_tick = now + max(0.0, scheduler_interval)
                for tick_tenant in tenant_list:
                    try:
                        scheduler_tick(tick_tenant)
                    except Exception:  # noqa: BLE001 - a failed tick must not kill the loop
                        logger.exception("scheduler tick failed for tenant %s", tick_tenant)
        job = None
        for tenant in tenant_list:
            job = queue.claim(tenant, worker_id)
            if job is not None:
                break
        if job is None:
            stop.wait(poll_interval)
            continue
        _dispatch(queue, handlers, job, tenant=_job_tenant(job, queue.default_tenant))


# --------------------------------------------------------------------- runner


def _placeholder(name: str) -> JobHandler:
    """Build a no-op handler standing in for a not-yet-wired lane."""

    def handler(job: dict[str, Any]) -> None:
        logger.warning(
            "placeholder handler %r has no implementation yet (job %s)",
            name,
            _row_get(job, "job_id", 0),
        )

    return handler


def _reflect_handler(
    settings: Settings | None = None,
    *,
    reflect_kwargs: Mapping[str, Any] | None = None,
) -> JobHandler:
    """Build a ``reflect`` handler that drives :meth:`ReflectWorker.run` (B2).

    The import is **deferred and optional**: ``worker`` must stay usable (and
    importable) without an LLM wired, so a missing :mod:`stratum.reflect`
    dependency degrades to :func:`_placeholder` rather than failing at import
    time. ``reflect_kwargs`` supplies the injected ``llm``/``store``/``graph``/
    ``curator`` (and any ``conn_factory``/``clock``); without an ``llm`` the
    worker cannot run, so the placeholder is kept.
    """
    try:
        from .reflect import ReflectWorker
    except Exception:  # noqa: BLE001 - reflect is optional at import time
        logger.exception("reflect handler unavailable; falling back to placeholder")
        return _placeholder("reflect")

    kwargs = dict(reflect_kwargs or {})
    if "llm" not in kwargs:
        logger.warning(
            "no llm injected for the reflect handler; using a placeholder. "
            "Pass reflect_kwargs={'llm': ...} to default_handlers()."
        )
        return _placeholder("reflect")

    worker = ReflectWorker(settings, **kwargs)

    def handler(job: dict[str, Any]) -> None:
        job_tenant = str(_row_get(job, "tenant_id", -1) or "default")
        scope = (job.get("payload") or {}).get("scope") if isinstance(job, Mapping) else None
        worker.run(job_tenant, scope=scope)

    return handler


def default_handlers(
    settings: Settings | None = None,
    *,
    reflect_kwargs: Mapping[str, Any] | None = None,
) -> dict[str, JobHandler]:
    """Handler registry for :func:`run_forever`.

    The kinds line up with the batch lanes from ``MASTER-PLAN.md``. The
    ``reflect`` lane (WP-09) is wired to :meth:`stratum.reflect.ReflectWorker.run`
    via an injected factory (:func:`_reflect_handler`), so ``worker`` never
    hard-depends on ``reflect``. ``ingest`` (WP-07) and ``curate`` (WP-08) remain
    placeholders until their own entry points land.

    Args:
        settings: Config source forwarded to the reflect worker.
        reflect_kwargs: Injected dependencies for :class:`ReflectWorker`
            (``llm``, ``store``, ``graph``, ``curator``, …). Pass an ``llm`` to
            actually run reflect; otherwise its handler stays a placeholder.
    """
    handlers: dict[str, JobHandler] = {name: _placeholder(name) for name in ("ingest", "curate")}
    handlers["reflect"] = _reflect_handler(settings, reflect_kwargs=reflect_kwargs)
    return handlers


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for ``python -m stratum.worker``.

    Options:
        ``--enqueue KIND`` enqueue one job (with ``--payload`` JSON) and exit;
        ``--drain`` process all currently ready jobs once, then exit;
        otherwise run the polling loop until interrupted (Ctrl-C).
    """
    parser = argparse.ArgumentParser(
        prog="stratum.worker",
        description="Stratum job-queue runner (SELECT ... FOR UPDATE SKIP LOCKED).",
    )
    parser.add_argument("--worker-id", default=f"worker-{os.getpid()}")
    parser.add_argument("--poll-interval", type=float, default=DEFAULT_POLL_INTERVAL)
    parser.add_argument("--stale-timeout", type=float, default=STALE_TIMEOUT_SECONDS)
    parser.add_argument(
        "--drain", action="store_true", help="process ready jobs once, then exit"
    )
    parser.add_argument("--enqueue", metavar="KIND", help="enqueue a job of KIND and exit")
    parser.add_argument("--payload", default="{}", help="JSON payload for --enqueue")
    args = parser.parse_args(argv)

    # Structured JSON logs on the process root logger (B20b): one JSON object
    # per record, shared with the MCP server and the observability module.
    from .observability import setup_logging  # noqa: PLC0415 - lazy import

    setup_logging("INFO")

    queue = JobQueue(default_tenant="default")

    if args.enqueue:
        payload = json.loads(args.payload)
        job_id = queue.enqueue("default", args.enqueue, payload)
        print(job_id)
        return 0

    queue.recover_stale(args.stale_timeout)
    handlers, scheduler_tick = _build_runtime_handlers()

    if args.drain:
        processed = 0
        while True:
            job = queue.claim("default", args.worker_id)
            if job is None:
                break
            _dispatch(queue, handlers, job, tenant=_job_tenant(job, "default"))
            processed += 1
        logger.info("drained %d job(s)", processed)
        return 0

    stop = threading.Event()
    try:
        run_forever(
            queue,
            handlers,
            worker_id=args.worker_id,
            poll_interval=args.poll_interval,
            stop_event=stop,
            scheduler_tick=scheduler_tick,
            tenants=_worker_tenants(),
        )
    except KeyboardInterrupt:
        stop.set()
        logger.info("worker %s stopped", args.worker_id)
    return 0


def _worker_tenants() -> list[str]:
    """Return the tenants this worker polls (B6).

    The claim is RLS-scoped, so a worker only ever sees jobs of the tenants it
    scans. ``STRATUM_WORKER_TENANTS`` (comma-separated) lists them; the default
    is just ``["default"]`` for the single-tenant Pi profile. A multi-tenant
    server lists every tenant whose reflect jobs it must run.
    """
    raw = os.environ.get("STRATUM_WORKER_TENANTS", "").strip()
    if not raw:
        return ["default"]
    tenants = [part.strip() for part in raw.split(",") if part.strip()]
    return tenants or ["default"]


def _build_runtime_handlers() -> tuple[dict[str, JobHandler], Callable[[str], None] | None]:
    """Build the real handler registry plus the reflect scheduler tick (B6).

    Wires the concrete collaborators — :class:`~stratum.llm.LLMClient`,
    :class:`~stratum.vector.MemoryStore`, :class:`~stratum.graph.TemporalGraph`,
    :class:`~stratum.curator.Curator` — into ``default_handlers`` as
    ``reflect_kwargs``, so a ``reflect`` job actually runs instead of hitting the
    placeholder. Returns the handler map and a bound
    :meth:`ReflectWorker.run_scheduler_tick` (``None`` when reflect is not
    importable, in which case the loop simply runs without the cron seam).
    """
    from .curator import Curator
    from .graph import TemporalGraph
    from .llm import LLMClient
    from .vector import MemoryStore

    settings = get_settings()
    try:
        from .reflect import ReflectWorker
    except Exception:  # noqa: BLE001 - reflect stays optional
        logger.exception("reflect unavailable; running without the scheduler tick")
        return default_handlers(settings), None

    llm = LLMClient(settings)
    store = MemoryStore(settings)
    graph = TemporalGraph(settings)
    curator = Curator(settings, store=store, graph=graph)
    reflect_worker = ReflectWorker(
        settings, llm=llm, store=store, graph=graph, curator=curator
    )
    handlers = default_handlers(
        settings,
        reflect_kwargs={
            "llm": llm,
            "store": store,
            "graph": graph,
            "curator": curator,
        },
    )
    return handlers, reflect_worker.run_scheduler_tick


if __name__ == "__main__":
    raise SystemExit(main())
