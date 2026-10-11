"""Bi-temporal temporal-graph layer (WP-06).

This module is the only place that reads/writes the ``entities``, ``facts`` and
``edges`` tables with bi-temporal semantics. It implements the invariants of
:doc:`ADR-0004 <../docs/adr/0004-bitemporal-invariants>`:

* **close-before-insert** — before a new fact is inserted for a given
  ``(tenant_id, subject_entity_id, predicate)``, any still-open fact for that
  key is closed (``valid_to`` / ``tx_to``) first. The partial unique index
  ``facts_one_open_per_pred_idx`` is checked at statement level, so an
  ``INSERT`` before the closing ``UPDATE`` would abort the transaction.
* **At most one open fact** per ``(tenant_id, subject_entity_id, predicate)``.

All SQL goes through :func:`stratum.db.connection` (RLS-scoped, app role),
which configures ``dict_row`` as the row factory.

Scope note: :class:`TemporalGraph` writes **only** the ``entities``, ``facts``
and ``edges`` tables. The append-only ``memory_events`` journal and the
``uri_registry`` URI catalogue are written by the Curator/Promotion layer
(WP-08), not here — this layer must stay free of side effects beyond those three
tables.

Timestamps accept ``str`` (ISO-8601, including a trailing ``Z``) or
:class:`datetime.datetime` and are normalised to timezone-aware UTC.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Callable, ContextManager, Iterable, Sequence

from psycopg.types.json import Jsonb

from . import db
from .config import Settings, get_settings

__all__ = ["TemporalGraph", "normalize_utc"]

_VEC_TYPES = ("halfvec", "vector")

# Columns returned by ``get_facts`` (explicit, so the dict shape is stable).
_FACT_COLUMNS = (
    "fact_id, tenant_id, subject_entity_id, predicate, object_text, "
    "object_entity_id, valid_from, valid_to, tx_from, tx_to, confidence, "
    "source_uri, source_span, superseded_by"
)


def normalize_utc(value: str | datetime | None) -> datetime | None:
    """Normalise ``value`` to a timezone-aware UTC :class:`datetime`.

    ``None`` stays ``None``. Naive datetimes are assumed to be UTC. ISO strings
    are parsed with :func:`datetime.datetime.fromisoformat` (a trailing ``Z``
    is accepted).
    """
    if value is None:
        return None
    if isinstance(value, str):
        dt = datetime.fromisoformat(value)
    elif isinstance(value, datetime):
        dt = value
    else:  # pragma: no cover - defensive
        raise TypeError(f"expected str | datetime | None, got {type(value)!r}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _format_embedding(vector: Iterable[float] | None) -> str | None:
    """Render an embedding as the pgvector text literal ``[a,b,c]``."""
    if vector is None:
        return None
    parts = ",".join(repr(float(x)) for x in vector)
    return f"[{parts}]"


def _json_param(value: Any) -> Jsonb | None:
    """Wrap ``value`` for a ``jsonb`` parameter with a psycopg Jsonb adapter.

    Using :class:`psycopg.types.json.Jsonb` gives a typed ``jsonb`` parameter and
    lets psycopg serialise dicts/lists correctly. ``None`` stays ``None``.
    """
    if value is None:
        return None
    return Jsonb(value)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class TemporalGraph:
    """Bi-temporal graph access over the ``entities``/``facts``/``edges`` tables.

    Args:
        settings: Configuration source; defaults to :func:`get_settings`.
            ``settings.vector_type`` selects the vector column cast
            (``halfvec`` on the Pi, ``vector`` on the server).
        conn_factory: Optional callable ``(tenant) -> contextmanager[Connection]``
            used to obtain a connection. Defaults to
            :func:`stratum.db.connection`. Injecting a fake factory is how the
            unit tests record the emitted SQL without a live Postgres.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        conn_factory: Callable[[str], ContextManager[Any]] | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        vec = self._settings.vector_type
        if vec not in _VEC_TYPES:
            raise ValueError(f"vector_type must be one of {_VEC_TYPES}, got {vec!r}")
        self._vec_type = vec
        self._connect: Callable[[str], ContextManager[Any]] = conn_factory or db.connection

    # ----------------------------------------------------------------- entities

    def upsert_entity(
        self,
        tenant: str,
        *,
        name: str,
        canonical_uri: str,
        type: str = "unknown",
        aliases: Sequence[str] | None = None,
        embedding: Iterable[float] | None = None,
    ) -> str:
        """Insert or update an entity identified by ``(tenant, canonical_uri)``.

        On conflict the mutable fields (``name``, ``type``, ``aliases`` and, if a
        new ``embedding`` is given, the embedding) are refreshed. Returns the
        ``entity_id`` (UUID as string).
        """
        sql = f"""
        INSERT INTO entities (tenant_id, name, canonical_uri, type, aliases, embedding)
        VALUES (%s, %s, %s, %s, %s, %s::{self._vec_type})
        ON CONFLICT (tenant_id, canonical_uri) DO UPDATE
            SET name = EXCLUDED.name,
                type = EXCLUDED.type,
                aliases = EXCLUDED.aliases,
                embedding = COALESCE(EXCLUDED.embedding, entities.embedding)
        RETURNING entity_id
        """
        params = (
            tenant,
            name,
            canonical_uri,
            type,
            list(aliases or []),
            _format_embedding(embedding),
        )
        with self._connect(tenant) as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
        if not row:
            raise RuntimeError("upsert_entity returned no row")
        return str(row["entity_id"])

    # -------------------------------------------------------------------- facts

    def add_fact(
        self,
        tenant: str,
        *,
        subject_entity_id: str,
        predicate: str,
        object_text: str | None = None,
        object_entity_id: str | None = None,
        valid_from: str | datetime | None = None,
        valid_to: str | datetime | None = None,
        confidence: float = 1.0,
        source_uri: str | None = None,
        source_span: Any | None = None,
        embedding: Iterable[float] | None = None,
    ) -> str:
        """Add a fact, closing any open fact for ``(subject, predicate)`` first.

        The closing ``UPDATE`` runs **before** the ``INSERT`` (close-before-insert,
        ADR-0004). The closed fact's ``valid_to`` is set to the new fact's
        ``valid_from`` and its ``tx_to`` to ``now()``. Returns the new ``fact_id``.

        Raises:
            ValueError: if neither ``object_text`` nor ``object_entity_id`` is given.
            ValueError: if ``valid_from`` predates the ``valid_from`` of the open
                fact for the same ``(tenant, subject, predicate)``. The schema
                CHECK ``valid_to IS NULL OR valid_from <= valid_to`` would reject
                the reconstructed predecessor otherwise (B6); a clear error beats
                a raw ``CheckViolation``.
        """
        if object_text is None and object_entity_id is None:
            raise ValueError("add_fact requires object_text or object_entity_id")
        vf = normalize_utc(valid_from) or _now()
        vt = normalize_utc(valid_to)

        # B6: the closing UPDATE rewrites the predecessor's valid_to to ``vf``.
        # If ``vf`` predates the predecessor's valid_from the CHECK would fail.
        # Read the predecessor first (same transaction, before the UPDATE) and
        # fail fast with a clear message instead of a raw CheckViolation.
        guard_sql = """
        SELECT valid_from
          FROM facts
         WHERE tenant_id = %s
           AND subject_entity_id = %s
           AND predicate = %s
           AND valid_to IS NULL
           AND tx_to IS NULL
        """
        close_sql = """
        UPDATE facts
           SET valid_to = %s, tx_to = now()
         WHERE tenant_id = %s
           AND subject_entity_id = %s
           AND predicate = %s
           AND valid_to IS NULL
           AND tx_to IS NULL
        """
        insert_sql = f"""
        INSERT INTO facts (tenant_id, subject_entity_id, predicate, object_text,
                           object_entity_id, valid_from, valid_to, confidence,
                           source_uri, source_span, embedding)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::{self._vec_type})
        RETURNING fact_id
        """
        insert_params = (
            tenant,
            subject_entity_id,
            predicate,
            object_text,
            object_entity_id,
            vf,
            vt,
            confidence,
            source_uri,
            _json_param(source_span),
            _format_embedding(embedding),
        )
        with self._connect(tenant) as conn, conn.cursor() as cur:
            cur.execute(guard_sql, (tenant, subject_entity_id, predicate))
            existing = cur.fetchone()
            if existing and vf < existing["valid_from"]:
                raise ValueError(
                    "valid_from predates the open fact's valid_from for "
                    f"(tenant={tenant!r}, subject={subject_entity_id!r}, "
                    f"predicate={predicate!r}): {vf.isoformat()} < "
                    f"{existing['valid_from'].isoformat()}"
                )
            cur.execute(close_sql, (vf, tenant, subject_entity_id, predicate))
            cur.execute(insert_sql, insert_params)
            row = cur.fetchone()
        if not row:
            raise RuntimeError("add_fact returned no row")
        return str(row["fact_id"])

    def correct_fact(
        self,
        tenant: str,
        *,
        old_fact_id: str,
        subject_entity_id: str | None = None,
        predicate: str | None = None,
        object_text: str | None = None,
        object_entity_id: str | None = None,
        valid_from: str | datetime | None = None,
        valid_to: str | datetime | None = None,
        confidence: float = 1.0,
        source_uri: str | None = None,
        source_span: Any | None = None,
        embedding: Iterable[float] | None = None,
    ) -> str:
        """Explicitly correct ``old_fact_id`` with a new fact.

        Within a single transaction: (1) close the old fact
        (``valid_to``, ``tx_to = now()``, ``superseded_by = <new id>``), then
        (2) insert the replacement. The new ``fact_id`` is generated up-front so
        the back-reference can be written in the closing ``UPDATE`` while keeping
        the mandated close-before-insert order.

        ``subject_entity_id`` and ``predicate`` are optional; when omitted they
        are read from the old fact (which must still be open, ``tx_to IS NULL``).

        Raises:
            LookupError: if no open fact with ``old_fact_id`` exists — either it
                is missing or it was already closed. The closing ``UPDATE``'s
                ``rowcount`` is asserted to be exactly ``1``, so a silently
                no-op correction can never insert a replacement (B3).
            ValueError: if neither ``object_text`` nor ``object_entity_id`` is
                given. Raised before any write.
        """
        new_id = str(uuid.uuid4())
        vf = normalize_utc(valid_from) or _now()
        vt = normalize_utc(valid_to)

        close_sql = """
        UPDATE facts
           SET valid_to = %s, tx_to = now(), superseded_by = %s
         WHERE tenant_id = %s
           AND fact_id = %s
           AND tx_to IS NULL
        """
        # Read-back only sees still-open facts, so a closed old fact is reported
        # as missing rather than resurrected.
        read_sql = """
        SELECT subject_entity_id, predicate
          FROM facts
         WHERE tenant_id = %s
           AND fact_id = %s
           AND tx_to IS NULL
        """
        insert_sql = f"""
        INSERT INTO facts (fact_id, tenant_id, subject_entity_id, predicate,
                           object_text, object_entity_id, valid_from, valid_to,
                           confidence, source_uri, source_span, embedding)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::{self._vec_type})
        RETURNING fact_id
        """
        with self._connect(tenant) as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    if subject_entity_id is None or predicate is None:
                        cur.execute(read_sql, (tenant, old_fact_id))
                        current = cur.fetchone()
                        if not current:
                            raise LookupError(f"old fact not found: {old_fact_id}")
                        subject_entity_id = subject_entity_id or str(
                            current["subject_entity_id"]
                        )
                        predicate = predicate or current["predicate"]
                    if object_text is None and object_entity_id is None:
                        raise ValueError(
                            "correct_fact requires object_text or object_entity_id"
                        )
                    cur.execute(close_sql, (vf, new_id, tenant, old_fact_id))
                    if cur.rowcount != 1:
                        raise LookupError(
                            "old fact not open (missing or already closed): "
                            f"{old_fact_id}"
                        )
                    cur.execute(
                        insert_sql,
                        (
                            new_id,
                            tenant,
                            subject_entity_id,
                            predicate,
                            object_text,
                            object_entity_id,
                            vf,
                            vt,
                            confidence,
                            source_uri,
                            _json_param(source_span),
                            _format_embedding(embedding),
                        ),
                    )
                    row = cur.fetchone()
        if not row:
            raise RuntimeError("correct_fact returned no row")
        return str(row["fact_id"])

    def get_facts(
        self,
        tenant: str,
        *,
        subject_entity_id: str | None = None,
        predicate: str | None = None,
        as_of_valid: str | datetime | None = None,
        as_of_tx: str | datetime | None = None,
        include_closed: bool = False,
    ) -> list[dict[str, Any]]:
        """Return facts matching the given filters.

        Args:
            subject_entity_id: Restrict to one subject.
            predicate: Restrict to one predicate.
            as_of_valid: Valid-time axis — keep facts valid *in the world* at
                this instant (``valid_from <= t AND (valid_to IS NULL OR
                valid_to > t)``).
            as_of_tx: Transaction-time axis — keep facts the *system knew* at
                this instant (``tx_from <= t AND (tx_to IS NULL OR tx_to > t)``).
            include_closed: When ``False`` and no ``as_of_*`` is given, only open
                facts (``valid_to IS NULL AND tx_to IS NULL``) are returned.
        """
        where: list[str] = ["tenant_id = %s"]
        params: list[Any] = [tenant]
        if subject_entity_id is not None:
            where.append("subject_entity_id = %s")
            params.append(subject_entity_id)
        if predicate is not None:
            where.append("predicate = %s")
            params.append(predicate)

        tav = normalize_utc(as_of_valid)
        if tav is not None:
            where.append("valid_from <= %s")
            params.append(tav)
            where.append("(valid_to IS NULL OR valid_to > %s)")
            params.append(tav)

        tat = normalize_utc(as_of_tx)
        if tat is not None:
            where.append("tx_from <= %s")
            params.append(tat)
            where.append("(tx_to IS NULL OR tx_to > %s)")
            params.append(tat)

        if not include_closed and tav is None and tat is None:
            where.append("valid_to IS NULL")
            where.append("tx_to IS NULL")

        sql = (
            f"SELECT {_FACT_COLUMNS} FROM facts WHERE "
            + " AND ".join(where)
            + " ORDER BY valid_from DESC, tx_from DESC"
        )
        with self._connect(tenant) as conn, conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            rows = cur.fetchall()
        return [dict(row) for row in rows]

    # -------------------------------------------------------------------- edges

    def add_edge(
        self,
        tenant: str,
        *,
        src_entity_id: str,
        dst_entity_id: str,
        rel_type: str,
        valid_from: str | datetime | None = None,
        valid_to: str | datetime | None = None,
        fact_id: str | None = None,
    ) -> str:
        """Insert a directed, optionally fact-backed edge. Returns ``edge_id``."""
        vf = normalize_utc(valid_from) or _now()
        vt = normalize_utc(valid_to)
        sql = """
        INSERT INTO edges (tenant_id, src_entity_id, dst_entity_id, rel_type,
                           valid_from, valid_to, fact_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        RETURNING edge_id
        """
        params = (tenant, src_entity_id, dst_entity_id, rel_type, vf, vt, fact_id)
        with self._connect(tenant) as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
        if not row:
            raise RuntimeError("add_edge returned no row")
        return str(row["edge_id"])

    def traverse(
        self,
        tenant: str,
        *,
        start_entity_id: str,
        rel_type: str | None = None,
        max_depth: int = 3,
        as_of_valid: str | datetime | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """Breadth-first walk over ``edges`` using a recursive CTE.

        Starts at ``start_entity_id`` and follows outgoing edges up to
        ``max_depth`` hops, optionally filtered to one ``rel_type`` and to edges
        valid at ``as_of_valid``. Returns rows with the traversed edge plus its
        ``depth`` (root edges have depth ``1``).

        A dense or cyclic graph can fan out exponentially in ``max_depth``, so
        the result is capped at ``limit`` rows (default ``1000``, B5). The cap is
        applied **after** ``ORDER BY depth, dst_entity_id``, so the shallowest
        reachable edges are always returned first and the truncation is
        deterministic; deep edges past the cap are dropped.
        """
        if max_depth < 1:
            raise ValueError("max_depth must be >= 1")
        if limit < 1:
            raise ValueError("limit must be >= 1")
        tav = normalize_utc(as_of_valid)

        base_where = ["e.tenant_id = %s", "e.src_entity_id = %s"]
        base_params: list[Any] = [tenant, start_entity_id]
        rec_where = ["e.tenant_id = %s", "r.depth < %s"]
        rec_params: list[Any] = [tenant, max_depth]
        if rel_type is not None:
            base_where.append("e.rel_type = %s")
            base_params.append(rel_type)
            rec_where.append("e.rel_type = %s")
            rec_params.append(rel_type)
        if tav is not None:
            base_where.append("e.valid_from <= %s")
            base_params.append(tav)
            base_where.append("(e.valid_to IS NULL OR e.valid_to > %s)")
            base_params.append(tav)
            rec_where.append("e.valid_from <= %s")
            rec_params.append(tav)
            rec_where.append("(e.valid_to IS NULL OR e.valid_to > %s)")
            rec_params.append(tav)

        sql = f"""
        WITH RECURSIVE reach AS (
            SELECT e.src_entity_id, e.dst_entity_id, e.rel_type, e.fact_id,
                   e.valid_from, e.valid_to, 1 AS depth
              FROM edges e
             WHERE {" AND ".join(base_where)}
            UNION ALL
            SELECT e.src_entity_id, e.dst_entity_id, e.rel_type, e.fact_id,
                   e.valid_from, e.valid_to, r.depth + 1 AS depth
              FROM edges e
              JOIN reach r ON e.src_entity_id = r.dst_entity_id
             WHERE {" AND ".join(rec_where)}
        )
        SELECT * FROM reach ORDER BY depth, dst_entity_id LIMIT %s
        """
        params = base_params + rec_params + [limit]
        with self._connect(tenant) as conn, conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            rows = cur.fetchall()
        return [dict(row) for row in rows]

    # -------------------------------------------------------------- consistency

    def contradiction_check(
        self,
        tenant: str,
        *,
        subject_entity_id: str,
        predicate: str,
        object_value: str | None,
    ) -> dict[str, Any]:
        """Check whether an open fact with a *different* object already exists.

        Used by the Curator to detect conflicting assertions. ``object_value`` is
        matched against ``object_text`` or, when it parses as a UUID, against
        ``object_entity_id``.

        Returns:
            ``{"contradiction": bool, "fact": dict | None}`` where ``fact`` is the
            first conflicting open fact (or ``None``).
        """
        text_val: str | None = None
        entity_val: str | None = None
        if object_value is not None:
            try:
                uuid.UUID(str(object_value))
                entity_val = str(object_value)
            except (ValueError, AttributeError, TypeError):
                text_val = str(object_value)

        # A contradiction is an open fact whose object differs from the asserted
        # one. ``IS DISTINCT FROM`` alone is wrong here: when the asserted object
        # is a text value, ``object_entity_id`` (NULL in that row) IS DISTINCT
        # FROM NULL evaluates to FALSE, so the row would never match. We instead
        # compare on whichever axis the assertion uses, with NULL-safe equality.
        if entity_val is not None:
            object_match = "object_entity_id IS NOT NULL AND object_entity_id IS DISTINCT FROM %s"
        else:
            object_match = "object_text IS NOT NULL AND object_text IS DISTINCT FROM %s"
        sql = f"""
        SELECT fact_id, subject_entity_id, predicate, object_text, object_entity_id,
               valid_from, confidence
          FROM facts
         WHERE tenant_id = %s
           AND subject_entity_id = %s
           AND predicate = %s
           AND valid_to IS NULL
           AND tx_to IS NULL
           AND {object_match}
         ORDER BY valid_from DESC
         LIMIT 1
        """
        params = (tenant, subject_entity_id, predicate, entity_val or text_val)
        with self._connect(tenant) as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
        conflicting = dict(row) if row else None
        return {"contradiction": conflicting is not None, "fact": conflicting}
