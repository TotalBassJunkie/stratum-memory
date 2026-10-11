"""Tests for the reflect batch worker (:mod:`stratum.reflect`, WP-09).

Everything is database-free: a *recording fake connection* models the tables
reflect touches (``jobs`` lock marker, ``memory_events`` watermark/journal,
``staging_candidates``, ``syntheses``, ``facts`` sample) and a deterministic
:class:`FakeLLM` meters tokens. Covered:

* the demand trigger (``>= 500`` events) and the 24 h staleness trigger,
* a hard token budget that aborts with a ``BUDGET_EXCEEDED`` event and no overrun,
* budget clamping to the hard ``reflect_max_tokens`` ceiling,
* the single-run DB lock (a second concurrent run must not start),
* deterministic embedding clustering,
* results landing **only** as ``pending`` staging candidates (no active write),
* the :data:`MAX_CLUSTERS` cap per run.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from stratum import reflect
from stratum.config import Settings
from stratum.reflect import (
    BUDGET_EXCEEDED_OP,
    MAX_CLUSTERS,
    REFLECT_LOCK_PURPOSE,
    ReflectWorker,
    cluster_embeddings,
)

# --------------------------------------------------------------------------- fakes


class _Usage:
    """Minimal ``Usage`` look-alike (only ``total_tokens`` is read)."""

    def __init__(self, total_tokens: int = 0) -> None:
        self.total_tokens = total_tokens


class FakeLLM:
    """Deterministic offline chat/embed double with token metering."""

    def __init__(
        self,
        *,
        chat_cost: int = 100,
        embed_cost: int = 10,
        skill: bool = True,
        dim: int = 4,
    ) -> None:
        self.chat_cost = chat_cost
        self.embed_cost = embed_cost
        self.skill = skill
        self.dim = dim
        self.chat_calls = 0
        self.embed_calls = 0
        self._total = 0

    def usage(self) -> _Usage:
        return _Usage(self._total)

    async def embed(self, texts: Any, *, model: str | None = None) -> list[list[float]]:
        if isinstance(texts, str):
            inputs = [texts]
        else:
            inputs = list(texts)
        self.embed_calls += 1
        self._total += self.embed_cost * len(inputs)
        vectors = []
        for text in inputs:
            seed = sum(text.encode("utf-8")) % 7
            vectors.append([((index + seed) % 5) / 5.0 for index in range(self.dim)])
        return vectors

    async def chat(self, messages: Any, **kwargs: Any) -> Any:
        self.chat_calls += 1
        self._total += self.chat_cost
        content = {
            "summary": f"summary {self.chat_calls}",
            "patterns": ["p1", "p2"],
            "skill_suggestion": "do the thing" if self.skill else None,
        }
        return type("ChatResult", (), {"content": content, "model": "fake-model"})()


def _norm(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip().upper()


def _unwrap(value: Any) -> Any:
    return getattr(value, "obj", value)


class FakeDB:
    """In-memory model of the tables reflect reads/writes."""

    def __init__(
        self,
        *,
        facts: list[dict[str, Any]] | None = None,
        events: list[dict[str, Any]] | None = None,
        lock_running: bool = False,
        advisory_locked: bool = False,
    ) -> None:
        self.facts = list(facts or [])
        self.events = list(events or [])
        self.candidates: list[dict[str, Any]] = []
        self.syntheses: list[dict[str, Any]] = []
        self.jobs: list[dict[str, Any]] = []
        self.statements: list[tuple[str, tuple]] = []
        self._job_seq = 0
        self._candidate_seq = 0
        self._event_seq = 0
        #: Simulates another session already holding the tenant advisory lock.
        self.advisory_locked = advisory_locked
        if lock_running:
            self.jobs.append(
                {
                    "job_id": "lock-existing",
                    "tenant_id": "default",
                    "kind": "reflect",
                    "payload": {"purpose": REFLECT_LOCK_PURPOSE},
                    "status": "running",
                }
            )

    def __call__(self, _tenant: str) -> "FakeConnection":
        return FakeConnection(self)

    # -- introspection used by tests ---------------------------------------

    def ops(self) -> list[str]:
        return [event["op"] for event in self.events]

    def active_writes(self) -> list[str]:
        """Any statement that would write an active table (must stay empty)."""
        active = ("INSERT INTO FACTS", "INSERT INTO ENTITIES", "INSERT INTO EDGES",
                  "INSERT INTO FILES", "INSERT INTO CHUNKS", "UPDATE FACTS")
        return [sql for sql, _ in self.statements if sql.upper().startswith(active)]

    def running_reflect_locks(self) -> list[dict[str, Any]]:
        return [
            job
            for job in self.jobs
            if job.get("kind") == "reflect"
            and job.get("status") == "running"
            and (job.get("payload") or {}).get("purpose") == REFLECT_LOCK_PURPOSE
        ]

    # -- statement handling --------------------------------------------------

    def handle(self, sql: str, params: tuple) -> tuple[Any, list[Any]]:
        """Return ``(fetchone_row, fetchall_rows)`` for one statement."""
        if "PG_TRY_ADVISORY_XACT_LOCK" in sql:
            return self._try_advisory_lock(), []
        if "AND PAYLOAD->>'PURPOSE' = %S" in sql and sql.startswith("SELECT 1 FROM JOBS"):
            return self._running_lock_exists(params), []
        if sql.startswith("INSERT INTO JOBS") and "'REFLECT'" in sql:
            return self._insert_lock(params), []
        if sql.startswith("INSERT INTO JOBS"):
            return self._enqueue(params), []
        if sql.startswith("UPDATE JOBS SET STATUS = 'DONE'"):
            self._release_lock(params)
            return None, []
        if sql.startswith("SELECT MAX(CREATED_AT) AS LAST_REFLECT"):
            return {"last_reflect": self._last_reflect(params[0])}, []
        if sql.startswith("SELECT COUNT(*) AS N FROM MEMORY_EVENTS"):
            return {"n": self._count_events(sql, params)}, []
        if sql.startswith("SELECT F.FACT_ID, F.PREDICATE"):
            return None, self._sample(sql, params)
        if sql.startswith("INSERT INTO STAGING_CANDIDATES"):
            return self._insert_candidate(params), []
        if sql.startswith("INSERT INTO MEMORY_EVENTS"):
            return self._insert_event(params), []
        if sql.startswith("INSERT INTO SYNTHESES"):
            return self._insert_synthesis(params), []
        raise AssertionError(f"unhandled SQL in fake: {sql!r}")

    def _try_advisory_lock(self) -> dict[str, Any]:
        return {"acquired": not self.advisory_locked}

    def _running_lock_exists(self, params: tuple) -> dict[str, Any] | None:
        tenant, purpose = params
        return (
            {"?column?": 1}
            if any(
                job.get("tenant_id") == tenant
                and job.get("kind") == "reflect"
                and job.get("status") == "running"
                and (job.get("payload") or {}).get("purpose") == purpose
                for job in self.jobs
            )
            else None
        )

    def _insert_lock(self, params: tuple) -> dict[str, Any]:
        tenant, payload, worker_id = params
        self._job_seq += 1
        job_id = f"lock-{self._job_seq}"
        self.jobs.append(
            {
                "job_id": job_id,
                "tenant_id": tenant,
                "kind": "reflect",
                "payload": _unwrap(payload),
                "status": "running",
                "locked_by": worker_id,
            }
        )
        return {"job_id": job_id}

    def _enqueue(self, params: tuple) -> dict[str, Any]:
        tenant, kind, payload, max_attempts, run_after = params
        self._job_seq += 1
        job_id = f"job-{self._job_seq}"
        self.jobs.append(
            {
                "job_id": job_id,
                "tenant_id": tenant,
                "kind": kind,
                "payload": _unwrap(payload),
                "status": "queued",
                "max_attempts": max_attempts,
                "run_after": run_after,
            }
        )
        return {"job_id": job_id}

    def _release_lock(self, params: tuple) -> None:
        (job_id,) = params
        for job in self.jobs:
            if job["job_id"] == job_id:
                job["status"] = "done"

    def _last_reflect(self, tenant: str) -> datetime | None:
        stamps = [
            event["created_at"]
            for event in self.events
            if event.get("tenant_id", tenant) == tenant and event["op"] == "REFLECT"
        ]
        return max(stamps) if stamps else None

    def _count_events(self, sql: str, params: tuple) -> int:
        tenant = params[0]
        since = params[1] if "CREATED_AT > %S" in sql and len(params) > 1 else None
        return sum(
            1
            for event in self.events
            if event.get("tenant_id", tenant) == tenant
            and event["op"] != "REFLECT"
            and (since is None or event["created_at"] > since)
        )

    def _sample(self, sql: str, params: tuple) -> list[dict[str, Any]]:
        tenant = params[0]
        index = 1
        scope = None
        if "STARTS_WITH(F.SOURCE_URI" in sql:
            scope = params[index]
            index += 2  # B6: scope and scope || '/'
        since = None
        if "F.CREATED_AT > %S" in sql:
            since = params[index]
            index += 1
        limit = params[index]
        rows = [
            fact
            for fact in self.facts
            if fact.get("tenant_id", tenant) == tenant
            and (
                scope is None
                or str(fact.get("source_uri") or "") == scope
                or str(fact.get("source_uri") or "").startswith(scope + "/")
            )
            and (since is None or fact["created_at"] > since)
        ]
        # B5: newest first, fact_id ascending as a deterministic tiebreaker
        # (matches SQL ``ORDER BY created_at DESC, fact_id``).
        rows.sort(key=lambda fact: str(fact["fact_id"]))
        rows.sort(key=lambda fact: fact["created_at"], reverse=True)
        return rows[: int(limit)]

    def _insert_candidate(self, params: tuple) -> dict[str, Any]:
        tenant, kind, payload, source_uri = params
        self._candidate_seq += 1
        candidate_id = f"cand-{self._candidate_seq}"
        self.candidates.append(
            {
                "candidate_id": candidate_id,
                "tenant_id": tenant,
                "kind": kind,
                "payload": _unwrap(payload),
                "source_uri": source_uri,
                "status": "pending",
            }
        )
        return {"candidate_id": candidate_id}

    def _insert_event(self, params: tuple) -> dict[str, Any]:
        tenant, op, target_uri, after, actor = params
        self._event_seq += 1
        event_id = f"ev-{self._event_seq}"
        self.events.append(
            {
                "event_id": event_id,
                "tenant_id": tenant,
                "op": op,
                "target_uri": target_uri,
                "after": _unwrap(after),
                "actor": actor,
                "created_at": datetime.now(timezone.utc),
            }
        )
        return {"event_id": event_id}

    def _insert_synthesis(self, params: tuple) -> dict[str, Any]:
        tenant, scope_uri, summary, payload, model, tokens = params
        self.syntheses.append(
            {"tenant_id": tenant, "summary": summary, "tokens_used": tokens}
        )
        return {"synthesis_id": f"syn-{len(self.syntheses)}"}


class FakeCursor:
    def __init__(self, conn: "FakeConnection") -> None:
        self._conn = conn
        self._one: Any = None
        self._many: list[Any] = []

    def execute(self, sql: str, params: tuple = ()) -> None:
        self._conn.db.statements.append((sql, params))
        self._one, self._many = self._conn.db.handle(_norm(sql), params)

    def fetchone(self) -> Any:
        return self._one

    def fetchall(self) -> list[Any]:
        return list(self._many)

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class FakeConnection:
    def __init__(self, db: FakeDB) -> None:
        self.db = db

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        pass

    def __enter__(self) -> "FakeConnection":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


# --------------------------------------------------------------------------- helpers

NOW = datetime(2026, 10, 11, 12, 0, 0, tzinfo=timezone.utc)


def _fixed_clock() -> datetime:
    return NOW


def _fact(
    fact_id: str,
    embedding: list[float],
    *,
    predicate: str = "pred",
    object_text: str = "obj",
    subject: str = "sub",
    source_uri: str = "stratum://docs/default/a",
    created_at: datetime | None = None,
) -> dict[str, Any]:
    literal = "[" + ",".join(repr(float(x)) for x in embedding) + "]"
    return {
        "fact_id": fact_id,
        "predicate": predicate,
        "object_text": object_text,
        "subject_name": subject,
        "source_uri": source_uri,
        "embedding_text": literal,
        "created_at": created_at or NOW,
    }


def _worker(db: FakeDB, llm: FakeLLM, **kwargs: Any) -> ReflectWorker:
    kwargs.setdefault("clock", _fixed_clock)
    return ReflectWorker(
        Settings(reflect_budget_tokens=20000, reflect_max_tokens=50000),
        llm=llm,
        store=object(),
        graph=object(),
        curator=object(),
        conn_factory=db,
        **kwargs,
    )


def _clock_at(**overrides: int) -> Any:
    """Return an injectable clock pinned to :data:`NOW` with given overrides."""
    base = NOW.replace(**overrides)
    return lambda: base


# --------------------------------------------------------------------------- triggers


def test_should_run_when_event_threshold_reached():
    db = FakeDB(
        events=[
            {"op": "REFLECT", "created_at": NOW - timedelta(hours=1), "tenant_id": "default"},
            *[
                {"op": "ADD", "created_at": NOW - timedelta(minutes=30), "tenant_id": "default"}
                for _ in range(500)
            ],
        ]
    )
    assert _worker(db, FakeLLM()).should_run("default") is True


def test_should_not_run_below_threshold_and_within_24h():
    db = FakeDB(
        events=[
            {"op": "REFLECT", "created_at": NOW - timedelta(hours=1), "tenant_id": "default"},
            *[
                {"op": "ADD", "created_at": NOW - timedelta(minutes=30), "tenant_id": "default"}
                for _ in range(10)
            ],
        ]
    )
    assert _worker(db, FakeLLM()).should_run("default") is False


def test_should_run_when_stale_over_24h():
    db = FakeDB(
        events=[
            {"op": "REFLECT", "created_at": NOW - timedelta(hours=25), "tenant_id": "default"},
        ]
    )
    assert _worker(db, FakeLLM()).should_run("default") is True


def test_should_run_when_never_reflected():
    assert _worker(FakeDB(), FakeLLM()).should_run("default") is True


def test_schedule_if_due_enqueues_reflect_job():
    db = FakeDB()  # never reflected -> due
    job_id = _worker(db, FakeLLM()).schedule_if_due("default")
    assert job_id is not None
    job = next(j for j in db.jobs if j["job_id"] == job_id)
    assert job["kind"] == "reflect"
    assert job["status"] == "queued"


def test_schedule_if_due_is_noop_when_not_due():
    db = FakeDB(
        events=[
            {"op": "REFLECT", "created_at": NOW - timedelta(hours=1), "tenant_id": "default"},
        ]
    )
    assert _worker(db, FakeLLM()).schedule_if_due("default") is None


# --------------------------------------------------------------------------- budget


def test_budget_aborts_with_event_and_no_overrun():
    db = FakeDB(facts=[_fact("f1", [1.0, 0.0, 0.0, 0.0]), _fact("f2", [0.0, 1.0, 0.0, 0.0])])
    llm = FakeLLM(chat_cost=5000)
    worker = _worker(db, llm)

    result = worker.run("default", budget_tokens=3000)

    assert result.truncated is True
    assert result.tokens_used > 3000
    assert BUDGET_EXCEEDED_OP in db.ops()
    # No overrun: the run stops after the single call that broke the ceiling.
    assert llm.chat_calls == 1
    # The aborted cluster was never staged.
    assert db.candidates == []
    budget_event = next(e for e in db.events if e["op"] == BUDGET_EXCEEDED_OP)
    assert budget_event["after"]["budget_tokens"] == 3000


def test_budget_is_clamped_to_hard_max():
    db = FakeDB(facts=[_fact("f1", [1.0, 0.0, 0.0, 0.0]), _fact("f2", [0.0, 1.0, 0.0, 0.0])])
    llm = FakeLLM(chat_cost=100000)
    worker = _worker(db, llm)

    # A caller asking for a huge budget is clamped to reflect_max_tokens.
    assert worker.effective_budget(10**9) == 50000

    result = worker.run("default", budget_tokens=10**9)

    assert result.budget_tokens == 50000
    assert result.truncated is True
    assert llm.chat_calls == 1
    event = next(e for e in db.events if e["op"] == BUDGET_EXCEEDED_OP)
    assert event["after"]["budget_tokens"] == 50000


def test_budget_stops_before_second_cluster():
    # Four orthogonal facts -> two clusters; the first call fits, the second does not.
    db = FakeDB(
        facts=[
            _fact("f1", [1.0, 0.0, 0.0, 0.0]),
            _fact("f2", [0.9, 0.1, 0.0, 0.0]),
            _fact("f3", [0.0, 0.0, 1.0, 0.0]),
            _fact("f4", [0.0, 0.0, 0.9, 0.1]),
        ]
    )
    llm = FakeLLM(chat_cost=5000)
    worker = _worker(db, llm)

    result = worker.run("default", budget_tokens=7000)

    assert result.truncated is True
    assert llm.chat_calls == 2  # one accepted, one that broke the ceiling
    assert len(db.candidates) == 2  # only the first cluster's synthesis + skill


# --------------------------------------------------------------------------- lock


def test_second_run_is_skipped_while_lock_held():
    db = FakeDB(
        facts=[_fact("f1", [1.0, 0.0, 0.0, 0.0]), _fact("f2", [0.0, 1.0, 0.0, 0.0])],
        lock_running=True,
    )
    llm = FakeLLM()
    worker = _worker(db, llm)

    result = worker.run("default")

    assert result.skipped is True
    assert result.reason == "reflect_already_running"
    assert llm.chat_calls == 0
    assert db.candidates == []


def test_lock_is_released_after_a_run():
    db = FakeDB(facts=[_fact("f1", [1.0, 0.0, 0.0, 0.0])])
    worker = _worker(db, FakeLLM())

    worker.run("default")

    assert db.running_reflect_locks() == []


def test_run_acquires_then_blocks_a_concurrent_run():
    db = FakeDB(facts=[_fact("f1", [1.0, 0.0, 0.0, 0.0])])
    # The first run leaves a fresh running lock behind -> second run must skip.
    db.jobs.append(
        {
            "job_id": "held",
            "tenant_id": "default",
            "kind": "reflect",
            "payload": {"purpose": REFLECT_LOCK_PURPOSE},
            "status": "running",
        }
    )
    result = _worker(db, FakeLLM()).run("default")
    assert result.skipped is True


# --------------------------------------------------------------------------- clustering


def test_clustering_is_deterministic():
    vectors = [
        [1.0, 0.0, 0.0],
        [0.0, 1.0, 0.0],
        [0.9, 0.1, 0.0],
        [0.1, 0.9, 0.0],
    ]
    first = cluster_embeddings(vectors, k=2)
    second = cluster_embeddings(vectors, k=2)
    assert first == second
    # Deterministic order: clusters sorted by their smallest member index.
    assert first == sorted(first, key=lambda members: members[0])
    assert all(members == sorted(members) for members in first)


def test_clustering_single_cluster_below_min_items():
    assert cluster_embeddings([[1.0, 0.0]]) == [[0]]
    assert cluster_embeddings([]) == []


def test_clustering_same_input_same_output_across_calls_worker():
    facts = [
        _fact("f1", [1.0, 0.0, 0.0, 0.0]),
        _fact("f2", [0.9, 0.1, 0.0, 0.0]),
        _fact("f3", [0.0, 0.0, 1.0, 0.0]),
        _fact("f4", [0.0, 0.0, 0.9, 0.1]),
    ]
    first = _worker(FakeDB(facts=facts), FakeLLM()).run("default")
    second = _worker(FakeDB(facts=facts), FakeLLM()).run("default")
    assert (first.clusters, first.staged_candidates) == (
        second.clusters,
        second.staged_candidates,
    )


# --------------------------------------------------------------------------- staging


def test_results_are_only_pending_candidates():
    db = FakeDB(
        facts=[
            _fact("f1", [1.0, 0.0, 0.0, 0.0]),
            _fact("f2", [0.9, 0.1, 0.0, 0.0]),
            _fact("f3", [0.0, 0.0, 1.0, 0.0]),
            _fact("f4", [0.0, 0.0, 0.9, 0.1]),
        ]
    )
    llm = FakeLLM(skill=True)
    result = _worker(db, llm).run("default")

    assert result.clusters == 2
    assert result.staged_candidates == len(db.candidates) == 4  # 2 clusters × (synth + skill)
    assert all(candidate["status"] == "pending" for candidate in db.candidates)
    assert {candidate["kind"] for candidate in db.candidates} == {"synthesis", "skill"}
    # Never a direct active write (graph/store untouched).
    assert db.active_writes() == []


def test_synthesis_only_candidate_when_no_skill_suggestion():
    db = FakeDB(facts=[_fact("f1", [1.0, 0.0, 0.0, 0.0])])
    result = _worker(db, FakeLLM(skill=False)).run("default")
    assert result.staged_candidates == 1
    assert db.candidates[0]["kind"] == "synthesis"
    assert db.candidates[0]["status"] == "pending"


def test_record_synthesis_is_off_by_default():
    db = FakeDB(facts=[_fact("f1", [1.0, 0.0, 0.0, 0.0])])
    _worker(db, FakeLLM()).run("default")
    assert db.syntheses == []


def test_reflect_event_is_logged_for_watermark():
    db = FakeDB(facts=[_fact("f1", [1.0, 0.0, 0.0, 0.0])])
    _worker(db, FakeLLM()).run("default")
    assert "REFLECT" in db.ops()


# --------------------------------------------------------------------------- caps


def test_cluster_count_is_capped():
    vectors = [[1.0 if i == j else 0.0 for j in range(25)] for i in range(25)]
    clusters = cluster_embeddings(vectors, k=100)
    assert len(clusters) <= MAX_CLUSTERS


def test_run_respects_max_clusters(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(reflect, "MAX_CLUSTERS", 2)
    facts = [
        _fact(f"f{i}", [1.0 if i == j else 0.0 for j in range(9)])
        for i in range(9)
    ]
    db = FakeDB(facts=facts)
    llm = FakeLLM()
    result = _worker(db, llm).run("default")

    assert result.clusters <= 2
    assert llm.chat_calls <= 2


# --------------------------------------------------------------------------- scope


def test_scope_filters_sample_and_is_validated():
    db = FakeDB(
        facts=[
            _fact("f1", [1.0, 0.0, 0.0, 0.0], source_uri="stratum://docs/default/proj/a"),
            _fact("f2", [0.0, 1.0, 0.0, 0.0], source_uri="stratum://docs/default/other/b"),
        ]
    )
    result = _worker(db, FakeLLM()).run("default", scope="stratum://docs/default/proj")
    assert result.staged_candidates >= 1
    assert all(
        candidate["source_uri"] == "stratum://docs/default/proj"
        for candidate in db.candidates
    )


def test_invalid_scope_is_rejected():
    with pytest.raises(ValueError):
        _worker(FakeDB(), FakeLLM()).run("default", scope="not-a-uri")


def test_scope_tenant_mismatch_is_rejected():
    with pytest.raises(ValueError):
        _worker(FakeDB(), FakeLLM()).run(
            "default", scope="stratum://docs/other/proj"
        )


# --------------------------------------------------------------------------- B6 scope boundary


def test_scope_prefix_does_not_match_sibling_segment():
    """B6: ``.../proj`` must not match ``.../projX`` (segment boundary)."""
    db = FakeDB(
        facts=[
            _fact("f1", [1.0, 0.0, 0.0, 0.0], source_uri="stratum://docs/default/proj/a"),
            _fact("f2", [0.0, 1.0, 0.0, 0.0], source_uri="stratum://docs/default/projX/c"),
        ]
    )
    result = _worker(db, FakeLLM()).run("default", scope="stratum://docs/default/proj")
    assert result.staged_candidates >= 1
    # Only f1 (inside the scope) is sampled; projX is excluded -> one cluster,
    # hence one synthesis + one skill candidate.
    assert len(db.candidates) == 2
    sample = _worker(db, FakeLLM())._load_sample(
        "default", "stratum://docs/default/proj", since=None, limit=10
    )
    assert [item["fact_id"] for item in sample] == ["f1"]


# --------------------------------------------------------------------------- B5 sample determinism


def test_sample_order_is_deterministic_on_equal_created_at():
    """B5: equal ``created_at`` ties break on ``fact_id`` (stable sample)."""
    same = NOW
    db = FakeDB(
        facts=[
            _fact("b", [1.0, 0.0, 0.0, 0.0], created_at=same),
            _fact("a", [0.0, 1.0, 0.0, 0.0], created_at=same),
            _fact("c", [0.0, 0.0, 1.0, 0.0], created_at=same),
        ]
    )
    sample = _worker(db, FakeLLM())._load_sample("default", None, since=None, limit=2)
    assert [item["fact_id"] for item in sample] == ["a", "b"]


# --------------------------------------------------------------------------- B3 timeout


def test_timeout_aborts_run_with_truncated_flag():
    """B3/§9: a tiny deadline aborts the cluster loop and flags truncation."""
    db = FakeDB(
        facts=[
            _fact("f1", [1.0, 0.0, 0.0, 0.0]),
            _fact("f2", [0.9, 0.1, 0.0, 0.0]),
            _fact("f3", [0.0, 0.0, 1.0, 0.0]),
            _fact("f4", [0.0, 0.0, 0.9, 0.1]),
        ]
    )
    llm = FakeLLM()
    # Monotonic clock already past a zero-second deadline.
    worker = _worker(db, llm, timeout_seconds=0.0, monotonic=lambda: 100.0)

    result = worker.run("default")

    assert result.truncated is True
    assert llm.chat_calls == 0  # aborted before the first cluster
    assert db.candidates == []


def test_timeout_logs_event_and_releases_lock():
    db = FakeDB(facts=[_fact("f1", [1.0, 0.0, 0.0, 0.0])])
    worker = _worker(db, FakeLLM(), timeout_seconds=0.0, monotonic=lambda: 100.0)

    worker.run("default")

    assert "TIMEOUT_EXCEEDED" in db.ops()
    assert db.running_reflect_locks() == []


# --------------------------------------------------------------------------- B1 advisory lock


def test_advisory_lock_blocks_a_concurrent_run():
    """B1: a held tenant advisory lock makes the run skip cleanly."""
    db = FakeDB(facts=[_fact("f1", [1.0, 0.0, 0.0, 0.0])], advisory_locked=True)
    llm = FakeLLM()

    result = _worker(db, llm).run("default")

    assert result.skipped is True
    assert result.reason == "reflect_already_running"
    assert llm.chat_calls == 0
    assert db.jobs == []  # nothing inserted


def test_advisory_lock_claim_is_taken_on_one_connection():
    """B1: the atomic advisory claim runs on a single connection/transaction."""
    db = FakeDB()
    worker = _worker(db, FakeLLM())
    job_id = worker._acquire_lock("default", None, "w1")

    assert job_id is not None
    assert any(
        "PG_TRY_ADVISORY_XACT_LOCK" in sql.upper() for sql, _ in db.statements
    )


def test_stale_running_marker_is_skipped_when_no_advisory_lock():
    """B1: even with a free advisory lock, an existing running marker skips."""
    db = FakeDB(facts=[_fact("f1", [1.0, 0.0, 0.0, 0.0])])
    db.jobs.append(
        {
            "job_id": "stale",
            "tenant_id": "default",
            "kind": "reflect",
            "payload": {"purpose": REFLECT_LOCK_PURPOSE},
            "status": "running",
        }
    )
    result = _worker(db, FakeLLM()).run("default")
    assert result.skipped is True


def test_advisory_lock_key_is_tenant_specific():
    assert reflect._advisory_lock_key("default") != reflect._advisory_lock_key("acme")


# --------------------------------------------------------------------------- B2 scheduler tick


def test_scheduler_tick_enqueues_at_three_utc_when_due():
    db = FakeDB()  # never reflected -> due
    worker = _worker(db, FakeLLM(), clock=_clock_at(hour=3))
    job_id = worker.run_scheduler_tick("default")
    assert job_id is not None
    job = next(j for j in db.jobs if j["job_id"] == job_id)
    assert job["kind"] == "reflect"
    assert job["status"] == "queued"


def test_scheduler_tick_is_noop_outside_three_utc():
    db = FakeDB()
    worker = _worker(db, FakeLLM(), clock=_clock_at(hour=12))
    assert worker.run_scheduler_tick("default") is None
    assert db.jobs == []


def test_scheduler_tick_is_noop_when_not_due():
    db = FakeDB(
        events=[
            {"op": "REFLECT", "created_at": NOW - timedelta(hours=1), "tenant_id": "default"},
        ]
    )
    worker = _worker(db, FakeLLM(), clock=_clock_at(hour=3))
    assert worker.run_scheduler_tick("default") is None
