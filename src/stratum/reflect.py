"""Reflect batch worker — periodic condensation into *candidates* (WP-09).

Reflect implements MASTER-PLAN §9 and :doc:`ADR-0006 <../docs/adr/0006-reflect-budget>`:
it periodically samples newly changed facts, clusters them by embedding, asks the
LLM to summarise each cluster (patterns + an optional skill suggestion) and writes
the results as ``staging_candidates`` with ``status='pending'``. It **never**
promotes anything into the active tables — that is exclusively the Curator's job
(WP-08, ADR-0005).

Cost and concurrency are hard-bounded:

* **Budget** — every run has a token ceiling
  (``Settings.reflect_budget_tokens``, default 20 000), clamped to the hard
  ``Settings.reflect_max_tokens`` (default 50 000). The LLM's token counter is
  sampled before and after the run; the moment the delta reaches the ceiling the
  run stops and a ``BUDGET_EXCEEDED`` row is appended to ``memory_events``
  (``truncated=True`` on the result).
* **Single run** — mutually exclusive via a Postgres **advisory transaction
  lock** derived from the tenant (``pg_try_advisory_xact_lock``, atomic without
  any schema change). The winner inserts a ``jobs`` row (``kind='reflect'``,
  ``status='running'``, ``payload->>'purpose'`` = the lock marker) whose
  ``running`` state is the run-duration marker; a second concurrent run finds the
  advisory lock held or the running marker present and returns immediately with
  ``skipped=True``. The marker is released (``done``) when the run ends.
  *(If a partial unique index is later preferred over the advisory lock, it would
  be ``CREATE UNIQUE INDEX … ON jobs (tenant_id) WHERE kind='reflect' AND
  status='running' AND payload->>'purpose'='reflect_lock'`` — see
  :data:`REFLECT_LOCK_INDEX_SQL`; WP-09 deliberately uses the advisory lock and
  does not require it.)*
* **Cluster cap** — at most :data:`MAX_CLUSTERS` (20) LLM calls per run.
* **Timeout** — a run is bounded to :data:`REFLECT_TIMEOUT_SECONDS` (default
  1800 s = 30 min, MASTER-PLAN §9). Exceeding it aborts the cluster loop with
  ``truncated=True`` and a ``TIMEOUT_EXCEEDED`` journal row.

Design rules (mirrors the rest of the package):

* All database access goes through the injectable ``conn_factory`` whose
  signature mirrors :func:`stratum.db.connection`; the default is the real,
  tenant-scoped RLS connection. Tests inject an in-memory fake.
* ``llm`` (chat + embeddings) is injected so a deterministic fake can drive the
  run offline.
* ``store`` / ``graph`` / ``curator`` are injected for interface parity with the
  other batch lanes; reflect itself only writes ``staging_candidates`` and the
  ``memory_events`` journal.
* Clustering is pure Python (no numpy required) and fully deterministic given
  its input and the fixed :data:`CLUSTER_SEED`.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import math
import random
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from psycopg.types.json import Jsonb

from . import db
from .config import Settings, get_settings
from .uri import UriError, parse_uri
from .vector import cosine_similarity
from .worker import JobQueue

__all__ = [
    "CLUSTER_SEED",
    "MAX_CLUSTERS",
    "MIN_CLUSTER_ITEMS",
    "REFLECT_INTERVAL_HOURS",
    "REFLECT_LOCK_INDEX_SQL",
    "REFLECT_LOCK_PURPOSE",
    "REFLECT_SCHEDULE_HOUR",
    "REFLECT_TIMEOUT_SECONDS",
    "SAMPLE_SIZE",
    "ReflectResult",
    "ReflectWorker",
    "cluster_embeddings",
]

logger = logging.getLogger("stratum.reflect")

#: Hard ceiling on the number of clusters (⇒ LLM calls) per run.
MAX_CLUSTERS = 20

#: Maximum number of facts sampled per run ("Chunk-Stichprobe statt Gesamtkorpus").
SAMPLE_SIZE = 200

#: Below this many sampled items no real clustering is meaningful → one cluster.
MIN_CLUSTER_ITEMS = 2

#: Fixed RNG seed for the k-means-style clustering (deterministic output).
CLUSTER_SEED = 0

#: Time-based trigger: a run is due once this many hours have passed.
REFLECT_INTERVAL_HOURS = 24

#: §9 timeout: hard ceiling on a single run's wall-clock duration (seconds).
#: Default 30 min; a run that reaches it aborts with ``truncated=True``.
REFLECT_TIMEOUT_SECONDS = 1800

#: Hour of day (UTC) at which the poll-based scheduler fires the daily run (B2).
REFLECT_SCHEDULE_HOUR = 3

#: ``jobs.payload->>'purpose'`` marker distinguishing a reflect *lock* row from a
#: queued ``kind='reflect'`` work row (a claimed work row is also ``running`` but
#: must not itself block the run).
REFLECT_LOCK_PURPOSE = "reflect_lock"

#: ``memory_events.op`` for a hard budget abort (append-only journal).
BUDGET_EXCEEDED_OP = "BUDGET_EXCEEDED"

#: ``memory_events.op`` for a run aborted by the §9 wall-clock timeout.
TIMEOUT_EXCEEDED_OP = "TIMEOUT_EXCEEDED"

#: The partial unique index that would let ``INSERT … ON CONFLICT DO NOTHING``
#: claim the single-run lock atomically without an advisory lock. WP-09 uses the
#: advisory-lock variant (no schema change); this constant is documentation for
#: the WP-02 migration should the index ever be preferred. It is a string only —
#: reflect never creates it.
REFLECT_LOCK_INDEX_SQL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS jobs_reflect_lock_uq ON jobs (tenant_id) "
    "WHERE kind = 'reflect' AND status = 'running' "
    "AND payload->>'purpose' = 'reflect_lock'"
)

#: ``memory_events.op`` marking a completed reflect run (also the "last reflect"
#: watermark used by :meth:`ReflectWorker.should_run`).
REFLECT_OP = "REFLECT"

_CLUSTER_SCHEMA: dict[str, Any] = {
    "name": "reflect_cluster",
    "schema": {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "patterns": {"type": "array", "items": {"type": "string"}},
            "skill_suggestion": {"type": ["string", "null"]},
        },
        "required": ["summary", "patterns", "skill_suggestion"],
    },
}

_REFLECT_SYSTEM_PROMPT = (
    "You are the reflect stage of a memory system. You receive a cluster of "
    "related facts. Return ONLY JSON of the shape "
    '{"summary": str, "patterns": [str, ...], "skill_suggestion": str|null}. '
    "``summary`` condenses the cluster into one or two sentences. ``patterns`` "
    "lists recurring themes. ``skill_suggestion`` proposes a reusable skill (or "
    "null when there is none). Never invent facts that are not supported by the "
    "input."
)

#: Column-selection for the fact sample (explicit, so the dict shape is stable).
_SAMPLE_SQL = (
    "SELECT f.fact_id, f.predicate, f.object_text, e.name AS subject_name, "
    "f.source_uri, f.embedding::text AS embedding_text, f.created_at "
    "FROM facts f LEFT JOIN entities e ON e.entity_id = f.subject_entity_id "
)

Clock = Callable[[], datetime]


@dataclass(frozen=True)
class ReflectResult:
    """Outcome of one :meth:`ReflectWorker.run`.

    Attributes:
        clusters: Number of clusters (topics) found in the sample.
        staged_candidates: Number of ``pending`` candidates actually written.
        tokens_used: LLM tokens consumed (``llm.usage()`` delta), 0 when skipped.
        truncated: ``True`` when the token budget aborted the run.
        skipped: ``True`` when a concurrent run already held the lock.
        reason: Stable token explaining a skip (e.g. ``reflect_already_running``).
        budget_tokens: The effective (clamped) budget used for this run.
    """

    clusters: int
    staged_candidates: int
    tokens_used: int
    truncated: bool
    skipped: bool = False
    reason: str | None = None
    budget_tokens: int = 0


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _row_get(row: Any, key: str, index: int) -> Any:
    """Read a column from a row that may be a mapping (``dict_row``) or sequence."""
    if isinstance(row, Mapping):
        return row[key]
    return row[index]


def _as_utc(value: Any) -> datetime | None:
    """Normalise ``value`` (datetime / ISO string / None) to aware UTC."""
    if value is None:
        return None
    if isinstance(value, str):
        text = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            value = datetime.fromisoformat(text)
        except ValueError:
            return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return None


def _parse_vector(value: Any) -> list[float] | None:
    """Parse a pgvector text literal (``[0.1,0.2]``) or a sequence into floats."""
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return [float(x) for x in value]
    text = str(value).strip().strip("[]")
    if not text:
        return None
    try:
        return [float(part) for part in text.split(",")]
    except ValueError:
        return None


def _resolve_awaitable(value: Any) -> Any:
    """Return ``value``, awaiting it first when it is a coroutine/awaitable.

    ``LLMClient.chat``/``embed`` are ``async``; :meth:`ReflectWorker.run` exposes
    a synchronous surface. When called from a running event loop the coroutine is
    executed on a short-lived thread with its own loop, so the sync API works both
    in a plain script and inside an async MCP handler (mirrors ``query.py``).
    """
    if not inspect.isawaitable(value):
        return value
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(value)

    result: dict[str, Any] = {}
    error: dict[str, BaseException] = {}

    def _run() -> None:
        try:
            result["value"] = asyncio.run(value)
        except BaseException as exc:  # noqa: BLE001 - re-raised on the caller thread
            error["exc"] = exc

    thread = threading.Thread(target=_run)
    thread.start()
    thread.join()
    if error:
        raise error["exc"]
    return result["value"]


def cluster_embeddings(
    embeddings: Sequence[Sequence[float]],
    *,
    k: int | None = None,
    seed: int = CLUSTER_SEED,
    max_iter: int = 25,
) -> list[list[int]]:
    """Cluster ``embeddings`` by cosine similarity (deterministic k-means-style).

    A light, dependency-free k-means over cosine similarity:

    * ``k`` defaults to ``round(sqrt(n))`` and is always clamped to
      ``[1, n]`` and to :data:`MAX_CLUSTERS`.
    * Centroids are seeded with ``random.Random(seed).sample`` over the (sorted)
      input indices, so a fixed ``seed`` and input yield a fixed result.
    * Points are assigned to the most cosine-similar centroid, centroids are the
      member means, and the loop runs until assignments stabilise (or
      ``max_iter``). The final clusters are sorted by their smallest member index.

    With fewer than :data:`MIN_CLUSTER_ITEMS` items a single cluster (or none) is
    returned — clustering too little data is pointless.

    Args:
        embeddings: One equal-length vector per item.
        k: Requested cluster count (``None`` ⇒ heuristic, clamped).
        seed: RNG seed for the deterministic centroid initialisation.
        max_iter: Maximum refinement iterations.

    Returns:
        A list of clusters, each a list of member indices in ascending order.
    """
    n = len(embeddings)
    if n == 0:
        return []
    if n < MIN_CLUSTER_ITEMS:
        return [list(range(n))]

    if k is None:
        k = max(1, int(round(math.sqrt(n))))
    k = max(1, min(int(k), n, MAX_CLUSTERS))
    if k == 1:
        return [list(range(n))]

    rng = random.Random(seed)
    dim = len(embeddings[0])
    centroids = [[float(x) for x in embeddings[i]] for i in rng.sample(range(n), k)]

    assignments: list[int] = [-1] * n
    for _ in range(max(1, int(max_iter))):
        current: list[int] = []
        for vector in embeddings:
            best_index = 0
            best_similarity = -2.0
            for index, centroid in enumerate(centroids):
                similarity = cosine_similarity(vector, centroid)
                if similarity > best_similarity:
                    best_similarity = similarity
                    best_index = index
            current.append(best_index)
        if current == assignments:
            break
        assignments = current
        sums = [[0.0] * dim for _ in range(k)]
        counts = [0] * k
        for item_index, cluster_index in enumerate(assignments):
            counts[cluster_index] += 1
            vector = embeddings[item_index]
            target = sums[cluster_index]
            for position in range(dim):
                target[position] += float(vector[position])
        for cluster_index in range(k):
            if counts[cluster_index]:
                centroids[cluster_index] = [
                    total / counts[cluster_index] for total in sums[cluster_index]
                ]

    groups: dict[int, list[int]] = {}
    for item_index, cluster_index in enumerate(assignments):
        groups.setdefault(cluster_index, []).append(item_index)
    clusters = [members for members in groups.values()]
    clusters.sort(key=lambda members: members[0])
    return clusters


def _advisory_lock_key(tenant: str) -> int:
    """Derive a signed 64-bit advisory-lock key from ``tenant`` (B1).

    ``pg_try_advisory_xact_lock`` takes one ``bigint``; hashing the tenant keeps
    distinct tenants on distinct keys. The value is stable across processes
    (SHA-256 of the tenant, first 8 bytes, big-endian, two's-complement).
    """
    digest = hashlib.sha256(str(tenant).encode("utf-8")).digest()[:8]
    return int.from_bytes(digest, "big", signed=True)


# --------------------------------------------------------------------------- #
# Worker
# --------------------------------------------------------------------------- #


class ReflectWorker:
    """Turns new facts into pending synthesis/skill *candidates* (WP-09).

    Args:
        settings: Configuration source; defaults to :func:`get_settings`.
        llm: LLM client (``chat`` + ``embed``, ``usage()``); injected so tests can
            drive a deterministic fake.
        store: Vector/KV store. Retained for interface parity with the other
            batch lanes; reflect reads facts directly (its own tenant-scoped SQL)
            and writes only candidates/journal rows.
        graph: Bi-temporal graph. Retained for interface parity; reflect does not
            mutate the graph.
        curator: Staging→active gate. Retained for interface parity; reflect must
            **never** promote (that invariant is the whole point of the staging
            gate), so the curator is not invoked here.
        conn_factory: ``(tenant) -> contextmanager[Connection]``; defaults to
            :func:`stratum.db.connection`. Injecting a fake keeps unit tests free
            of a live Postgres.
        clock: Callable returning the current aware-UTC time; injectable for the
            24 h staleness trigger.
        record_synthesis: When ``True`` an extra run-log row is written to the
            ``syntheses`` table (scope, summary, model, ``tokens_used``). Off by
            default, so the Curator stays the only writer of active tables
            (ADR-0005).
        timeout_seconds: §9 wall-clock ceiling for a single run (seconds);
            defaults to :data:`REFLECT_TIMEOUT_SECONDS` (1800 = 30 min).
        monotonic: Monotonic clock used for the §9 timeout check; injectable so a
            test can drive an artificial (tiny) deadline. Defaults to
            :func:`time.monotonic`.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        llm: Any,
        store: Any,
        graph: Any,
        curator: Any,
        conn_factory: Callable[[str], Any] | None = None,
        clock: Clock | None = None,
        record_synthesis: bool = False,
        timeout_seconds: float | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._llm = llm
        self._store = store
        self._graph = graph
        self._curator = curator
        self._connect: Callable[[str], Any] = conn_factory or db.connection
        self._clock: Clock = clock or (lambda: datetime.now(timezone.utc))
        self._record_synthesis = bool(record_synthesis)
        self._timeout_seconds = (
            float(REFLECT_TIMEOUT_SECONDS) if timeout_seconds is None else float(timeout_seconds)
        )
        self._monotonic: Callable[[], float] = monotonic or time.monotonic

    # ----------------------------------------------------------------- budget

    def effective_budget(self, budget_tokens: int | None = None) -> int:
        """Return the effective token ceiling, clamped to ``reflect_max_tokens``.

        ``budget_tokens`` defaults to ``Settings.reflect_budget_tokens`` and is
        always ``max(0, min(requested, Settings.reflect_max_tokens))`` so a caller
        can never raise the hard maximum.
        """
        raw = self.settings.reflect_budget_tokens if budget_tokens is None else budget_tokens
        return max(0, min(int(raw), int(self.settings.reflect_max_tokens)))

    # --------------------------------------------------------------- triggers

    def should_run(self, tenant: str, *, since: datetime | None = None) -> bool:
        """Return ``True`` when a reflect run is due for ``tenant``.

        Due when **either** the number of non-reflect ``memory_events`` since the
        last reflect reaches ``Settings.reflect_events_trigger`` (default 500),
        **or** more than :data:`REFLECT_INTERVAL_HOURS` (24 h) have passed since
        the last reflect. A tenant that has never reflected is always due.

        Args:
            tenant: Tenant id (RLS scope).
            since: Optional explicit watermark; defaults to the latest ``REFLECT``
                event's ``created_at`` (``None`` ⇒ no prior run).
        """
        last = _as_utc(since) if since is not None else self._last_reflect_at(tenant)
        count = self._count_events_since(tenant, last)
        if count >= int(self.settings.reflect_events_trigger):
            return True
        if last is None:
            return True
        return (self._clock() - last) > timedelta(hours=REFLECT_INTERVAL_HOURS)

    def schedule_if_due(self, tenant: str) -> str | None:
        """Enqueue a ``kind='reflect'`` job when :meth:`should_run`, else no-op.

        Returns the new ``job_id`` or ``None`` when not due.
        """
        if not self.should_run(tenant):
            return None
        queue = JobQueue(self.settings, conn_factory=self._connect)
        return queue.enqueue(tenant, "reflect", {})

    def run_scheduler_tick(self, tenant: str) -> str | None:
        """Poll-based cron seam: fire the daily reflect run when due (B2).

        The worker loop calls this each tick (no external crontab). It only
        enqueues when the wall clock has reached :data:`REFLECT_SCHEDULE_HOUR`
        (03:00 UTC) *and* :meth:`should_run` says a run is due. The clock is read
        via ``self._clock()``, so a test can pin it. Returns the new ``job_id``
        when a job was enqueued, else ``None``.
        """
        now = self._clock()
        if now.hour != REFLECT_SCHEDULE_HOUR:
            return None
        return self.schedule_if_due(tenant)

    # ------------------------------------------------------------------- run

    def run(
        self,
        tenant: str,
        *,
        scope: str | None = None,
        budget_tokens: int | None = None,
    ) -> ReflectResult:
        """Run one reflect batch and stage its results as pending candidates.

        Args:
            tenant: Tenant id (RLS scope).
            scope: Optional sub-tree URI (``stratum://…``) restricting the sample
                to facts whose ``source_uri`` is under that prefix.
            budget_tokens: Per-run token ceiling; clamped via
                :meth:`effective_budget`.

        Returns:
            A :class:`ReflectResult`. When another run already holds the lock the
            result is ``skipped=True`` (no LLM call, no write).
        """
        scope_uri = self._resolve_scope(tenant, scope)
        budget = self.effective_budget(budget_tokens)
        worker_id = f"reflect-{uuid.uuid4().hex[:8]}"
        job_id = self._acquire_lock(tenant, scope_uri, worker_id)
        if job_id is None:
            logger.info("reflect: another run is active for tenant %s", tenant)
            return ReflectResult(
                0, 0, 0, False, skipped=True, reason="reflect_already_running",
                budget_tokens=budget,
            )
        try:
            return self._run_locked(tenant, scope_uri, budget)
        finally:
            self._release_lock(tenant, job_id)

    def _run_locked(self, tenant: str, scope_uri: str | None, budget: int) -> ReflectResult:
        """The body of a run, once the single-run lock is held."""
        start = self._monotonic()
        start_tokens = self._llm.usage().total_tokens
        since = self._last_reflect_at(tenant)

        sample = self._load_sample(tenant, scope_uri, since=since, limit=SAMPLE_SIZE)
        items, matrix = self._prepare_items(sample)
        groups = cluster_embeddings(matrix) if len(matrix) >= MIN_CLUSTER_ITEMS else (
            [list(range(len(matrix)))] if matrix else []
        )
        groups = groups[:MAX_CLUSTERS]

        staged = 0
        truncated = False
        summaries: list[dict[str, Any]] = []

        for group in groups:
            if self._deadline_exceeded(start):
                truncated = True
                self._log_timeout_exceeded(tenant, start, scope_uri)
                break

            used = self._llm.usage().total_tokens - start_tokens
            if used >= budget:
                truncated = True
                self._log_budget_exceeded(tenant, used, budget, scope_uri)
                break

            summary = self._summarize([items[index] for index in group])
            used = self._llm.usage().total_tokens - start_tokens
            if used > budget:
                # The cluster's own call crossed the ceiling: abort *before*
                # staging it, so no further call is ever made (no overrun).
                truncated = True
                self._log_budget_exceeded(tenant, used, budget, scope_uri)
                break

            summaries.append(summary)
            staged += self._stage_cluster(tenant, summary, scope_uri, used)

        tokens_used = self._llm.usage().total_tokens - start_tokens
        self._log_reflect(
            tenant, scope_uri, len(groups), staged, tokens_used, truncated, budget
        )
        if self._record_synthesis and summaries:
            self._record_run_synthesis(tenant, scope_uri, summaries, tokens_used)
        return ReflectResult(
            len(groups), staged, tokens_used, truncated, budget_tokens=budget
        )

    # -------------------------------------------------------------- deadline

    def _deadline_exceeded(self, start: float) -> bool:
        """Return ``True`` when ``start + timeout`` has passed (B3, §9)."""
        return (self._monotonic() - start) >= self._timeout_seconds

    # ------------------------------------------------------------- lock (jobs)

    def _acquire_lock(self, tenant: str, scope_uri: str | None, worker_id: str) -> str | None:
        """Atomically claim the single-run lock (B1).

        Two guards, checked on **one connection** so the advisory transaction lock
        lives across both statements:

        1. ``pg_try_advisory_xact_lock(key)`` with a key derived from ``tenant``
           (:func:`_advisory_lock_key`) — **atomic without any schema change**.
           If another transaction holds it, return ``None`` immediately.
        2. The pre-existing ``running`` lock-marker check is kept as a
           defence-in-depth skip: if a stale marker already exists (e.g. a crashed
           run whose advisory lock has since been released), skip cleanly instead
           of inserting a second marker.

        On success one ``running`` reflect row (``payload->>'purpose' =
        'reflect_lock'``) is inserted and its ``job_id`` returned. The advisory
        lock is transaction-scoped and released on commit.

        *Index alternative:* were a partial unique index present instead
        (:data:`REFLECT_LOCK_INDEX_SQL`), the claim could be
        ``INSERT … ON CONFLICT DO NOTHING RETURNING job_id``. WP-09 does not
        require that index.
        """
        key = _advisory_lock_key(tenant)
        with self._connect(tenant) as conn, conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_xact_lock(%s) AS acquired", (key,))
            row = cur.fetchone()
            if row is None or not bool(_row_get(row, "acquired", 0)):
                conn.commit()
                return None

            cur.execute(
                "SELECT 1 FROM jobs "
                "WHERE tenant_id = %s AND kind = 'reflect' AND status = 'running' "
                "AND payload->>'purpose' = %s",
                (tenant, REFLECT_LOCK_PURPOSE),
            )
            if cur.fetchone() is not None:
                conn.commit()
                return None

            payload = Jsonb({"purpose": REFLECT_LOCK_PURPOSE, "scope": scope_uri})
            cur.execute(
                "INSERT INTO jobs "
                "(tenant_id, kind, payload, status, locked_by, locked_at, attempts, max_attempts) "
                "VALUES (%s, 'reflect', %s, 'running', %s, now(), 1, 1) RETURNING job_id",
                (tenant, payload, worker_id),
            )
            inserted = cur.fetchone()
            conn.commit()
        if inserted is None:
            return None
        return str(_row_get(inserted, "job_id", 0))

    def _release_lock(self, tenant: str, job_id: str) -> None:
        """Mark the lock marker ``done`` and release its claim."""
        sql = (
            "UPDATE jobs SET status = 'done', locked_by = NULL, locked_at = NULL "
            "WHERE job_id = %s"
        )
        try:
            with self._connect(tenant) as conn, conn.cursor() as cur:
                cur.execute(sql, (job_id,))
                conn.commit()
        except Exception:  # noqa: BLE001 - a failed release must not mask the result
            logger.exception("reflect: could not release lock job %s", job_id)

    # --------------------------------------------------------------- sampling

    def _resolve_scope(self, tenant: str, scope: str | None) -> str | None:
        """Validate ``scope`` as a ``stratum://`` sub-tree URI for ``tenant``."""
        if scope is None or str(scope).strip() == "":
            return None
        try:
            parsed = parse_uri(str(scope))
        except UriError as exc:
            raise ValueError(f"invalid reflect scope URI: {scope!r}") from exc
        if parsed.tenant != tenant:
            raise ValueError(
                f"scope tenant {parsed.tenant!r} does not match tenant {tenant!r}"
            )
        return parsed.uri

    def _load_sample(
        self,
        tenant: str,
        scope_uri: str | None,
        *,
        since: datetime | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Load a bounded sample of facts changed since the last reflect.

        A scope restricts to facts whose ``source_uri`` is under the prefix; the
        newest ``limit`` facts win, so the batch stays a *sample*, never the whole
        corpus.
        """
        clauses = ["f.tenant_id = %s"]
        params: list[Any] = [tenant]
        if scope_uri:
            # B6: match the scope itself *or* a path segment beneath it, never a
            # sibling that merely shares the prefix (``.../projX`` vs ``.../proj``).
            clauses.append("(f.source_uri = %s OR starts_with(f.source_uri, %s))")
            params.extend([scope_uri, scope_uri + "/"])
        if since is not None:
            clauses.append("f.created_at > %s")
            params.append(since)
        sql = (
            _SAMPLE_SQL + "WHERE " + " AND ".join(clauses)
            + " ORDER BY f.created_at DESC, f.fact_id LIMIT %s"  # B5: stable tiebreaker
        )
        params.append(int(limit))
        with self._connect(tenant) as conn, conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            rows = cur.fetchall()
        return [self._row_to_item(row) for row in rows]

    @staticmethod
    def _row_to_item(row: Any) -> dict[str, Any]:
        fact_id = _row_get(row, "fact_id", 0)
        predicate = _row_get(row, "predicate", 1)
        object_text = _row_get(row, "object_text", 2)
        subject_name = _row_get(row, "subject_name", 3)
        source_uri = _row_get(row, "source_uri", 4)
        embedding_text = _row_get(row, "embedding_text", 5)
        text = " ".join(
            part
            for part in (str(subject_name or ""), str(predicate or ""), str(object_text or ""))
            if part.strip()
        )
        return {
            "fact_id": None if fact_id is None else str(fact_id),
            "uri": source_uri,
            "text": text,
            "embedding": _parse_vector(embedding_text),
            "created_at": _row_get(row, "created_at", 6),
        }

    def _prepare_items(
        self, items: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], list[list[float]]]:
        """Embed any item missing a vector, then return (kept items, matrix).

        Items without a usable text/embedding are dropped so the cosine matrix is
        well-formed. Any embedding call counts against the run budget.
        """
        missing = [item for item in items if item.get("embedding") is None and item.get("text")]
        if missing:
            vectors = _resolve_awaitable(self._llm.embed([item["text"] for item in missing]))
            vectors = list(vectors or [])
            for index, item in enumerate(missing):
                if index < len(vectors):
                    item["embedding"] = [float(x) for x in vectors[index]]

        kept: list[dict[str, Any]] = []
        matrix: list[list[float]] = []
        for item in items:
            vector = item.get("embedding")
            if not vector:
                continue
            kept.append(item)
            matrix.append([float(x) for x in vector])
        return kept, matrix

    # -------------------------------------------------------------- summarize

    def _summarize(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        """Ask the LLM to summarise one cluster (schema-bound JSON)."""
        lines = [f"- {item['text']}" for item in items if item.get("text")]
        messages = [
            {"role": "system", "content": _REFLECT_SYSTEM_PROMPT},
            {"role": "user", "content": "Cluster facts:\n" + "\n".join(lines)},
        ]
        result = _resolve_awaitable(self._llm.chat(messages, json_schema=_CLUSTER_SCHEMA))
        content = getattr(result, "content", result)
        if isinstance(content, str):
            try:
                content = json.loads(content)
            except ValueError:
                content = {}
        if not isinstance(content, Mapping):
            content = {}

        patterns = content.get("patterns") or []
        if not isinstance(patterns, list):
            patterns = [str(patterns)]
        skill = content.get("skill_suggestion")
        model = getattr(result, "model", None) or self.settings.llm_chat_model
        return {
            "summary": str(content.get("summary") or ""),
            "patterns": [str(pattern) for pattern in patterns],
            "skill_suggestion": None if skill in (None, "") else str(skill),
            "cluster_texts": [item.get("text") for item in items],
            "model": model,
        }

    # ---------------------------------------------------------------- staging

    def _stage_cluster(
        self, tenant: str, summary: dict[str, Any], scope_uri: str | None, tokens_used: int
    ) -> int:
        """Write a cluster's synthesis (and optional skill) as pending candidates.

        Never promotes: ``status='pending'`` is the only value written.
        """
        source_uri = scope_uri or self._reflect_uri(tenant)
        staged = 0

        synthesis_payload = {
            "scope_uri": scope_uri,
            "summary": summary["summary"],
            "patterns": summary["patterns"],
            "cluster": summary["cluster_texts"],
            "model": summary["model"],
            "tokens_used": tokens_used,
        }
        self._insert_candidate(
            tenant, kind="synthesis", payload=synthesis_payload, source_uri=source_uri
        )
        staged += 1

        if summary.get("skill_suggestion"):
            skill_payload = {
                "scope_uri": scope_uri,
                "description": summary["skill_suggestion"],
                "name": self._skill_name(summary["summary"]),
                "summary": summary["summary"],
                "model": summary["model"],
                "tokens_used": tokens_used,
            }
            self._insert_candidate(
                tenant, kind="skill", payload=skill_payload, source_uri=source_uri
            )
            staged += 1

        return staged

    def _insert_candidate(
        self, tenant: str, *, kind: str, payload: Mapping[str, Any], source_uri: str
    ) -> str:
        """Insert one ``pending`` staging candidate and return its id."""
        sql = (
            "INSERT INTO staging_candidates "
            "(tenant_id, kind, payload, source_uri, confidence, status) "
            "VALUES (%s, %s, %s, %s, 1.0, 'pending') RETURNING candidate_id"
        )
        with self._connect(tenant) as conn, conn.cursor() as cur:
            cur.execute(sql, (tenant, kind, Jsonb(dict(payload)), source_uri))
            row = cur.fetchone()
            conn.commit()
        if row is None:
            raise RuntimeError("staging_candidates insert returned no row")
        return str(_row_get(row, "candidate_id", 0))

    @staticmethod
    def _skill_name(summary: str) -> str:
        base = (summary or "reflect").strip().lower()
        slug = "".join(ch if ch.isalnum() else "-" for ch in base)[:60].strip("-")
        return slug or "reflect"

    # --------------------------------------------------------------- journal

    def _log_budget_exceeded(
        self, tenant: str, tokens_used: int, budget: int, scope_uri: str | None
    ) -> None:
        """Append a ``BUDGET_EXCEEDED`` row to the append-only journal."""
        after = {
            "tokens_used": int(tokens_used),
            "budget_tokens": int(budget),
            "scope": scope_uri,
        }
        self._insert_event(tenant, BUDGET_EXCEEDED_OP, scope_uri, after)

    def _log_timeout_exceeded(
        self, tenant: str, start: float, scope_uri: str | None
    ) -> None:
        """Append a ``TIMEOUT_EXCEEDED`` row (B3, §9 30-min abort)."""
        after = {
            "elapsed_seconds": float(self._monotonic() - start),
            "timeout_seconds": float(self._timeout_seconds),
            "scope": scope_uri,
        }
        self._insert_event(tenant, TIMEOUT_EXCEEDED_OP, scope_uri, after)

    def _log_reflect(
        self,
        tenant: str,
        scope_uri: str | None,
        clusters: int,
        staged: int,
        tokens_used: int,
        truncated: bool,
        budget: int,
    ) -> None:
        """Append a ``REFLECT`` row (also the watermark for :meth:`should_run`)."""
        after = {
            "scope": scope_uri,
            "clusters": int(clusters),
            "staged_candidates": int(staged),
            "tokens_used": int(tokens_used),
            "truncated": bool(truncated),
            "budget_tokens": int(budget),
        }
        self._insert_event(tenant, REFLECT_OP, scope_uri, after)

    def _insert_event(
        self, tenant: str, op: str, target_uri: str | None, after: Mapping[str, Any]
    ) -> str:
        sql = (
            "INSERT INTO memory_events (tenant_id, op, target_uri, before, after, actor) "
            "VALUES (%s, %s, %s, NULL, %s, %s) RETURNING event_id"
        )
        with self._connect(tenant) as conn, conn.cursor() as cur:
            cur.execute(sql, (tenant, op, target_uri, Jsonb(dict(after)), "reflect"))
            row = cur.fetchone()
            conn.commit()
        if row is None:
            raise RuntimeError("memory_events insert returned no row")
        return str(_row_get(row, "event_id", 0))

    def _record_run_synthesis(
        self,
        tenant: str,
        scope_uri: str | None,
        summaries: list[dict[str, Any]],
        tokens_used: int,
    ) -> None:
        """Optionally log the run summary to ``syntheses`` (off by default)."""
        summary_text = "\n\n".join(s["summary"] for s in summaries if s.get("summary"))
        payload = {"summaries": summaries, "scope": scope_uri}
        sql = (
            "INSERT INTO syntheses (tenant_id, scope_uri, summary, payload, model, tokens_used) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING synthesis_id"
        )
        model = summaries[0].get("model") if summaries else self.settings.llm_chat_model
        with self._connect(tenant) as conn, conn.cursor() as cur:
            cur.execute(
                sql,
                (tenant, scope_uri, summary_text, Jsonb(payload), model, int(tokens_used)),
            )
            conn.commit()

    # ------------------------------------------------------------- watermark

    def _last_reflect_at(self, tenant: str) -> datetime | None:
        """Return the ``created_at`` of the newest ``REFLECT`` event, or ``None``."""
        sql = (
            "SELECT max(created_at) AS last_reflect FROM memory_events "
            "WHERE tenant_id = %s AND op = 'REFLECT'"
        )
        with self._connect(tenant) as conn, conn.cursor() as cur:
            cur.execute(sql, (tenant,))
            row = cur.fetchone()
        if row is None:
            return None
        return _as_utc(_row_get(row, "last_reflect", 0))

    def _count_events_since(self, tenant: str, since: datetime | None) -> int:
        """Count non-``REFLECT`` ``memory_events`` newer than ``since``."""
        if since is None:
            sql = (
                "SELECT count(*) AS n FROM memory_events "
                "WHERE tenant_id = %s AND op <> 'REFLECT'"
            )
            params: tuple[Any, ...] = (tenant,)
        else:
            sql = (
                "SELECT count(*) AS n FROM memory_events "
                "WHERE tenant_id = %s AND op <> 'REFLECT' AND created_at > %s"
            )
            params = (tenant, since)
        with self._connect(tenant) as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
        if row is None:
            return 0
        return int(_row_get(row, "n", 0) or 0)

    @staticmethod
    def _reflect_uri(tenant: str) -> str:
        """Deterministic provenance URI for reflect-derived candidates."""
        return f"stratum://syntheses/{tenant}/reflect"
