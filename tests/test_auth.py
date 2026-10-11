"""Tests for the multi-tenant auth layer (:mod:`stratum.auth`, WP-12).

These tests are database-free: a :class:`FakeDB` simulates the ``api_keys`` and
``audit_log`` tables in memory and records every emitted statement. This lets us
assert the security-relevant invariants without a live Postgres:

* the plaintext key is returned exactly once and **never** stored or logged,
* only the ``key_prefix`` and an Argon2id hash land in ``api_keys``,
* the hash verifies against the plaintext (and only it),
* revoked keys and wrong keys resolve to ``None``,
* the ``Authorizer`` enforces scopes and the tenant boundary (``admin`` excepted),
* ``log_audit`` appends a row to ``audit_log``.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import UTC, datetime

import pytest
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from psycopg.types.json import Jsonb

from stratum.auth import (
    KEY_PREFIX_LEN,
    SCOPES,
    AuthContext,
    Authorizer,
    ScopeError,
    create_key,
    log_audit,
    require_scope,
    revoke_key,
    verify_key,
)

_NOW = datetime(2026, 10, 11, tzinfo=UTC)


def _norm(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip()


# --------------------------------------------------------------------------- #
# In-memory fakes
# --------------------------------------------------------------------------- #


class FakeCursor:
    """Interprets the handful of statements auth.py emits against in-memory tables."""

    def __init__(self, store: FakeDB) -> None:
        self._store = store
        self._result: object = None
        self.rowcount: int | None = None

    def execute(self, sql: str, params: tuple | dict = ()) -> None:
        s = _norm(sql)
        self._store.statements.append((s, params))
        if s.startswith("INSERT INTO api_keys"):
            tenant_id, key_prefix, key_hash, scopes = params
            row = {
                "key_id": str(uuid.uuid4()),
                "tenant_id": tenant_id,
                "key_prefix": key_prefix,
                "key_hash": key_hash,
                "scopes": list(scopes),
                "revoked_at": None,
                "created_at": _NOW,
            }
            self._store.api_keys.append(row)
            self._result = {"key_id": row["key_id"], "created_at": row["created_at"]}
            self.rowcount = 1
        elif s.startswith("SELECT") and "FROM api_keys" in s and "WHERE key_id" in s:
            key_id = params[0]
            matches = [r for r in self._store.api_keys if r["key_id"] == key_id]
            self._result = dict(matches[0]) if matches else None
            self.rowcount = len(matches)
        elif s.startswith("SELECT") and "FROM api_keys" in s:
            prefix = params[0]
            self._result = [
                dict(r) for r in self._store.api_keys if r["key_prefix"] == prefix
            ]
            self.rowcount = len(self._result)
        elif s.startswith("UPDATE api_keys") and "revoked_at" in s:
            key_id = params[0]
            matched = [
                r
                for r in self._store.api_keys
                if r["key_id"] == key_id and r["revoked_at"] is None
            ]
            for r in matched:
                r["revoked_at"] = _NOW
            self._result = None
            self.rowcount = len(matched)
        elif s.startswith("INSERT INTO audit_log"):
            self._store.audit_log.append(params)
            self._result = None
            self.rowcount = 1
        else:  # pragma: no cover - unexpected statement
            self._result = None

    def fetchone(self) -> object:
        return self._result if isinstance(self._result, dict) else None

    def fetchall(self) -> list:
        return self._result if isinstance(self._result, list) else []

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class FakeConnection:
    def __init__(self, store: FakeDB) -> None:
        self._store = store

    def cursor(self) -> FakeCursor:
        return FakeCursor(self._store)

    def commit(self) -> None:
        self._store.commits += 1

    def __enter__(self) -> FakeConnection:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class FakeDB:
    """Simulates the ``api_keys`` and ``audit_log`` tables in memory."""

    def __init__(self) -> None:
        self.api_keys: list[dict] = []
        self.audit_log: list[tuple] = []
        self.statements: list[tuple[str, tuple]] = []
        self.factory_calls: list[tuple[str | None, bool]] = []
        self.commits = 0

    def factory(self, tenant: str | None, *, bypass_rls: bool = False) -> FakeConnection:
        self.factory_calls.append((tenant, bypass_rls))
        return FakeConnection(self)

    def revoke(self, key_id: str) -> None:
        for row in self.api_keys:
            if row["key_id"] == key_id:
                row["revoked_at"] = _NOW


def _db() -> FakeDB:
    return FakeDB()


# --------------------------------------------------------------------------- #
# create_key
# --------------------------------------------------------------------------- #


def test_create_key_stores_prefix_and_argon2_hash_not_plaintext():
    db = _db()
    plaintext, record = create_key("acme", ["read", "write"], conn_factory=db.factory)

    assert len(db.api_keys) == 1
    stored = db.api_keys[0]
    assert stored["tenant_id"] == "acme"
    assert stored["key_prefix"] == plaintext[:KEY_PREFIX_LEN]
    assert stored["scopes"] == ["read", "write"]

    key_hash = stored["key_hash"]
    assert key_hash.startswith("$argon2id$")
    assert key_hash != plaintext
    assert plaintext not in key_hash
    # The stored hash really is the plaintext's hash.
    assert PasswordHasher().verify(key_hash, plaintext) is True

    # The record never leaks the plaintext or the hash.
    dumped = json.dumps(record, default=str)
    assert plaintext not in dumped
    assert key_hash not in dumped
    assert record["tenant"] == "acme"
    assert record["scopes"] == ["read", "write"]
    assert record["key_id"]


def test_create_key_commits_transaction():
    # B1: without an explicit commit the pool rolls the INSERT back and the key
    # never lands in the database.
    db = _db()
    create_key("acme", ["read"], conn_factory=db.factory)
    assert db.commits >= 1
    assert len(db.api_keys) == 1


def test_create_key_plaintext_format_and_prefix():
    db = _db()
    plaintext, record = create_key("default", ["read"], conn_factory=db.factory)
    assert plaintext.startswith("stk_default_")
    assert plaintext.count("stk_") == 1
    assert record["key_prefix"] == plaintext[:12]
    assert len(record["key_prefix"]) == KEY_PREFIX_LEN


def test_create_key_dedups_scopes():
    db = _db()
    _, record = create_key("acme", ["read", "read", "admin"], conn_factory=db.factory)
    assert record["scopes"] == ["admin", "read"]


def test_create_key_rejects_unknown_scope():
    db = _db()
    with pytest.raises(ValueError, match="invalid scope"):
        create_key("acme", ["read", "superuser"], conn_factory=db.factory)
    assert db.api_keys == []


def test_create_key_rejects_empty_scopes():
    db = _db()
    with pytest.raises(ValueError):
        create_key("acme", [], conn_factory=db.factory)
    assert db.api_keys == []


def test_create_key_rejects_tenant_with_underscore():
    db = _db()
    with pytest.raises(ValueError, match="key separator"):
        create_key("a_b", ["read"], conn_factory=db.factory)


def test_scopes_constant_is_exactly_the_four_scopes():
    assert set(SCOPES) == {"read", "write", "promote", "admin"}


# --------------------------------------------------------------------------- #
# verify_key
# --------------------------------------------------------------------------- #


def test_verify_key_returns_context_with_tenant_and_scopes():
    db = _db()
    plaintext, record = create_key("acme", ["read", "promote"], conn_factory=db.factory)

    ctx = verify_key(plaintext, conn_factory=db.factory)
    assert isinstance(ctx, AuthContext)
    assert ctx.tenant == "acme"
    assert ctx.scopes == {"read", "promote"}
    assert ctx.key_id == record["key_id"]


def test_verify_key_lookup_uses_bypass_rls():
    db = _db()
    plaintext, _ = create_key("acme", ["read"], conn_factory=db.factory)
    db.factory_calls.clear()

    verify_key(plaintext, conn_factory=db.factory)
    # verify_key cannot know the tenant yet, so it must bypass RLS with tenant=None.
    assert db.factory_calls == [(None, True)]


def test_verify_key_wrong_key_returns_none():
    db = _db()
    create_key("acme", ["read"], conn_factory=db.factory)

    # Same prefix, different secret -> Argon2 mismatch -> None.
    fake = "stk_acme_" + "a" * 40
    assert verify_key(fake, conn_factory=db.factory) is None


def test_verify_key_unknown_prefix_returns_none():
    db = _db()
    create_key("acme", ["read"], conn_factory=db.factory)
    assert verify_key("stk_other_zzzzzzzzzzzzzzzz", conn_factory=db.factory) is None


def test_verify_key_revoked_returns_none():
    db = _db()
    plaintext, record = create_key("acme", ["read"], conn_factory=db.factory)
    assert verify_key(plaintext, conn_factory=db.factory) is not None

    db.revoke(record["key_id"])
    assert verify_key(plaintext, conn_factory=db.factory) is None


def test_verify_key_malformed_returns_none():
    db = _db()
    assert verify_key("stk_short", conn_factory=db.factory) is None
    assert verify_key("", conn_factory=db.factory) is None
    # A too-short input never reaches the database.
    assert db.factory_calls == []


def test_verify_key_invalid_stored_hash_does_not_break_prefix_group():
    # B4: one corrupt/foreign-format hash under the same prefix must not abort
    # verification of the whole group; the sibling valid row still verifies.
    db = _db()
    plaintext, record = create_key("acme", ["read"], conn_factory=db.factory)
    prefix = plaintext[:KEY_PREFIX_LEN]
    # A second row shares the prefix but carries an unusable hash (not Argon2).
    db.api_keys.append(
        {
            "key_id": str(uuid.uuid4()),
            "tenant_id": "acme",
            "key_prefix": prefix,
            "key_hash": "not-a-valid-argon2-hash",
            "scopes": ["read"],
            "revoked_at": None,
            "created_at": _NOW,
        }
    )

    ctx = verify_key(plaintext, conn_factory=db.factory)
    assert isinstance(ctx, AuthContext)
    assert ctx.key_id == record["key_id"]


def test_verify_key_invalid_hash_is_tolerated_and_yields_none():
    # The corrupt row alone (no valid sibling) must also just yield None.
    db = _db()
    prefix = "stk_corrupt_"
    db.api_keys.append(
        {
            "key_id": str(uuid.uuid4()),
            "tenant_id": "acme",
            "key_prefix": prefix,
            "key_hash": "not-a-valid-argon2-hash",
            "scopes": ["read"],
            "revoked_at": None,
            "created_at": _NOW,
        }
    )
    assert verify_key(prefix + "x" * 32, conn_factory=db.factory) is None


def test_verify_key_unknown_prefix_still_runs_dummy_verify(monkeypatch):
    # B5: an unknown prefix must not short-circuit — it performs a real Argon2
    # verify against the throw-away hash so the timing matches a known prefix.
    db = _db()
    calls: list[str] = []
    real_verify = PasswordHasher.verify

    def spy(self, hash, password):
        calls.append(hash)
        return real_verify(self, hash, password)

    monkeypatch.setattr(PasswordHasher, "verify", spy)

    assert verify_key("stk_other_zzzzzzzzzzzzzzzz", conn_factory=db.factory) is None
    assert len(calls) == 1, "expected exactly one Argon2 verify for an unknown prefix"


def test_verify_key_known_prefix_runs_verify_once(monkeypatch):
    # Symmetry check for B5: a known-but-wrong key also runs exactly one verify.
    db = _db()
    create_key("acme", ["read"], conn_factory=db.factory)

    calls: list[str] = []
    real_verify = PasswordHasher.verify

    def spy(self, hash, password):
        calls.append(hash)
        return real_verify(self, hash, password)

    monkeypatch.setattr(PasswordHasher, "verify", spy)
    assert verify_key("stk_acme_" + "a" * 40, conn_factory=db.factory) is None
    assert len(calls) == 1


def test_plaintext_never_appears_in_stored_columns():
    db = _db()
    plaintext, _ = create_key("acme", ["admin"], conn_factory=db.factory)
    stored = db.api_keys[0]
    for value in stored.values():
        assert plaintext not in str(value)
    # Argon2 hash cannot be replayed to reconstruct the key either.
    with pytest.raises(VerifyMismatchError):
        PasswordHasher().verify(stored["key_hash"], "stk_acme_not-the-key")


# --------------------------------------------------------------------------- #
# require_scope
# --------------------------------------------------------------------------- #


def test_require_scope_passes_when_present():
    ctx = AuthContext("acme", {"read"}, "id")
    require_scope(ctx, "read")  # no raise


def test_require_scope_raises_when_missing():
    ctx = AuthContext("acme", {"read"}, "id")
    with pytest.raises(ScopeError):
        require_scope(ctx, "write")


def test_admin_scope_implies_every_scope():
    ctx = AuthContext("acme", {"admin"}, "id")
    for scope in SCOPES:
        require_scope(ctx, scope)
    assert ctx.has_scope("promote") is True


# --------------------------------------------------------------------------- #
# Authorizer
# --------------------------------------------------------------------------- #


def test_authorizer_missing_scope_raises_scope_error():
    authz = Authorizer()
    ctx = AuthContext("acme", {"read"}, "id")
    with pytest.raises(ScopeError):
        authz.authorize(ctx, scope="write", tenant="acme")


def test_authorizer_tenant_mismatch_raises_permission_error():
    authz = Authorizer()
    ctx = AuthContext("acme", {"read", "write"}, "id")
    with pytest.raises(PermissionError):
        authz.authorize(ctx, scope="read", tenant="other")


def test_authorizer_same_tenant_ok():
    authz = Authorizer()
    ctx = AuthContext("acme", {"read"}, "id")
    authz.authorize(ctx, scope="read", tenant="acme")  # no raise


def test_authorizer_admin_cross_tenant_ok():
    authz = Authorizer()
    ctx = AuthContext("acme", {"admin"}, "id")
    authz.authorize(ctx, scope="write", tenant="other")  # no raise


def test_authorizer_scope_checked_before_tenant():
    # A non-admin key with an insufficient scope fails on scope even for its own tenant.
    authz = Authorizer()
    ctx = AuthContext("acme", {"read"}, "id")
    with pytest.raises(ScopeError):
        authz.authorize(ctx, scope="admin", tenant="acme")


def test_authorizer_unknown_scope_raises_value_error():
    authz = Authorizer()
    ctx = AuthContext("acme", {"admin"}, "id")
    with pytest.raises(ValueError, match="unknown scope"):
        authz.authorize(ctx, scope="delete-everything", tenant="acme")


def test_authorizer_no_tenant_argument_fails_closed_for_non_admin():
    # B6: a non-admin key that omits the tenant must NOT silently skip tenant
    # isolation — fail-closed with PermissionError.
    authz = Authorizer()
    ctx = AuthContext("acme", {"read"}, "id")
    with pytest.raises(PermissionError, match="tenant is required"):
        authz.authorize(ctx, scope="read")


def test_authorizer_no_tenant_argument_ok_for_admin():
    # An admin key may act across tenants, so omitting tenant is allowed for it.
    authz = Authorizer()
    ctx = AuthContext("acme", {"admin"}, "id")
    authz.authorize(ctx, scope="read")  # no raise


def test_authorizer_require_tenant_false_allows_omitted_tenant():
    # The kill-switch still exists: require_tenant=False disables the boundary.
    authz = Authorizer(require_tenant=False)
    ctx = AuthContext("acme", {"read"}, "id")
    authz.authorize(ctx, scope="read")  # no raise


# --------------------------------------------------------------------------- #
# log_audit
# --------------------------------------------------------------------------- #


def test_log_audit_inserts_row_with_detail():
    db = _db()
    log_audit(
        "acme",
        "key:123",
        "ADD",
        target_uri="stratum://facts/acme/ada",
        detail={"fact_id": "f1"},
        conn_factory=db.factory,
    )

    assert len(db.audit_log) == 1
    tenant, actor, action, target_uri, detail = db.audit_log[0]
    assert (tenant, actor, action) == ("acme", "key:123", "ADD")
    assert target_uri == "stratum://facts/acme/ada"
    assert isinstance(detail, Jsonb)
    assert detail.obj == {"fact_id": "f1"}


def test_log_audit_inserts_row_without_detail():
    db = _db()
    log_audit("acme", "curator", "FORGET", conn_factory=db.factory)
    tenant, actor, action, target_uri, detail = db.audit_log[0]
    assert (tenant, actor, action, target_uri) == ("acme", "curator", "FORGET", None)
    assert detail is None


def test_log_audit_uses_tenant_scoped_connection():
    db = _db()
    log_audit("acme", "actor", "ADD", conn_factory=db.factory)
    # Audit writes are tenant-scoped (no RLS bypass).
    assert db.factory_calls == [("acme", False)]


def test_log_audit_commits_transaction():
    # B1: an uncommitted audit row is rolled back by the pool and lost.
    db = _db()
    log_audit("acme", "actor", "ADD", conn_factory=db.factory)
    assert db.commits >= 1
    assert len(db.audit_log) == 1


def test_log_audit_emits_insert_statement():
    db = _db()
    log_audit("acme", "actor", "ADD", conn_factory=db.factory)
    assert any(
        s.startswith("INSERT INTO audit_log") for s, _ in db.statements
    )


def test_log_audit_does_not_persist_plaintext():
    db = _db()
    plaintext, _ = create_key("acme", ["write"], conn_factory=db.factory)
    log_audit("acme", "actor", "ADD", detail={"note": "ok"}, conn_factory=db.factory)
    dumped = json.dumps(db.audit_log, default=str)
    assert plaintext not in dumped


# --------------------------------------------------------------------------- #
# revoke_key
# --------------------------------------------------------------------------- #


def test_revoke_key_sets_revoked_at_and_commits():
    db = _db()
    plaintext, record = create_key("acme", ["read"], conn_factory=db.factory)
    assert verify_key(plaintext, conn_factory=db.factory) is not None

    db.commits = 0
    assert revoke_key(record["key_id"], conn_factory=db.factory) is True
    assert db.commits >= 1
    stored = next(r for r in db.api_keys if r["key_id"] == record["key_id"])
    assert stored["revoked_at"] is not None

    # The revoked key no longer resolves.
    assert verify_key(plaintext, conn_factory=db.factory) is None


def test_revoke_key_unknown_id_returns_false_without_update():
    db = _db()
    create_key("acme", ["read"], conn_factory=db.factory)
    assert revoke_key(str(uuid.uuid4()), conn_factory=db.factory) is False
    assert not any(s.startswith("UPDATE api_keys") for s, _ in db.statements)


def test_revoke_key_is_idempotent():
    db = _db()
    _, record = create_key("acme", ["read"], conn_factory=db.factory)
    assert revoke_key(record["key_id"], conn_factory=db.factory) is True
    # A second revoke finds no unrevoked row and reports False.
    assert revoke_key(record["key_id"], conn_factory=db.factory) is False


# --------------------------------------------------------------------------- #
# Integration tests (need a real Postgres) — skipped unless STRATUM_TEST_DB set
# --------------------------------------------------------------------------- #

_TEST_DB = os.getenv("STRATUM_TEST_DB")
requires_db = pytest.mark.skipif(
    not _TEST_DB, reason="set STRATUM_TEST_DB to a Postgres DSN to run integration tests"
)


@pytest.mark.integration
@requires_db
def test_integration_create_key_verify_and_cross_tenant_rls():
    """B2/B3: a real Postgres with RLS proves key lifecycle and tenant isolation.

    Requires the schema from init/003_schema.sql (and RLS from init/004_rls.sql)
    to exist in STRATUM_TEST_DB, plus the app role ``stratum_app`` from
    init/002_app_role.sql. Every application connection is forced into the
    non-owner app role (``options='-c role=stratum_app'``) so RLS actually
    applies even when the test DSN connects as the (superuser) owner.
    """
    import psycopg
    from psycopg.rows import dict_row as _dict_row
    from psycopg_pool import ConnectionPool

    from stratum import db as _db
    from stratum.config import get_settings

    # Tenant ids must not contain "_" (reserved as the API-key separator), so the
    # integration tenant uses a hyphen.
    tenant = f"it-{uuid.uuid4().hex[:8]}"
    other = f"it-{uuid.uuid4().hex[:8]}"
    with psycopg.connect(_TEST_DB) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tenants (tenant_id, name) VALUES (%s, %s), (%s, %s)",
            (tenant, "integration", other, "integration"),
        )

    get_settings.cache_clear()
    _db.close_pool()
    # Force the non-privileged app role so RLS applies even when the test DSN
# connects as the owner/superuser. Passed as a connection kwarg (libpq
# `options`), not embedded in the conninfo string.
    _db._POOL = ConnectionPool(
        conninfo=_TEST_DB,
        min_size=1,
        max_size=2,
        kwargs={"row_factory": _dict_row, "options": "-c role=stratum_app"},
        open=True,
    )
    try:
        # --- B2: create -> verify through the app role ---------------------
        plaintext, record = create_key(tenant, ["read", "write"])
        ctx = verify_key(plaintext)
        assert ctx is not None
        assert ctx.tenant == tenant
        assert ctx.scopes == {"read", "write"}
        assert ctx.key_id == record["key_id"]

        # A wrong secret with the same prefix must not verify. Flip the last
        # character to something guaranteed different (avoids the 1/32 flake
        # where the random key already ends in the chosen replacement char).
        flipped = "0" if plaintext[-1] != "0" else "1"
        assert verify_key(plaintext[:-1] + flipped) is None

        # --- B3: cross-tenant SELECT yields zero rows ----------------------
        # The api_keys row belongs to `tenant`; a connection scoped to another
        # tenant (different `stratum.tenant` GUC) must not see it under RLS.
        with _db.connection(other) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT key_id FROM api_keys WHERE key_prefix = %s",
                (record["key_prefix"],),
            )
            rows = cur.fetchall()
        assert rows == [], "RLS must hide another tenant's api_keys row"

        # The owning tenant *does* see it (sanity check that the query works).
        with _db.connection(tenant) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT key_id FROM api_keys WHERE key_prefix = %s",
                (record["key_prefix"],),
            )
            assert len(cur.fetchall()) == 1
    finally:
        with psycopg.connect(_TEST_DB) as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM api_keys WHERE tenant_id = %s", (tenant,))
            cur.execute(
                "DELETE FROM tenants WHERE tenant_id IN (%s, %s)", (tenant, other)
            )
        _db.close_pool()
        get_settings.cache_clear()
