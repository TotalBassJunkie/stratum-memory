"""Tests for the bi-temporal graph layer (:mod:`stratum.graph`).

The fast tests use a *recording connection* that captures every SQL statement
and its parameters. This lets us assert the structural invariants without a live
Postgres:

* ``add_fact`` / ``correct_fact`` emit the closing ``UPDATE`` **before** the
  ``INSERT`` (close-before-insert, ADR-0004),
* ``correct_fact`` asserts the closing ``UPDATE`` touched exactly one row and
  raises ``LookupError`` otherwise (missing or already-closed old fact, B3),
* ``add_fact`` rejects a ``valid_from`` predating the open fact's (B6),
* ``as_of_valid`` / ``as_of_tx`` produce the correct WHERE clauses,
* ``traverse`` builds a recursive CTE and caps the result with ``LIMIT`` (B5),
* ``contradiction_check`` builds the expected query.

Real database tests are marked ``integration`` and skipped unless the
``STRATUM_TEST_DB`` environment variable is set (CI provides a Postgres DSN).
"""

from __future__ import annotations

import os
import re
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from stratum.graph import TemporalGraph, normalize_utc

# --------------------------------------------------------------------------- #
# Recording fakes
# --------------------------------------------------------------------------- #


def _norm(sql: str) -> str:
    """Collapse all whitespace so statement classification is stable."""
    return re.sub(r"\s+", " ", sql).strip()


class _NullTransaction:
    """Stand-in for ``psycopg.Connection.transaction()`` (a context manager)."""

    def __enter__(self) -> _NullTransaction:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class RecordingConnection:
    """Fake psycopg connection that records emitted statements.

    ``responses`` is a FIFO of results, one entry consumed per ``execute``.
    ``fetchone`` returns dict entries, ``fetchall`` returns list entries; any
    other entry yields ``None`` / ``[]`` respectively.

    ``on_after_execute`` (if set) is called as ``(sql, params, cursor)`` after
    each ``execute`` so a test can set ``cursor.rowcount`` (the recording cursor
    defaults to ``rowcount = None``, which is *not* ``1`` — so any path that
    asserts ``rowcount == 1`` must set it explicitly).
    """

    def __init__(self, responses: list[Any] | None = None) -> None:
        self.statements: list[tuple[str, tuple]] = []
        self.responses: list[Any] = list(responses or [])
        self.transactions = 0
        self.commits = 0
        self.on_execute: Any = None
        self.on_after_execute: Any = None

    def cursor(self) -> RecordingCursor:
        return RecordingCursor(self)

    def transaction(self) -> _NullTransaction:
        self.transactions += 1
        return _NullTransaction()

    def commit(self) -> None:
        self.commits += 1

    def __enter__(self) -> RecordingConnection:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class RecordingCursor:
    def __init__(self, conn: RecordingConnection) -> None:
        self._conn = conn
        self._last: Any = None
        # ``None`` mirrors psycopg's "unknown" for a statement that has not run.
        # A code path asserting ``rowcount == 1`` must therefore set it.
        self.rowcount: int | None = None

    def execute(self, sql: str, params: tuple | dict = ()) -> None:
        self._conn.statements.append((_norm(sql), params))
        if self._conn.on_execute is not None:
            self._last = self._conn.on_execute(sql, params)
        else:
            self._last = self._conn.responses.pop(0) if self._conn.responses else None
        if self._conn.on_after_execute is not None:
            self._conn.on_after_execute(sql, params, self)

    def fetchone(self) -> Any:
        return self._last if isinstance(self._last, dict) else None

    def fetchall(self) -> Any:
        return self._last if isinstance(self._last, list) else []

    def __enter__(self) -> RecordingCursor:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def _graph(responses: list[Any] | None = None) -> tuple[TemporalGraph, RecordingConnection]:
    """Build a TemporalGraph wired to a fresh recording connection."""
    conn = RecordingConnection(responses)

    def factory(_tenant: str) -> RecordingConnection:
        return conn

    graph = TemporalGraph(conn_factory=factory)
    return graph, conn


def _kinds(conn: RecordingConnection) -> list[str]:
    """Return the leading keyword of each recorded statement, uppercased."""
    out = []
    for sql, _ in conn.statements:
        match = re.match(r"([A-Za-z]+)", sql)
        out.append(match.group(1).upper() if match else "")
    return out


# --------------------------------------------------------------------------- #
# normalize_utc
# --------------------------------------------------------------------------- #


def test_normalize_utc_none():
    assert normalize_utc(None) is None


def test_normalize_utc_naive_datetime_assumed_utc():
    got = normalize_utc(datetime(2026, 1, 2, 3, 4, 5))
    assert got == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert got.tzinfo is not None


def test_normalize_utc_iso_z_suffix():
    got = normalize_utc("2026-01-02T03:04:05Z")
    assert got == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def test_normalize_utc_offset_converted_to_utc():
    got = normalize_utc("2026-01-02T05:04:05+02:00")
    assert got == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert got.utcoffset().total_seconds() == 0


# --------------------------------------------------------------------------- #
# upsert_entity
# --------------------------------------------------------------------------- #


def test_upsert_entity_emits_on_conflict_and_returns_id():
    eid = str(uuid.uuid4())
    graph, conn = _graph(responses=[{"entity_id": eid}])

    result = graph.upsert_entity(
        "default",
        name="Ada",
        canonical_uri="stratum://entities/default/ada",
        type="person",
        aliases=["ada.l"],
    )

    assert result == eid
    assert len(conn.statements) == 1
    sql, params = conn.statements[0]
    assert sql.upper().startswith("INSERT INTO ENTITIES")
    assert "ON CONFLICT (TENANT_ID, CANONICAL_URI) DO UPDATE" in sql.upper()
    assert params[0] == "default"
    assert params[1] == "Ada"
    assert params[3] == "person"
    assert params[4] == ["ada.l"]
    assert params[5] is None  # no embedding
    # vector cast uses the configured type
    assert "::halfvec" in sql


# --------------------------------------------------------------------------- #
# add_fact: close-before-insert
# --------------------------------------------------------------------------- #


def test_add_fact_closes_before_insert():
    fid = str(uuid.uuid4())
    graph, conn = _graph(responses=[None, None, {"fact_id": fid}])

    result = graph.add_fact(
        "default",
        subject_entity_id=str(uuid.uuid4()),
        predicate="plan",
        object_text="pro",
        valid_from="2026-01-01T00:00:00Z",
    )

    assert result == fid
    assert _kinds(conn) == ["SELECT", "UPDATE", "INSERT"]

    close_sql, close_params = conn.statements[1]
    assert close_sql.upper().startswith("UPDATE FACTS")
    assert "VALID_TO IS NULL" in close_sql.upper()
    assert "TX_TO IS NULL" in close_sql.upper()
    assert close_params[3] == "plan"

    insert_sql, insert_params = conn.statements[2]
    assert insert_sql.upper().startswith("INSERT INTO FACTS")
    assert insert_params[3] == "pro"
    # valid_from is the normalised param at index 5
    assert insert_params[5] == datetime(2026, 1, 1, tzinfo=UTC)


def test_add_fact_requires_an_object():
    graph, _ = _graph()
    with pytest.raises(ValueError):
        graph.add_fact("default", subject_entity_id=str(uuid.uuid4()), predicate="p")


def test_add_fact_past_valid_to_without_valid_from_uses_valid_to_as_start():
    """A point-in-time fact (valid_to set, valid_from omitted) must not default
    valid_from to now() — that violates ``valid_from <= valid_to`` (facts_check).

    Regression: promoting a curated fact with ``valid_to="2026-07-25"`` and no
    ``valid_from`` raised CheckViolation on the live store.
    """
    from stratum.graph import normalize_utc

    graph, conn = _graph(responses=[None, None, {"fact_id": str(uuid.uuid4())}])
    past = "2026-07-25T00:00:00+00:00"
    graph.add_fact(
        "default",
        subject_entity_id=str(uuid.uuid4()),
        predicate="has updates until",
        object_text="2026-07-25",
        valid_to=past,
    )
    insert_sql, params = conn.statements[2]
    # params order: tenant, subject, predicate, object_text, object_entity_id,
    # valid_from, valid_to, confidence, ...
    vf, vt = params[5], params[6]
    assert vf == normalize_utc(past), "valid_from must equal valid_to, not now()"
    assert vf <= vt


def test_add_fact_uses_configured_vector_type():
    from stratum.config import Settings

    settings = Settings(vector_type="vector", embedding_dim=1024)
    conn = RecordingConnection(responses=[None, None, {"fact_id": str(uuid.uuid4())}])
    graph = TemporalGraph(settings, conn_factory=lambda _t: conn)

    graph.add_fact(
        "default",
        subject_entity_id=str(uuid.uuid4()),
        predicate="p",
        object_text="o",
    )
    insert_sql = conn.statements[2][0]
    assert "::vector" in insert_sql


def test_add_fact_guard_select_reads_open_fact():
    """B6: the guard SELECT runs first and only considers open facts."""
    subject = str(uuid.uuid4())
    graph, conn = _graph(responses=[None, None, {"fact_id": "n"}])
    graph.add_fact(
        "default",
        subject_entity_id=subject,
        predicate="plan",
        object_text="pro",
    )
    guard_sql, guard_params = conn.statements[0]
    assert guard_sql.upper().startswith("SELECT VALID_FROM")
    assert "VALID_TO IS NULL" in guard_sql.upper()
    assert "TX_TO IS NULL" in guard_sql.upper()
    assert guard_params == ("default", subject, "plan")
    assert _kinds(conn) == ["SELECT", "UPDATE", "INSERT"]


def test_add_fact_rejects_valid_from_before_open_fact():
    """B6: a valid_from before the open fact's valid_from is a clear ValueError."""
    conn = RecordingConnection()
    graph = TemporalGraph(conn_factory=lambda _t: conn)

    def driver(sql: str, _params: tuple) -> Any:
        if _norm(sql).upper().startswith("SELECT"):
            return {"valid_from": datetime(2026, 6, 1, tzinfo=UTC)}
        raise AssertionError("no write may happen after the B6 guard fails")

    conn.on_execute = driver  # type: ignore[attr-defined]

    with pytest.raises(ValueError, match="valid_from predates"):
        graph.add_fact(
            "default",
            subject_entity_id=str(uuid.uuid4()),
            predicate="plan",
            object_text="pro",
            valid_from="2026-01-01T00:00:00Z",
        )

    # Only the guard SELECT ran; no UPDATE, no INSERT.
    assert _kinds(conn) == ["SELECT"]


def test_add_fact_allows_valid_from_after_open_fact():
    """B6: a valid_from at/after the open fact's valid_from proceeds normally."""
    conn = RecordingConnection()
    graph = TemporalGraph(conn_factory=lambda _t: conn)

    def driver(sql: str, params: tuple) -> Any:
        if _norm(sql).upper().startswith("SELECT"):
            return {"valid_from": datetime(2026, 1, 1, tzinfo=UTC)}
        if _norm(sql).upper().startswith("INSERT"):
            return {"fact_id": "new"}
        return None

    conn.on_execute = driver  # type: ignore[attr-defined]

    fid = graph.add_fact(
        "default",
        subject_entity_id=str(uuid.uuid4()),
        predicate="plan",
        object_text="pro",
        valid_from="2026-06-01T00:00:00Z",
    )
    assert fid == "new"
    assert _kinds(conn) == ["SELECT", "UPDATE", "INSERT"]


def test_add_fact_without_open_fact_proceeds():
    """B6: with no open fact the guard passes (no predecessor to contradict)."""
    graph, conn = _graph(responses=[None, None, {"fact_id": "n"}])
    graph.add_fact(
        "default",
        subject_entity_id=str(uuid.uuid4()),
        predicate="plan",
        object_text="pro",
        valid_from="2020-01-01T00:00:00Z",
    )
    assert _kinds(conn) == ["SELECT", "UPDATE", "INSERT"]



# --------------------------------------------------------------------------- #
# correct_fact
# --------------------------------------------------------------------------- #


def test_correct_fact_closes_then_inserts_in_transaction():
    # correct_fact returns the generated UUID; the fake INSERT response must
    # echo the same id the closing UPDATE referenced. The response list is
    # evaluated lazily per execute() call.
    conn = RecordingConnection()
    graph = TemporalGraph(conn_factory=lambda _t: conn)

    def driver(sql: str, params: tuple) -> Any:
        # First execute is the closing UPDATE, second is the INSERT.
        if _norm(sql).upper().startswith("INSERT"):
            return {"fact_id": params[0]}
        return None

    def after(sql: str, params: tuple, cur: RecordingCursor) -> None:
        # The closing UPDATE must report exactly one affected row (B3).
        if _norm(sql).upper().startswith("UPDATE"):
            cur.rowcount = 1

    conn.on_execute = driver  # type: ignore[attr-defined]
    conn.on_after_execute = after

    subject = str(uuid.uuid4())
    result = graph.correct_fact(
        "default",
        old_fact_id=str(uuid.uuid4()),
        subject_entity_id=subject,
        predicate="plan",
        object_text="enterprise",
    )

    assert _kinds(conn) == ["UPDATE", "INSERT"]
    close_sql, close_params = conn.statements[0]
    insert_sql, insert_params = conn.statements[1]
    assert "SUPERSEDED_BY = %S" in close_sql.upper()
    # superseded_by references the new fact id, written before the insert
    assert close_params[1] == insert_params[0] == result
    assert conn.transactions == 1


def test_correct_fact_reads_subject_and_predicate_when_omitted():
    old_id = str(uuid.uuid4())
    subject = str(uuid.uuid4())
    conn = RecordingConnection(
        responses=[
            {"subject_entity_id": subject, "predicate": "plan"},  # SELECT
            None,  # UPDATE
            {"fact_id": "generated"},  # INSERT
        ]
    )
    graph = TemporalGraph(conn_factory=lambda _t: conn)

    def after(sql: str, params: tuple, cur: RecordingCursor) -> None:
        if _norm(sql).upper().startswith("UPDATE"):
            cur.rowcount = 1

    conn.on_after_execute = after

    graph.correct_fact(
        "default", old_fact_id=old_id, object_text="enterprise"
    )

    assert _kinds(conn) == ["SELECT", "UPDATE", "INSERT"]
    # The read-back SELECT targets the old fact and only considers open facts
    sql0, params0 = conn.statements[0]
    assert params0 == ("default", old_id)
    assert "TX_TO IS NULL" in sql0.upper()
    # The inserted row carries the read-back subject/predicate
    insert_params = conn.statements[2][1]
    assert insert_params[2] == subject
    assert insert_params[3] == "plan"
    assert conn.transactions == 1


def test_correct_fact_raises_when_old_missing():
    conn = RecordingConnection(responses=[None])
    graph = TemporalGraph(conn_factory=lambda _t: conn)
    with pytest.raises(LookupError):
        graph.correct_fact("default", old_fact_id=str(uuid.uuid4()), object_text="x")


def test_correct_fact_requires_object():
    conn = RecordingConnection()
    graph = TemporalGraph(conn_factory=lambda _t: conn)
    with pytest.raises(ValueError):
        graph.correct_fact(
            "default",
            old_fact_id=str(uuid.uuid4()),
            subject_entity_id=str(uuid.uuid4()),
            predicate="plan",
        )
    # No statement is emitted: the validation precedes any write.
    assert conn.statements == []


def test_correct_fact_missing_fact_raises_and_does_not_insert():
    """B3 (a): a missing old fact raises LookupError and never INSERTs."""
    conn = RecordingConnection(responses=[None])  # read-back SELECT -> no row
    graph = TemporalGraph(conn_factory=lambda _t: conn)

    with pytest.raises(LookupError):
        graph.correct_fact(
            "default", old_fact_id=str(uuid.uuid4()), object_text="enterprise"
        )

    # Only the read-back SELECT ran; no UPDATE and crucially no INSERT.
    assert _kinds(conn) == ["SELECT"]
    assert all(not sql.upper().startswith("INSERT") for sql, _ in conn.statements)


def test_correct_fact_rowcount_zero_raises_and_does_not_insert():
    """B3 (b) / B2: closing UPDATE touching != 1 row raises LookupError.

    The old fact is located by the read-back SELECT, but the closing UPDATE
    affects no row (e.g. it was closed concurrently). correct_fact must abort
    with LookupError and must not insert a replacement.
    """
    old_id = str(uuid.uuid4())
    subject = str(uuid.uuid4())
    conn = RecordingConnection()
    graph = TemporalGraph(conn_factory=lambda _t: conn)

    def driver(sql: str, _params: tuple) -> Any:
        if _norm(sql).upper().startswith("SELECT"):
            return {"subject_entity_id": subject, "predicate": "plan"}
        if _norm(sql).upper().startswith("INSERT"):
            return {"fact_id": "should-not-happen"}
        return None

    def after(sql: str, _params: tuple, cur: RecordingCursor) -> None:
        if _norm(sql).upper().startswith("UPDATE"):
            cur.rowcount = 0

    conn.on_execute = driver  # type: ignore[attr-defined]
    conn.on_after_execute = after

    with pytest.raises(LookupError):
        graph.correct_fact(
            "default", old_fact_id=old_id, object_text="enterprise"
        )

    # SELECT + UPDATE ran; the INSERT never did.
    assert _kinds(conn) == ["SELECT", "UPDATE"]
    assert all(not sql.upper().startswith("INSERT") for sql, _ in conn.statements)


def test_correct_fact_unknown_rowcount_raises_and_does_not_insert():
    """B2: a driver that does not report rowcount (``None``) is treated as != 1."""
    conn = RecordingConnection()
    graph = TemporalGraph(conn_factory=lambda _t: conn)

    def driver(sql: str, _params: tuple) -> Any:
        if _norm(sql).upper().startswith("SELECT"):
            return {"subject_entity_id": str(uuid.uuid4()), "predicate": "p"}
        return None

    conn.on_execute = driver  # type: ignore[attr-defined]
    # No on_after_execute -> cursor.rowcount stays None.

    with pytest.raises(LookupError):
        graph.correct_fact(
            "default", old_fact_id=str(uuid.uuid4()), object_text="x"
        )

    assert _kinds(conn) == ["SELECT", "UPDATE"]


def test_correct_fact_read_back_filters_closed_facts():
    """B3: the read-back SELECT excludes already-closed facts (``tx_to IS NULL``).

    An already-closed fact therefore yields no row -> LookupError, and no
    replacement is inserted.
    """
    conn = RecordingConnection(responses=[None])  # closed -> filtered out
    graph = TemporalGraph(conn_factory=lambda _t: conn)

    with pytest.raises(LookupError):
        graph.correct_fact(
            "default", old_fact_id=str(uuid.uuid4()), object_text="x"
        )

    read_sql, _ = conn.statements[0]
    assert read_sql.upper().startswith("SELECT")
    assert "TX_TO IS NULL" in read_sql.upper()
    assert _kinds(conn) == ["SELECT"]


# --------------------------------------------------------------------------- #
# get_facts: as_of clauses
# --------------------------------------------------------------------------- #


def test_get_facts_default_only_open():
    graph, conn = _graph(responses=[])
    graph.get_facts("default")
    sql, params = conn.statements[0]
    assert "VALID_TO IS NULL" in sql.upper()
    assert "TX_TO IS NULL" in sql.upper()
    assert params == ("default",)


def test_get_facts_as_of_valid_clause():
    graph, conn = _graph(responses=[])
    graph.get_facts("default", as_of_valid="2026-05-01T00:00:00Z")
    sql, params = conn.statements[0]
    assert "VALID_FROM <= %S" in sql.upper()
    assert "(VALID_TO IS NULL OR VALID_TO > %S)" in sql.upper()
    # no implicit "open only" guard when as_of is given
    assert "TX_TO IS NULL" not in sql.upper()
    instant = datetime(2026, 5, 1, tzinfo=UTC)
    assert params.count(instant) == 2  # both sides of the valid-time window


def test_get_facts_as_of_tx_clause():
    graph, conn = _graph(responses=[])
    graph.get_facts("default", as_of_tx="2026-05-01T00:00:00Z")
    sql, params = conn.statements[0]
    assert "TX_FROM <= %S" in sql.upper()
    assert "(TX_TO IS NULL OR TX_TO > %S)" in sql.upper()
    assert "VALID_TO IS NULL" not in sql.upper()
    instant = datetime(2026, 5, 1, tzinfo=UTC)
    assert params.count(instant) == 2


def test_get_facts_both_axes_and_filters():
    subject = str(uuid.uuid4())
    graph, conn = _graph(responses=[])
    graph.get_facts(
        "default",
        subject_entity_id=subject,
        predicate="plan",
        as_of_valid="2026-05-01T00:00:00Z",
        as_of_tx="2026-06-01T00:00:00Z",
    )
    sql, params = conn.statements[0]
    upper = sql.upper()
    assert "SUBJECT_ENTITY_ID = %S" in upper
    assert "PREDICATE = %S" in upper
    assert "VALID_FROM <= %S" in upper
    assert "TX_FROM <= %S" in upper
    assert params[0] == "default"
    assert params[1] == subject
    assert params[2] == "plan"


def test_get_facts_include_closed_drops_open_guard():
    graph, conn = _graph(responses=[])
    graph.get_facts("default", include_closed=True)
    sql, _ = conn.statements[0]
    assert "VALID_TO IS NULL" not in sql.upper()
    assert "TX_TO IS NULL" not in sql.upper()


def test_get_facts_returns_dicts():
    fid = str(uuid.uuid4())
    graph, _ = _graph(responses=[[{"fact_id": fid, "predicate": "plan"}]])
    rows = graph.get_facts("default")
    assert rows == [{"fact_id": fid, "predicate": "plan"}]


# --------------------------------------------------------------------------- #
# add_edge / traverse
# --------------------------------------------------------------------------- #


def test_add_edge_returns_id():
    eid = str(uuid.uuid4())
    graph, conn = _graph(responses=[{"edge_id": eid}])
    result = graph.add_edge(
        "default",
        src_entity_id=str(uuid.uuid4()),
        dst_entity_id=str(uuid.uuid4()),
        rel_type="works_at",
    )
    assert result == eid
    sql, params = conn.statements[0]
    assert sql.upper().startswith("INSERT INTO EDGES")
    assert params[3] == "works_at"


def test_traverse_builds_recursive_cte():
    graph, conn = _graph(responses=[])
    graph.traverse(
        "default",
        start_entity_id=str(uuid.uuid4()),
        rel_type="works_at",
        max_depth=4,
    )
    sql, params = conn.statements[0]
    upper = sql.upper()
    assert "WITH RECURSIVE REACH AS" in upper
    assert upper.count("FROM EDGES E") == 2  # anchor + recursive term
    assert "JOIN REACH R ON E.SRC_ENTITY_ID = R.DST_ENTITY_ID" in upper
    assert "R.DEPTH < %S" in upper
    assert "E.REL_TYPE = %S" in upper
    # rel_type appears in both terms
    assert params.count("works_at") == 2
    assert 4 in params  # max_depth bound


def test_traverse_without_rel_type_has_no_rel_filter():
    graph, conn = _graph(responses=[])
    graph.traverse("default", start_entity_id=str(uuid.uuid4()))
    sql, _ = conn.statements[0]
    assert "E.REL_TYPE = %S" not in sql.upper()
    assert "E.REL_TYPE," in sql.upper()  # column still projected


def test_traverse_as_of_valid_filters_both_terms():
    graph, conn = _graph(responses=[])
    graph.traverse(
        "default",
        start_entity_id=str(uuid.uuid4()),
        as_of_valid="2026-05-01T00:00:00Z",
    )
    sql, params = conn.statements[0]
    upper = sql.upper()
    assert upper.count("E.VALID_FROM <= %S") == 2
    assert upper.count("(E.VALID_TO IS NULL OR E.VALID_TO > %S)") == 2
    instant = datetime(2026, 5, 1, tzinfo=UTC)
    assert params.count(instant) == 4


def test_traverse_rejects_bad_depth():
    graph, _ = _graph()
    with pytest.raises(ValueError):
        graph.traverse("default", start_entity_id=str(uuid.uuid4()), max_depth=0)


def test_traverse_caps_result_with_limit():
    """B5: traverse applies a default LIMIT of 1000 to bound path explosion."""
    graph, conn = _graph(responses=[])
    graph.traverse("default", start_entity_id=str(uuid.uuid4()))
    sql, params = conn.statements[0]
    assert "LIMIT %S" in sql.upper()
    assert params[-1] == 1000  # default cap is the last bound parameter


def test_traverse_limit_is_configurable():
    graph, conn = _graph(responses=[])
    graph.traverse("default", start_entity_id=str(uuid.uuid4()), limit=25)
    _, params = conn.statements[0]
    assert params[-1] == 25


def test_traverse_rejects_bad_limit():
    graph, _ = _graph()
    with pytest.raises(ValueError):
        graph.traverse("default", start_entity_id=str(uuid.uuid4()), limit=0)


# --------------------------------------------------------------------------- #
# contradiction_check
# --------------------------------------------------------------------------- #


def test_contradiction_check_detects_different_text_object():
    other = str(uuid.uuid4())
    graph, conn = _graph(responses=[{"fact_id": other, "object_text": "pro"}])

    result = graph.contradiction_check(
        "default",
        subject_entity_id=str(uuid.uuid4()),
        predicate="plan",
        object_value="enterprise",
    )

    assert result["contradiction"] is True
    assert result["fact"]["object_text"] == "pro"
    sql, params = conn.statements[0]
    upper = sql.upper()
    assert "VALID_TO IS NULL" in upper
    assert "TX_TO IS NULL" in upper
    # Text assertions compare on the text axis (NULL-safe), not on the entity
    # axis: "object_entity_id IS DISTINCT FROM NULL" would be FALSE and would
    # never detect a contradiction.
    assert "OBJECT_TEXT IS DISTINCT FROM %S" in upper
    assert "OBJECT_ENTITY_ID IS NOT NULL" not in upper
    # text object -> single text param bound at the end
    assert params[-1] == "enterprise"


def test_contradiction_check_uuid_routes_to_entity_param():
    graph, conn = _graph(responses=[])
    target = str(uuid.uuid4())
    graph.contradiction_check(
        "default",
        subject_entity_id=str(uuid.uuid4()),
        predicate="works_at",
        object_value=target,
    )
    sql, params = conn.statements[0]
    upper = sql.upper()
    assert "OBJECT_ENTITY_ID IS NOT NULL" in upper
    assert "OBJECT_ENTITY_ID IS DISTINCT FROM %S" in upper
    assert "OBJECT_TEXT IS DISTINCT FROM %S" not in upper
    assert params[-1] == target


def test_contradiction_check_none_object_never_matches():
    graph, conn = _graph(responses=[{"fact_id": str(uuid.uuid4()), "object_text": "x"}])
    result = graph.contradiction_check(
        "default",
        subject_entity_id=str(uuid.uuid4()),
        predicate="plan",
        object_value=None,
    )
    # With no asserted object there is nothing to contradict: the text axis is
    # used with a NULL-safe comparison that requires object_text IS NOT NULL.
    assert result["contradiction"] is True
    sql, params = conn.statements[0]
    assert "OBJECT_TEXT IS NOT NULL" in sql.upper()
    assert params[-1] is None


def test_contradiction_check_no_match():
    graph, _ = _graph(responses=[])
    result = graph.contradiction_check(
        "default",
        subject_entity_id=str(uuid.uuid4()),
        predicate="plan",
        object_value="pro",
    )
    assert result == {"contradiction": False, "fact": None}


# --------------------------------------------------------------------------- #
# Integration tests (need a real Postgres) — skipped unless STRATUM_TEST_DB set
# --------------------------------------------------------------------------- #

_TEST_DB = os.getenv("STRATUM_TEST_DB")
requires_db = pytest.mark.skipif(
    not _TEST_DB, reason="set STRATUM_TEST_DB to a Postgres DSN to run integration tests"
)


@pytest.mark.integration
@requires_db
def test_integration_close_before_insert_roundtrip():
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

    # The graph uses stratum.db.connection via the module pool; point that pool
    # at the test DSN for the duration of the test.
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
        graph = TemporalGraph(Settings())
        eid = graph.upsert_entity(
            tenant,
            name="Ada",
            canonical_uri=f"stratum://entities/{tenant}/ada",
        )
        f1 = graph.add_fact(tenant, subject_entity_id=eid, predicate="plan", object_text="pro")
        f2 = graph.add_fact(
            tenant, subject_entity_id=eid, predicate="plan", object_text="enterprise"
        )
        assert f1 != f2

        open_rows = graph.get_facts(tenant, subject_entity_id=eid, predicate="plan")
        assert len(open_rows) == 1
        assert open_rows[0]["object_text"] == "enterprise"

        all_rows = graph.get_facts(
            tenant, subject_entity_id=eid, predicate="plan", include_closed=True
        )
        assert len(all_rows) == 2

        check = graph.contradiction_check(
            tenant, subject_entity_id=eid, predicate="plan", object_value="pro"
        )
        assert check["contradiction"] is True
    finally:
        with psycopg.connect(_TEST_DB) as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM facts WHERE tenant_id = %s", (tenant,))
            cur.execute("DELETE FROM edges WHERE tenant_id = %s", (tenant,))
            cur.execute("DELETE FROM entities WHERE tenant_id = %s", (tenant,))
            cur.execute("DELETE FROM tenants WHERE tenant_id = %s", (tenant,))
        _db.close_pool()
        get_settings.cache_clear()


@pytest.mark.integration
@requires_db
def test_integration_correct_fact_time_travel_and_supersession():
    """B2: correction path — as_of_tx time travel + superseded_by chain.

    Requires the schema from init/003_schema.sql to exist in STRATUM_TEST_DB.
    """
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
        graph = TemporalGraph(Settings())
        eid = graph.upsert_entity(
            tenant,
            name="Ada",
            canonical_uri=f"stratum://entities/{tenant}/ada",
        )
        f1 = graph.add_fact(
            tenant, subject_entity_id=eid, predicate="plan", object_text="pro"
        )
        f2 = graph.correct_fact(tenant, old_fact_id=f1, object_text="enterprise")
        assert f2 != f1

        # Only one open fact survives, and it is the replacement.
        open_rows = graph.get_facts(tenant, subject_entity_id=eid, predicate="plan")
        assert len(open_rows) == 1
        assert open_rows[0]["object_text"] == "enterprise"

        # Supersession chain: the old row points at the new one and is closed.
        all_rows = {
            r["fact_id"]: r
            for r in graph.get_facts(
                tenant, subject_entity_id=eid, predicate="plan", include_closed=True
            )
        }
        old, new = all_rows[f1], all_rows[f2]
        assert old["superseded_by"] == f2
        assert old["tx_to"] is not None
        assert new["superseded_by"] is None

        # Time travel (transaction time) anchored on the rows themselves, so the
        # assertions do not depend on wall-clock skew between test and DB.
        # At the old row's tx_from only the old fact was known.
        known_before = graph.get_facts(
            tenant, subject_entity_id=eid, predicate="plan", as_of_tx=old["tx_from"]
        )
        assert [r["fact_id"] for r in known_before] == [f1]
        assert known_before[0]["object_text"] == "pro"

        # At the new row's tx_from the old fact is already superseded.
        known_after = graph.get_facts(
            tenant, subject_entity_id=eid, predicate="plan", as_of_tx=new["tx_from"]
        )
        assert [r["fact_id"] for r in known_after] == [f2]

        # Valid-time time travel still sees the original value at its instant.
        valid_before = graph.get_facts(
            tenant,
            subject_entity_id=eid,
            predicate="plan",
            as_of_valid=old["valid_from"],
        )
        assert {r["fact_id"] for r in valid_before} == {f1}

        # B3: re-correcting the now-closed fact must fail loudly, not silently.
        with pytest.raises(LookupError):
            graph.correct_fact(tenant, old_fact_id=f1, object_text="free")
        # A missing fact fails the same way.
        with pytest.raises(LookupError):
            graph.correct_fact(
                tenant, old_fact_id=str(uuid.uuid4()), object_text="ghost"
            )
    finally:
        with psycopg.connect(_TEST_DB) as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM facts WHERE tenant_id = %s", (tenant,))
            cur.execute("DELETE FROM edges WHERE tenant_id = %s", (tenant,))
            cur.execute("DELETE FROM entities WHERE tenant_id = %s", (tenant,))
            cur.execute("DELETE FROM tenants WHERE tenant_id = %s", (tenant,))
        _db.close_pool()
        get_settings.cache_clear()
