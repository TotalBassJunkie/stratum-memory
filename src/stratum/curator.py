"""Staging → Aktivbestand Curator-Gate (WP-08).

The Curator is the **only** bridge from ``staging_candidates`` into the active
tables (``facts``/``entities``/``files``/``syntheses`` and the ``uri_registry``).
Nothing else may promote a candidate (ADR-0005, MASTER-PLAN §8).

Every candidate is checked against **seven criteria**; all must hold:

1. **Dedup** — the cosine similarity to the nearest active fact decides the
   ``ADD``/``UPDATE``/``NOOP``/``DELETE`` action. ``ADD`` is forbidden at/above
   ``Settings.dedup_threshold`` (0.92): a candidate that still intends ``ADD``
   is rejected as ``duplicate``. A fact candidate that carries **no**
   ``embedding`` cannot be deduplicated at all, so it is rejected as
   ``no_embedding`` rather than waved through unchecked (B1: no unchecked fact
   may reach the active store).
2. **Contradiction** — an open fact with the same ``(subject, predicate)`` but an
   incompatible object is a contradiction *unless* the candidate intends an
   ``UPDATE``/``DELETE`` (then the old fact is closed by close-before-insert).
   The check **must** run: the subject entity is resolved from
   ``subject_entity_id`` or its canonical URI/name, and a candidate whose object
   value cannot be determined — or whose subject entity cannot be resolved — is
   rejected as ``contradiction_unverifiable`` (B1: never silently skip it).
3. **Confidence** — ``confidence >= Settings.min_confidence`` (0.6).
4. **Provenance** — ``source_uri`` set and non-empty.
5. **Safety** — no obvious secrets (API keys, private-key headers, passwords) and
   no prompt-injection markers in the payload.
6. **Tenant** — the candidate's ``tenant_id`` equals the auth tenant (or the
   caller is admin).
7. **Time consistency** — ``valid_from <= valid_to`` (if set) and ``valid_from``
   not more than one year in the future.

Failures are reported as a :class:`Verdict` with a stable ``reason`` token and no
write happens; :meth:`Curator.promote` persists approved candidates and appends a
``memory_events`` row + a ``uri_registry`` row for every decision.

All database access goes through the injectable ``conn_factory``
(``stratum.db.connection`` by default); :class:`~stratum.vector.MemoryStore` and
:class:`~stratum.graph.TemporalGraph` are injected for the dedup/contradiction/
entity/fact work so the unit tests run without a live Postgres.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, ContextManager

from psycopg.types.json import Jsonb

from . import db
from .config import Settings, get_settings
from .graph import TemporalGraph, normalize_utc
from .uri import UriError, parse_uri
from .vector import MemoryStore

__all__ = ["Curator", "Verdict"]

#: A ``valid_from`` further than this into the future is a time-consistency error.
_ONE_YEAR = timedelta(days=365)

#: Reduce an arbitrary label to a URI-safe segment (``[a-z0-9._~-]``).
_SLUG_RE = re.compile(r"[^a-z0-9._~-]+")

#: Obvious secrets / prompt-injection markers (criterion 5). Deliberately narrow
#: so ordinary prose does not trip the gate.
_UNSAFE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Private-key / certificate headers.
    re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----"),
    # AWS access key id.
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    # OpenAI-style / generic prefixed API keys (sk-, pk-, rk-).
    re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}\b"),
    # GitHub tokens.
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    # Slack tokens.
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    # Google API keys.
    re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    # key: value / key = value secrets.
    re.compile(
        r"(?i)\b(?:password|passwd|pwd|api[_-]?key|secret|access[_-]?token|"
        r"client[_-]?secret)\b\s*[:=]\s*\S"
    ),
    # Prompt-injection markers.
    re.compile(r"(?i)ignore\s+(?:all\s+)?(?:previous|prior|above)\s+instructions"),
    re.compile(r"(?i)disregard\s+(?:all\s+)?(?:previous|prior|above)"),
    re.compile(r"(?im)^\s*(?:system|assistant)\s*:"),
)

#: Default action per candidate kind when no dedup classifier runs.
_DEFAULT_ACTION: dict[str, str] = {
    "fact": "ADD",
    "entity": "PROMOTE",
    "doc": "PROMOTE",
    "skill": "PROMOTE",
    "synthesis": "PROMOTE",
}

#: ``memory_events.op`` values that mirror the dedup action directly.
_ACTION_OPS = frozenset({"ADD", "UPDATE", "NOOP", "DELETE"})


@dataclass(frozen=True)
class Verdict:
    """Outcome of :meth:`Curator.validate` for a single candidate.

    Attributes:
        approved: Whether the candidate may be promoted.
        reason: Stable rejection token (``duplicate``, ``no_embedding``,
            ``contradiction``, ``contradiction_unverifiable``,
            ``low_confidence``, ``no_provenance``, ``unsafe``,
            ``tenant_mismatch``, ``invalid_time``) or ``None`` when approved.
        action: The dedup/gate action (``ADD``/``UPDATE``/``NOOP``/``DELETE``/
            ``PROMOTE``).
    """

    approved: bool
    reason: str | None
    action: str


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _slug(value: Any) -> str:
    """Reduce ``value`` to a URI-safe segment, never empty."""
    text = str(value or "").strip().lower()
    slug = _SLUG_RE.sub("-", text).strip("-")
    return slug or "unknown"


def _vector_literal(embedding: Any) -> str | None:
    """Render an embedding as the pgvector text literal ``[a,b,c]``."""
    if embedding is None:
        return None
    return "[" + ",".join(repr(float(x)) for x in embedding) + "]"


def _as_dict(row: Any) -> dict[str, Any]:
    """Normalise a row (dict / mapping) to a plain dict."""
    if row is None:
        return {}
    if isinstance(row, Mapping):
        return dict(row)
    raise TypeError(f"expected a mapping row, got {type(row)!r}")


def _payload(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Return the candidate's payload as a dict.

    Accepts a nested ``payload`` mapping, a JSON string, or a candidate that *is*
    the payload (no ``payload`` key) so callers may pass either shape.
    """
    raw = candidate.get("payload")
    if isinstance(raw, Mapping):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {k: v for k, v in candidate.items() if k != "payload"}


def _lookup(candidate: Mapping[str, Any], payload: Mapping[str, Any], key: str) -> Any:
    """Read ``key`` from the payload first, then the candidate row."""
    value = payload.get(key)
    if value is not None:
        return value
    return candidate.get(key)


def _collect_strings(value: Any, out: list[str]) -> None:
    """Recursively collect every string in a JSON-like structure."""
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, Mapping):
        for item in value.values():
            _collect_strings(item, out)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            _collect_strings(item, out)


def _scan_text(candidate: Mapping[str, Any]) -> str:
    """Concatenate every string a candidate carries, for the safety scan."""
    parts: list[str] = []
    _collect_strings(candidate.get("payload"), parts)
    for key in ("content", "text", "summary", "object_text", "l0", "l1"):
        value = candidate.get(key)
        if isinstance(value, str):
            parts.append(value)
    return "\n".join(parts)


def _contains_unsafe(text: str) -> bool:
    """Return ``True`` when ``text`` matches a secret/injection pattern."""
    return any(pattern.search(text) for pattern in _UNSAFE_PATTERNS)


def _intent(payload: Mapping[str, Any]) -> str | None:
    """Return the candidate's explicitly intended action, upper-cased."""
    raw = payload.get("action") or payload.get("op")
    if isinstance(raw, str) and raw.strip():
        return raw.strip().upper()
    return None


def _object_text(payload: Mapping[str, Any]) -> str | None:
    """Return the fact's object text, honouring the WP-07 payload contract.

    WP-07 stages facts with ``object_text`` (or the raw ``object``); both must
    be accepted so the curator never sees a fact whose object silently vanishes
    (B2). Returns ``None`` when neither key carries a non-empty value.
    """
    value = payload.get("object_text") or payload.get("object")
    if value is None:
        return None
    text = str(value)
    return text if text.strip() else None


def _slug_uri(value: Any) -> str | None:
    """Return a ``stratum://`` URI unchanged, else ``None`` (for URI identity)."""
    if isinstance(value, str) and value.strip().startswith("stratum://"):
        return value.strip()
    return None


def _confidence(candidate: Mapping[str, Any], payload: Mapping[str, Any]) -> float:
    """Return the candidate confidence (1.0 default, matching the schema)."""
    value = _lookup(candidate, payload, "confidence")
    if value is None:
        return 1.0
    return float(value)


def _time_violation(payload: Mapping[str, Any], *, now: datetime | None = None) -> bool:
    """Return ``True`` when the payload's temporal window is inconsistent."""
    try:
        valid_from = normalize_utc(payload.get("valid_from"))
        valid_to = normalize_utc(payload.get("valid_to"))
    except (ValueError, TypeError):
        return True
    if valid_from is not None and valid_to is not None and valid_from > valid_to:
        return True
    if valid_from is not None:
        reference = now or datetime.now(timezone.utc)
        if valid_from > reference + _ONE_YEAR:
            return True
    return False


# --------------------------------------------------------------------------- #
# Curator
# --------------------------------------------------------------------------- #


class Curator:
    """Validates and promotes ``staging_candidates`` into the active store.

    Args:
        settings: Configuration source; defaults to :func:`get_settings`.
        store: Vector/dedup store, defaults to :class:`MemoryStore`. Used for
            :meth:`MemoryStore.decide`.
        graph: Temporal graph, defaults to :class:`TemporalGraph` wired to the
            same ``conn_factory``. Used for ``contradiction_check``,
            ``upsert_entity`` and ``add_fact``.
        conn_factory: Callable ``(tenant) -> contextmanager[Connection]`` used
            for the Curator's own persistence (candidate load/status,
            ``memory_events`` and ``uri_registry``). Defaults to
            :func:`stratum.db.connection`; injecting a fake keeps unit tests
            free of a live Postgres.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        store: Any | None = None,
        graph: Any | None = None,
        conn_factory: Callable[[str], ContextManager[Any]] | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._factory: Callable[[str], ContextManager[Any]] = (
            conn_factory or db.connection
        )
        # One connection per ``promote`` decision (B2/M2). ``_connection`` yields
        # the in-flight connection when a unit of work is open, so persistence,
        # the graph write, the event and the status update all share one
        # transaction; a failure rolls the whole decision back (no half-write).
        self._active: ContextVar[Any] = ContextVar("stratum_curator_conn", default=None)
        self._store = store or MemoryStore(self.settings)
        self._graph = graph or TemporalGraph(self.settings, conn_factory=self._connection)
        vec = self.settings.vector_type
        self._vec_type = vec if vec in ("halfvec", "vector") else "halfvec"
        self._max_promotions = int(self.settings.curator_max_promotions_per_run)

    # ----------------------------------------------------------- connections

    @contextmanager
    def _connection(self, tenant: str) -> Any:
        """Yield the in-flight decision connection, else open a fresh one.

        A live unit of work (:meth:`_unit_of_work`) puts its connection into
        ``self._active``; every writer — including the injected
        :class:`~stratum.graph.TemporalGraph`, which shares ``_connection`` as
        its factory — then reuses it, so the whole decision is one transaction.
        """
        active = self._active.get()
        if active is not None:
            yield active
            return
        with self._factory(tenant) as conn:
            yield conn

    @contextmanager
    def _unit_of_work(self, tenant: str) -> Any:
        """Open exactly one connection/transaction for a decision (M2).

        Re-entrant: a nested call inside an already-open unit of work yields the
        same connection without committing early, so helpers can be composed
        freely. The outer ``with self._factory(...)`` commits once on success and
        rolls back on any exception (B2).
        """
        active = self._active.get()
        if active is not None:
            yield active
            return
        with self._factory(tenant) as conn:
            token = self._active.set(conn)
            try:
                yield conn
            finally:
                self._active.reset(token)

    # -------------------------------------------------------------- validation

    def validate(
        self,
        tenant: str,
        candidate: Mapping[str, Any],
        *,
        admin: bool = False,
        now: datetime | None = None,
    ) -> Verdict:
        """Run the seven criteria and return a :class:`Verdict` (no writes).

        Criteria are evaluated in numeric order (1→7); the first failing one
        determines the ``reason``. ``admin=True`` bypasses the tenant criterion.

        Args:
            tenant: Auth tenant (RLS scope). Dedup/contradiction queries always
                run under this tenant, never the candidate's claimed tenant.
            candidate: A candidate row (or a bare payload mapping).
            admin: Trusted caller that may promote cross-tenant candidates.
            now: Injectable reference instant for the future-time check.
        """
        payload = _payload(candidate)
        kind = str(_lookup(candidate, payload, "kind") or "fact")

        # ---- 1. Dedup -------------------------------------------------------
        intended = _intent(payload)
        embedding = payload.get("embedding")
        if kind == "fact":
            if embedding is None:
                # B1: without an embedding the dedup criterion cannot run, so the
                # fact must never be waved through as an ADD into the active
                # store. Reject it as an un-deduplicable candidate.
                return Verdict(False, "no_embedding", intended or "ADD")
            decision = self._store.decide(
                tenant,
                candidate=payload,
                embedding=embedding,
                threshold=self.settings.dedup_threshold,
            )
            if intended == "ADD":
                if decision != "ADD":
                    # ≥ threshold similarity forbids ADD (ADR-0005).
                    return Verdict(False, "duplicate", decision)
                action = "ADD"
            elif intended in ("UPDATE", "DELETE"):
                # M1: an explicit UPDATE/DELETE write intent wins over the
                # classifier — a DELETE must never be inverted into a new active
                # fact, and an UPDATE closes-then-inserts.
                action = intended
            else:
                action = decision
        else:
            action = intended or _DEFAULT_ACTION.get(kind, "PROMOTE")

        # ---- 2. Contradiction ----------------------------------------------
        if kind == "fact" and action not in ("UPDATE", "DELETE"):
            predicate = payload.get("predicate")
            object_value = payload.get("object_entity_id") or _object_text(payload)
            if object_value is None:
                # B1: an object value that cannot be determined makes the
                # contradiction check impossible → refuse, never silently skip.
                return Verdict(False, "contradiction_unverifiable", action)
            subject_entity_id = self._resolve_subject_id(tenant, payload)
            if subject_entity_id:
                if not predicate:
                    # A resolvable subject without a predicate cannot be checked
                    # either — refuse rather than skip the criterion.
                    return Verdict(False, "contradiction_unverifiable", action)
                # The check is always run when the subject resolves; an
                # unresolvable subject has no open fact and cannot contradict.
                check = self._graph.contradiction_check(
                    tenant,
                    subject_entity_id=subject_entity_id,
                    predicate=predicate,
                    object_value=object_value,
                )
                if check.get("contradiction"):
                    return Verdict(False, "contradiction", action)

        # ---- 3. Confidence --------------------------------------------------
        if _confidence(candidate, payload) < self.settings.min_confidence:
            return Verdict(False, "low_confidence", action)

        # ---- 4. Provenance --------------------------------------------------
        source_uri = _lookup(candidate, payload, "source_uri")
        if not source_uri or not str(source_uri).strip():
            return Verdict(False, "no_provenance", action)

        # ---- 5. Safety ------------------------------------------------------
        if _contains_unsafe(_scan_text(candidate)):
            return Verdict(False, "unsafe", action)

        # ---- 6. Tenant ------------------------------------------------------
        candidate_tenant = _lookup(candidate, payload, "tenant_id")
        if not admin and candidate_tenant != tenant:
            return Verdict(False, "tenant_mismatch", action)

        # ---- 7. Time consistency -------------------------------------------
        if _time_violation(payload, now=now):
            return Verdict(False, "invalid_time", action)

        return Verdict(True, None, action)

    # --------------------------------------------------------------- promote

    def promote(
        self,
        tenant: str,
        candidate_id: str,
        *,
        actor: str | None = None,
        admin: bool = False,
    ) -> dict[str, Any]:
        """Validate and, when approved, persist a single candidate.

        Returns a dict with ``status``, ``action``, ``uri``, ``event_id`` and
        ``reason``. Already-decided candidates are returned unchanged
        (idempotent): promoting an ``approved`` candidate a second time emits no
        new ``memory_events`` row and writes nothing.

        **RLS scope (B15):** persistence always runs on ``self._factory(tenant)``
        — the app role and its per-tenant RLS context — even when ``admin=True``.
        The ``admin`` flag loosens exactly one thing: the candidate *lookup*
        (:meth:`_load_candidate`) may find a row whose ``tenant_id`` differs from
        ``tenant``, so a trusted cross-tenant promote can reach it. The write
        then still happens under the ``tenant`` scope the caller named, which is
        why an admin caller must pass the **target** tenant as ``tenant`` (not
        the admin's own tenant). No ``bypass_rls`` connection is ever opened
        here, by design: cross-tenant reads are the only relaxation.
        """
        # B2/M2: the whole decision — candidate load, status, persistence, the
        # graph write, the event and the uri_registry row — shares exactly one
        # connection/transaction. Any failure rolls everything back, so a crash
        # can never leave a committed ``upsert_entity``/``add_fact`` behind a
        # half-written decision.
        with self._unit_of_work(tenant):
            candidate = self._load_candidate(tenant, candidate_id, admin=admin)
            if candidate is None:
                raise LookupError(f"candidate not found: {candidate_id}")

            status = candidate.get("status")
            if status == "approved":
                return {
                    "status": "approved",
                    "action": "NOOP",
                    "uri": None,
                    "event_id": None,
                    "reason": None,
                    "idempotent": True,
                }
            if status == "rejected":
                return {
                    "status": "rejected",
                    "action": "NOOP",
                    "uri": None,
                    "event_id": None,
                    "reason": candidate.get("reason"),
                    "idempotent": True,
                }

            verdict = self.validate(tenant, candidate, admin=admin)
            if not verdict.approved:
                self._set_status(tenant, candidate_id, "rejected", verdict.reason)
                return {
                    "status": "rejected",
                    "action": verdict.action,
                    "uri": None,
                    "event_id": None,
                    "reason": verdict.reason,
                }

            if verdict.action == "NOOP":
                # ADR-0005: NOOP leaves the stored fact untouched — the candidate
                # is a restatement of an already-active fact, so no active row is
                # written and no ref_id may be invented (m1: the registry entry's
                # existing ref_id is preserved, never nulled).
                uri = self._existing_uri(tenant, candidate)
                ref_id = None
            else:
                uri, ref_id = self._persist(tenant, candidate, verdict.action)
            event_id = self._log_event(
                tenant, candidate, action=verdict.action, uri=uri, actor=actor
            )
            self._register_uri(tenant, uri, candidate, ref_id)
            self._set_status(tenant, candidate_id, "approved", None)
            return {
                "status": "approved",
                "action": verdict.action,
                "uri": uri,
                "event_id": event_id,
                "reason": None,
            }

    def promote_batch(
        self, tenant: str, *, limit: int | None = None
    ) -> dict[str, int]:
        """Promote up to ``limit`` pending candidates (avalanche protection).

        ``limit`` defaults to ``Settings.curator_max_promotions_per_run`` (200)
        and is always clamped to that ceiling (m4), so a caller passing a larger
        ``limit`` cannot exceed the configured per-run cap.
        """
        effective = self._max_promotions if limit is None else int(limit)
        if effective < 0:
            raise ValueError(f"limit must be >= 0, got {effective!r}")
        effective = min(effective, self._max_promotions)
        if effective == 0:
            return {"processed": 0, "approved": 0, "rejected": 0, "limit": 0}

        rows = self._pending_candidates(tenant, effective)[:effective]
        approved = rejected = 0
        for row in rows:
            candidate_id = str(_as_dict(row).get("candidate_id"))
            result = self.promote(tenant, candidate_id)
            if result["status"] == "approved":
                approved += 1
            else:
                rejected += 1
        return {
            "processed": approved + rejected,
            "approved": approved,
            "rejected": rejected,
            "limit": effective,
        }

    # ------------------------------------------------------------- persistence

    def _existing_uri(self, tenant: str, candidate: Mapping[str, Any]) -> str:
        """Return the URI a NOOP candidate refers to (no write happens)."""
        payload = _payload(candidate)
        if payload.get("uri"):
            return str(payload["uri"])
        kind = str(candidate.get("kind") or "fact")
        if kind == "entity":
            return payload.get("canonical_uri") or _entity_uri(
                tenant, payload.get("name") or payload.get("subject")
            )
        if kind == "synthesis":
            return _synthesis_uri(tenant, payload)
        if kind in ("doc", "skill"):
            return _doc_uri(tenant, payload)
        return _fact_uri(tenant, payload)

    def _load_candidate(
        self, tenant: str, candidate_id: str, *, admin: bool = False
    ) -> dict[str, Any] | None:
        """Load one staging candidate.

        ``admin=False`` scopes the lookup to the auth tenant (RLS). ``admin=True``
        loosens the tenant filter (m5) so a trusted cross-tenant promote can load
        a candidate that belongs to another tenant — otherwise the admin branch in
        :meth:`promote` could never be reached.
        """
        if admin:
            sql = (
                "SELECT candidate_id, tenant_id, kind, payload, source_uri, confidence, "
                "status, reason, ingest_id, created_at "
                "FROM staging_candidates "
                "WHERE candidate_id = %s"
            )
            params: tuple[Any, ...] = (candidate_id,)
        else:
            sql = (
                "SELECT candidate_id, tenant_id, kind, payload, source_uri, confidence, "
                "status, reason, ingest_id, created_at "
                "FROM staging_candidates "
                "WHERE tenant_id = %s AND candidate_id = %s"
            )
            params = (tenant, candidate_id)
        with self._connection(tenant) as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
        return _as_dict(row) if row is not None else None

    def _resolve_subject_id(self, tenant: str, payload: Mapping[str, Any]) -> str | None:
        """Resolve a fact candidate's subject entity id for criterion 2 (B1).

        Resolution mirrors :meth:`_persist` so the identity checked is the one
        the fact would be written under, but it only ever *returns a lookup
        result* — never a synthetic URI (``facts.subject_entity_id`` is a
        ``uuid`` column, so a URI would be an invalid parameter):

        1. an explicit ``subject_entity_id`` — used verbatim;
        2. an injectable ``resolve_subject`` hook on the graph (tests/adapters);
        3. a lookup in ``entities`` by the canonical URI, which is the
           ``subject_uri``/``canonical_uri`` if present, else the deterministic
           entity URI derived from the subject name (the same key
           :meth:`_persist` would upsert).

        Returns ``None`` when no entity exists for the subject — with no entity
        there can be no open fact, so no contradiction is possible.
        """
        explicit = payload.get("subject_entity_id")
        if explicit:
            return str(explicit)
        canonical = _slug_uri(payload.get("subject_uri")) or _slug_uri(
            payload.get("canonical_uri")
        )
        subject = payload.get("subject") or payload.get("name")
        if not canonical and subject:
            canonical = _entity_uri(tenant, subject)
        resolver = getattr(self._graph, "resolve_subject", None)
        if resolver is not None:
            resolved = resolver(tenant, subject_uri=canonical, subject=subject)
            if resolved:
                return str(resolved)
        if not canonical:
            return None
        sql = (
            "SELECT entity_id FROM entities "
            "WHERE tenant_id = %s AND canonical_uri = %s LIMIT 1"
        )
        with self._connection(tenant) as conn, conn.cursor() as cur:
            cur.execute(sql, (tenant, canonical))
            row = cur.fetchone()
        return str(_as_dict(row).get("entity_id")) if row is not None else None

    def _pending_candidates(self, tenant: str, limit: int) -> list[Any]:
        sql = (
            "SELECT candidate_id FROM staging_candidates "
            "WHERE tenant_id = %s AND status = 'pending' "
            "ORDER BY created_at LIMIT %s"
        )
        with self._connection(tenant) as conn, conn.cursor() as cur:
            cur.execute(sql, (tenant, limit))
            rows = cur.fetchall()
        return list(rows)

    def _set_status(
        self, tenant: str, candidate_id: str, status: str, reason: str | None
    ) -> None:
        sql = (
            "UPDATE staging_candidates SET status = %s, reason = %s "
            "WHERE tenant_id = %s AND candidate_id = %s"
        )
        with self._connection(tenant) as conn, conn.cursor() as cur:
            cur.execute(sql, (status, reason, tenant, candidate_id))

    def _log_event(
        self,
        tenant: str,
        candidate: Mapping[str, Any],
        *,
        action: str,
        uri: str | None,
        actor: str | None,
    ) -> str:
        op = action if action in _ACTION_OPS else "PROMOTE"
        after = {
            "candidate_id": candidate.get("candidate_id"),
            "kind": candidate.get("kind"),
            "action": action,
        }
        sql = (
            "INSERT INTO memory_events (tenant_id, op, target_uri, before, after, actor) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING event_id"
        )
        params = (tenant, op, uri, None, Jsonb(after), actor)
        with self._connection(tenant) as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
        if row is None:
            raise RuntimeError("memory_events insert returned no row")
        return str(_as_dict(row).get("event_id"))

    def _register_uri(
        self,
        tenant: str,
        uri: str | None,
        candidate: Mapping[str, Any],
        ref_id: str | None,
    ) -> None:
        if not uri:
            return
        try:
            bereich = parse_uri(uri).bereich
        except UriError:
            return
        kind = str(candidate.get("kind") or "doc")
        sql = (
            "INSERT INTO uri_registry (uri, bereich, tenant, kind, ref_id) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (uri) DO UPDATE SET bereich = EXCLUDED.bereich, "
            "tenant = EXCLUDED.tenant, kind = EXCLUDED.kind, "
            # m1: a NOOP candidate carries no ref_id; keep the registry's
            # existing pointer instead of nulling it out.
            "ref_id = COALESCE(EXCLUDED.ref_id, uri_registry.ref_id)"
        )
        with self._connection(tenant) as conn, conn.cursor() as cur:
            cur.execute(sql, (uri, bereich, tenant, kind, ref_id))

    def _persist(
        self, tenant: str, candidate: Mapping[str, Any], action: str
    ) -> tuple[str, str | None]:
        """Write an approved candidate into its active table.

        Returns ``(uri, ref_id)``. For facts the entity is resolved/created and
        :meth:`TemporalGraph.add_fact` performs close-before-insert, so an
        ``UPDATE`` closes the previous open fact in the same transaction before
        the new row is inserted (ADR-0004). A ``DELETE`` closes the open fact and
        inserts nothing (M1) — it must never fabricate a new active fact.
        """
        kind = str(candidate.get("kind") or "fact")
        payload = _payload(candidate)
        source_uri = _lookup(candidate, payload, "source_uri")

        if kind == "fact":
            subject_entity_id = payload.get("subject_entity_id")
            if not subject_entity_id:
                subject_entity_id = self._graph.upsert_entity(
                    tenant,
                    name=payload.get("subject") or payload.get("name") or "unknown",
                    canonical_uri=payload.get("subject_uri")
                    or _entity_uri(tenant, payload.get("subject")),
                    type=payload.get("subject_type") or "unknown",
                    aliases=payload.get("aliases"),
                    embedding=payload.get("subject_embedding"),
                )
            uri = payload.get("uri") or _fact_uri(tenant, payload)
            if action == "DELETE":
                # M1: DELETE closes the open fact for (subject, predicate) and
                # writes no new active row. If there is nothing open to close the
                # decision is a no-op, still journaled as DELETE by the caller.
                self._close_open_fact(
                    tenant,
                    subject_entity_id=str(subject_entity_id),
                    predicate=payload.get("predicate") or "related_to",
                )
                return uri, None
            fact_id = self._graph.add_fact(
                tenant,
                subject_entity_id=subject_entity_id,
                predicate=payload.get("predicate") or "related_to",
                object_text=_object_text(payload),
                object_entity_id=payload.get("object_entity_id"),
                valid_from=payload.get("valid_from"),
                valid_to=payload.get("valid_to"),
                confidence=_confidence(candidate, payload),
                source_uri=source_uri,
                source_span=payload.get("source_span"),
                embedding=payload.get("embedding"),
            )
            return uri, fact_id

        if kind == "entity":
            canonical_uri = (
                payload.get("canonical_uri")
                or payload.get("uri")
                or _entity_uri(tenant, payload.get("name") or payload.get("subject"))
            )
            entity_id = self._graph.upsert_entity(
                tenant,
                name=payload.get("name") or payload.get("subject") or "unknown",
                canonical_uri=canonical_uri,
                type=payload.get("type") or "unknown",
                aliases=payload.get("aliases"),
                embedding=payload.get("embedding"),
            )
            # m2: return the *computed* canonical URI (never ``None``), so the
            # registry row and the event carry the entity's real URI.
            return str(canonical_uri), entity_id

        if kind == "synthesis":
            synthesis_id = self._insert_synthesis(tenant, candidate, payload)
            uri = payload.get("uri") or _synthesis_uri(tenant, payload)
            return uri, synthesis_id

        file_id = self._upsert_file(tenant, candidate, payload)
        uri = payload.get("uri") or _doc_uri(tenant, payload)
        return uri, file_id

    def _close_open_fact(
        self, tenant: str, *, subject_entity_id: str, predicate: str
    ) -> None:
        """Close the open fact for ``(subject, predicate)`` in place (M1).

        Sets ``valid_to``/``tx_to`` so the fact leaves the active set without a
        replacement being inserted. Uses the in-flight decision connection, so
        the close participates in the same transaction as the DELETE event.
        """
        sql = (
            "UPDATE facts SET valid_to = GREATEST(now(), valid_from), tx_to = now() "
            "WHERE tenant_id = %s AND subject_entity_id = %s AND predicate = %s "
            "AND valid_to IS NULL AND tx_to IS NULL"
        )
        with self._connection(tenant) as conn, conn.cursor() as cur:
            cur.execute(sql, (tenant, subject_entity_id, predicate))

    def _upsert_file(
        self,
        tenant: str,
        candidate: Mapping[str, Any],
        payload: Mapping[str, Any],
    ) -> str:
        uri = payload.get("uri") or _doc_uri(tenant, payload)
        path = payload.get("path") or uri
        content_hash = payload.get("content_hash") or ""
        sql = (
            "INSERT INTO files (tenant_id, uri, path, content_hash, l0, l1, "
            "size_bytes, git_commit, embedding) "
            f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::{self._vec_type}) "
            "ON CONFLICT (tenant_id, uri) DO UPDATE SET path = EXCLUDED.path, "
            "content_hash = EXCLUDED.content_hash, l0 = EXCLUDED.l0, "
            "l1 = EXCLUDED.l1, size_bytes = EXCLUDED.size_bytes, "
            "git_commit = EXCLUDED.git_commit, "
            "embedding = COALESCE(EXCLUDED.embedding, files.embedding), "
            "updated_at = now() RETURNING file_id"
        )
        params = (
            tenant,
            uri,
            path,
            content_hash,
            payload.get("l0"),
            payload.get("l1"),
            int(payload.get("size_bytes") or 0),
            payload.get("git_commit"),
            _vector_literal(payload.get("embedding")),
        )
        with self._connection(tenant) as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
        if row is None:
            raise RuntimeError("files upsert returned no row")
        return str(_as_dict(row).get("file_id"))

    def _insert_synthesis(
        self,
        tenant: str,
        candidate: Mapping[str, Any],
        payload: Mapping[str, Any],
    ) -> str:
        sql = (
            "INSERT INTO syntheses (tenant_id, scope_uri, summary, payload, model, "
            "tokens_used) VALUES (%s, %s, %s, %s, %s, %s) RETURNING synthesis_id"
        )
        params = (
            tenant,
            payload.get("scope_uri"),
            payload.get("summary") or "",
            Jsonb(dict(payload)),
            payload.get("model"),
            int(payload.get("tokens_used") or 0),
        )
        with self._connection(tenant) as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
        if row is None:
            raise RuntimeError("syntheses insert returned no row")
        return str(_as_dict(row).get("synthesis_id"))


# --------------------------------------------------------------------------- #
# URI builders
# --------------------------------------------------------------------------- #


def _entity_uri(tenant: str, subject: Any) -> str:
    return f"stratum://entities/{tenant}/{_slug(subject)}"


def _fact_uri(tenant: str, payload: Mapping[str, Any]) -> str:
    subject = payload.get("subject") or payload.get("name") or "unknown"
    predicate = payload.get("predicate") or "related"
    return f"stratum://facts/{tenant}/{_slug(subject)}/{_slug(predicate)}"


def _doc_uri(tenant: str, payload: Mapping[str, Any]) -> str:
    path = payload.get("path") or payload.get("title") or "document"
    return f"stratum://docs/{tenant}/{_slug(path)}"


def _synthesis_uri(tenant: str, payload: Mapping[str, Any]) -> str:
    scope = payload.get("scope_uri") or payload.get("scope") or "synthesis"
    return f"stratum://syntheses/{tenant}/{_slug(scope)}"
