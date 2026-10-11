"""Tests for the job-queue runner (:mod:`stratum.worker`, WP-05b).

The fast tests are database-free: a *recording fake connection* captures every
SQL statement and, on top of a shared in-memory ``jobs`` list, simulates the row
transitions the real statements would perform. That lets us assert:

* ``enqueue`` inserts a ``queued`` row,
* ``claim`` emits the ``FOR UPDATE SKIP LOCKED`` subquery and only claims
  ``queued`` rows that are already due,
* **parallel workers never double-claim** the same job,
* ``fail`` requeues with a future ``run_after`` while attempts remain and marks
  the job ``failed`` once ``attempts == max_attempts``,
* ``recover_stale`` requeues a stale ``running`` row,
* ``run_forever`` processes queued jobs and exits on its ``stop_event``.

Real-database variants are marked ``integration`` and skipped unless the
``STRATUM_TEST_DB`` environment variable holds a Postgres DSN (see test_graph.py).
"""

from __future__ import annotations

import os
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from stratum import reflect
from stratum.worker import JobQueue, backoff_seconds, default_handlers, run_forever

# --------------------------------------------------------------------------- #
# Recording fake connection simulating the ``jobs`` table
# --------------------------------------------------------------------------- #


def _norm(sql: str) -> str:
    """Collapse all whitespace so statement classification is stable."""
    return re.sub(r"\s+", " ", sql).strip()


def _unwrap(value: Any) -> Any:
    """Unwrap a psycopg ``Jsonb`` wrapper to its Python object (identity otherwise)."""
    return getattr(value, "obj", value)


class FakeJobDB:
    """Shared in-memory ``jobs`` store used by one or more fake connections.

    The store is protected by a lock so that the parallel-claim test exercises
    the same "one row, one winner" guarantee that ``FOR UPDATE SKIP LOCKED``
    gives in Postgres: a claim atomically moves the oldest due ``queued`` row to
    ``running``, so a second concurrent claim gets the *next* row (or ``None``).
    """

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.statements: list[tuple[str, tuple]] = []
        self.lock = threading.Lock()
        # Origin of the fake clock. Ephemeral reads (claim/fail/complete/stats)
        # use wall-clock time so a backoff-requeued job really becomes due; a
        # test may pin ``now`` to freeze scheduling assertions exactly.
        self.origin = datetime.now(timezone.utc)
        self._mono0 = time.monotonic()
        self.now: datetime | None = None

    @property
    def _clock(self) -> datetime:
        """The current fake time: the pinned ``now`` if set, else wall clock."""
        if self.now is not None:
            return self.now
        return self.origin + timedelta(seconds=time.monotonic() - self._mono0)

    # -- introspection helpers used by tests -------------------------------

    def get(self, job_id: str) -> dict[str, Any]:
        for row in self.rows:
            if row["job_id"] == job_id:
                return row
        raise KeyError(job_id)

    def add_running(self, *, kind: str, locked_at: datetime, attempts: int = 1) -> str:
        """Insert a ``running`` row directly (simulates a crashed worker)."""
        job_id = str(uuid.uuid4())
        self.rows.append(
            {
                "job_id": job_id,
                "kind": kind,
                "payload": {},
                "status": "running",
                "attempts": attempts,
                "max_attempts": 3,
                "run_after": self._clock,
                "locked_by": "dead-worker",
                "locked_at": locked_at,
                "last_error": None,
                "created_at": self._clock,
            }
        )
        return job_id

    # -- connection plumbing ------------------------------------------------

    def factory(self, _tenant: str) -> "FakeConnection":
        return FakeConnection(self)


class FakeCursor:
    def __init__(self, conn: "FakeConnection") -> None:
        self._conn = conn
        self.db = conn.db
        self._rows: list[dict[str, Any]] = []
        self.rowcount: int | None = None

    # psycopg cursor protocol
    def execute(self, sql: str, params: tuple | dict = ()) -> None:
        self._conn.db.statements.append((_norm(sql), params))
        self._rows = []
        self.rowcount = None
        norm = _norm(sql).upper()
        args = tuple(params) if not isinstance(params, dict) else ()

        if norm.startswith("INSERT INTO JOBS"):
            self._insert(args)
        elif norm.startswith("SELECT ATTEMPTS, MAX_ATTEMPTS FROM JOBS"):
            self._select_attempts(args)
        elif norm.startswith("UPDATE JOBS SET STATUS = 'RUNNING'"):
            self._claim(norm, args)
        elif "SET STATUS = 'DONE'" in norm:
            self._set_status(args[0], "done")
        elif "SET STATUS = 'QUEUED'" in norm and "RUN_AFTER" in norm:
            self._requeue_with_backoff(args)
        elif "SET STATUS = 'FAILED'" in norm:
            self._set_status(args[1], "failed", last_error=args[0])
        elif "SET STATUS = 'QUEUED'" in norm and "STATUS = 'RUNNING'" in norm:
            self._recover_stale(args)
        elif norm.startswith("SELECT STATUS, COUNT"):
            self._stats()
        else:  # pragma: no cover - guards against unrecognised SQL in tests
            raise AssertionError(f"unhandled SQL in fake: {sql!r}")

    def fetchone(self) -> dict[str, Any] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[dict[str, Any]]:
        return list(self._rows)

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    # -- statement implementations -----------------------------------------

    def _insert(self, args: tuple) -> None:
        tenant, kind, payload, max_attempts, run_after = args
        job_id = str(uuid.uuid4())
        self.db.rows.append(
            {
                "job_id": job_id,
                "tenant_id": tenant,
                "kind": kind,
                "payload": _unwrap(payload),
                "status": "queued",
                "attempts": 0,
                "max_attempts": int(max_attempts),
                "run_after": run_after if run_after is not None else self.db._clock,
                "locked_by": None,
                "locked_at": None,
                "last_error": None,
                "created_at": self.db._clock,
            }
        )
        self._rows = [{"job_id": job_id}]

    def _select_attempts(self, args: tuple) -> None:
        try:
            row = self.db.get(args[0])
        except KeyError:
            self._rows = []
            return
        self._rows = [
            {"attempts": row["attempts"], "max_attempts": row["max_attempts"]}
        ]

    def _claim(self, norm: str, args: tuple) -> None:
        worker_id = args[0]
        kinds = list(args[1]) if "KIND = ANY" in norm and len(args) > 1 else None
        with self.db.lock:
            candidates = [
                row
                for row in self.db.rows
                if row["status"] == "queued"
                and row["run_after"] <= self.db._clock
                and (kinds is None or row["kind"] in kinds)
            ]
            if not candidates:
                self.rowcount = 0
                return
            row = min(candidates, key=lambda r: r["run_after"])
            row["status"] = "running"
            row["locked_by"] = worker_id
            row["locked_at"] = self.db._clock
            row["attempts"] += 1
            self.rowcount = 1
            self._rows = [dict(row)]

    def _set_status(self, job_id: str, status: str, *, last_error: str | None = None) -> None:
        row = self.db.get(job_id)
        row["status"] = status
        row["locked_by"] = None
        row["locked_at"] = None
        if last_error is not None:
            row["last_error"] = last_error
        self.rowcount = 1

    def _requeue_with_backoff(self, args: tuple) -> None:
        secs, error, job_id = args
        row = self.db.get(job_id)
        row["status"] = "queued"
        row["run_after"] = self.db._clock + timedelta(seconds=float(secs))
        row["last_error"] = error
        row["locked_by"] = None
        row["locked_at"] = None
        self.rowcount = 1

    def _recover_stale(self, args: tuple) -> None:
        (secs,) = args
        cutoff = self.db._clock - timedelta(seconds=float(secs))
        count = 0
        with self.db.lock:
            for row in self.db.rows:
                if row["status"] == "running" and row["locked_at"] is not None and row["locked_at"] < cutoff:
                    row["status"] = "queued"
                    row["locked_by"] = None
                    row["locked_at"] = None
                    count += 1
        self.rowcount = count

    def _stats(self) -> None:
        counts: dict[str, int] = {}
        for row in self.db.rows:
            counts[row["status"]] = counts.get(row["status"], 0) + 1
        self._rows = [{"status": s, "n": n} for s, n in sorted(counts.items())]


class FakeConnection:
    """Minimal psycopg connection stand-in backed by a :class:`FakeJobDB`."""

    def __init__(self, db: FakeJobDB) -> None:
        self.db = db
        self.commits = 0

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def __enter__(self) -> "FakeConnection":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def _queue(db: FakeJobDB) -> JobQueue:
    """Build a JobQueue wired to the shared fake store."""
    return JobQueue(conn_factory=db.factory)


# --------------------------------------------------------------------------- #
# enqueue
# --------------------------------------------------------------------------- #


def test_enqueue_inserts_queued_job():
    db = FakeJobDB()
    queue = _queue(db)

    job_id = queue.enqueue("default", "ingest", {"uri": "stratum://docs/default/a"})

    assert db.get(job_id)["status"] == "queued"
    sql, params = db.statements[0]
    assert sql.upper().startswith("INSERT INTO JOBS")
    assert "TENANT_ID" in sql.upper()
    assert "'QUEUED'" in sql.upper()
    assert params[0] == "default"
    assert params[1] == "ingest"
    assert _unwrap(params[2]) == {"uri": "stratum://docs/default/a"}


def test_enqueue_writes_tenant_id_explicitly():
    """B4: ``tenant_id`` is in the column list and params, not left to the default."""
    db = FakeJobDB()
    queue = _queue(db)

    job_id = queue.enqueue("acme", "ingest")

    sql, params = db.statements[0]
    assert "TENANT_ID" in sql.upper()
    assert params[0] == "acme"
    assert db.get(job_id)["tenant_id"] == "acme"


def test_enqueue_defaults_max_attempts_and_payload():
    db = FakeJobDB()
    queue = _queue(db)

    job_id = queue.enqueue("default", "reflect")

    row = db.get(job_id)
    assert row["max_attempts"] == 3
    assert row["payload"] == {}
    assert row["attempts"] == 0


# --------------------------------------------------------------------------- #
# claim
# --------------------------------------------------------------------------- #


def test_claim_uses_skip_locked_subquery():
    db = FakeJobDB()
    queue = _queue(db)
    queue.enqueue("default", "ingest")

    job = queue.claim("default", "w1")

    assert job is not None
    claim_sql = next(
        sql for sql, _ in db.statements if sql.upper().startswith("UPDATE JOBS SET STATUS = 'RUNNING'")
    )
    upper = claim_sql.upper()
    assert "FOR UPDATE SKIP LOCKED" in upper
    assert "ORDER BY RUN_AFTER" in upper
    assert "STATUS = 'QUEUED'" in upper
    assert "RETURNING *" in upper


def test_claim_marks_job_running_and_increments_attempts():
    db = FakeJobDB()
    queue = _queue(db)
    job_id = queue.enqueue("default", "ingest")

    job = queue.claim("default", "w1")

    assert job is not None
    assert job["job_id"] == job_id
    assert job["status"] == "running"
    assert job["locked_by"] == "w1"
    assert job["attempts"] == 1


def test_claim_only_returns_queued_and_due_jobs():
    db = FakeJobDB()
    db.now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)  # pinned clock
    queue = _queue(db)
    # A job scheduled in the future must not be claimed yet.
    future = queue.enqueue("default", "ingest", run_after=db.now + timedelta(hours=1))

    assert queue.claim("default", "w1") is None
    assert db.get(future)["status"] == "queued"

    # Once it is due, it is claimable.
    db.get(future)["run_after"] = db.now - timedelta(seconds=1)
    assert queue.claim("default", "w1")["job_id"] == future


def test_claim_skips_non_queued_jobs():
    db = FakeJobDB()
    queue = _queue(db)
    done_id = queue.enqueue("default", "ingest")
    db.get(done_id)["status"] = "done"
    queued_id = queue.enqueue("default", "ingest")

    job = queue.claim("default", "w1")

    assert job is not None
    assert job["job_id"] == queued_id


def test_claim_returns_none_when_empty():
    db = FakeJobDB()
    assert _queue(db).claim("default", "w1") is None


def test_claim_can_filter_by_kind():
    db = FakeJobDB()
    queue = _queue(db)
    queue.enqueue("default", "ingest")
    reflect_id = queue.enqueue("default", "reflect")

    job = queue.claim("default", "w1", kinds=["reflect"])

    assert job is not None
    assert job["job_id"] == reflect_id
    assert job["kind"] == "reflect"


# --------------------------------------------------------------------------- #
# parallel workers: no double claim
# --------------------------------------------------------------------------- #


def test_parallel_claims_never_return_the_same_job():
    db = FakeJobDB()
    q1 = _queue(db)
    q2 = _queue(db)
    first = q1.enqueue("default", "ingest")
    second = q2.enqueue("default", "ingest")

    barrier = threading.Barrier(2)
    results: list[dict[str, Any] | None] = [None, None]

    def worker(index: int, queue: JobQueue) -> None:
        barrier.wait()  # start both claims as simultaneously as possible
        results[index] = queue.claim("default", f"w{index}")

    threads = [
        threading.Thread(target=worker, args=(0, q1)),
        threading.Thread(target=worker, args=(1, q2)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    claimed = [r for r in results if r is not None]
    assert len(claimed) == 2, "both workers must claim exactly one job"
    ids = {r["job_id"] for r in claimed}
    assert ids == {first, second}
    assert len(ids) == 2, "the two workers must not claim the same job"


# --------------------------------------------------------------------------- #
# complete / fail
# --------------------------------------------------------------------------- #


def test_complete_marks_done():
    db = FakeJobDB()
    queue = _queue(db)
    job_id = queue.enqueue("default", "ingest")
    queue.claim("default", "w1")

    queue.complete(job_id)

    assert db.get(job_id)["status"] == "done"


def test_fail_below_max_requeues_with_future_backoff():
    db = FakeJobDB()
    db.now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)  # pinned clock
    queue = _queue(db)
    job_id = queue.enqueue("default", "ingest", max_attempts=3)
    queue.claim("default", "w1")  # attempts -> 1

    queue.fail(job_id, "boom")

    row = db.get(job_id)
    assert row["status"] == "queued"
    assert row["last_error"] == "boom"
    assert row["run_after"] > db.now
    assert row["run_after"] == db.now + timedelta(seconds=backoff_seconds(1))
    assert row["locked_by"] is None


def test_fail_at_max_marks_failed():
    db = FakeJobDB()
    queue = _queue(db)
    job_id = queue.enqueue("default", "ingest", max_attempts=3)
    db.get(job_id)["attempts"] = 3

    queue.fail(job_id, "gave up")

    row = db.get(job_id)
    assert row["status"] == "failed"
    assert row["last_error"] == "gave up"


def test_fail_unknown_job_is_a_noop():
    db = FakeJobDB()
    _queue(db).fail(str(uuid.uuid4()), "nobody")


# --------------------------------------------------------------------------- #
# recover_stale
# --------------------------------------------------------------------------- #


def test_recover_stale_requeues_old_running_jobs():
    db = FakeJobDB()
    db.now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)  # pinned clock
    queue = _queue(db)
    stale = db.add_running(kind="ingest", locked_at=db.now - timedelta(seconds=600))
    fresh = db.add_running(kind="ingest", locked_at=db.now - timedelta(seconds=1))

    recovered = queue.recover_stale(timeout_seconds=300)

    assert recovered == 1
    assert db.get(stale)["status"] == "queued"
    assert db.get(stale)["locked_by"] is None
    assert db.get(fresh)["status"] == "running"


# --------------------------------------------------------------------------- #
# stats
# --------------------------------------------------------------------------- #


def test_stats_counts_per_status():
    db = FakeJobDB()
    queue = _queue(db)
    a = queue.enqueue("default", "ingest")
    queue.enqueue("default", "ingest")
    queue.claim("default", "w1", kinds=["ingest"])
    queue.complete(a)

    assert queue.stats("default") == {"queued": 1, "done": 1}


# --------------------------------------------------------------------------- #
# run_forever
# --------------------------------------------------------------------------- #


def test_run_forever_processes_two_jobs_and_stops_on_event():
    db = FakeJobDB()
    queue = _queue(db)
    queue.enqueue("default", "tick", {"n": 1})
    queue.enqueue("default", "tick", {"n": 2})

    processed: list[int] = []
    stop = threading.Event()

    def handler(job: dict[str, Any]) -> None:
        processed.append(job["payload"]["n"])
        if len(processed) >= 2:
            stop.set()

    run_forever(
        queue,
        {"tick": handler},
        worker_id="w1",
        poll_interval=0.01,
        stop_event=stop,
    )

    assert sorted(processed) == [1, 2]
    assert queue.stats("default") == {"done": 2}


def test_run_forever_fails_job_without_handler():
    db = FakeJobDB()
    queue = _queue(db)
    job_id = queue.enqueue("default", "unknown-kind", max_attempts=1)

    stop = threading.Event()

    def on_tick(_job: dict[str, Any]) -> None:
        stop.set()

    # No handler for "unknown-kind": the first claim settles it as failed, and
    # "tick" (queued second) stops the loop.
    queue.enqueue("default", "tick")
    run_forever(
        queue,
        {"tick": on_tick},
        worker_id="w1",
        poll_interval=0.01,
        stop_event=stop,
    )

    row = db.get(job_id)
    assert row["status"] == "failed"
    assert "no handler" in row["last_error"]


def test_run_forever_retries_then_succeeds():
    db = FakeJobDB()
    queue = _queue(db)
    job_id = queue.enqueue("default", "flaky", max_attempts=3)

    attempts: list[int] = []
    stop = threading.Event()

    def handler(job: dict[str, Any]) -> None:
        attempts.append(job["attempts"])
        if job["attempts"] < 2:
            raise RuntimeError("transient")
        stop.set()

    run_forever(
        queue,
        {"flaky": handler},
        worker_id="w1",
        poll_interval=0.01,
        stop_event=stop,
    )

    assert attempts == [1, 2]
    assert db.get(job_id)["status"] == "done"


# --------------------------------------------------------------------------- #
# scheduler tick seam (B2)
# --------------------------------------------------------------------------- #


def test_run_forever_invokes_scheduler_tick():
    db = FakeJobDB()
    queue = _queue(db)
    queue.enqueue("default", "tick")

    ticks: list[str] = []
    stop = threading.Event()

    def handler(_job: dict[str, Any]) -> None:
        stop.set()

    run_forever(
        queue,
        {"tick": handler},
        worker_id="w1",
        poll_interval=0.01,
        stop_event=stop,
        scheduler_tick=ticks.append,
        scheduler_interval=0.0,
    )

    assert ticks and ticks[0] == "default"


def test_run_forever_survives_a_failing_scheduler_tick():
    db = FakeJobDB()
    queue = _queue(db)
    queue.enqueue("default", "tick")

    stop = threading.Event()

    def boom(_tenant: str) -> None:
        raise RuntimeError("scheduler down")

    def handler(_job: dict[str, Any]) -> None:
        stop.set()

    run_forever(
        queue,
        {"tick": handler},
        worker_id="w1",
        poll_interval=0.01,
        stop_event=stop,
        scheduler_tick=boom,
        scheduler_interval=0.0,
    )

    assert queue.stats("default") == {"done": 1}


def test_run_forever_settles_job_under_its_own_tenant():
    """B6: a job enqueued for tenant ``acme`` is completed under ``acme``, not
    the queue's ``default_tenant``."""
    db = FakeJobDB()
    # Insert a job for a non-default tenant directly (the fake keeps tenant_id).
    db.rows.append(
        {
            "job_id": "job-acme",
            "tenant_id": "acme",
            "kind": "tick",
            "payload": {},
            "status": "queued",
            "attempts": 0,
            "max_attempts": 3,
            "run_after": db._clock,
            "locked_by": None,
            "locked_at": None,
            "last_error": None,
            "created_at": db._clock,
        }
    )

    recorded: list[tuple[str, str]] = []

    class _SpyQueue(JobQueue):
        def complete(self, job_id: str, *, tenant: str | None = None) -> None:
            recorded.append(("complete", tenant or self.default_tenant))
            super().complete(job_id, tenant=tenant)

    spy = _SpyQueue(conn_factory=db.factory)
    stop = threading.Event()

    def handler(_job: dict[str, Any]) -> None:
        stop.set()

    run_forever(
        spy,
        {"tick": handler},
        worker_id="w1",
        poll_interval=0.01,
        stop_event=stop,
        tenants=["default", "acme"],
    )

    assert recorded == [("complete", "acme")]
    assert db.get("job-acme")["status"] == "done"


def test_scheduler_tick_runs_for_every_tenant():
    """B6/B2: the scheduler tick is invoked once per configured tenant."""
    db = FakeJobDB()
    queue = _queue(db)
    ticks: list[str] = []
    stop = threading.Event()

    def tick(tenant: str) -> None:
        ticks.append(tenant)
        if len(ticks) >= 2:
            stop.set()

    run_forever(
        queue,
        {},
        worker_id="w1",
        poll_interval=0.01,
        stop_event=stop,
        scheduler_tick=tick,
        scheduler_interval=0.0,
        tenants=["default", "acme"],
    )
    assert ticks == ["default", "acme"]


def test_run_forever_fails_job_under_its_own_tenant():
    """B6: a failing non-default-tenant job is requeued/failed under its tenant."""
    db = FakeJobDB()
    db.rows.append(
        {
            "job_id": "job-fail",
            "tenant_id": "acme",
            "kind": "boom",
            "payload": {},
            "status": "queued",
            "attempts": 0,
            "max_attempts": 1,
            "run_after": db._clock,
            "locked_by": None,
            "locked_at": None,
            "last_error": None,
            "created_at": db._clock,
        }
    )

    recorded: list[str | None] = []

    class _SpyQueue(JobQueue):
        def fail(self, job_id: str, error: str, *, tenant: str | None = None) -> None:
            recorded.append(tenant)
            super().fail(job_id, error, tenant=tenant)

    spy = _SpyQueue(conn_factory=db.factory)
    stop = threading.Event()

    def handler(_job: dict[str, Any]) -> None:
        stop.set()
        raise RuntimeError("boom")

    run_forever(
        spy,
        {"boom": handler},
        worker_id="w1",
        poll_interval=0.01,
        stop_event=stop,
    )

    assert recorded == ["acme"]
    assert db.get("job-fail")["status"] == "failed"


# --------------------------------------------------------------------------- #
# reflect handler wiring (B2)
# --------------------------------------------------------------------------- #


def _stub_reflect_worker(monkeypatch: pytest.MonkeyPatch, calls: list[tuple[Any, ...]]) -> None:
    """Replace ``ReflectWorker`` with a stub recording ``run`` invocations."""

    class _Stub:
        def __init__(self, settings: Any = None, **kwargs: Any) -> None:
            self.settings = settings
            self.kwargs = kwargs

        def run(self, tenant: str, *, scope: Any = None, budget_tokens: Any = None) -> Any:
            calls.append((tenant, scope))
            return object()

    monkeypatch.setattr(reflect, "ReflectWorker", _Stub)


def test_default_handlers_wires_reflect_handler(monkeypatch: pytest.MonkeyPatch):
    calls: list[tuple[Any, ...]] = []
    _stub_reflect_worker(monkeypatch, calls)

    handlers = default_handlers(reflect_kwargs={"llm": object()})
    assert "reflect" in handlers

    handlers["reflect"]({"job_id": "j1", "kind": "reflect", "tenant_id": "acme", "payload": {}})
    assert calls == [("acme", None)]


def test_default_handlers_reflect_handler_passes_scope(monkeypatch: pytest.MonkeyPatch):
    calls: list[tuple[Any, ...]] = []
    _stub_reflect_worker(monkeypatch, calls)

    handlers = default_handlers(reflect_kwargs={"llm": object()})
    handlers["reflect"](
        {"job_id": "j1", "kind": "reflect", "tenant_id": "default",
         "payload": {"scope": "stratum://docs/default/proj"}}
    )
    assert calls == [("default", "stratum://docs/default/proj")]


def test_default_handlers_reflect_falls_back_without_llm():
    handlers = default_handlers()  # no reflect_kwargs -> placeholder
    # A placeholder logs but does not raise.
    handlers["reflect"]({"job_id": "j1", "kind": "reflect", "payload": {}})


def test_build_runtime_handlers_wires_reflect_and_scheduler():
    """B6a: the runtime builder wires a real reflect handler + scheduler tick.

    The collaborators are constructed but never connect (lazy); the test only
    asserts the wiring shape — a ``reflect`` handler that is not the no-op
    placeholder and a callable ``run_scheduler_tick``.
    """
    from stratum import worker as worker_mod

    handlers, tick = worker_mod._build_runtime_handlers()
    assert "reflect" in handlers
    assert callable(tick)
    # The reflect handler is a real one bound to a ReflectWorker (not the
    # no-op placeholder used when no llm is injected): its closure captures the
    # worker instance.
    from stratum.reflect import ReflectWorker

    closure_values = [
        cell.cell_contents for cell in (handlers["reflect"].__closure__ or ())
    ]
    assert any(isinstance(value, ReflectWorker) for value in closure_values)


# --------------------------------------------------------------------------- #
# backoff_seconds
# --------------------------------------------------------------------------- #


def test_backoff_seconds_is_exponential_and_capped():
    assert backoff_seconds(1) == 2
    assert backoff_seconds(2) == 4
    assert backoff_seconds(3) == 8
    assert backoff_seconds(30) == 3600  # capped


# --------------------------------------------------------------------------- #
# Integration tests (need a real Postgres) — skipped unless STRATUM_TEST_DB set
# --------------------------------------------------------------------------- #

_TEST_DB = os.getenv("STRATUM_TEST_DB")
requires_db = pytest.mark.skipif(
    not _TEST_DB, reason="set STRATUM_TEST_DB to a Postgres DSN to run integration tests"
)


@pytest.mark.integration
@requires_db
def test_integration_enqueue_claim_complete_roundtrip():
    """Requires the schema from init/003_schema.sql to exist in STRATUM_TEST_DB."""
    import psycopg
    from psycopg.rows import dict_row as _dict_row
    from psycopg_pool import ConnectionPool

    from stratum import db as _db
    from stratum.config import Settings, get_settings

    tenant = f"it_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(_TEST_DB) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tenants (tenant_id, name) VALUES (%s, %s)",
            (tenant, "integration"),
        )

    get_settings.cache_clear()
    _db.close_pool()
    _db._POOL = ConnectionPool(
        conninfo=_TEST_DB,
        min_size=1,
        max_size=2,
        kwargs={"row_factory": _dict_row},
        open=True,
    )
    try:
        queue = JobQueue(Settings())
        queue.enqueue(tenant, "ingest", {"n": 1})
        assert queue.stats(tenant).get("queued", 0) >= 1

        job = queue.claim(tenant, "w-it")
        assert job is not None
        assert job["status"] == "running"
        queue.complete(str(job["job_id"]))

        assert queue.stats(tenant).get("done", 0) >= 1

        # Parallel claims on separate queues return distinct jobs.
        a = queue.enqueue(tenant, "ingest")
        b = queue.enqueue(tenant, "ingest")
        other = JobQueue(Settings())
        c1 = queue.claim(tenant, "w1")
        c2 = other.claim(tenant, "w2")
        assert {c1["job_id"], c2["job_id"]} == {a, b}
    finally:
        with psycopg.connect(_TEST_DB) as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM jobs WHERE kind = %s", ("ingest",))
            cur.execute("DELETE FROM tenants WHERE tenant_id = %s", (tenant,))
        _db.close_pool()
        get_settings.cache_clear()
