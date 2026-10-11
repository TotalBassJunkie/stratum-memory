"""Multi-tenant authentication and authorisation (WP-12).

Implements the security concept of ``MASTER-PLAN.md`` §11:

* **API keys** in the form ``stk_<tenant>_<random>``. Only the first
  :data:`KEY_PREFIX_LEN` characters (the *key prefix*) and an **Argon2id** hash
  of the full key are persisted in ``api_keys``; the plaintext key is returned
  exactly once, at creation time, and never stored or logged.
* **Scopes** ``read``, ``write``, ``promote`` and ``admin``. Every tool call is
  checked for the required scope *and* the target tenant; an ``admin`` key may
  act across tenants.
* **Audit trail** — :func:`log_audit` appends a row to ``audit_log`` for every
  writing operation (actor, action, target, optional detail).

Key verification needs to read ``api_keys`` before the tenant is known, so the
lookup runs on a connection with ``bypass_rls=True`` (the RLS GUC is not yet
set). Key creation and audit logging are tenant-scoped and use the normal
app-role connection. All database access goes through :mod:`stratum.db` or an
injected ``conn_factory``, which keeps this module unit-testable without a live
Postgres.
"""

from __future__ import annotations

import base64
import secrets
from collections.abc import Callable, Iterable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from psycopg.types.json import Jsonb

from . import db

__all__ = [
    "SCOPES",
    "KEY_PREFIX_LEN",
    "AuthContext",
    "ScopeError",
    "Authorizer",
    "create_key",
    "verify_key",
    "revoke_key",
    "require_scope",
    "log_audit",
]

#: The complete set of valid scopes (MASTER-PLAN §11).
SCOPES: frozenset[str] = frozenset({"read", "write", "promote", "admin"})

#: ``key_prefix`` is the first 12 characters of the plaintext key.
KEY_PREFIX_LEN = 12

#: Prefix marking a Stratum API key.
_KEY_SCHEME = "stk"

#: Defaults for key material generation.
_RANDOM_BYTES = 20

#: A ``conn_factory`` is ``(tenant, *, bypass_rls=False) -> contextmanager``.
ConnFactory = Callable[..., AbstractContextManager[Any]]

_HASHER = PasswordHasher()


@lru_cache(maxsize=1)
def _dummy_hash() -> str:
    """Return a throw-away Argon2id hash used to equalise timing (B5).

    The hash is never validated against a stored key: on a prefix with zero
    candidates the code still runs a full Argon2 ``verify`` against this hash so
    a caller cannot tell an unknown prefix from a known one by response time.
    """
    return _HASHER.hash(secrets.token_bytes(16))


class ScopeError(Exception):
    """Raised when an :class:`AuthContext` lacks a required scope."""


@dataclass
class AuthContext:
    """The authenticated identity resolved from a valid API key.

    Attributes:
        tenant: The tenant the key belongs to.
        scopes: The scopes granted to the key.
        key_id: The ``api_keys.key_id`` (UUID as string) of the matched key.
    """

    tenant: str
    scopes: set[str] = field(default_factory=set)
    key_id: str = ""

    def has_scope(self, scope: str) -> bool:
        """Return ``True`` if the context grants ``scope``.

        An ``admin`` scope implies every other scope.
        """
        return "admin" in self.scopes or scope in self.scopes


class Authorizer:
    """Checks an :class:`AuthContext` for a scope and tenant boundary.

    Args:
        require_tenant: When ``True`` (default), a tenant argument passed to
            :meth:`authorize` must match the context's tenant unless the
            context holds the ``admin`` scope.
    """

    def __init__(self, *, require_tenant: bool = True) -> None:
        self._require_tenant = require_tenant

    def authorize(self, ctx: AuthContext, *, scope: str, tenant: str | None = None) -> None:
        """Authorise ``ctx`` for ``scope`` (optionally scoped to ``tenant``).

        Args:
            ctx: The authenticated context.
            scope: Required scope (one of :data:`SCOPES`).
            tenant: The tenant the operation targets. A non-``admin`` key must
                name *its own* tenant — omitting it (``None``) is **not** a
                licence to skip tenant isolation (B6). Only an ``admin`` key may
                call with no explicit tenant, since it may act across tenants.

        Raises:
            ValueError: If ``scope`` is not a known scope.
            ScopeError: If ``ctx`` does not hold ``scope`` (and is not ``admin``).
            PermissionError: If tenant isolation cannot be established — either
                ``tenant`` differs from the context's tenant (non-admin), or no
                ``tenant`` was supplied by a non-admin key (fail-closed).
        """
        if scope not in SCOPES:
            raise ValueError(f"unknown scope {scope!r}; valid scopes: {sorted(SCOPES)}")
        if not ctx.has_scope(scope):
            raise ScopeError(
                f"key {ctx.key_id or '<unknown>'} lacks scope {scope!r} "
                f"(has {sorted(ctx.scopes)})"
            )
        if self._require_tenant and not ctx.has_scope("admin"):
            if tenant is None:
                raise PermissionError(
                    f"tenant is required: key {ctx.key_id or '<unknown>'} "
                    f"(tenant {ctx.tenant!r}) must name a tenant to act on"
                )
            if tenant != ctx.tenant:
                raise PermissionError(
                    f"tenant mismatch: key tenant {ctx.tenant!r} cannot act on {tenant!r}"
                )


def _connect(
    conn_factory: ConnFactory | None,
    tenant: str | None,
    *,
    bypass_rls: bool = False,
) -> AbstractContextManager[Any]:
    """Return a connection context manager for the given factory/tenant."""
    if conn_factory is not None:
        return conn_factory(tenant, bypass_rls=bypass_rls)
    return db.connection(tenant, bypass_rls=bypass_rls)


def _validate_scopes(scopes: Iterable[str]) -> list[str]:
    """Validate and normalise ``scopes``; raise :class:`ValueError` if invalid."""
    resolved = list(dict.fromkeys(scopes))  # de-duplicate, keep first-seen order
    if not resolved:
        raise ValueError("at least one scope is required")
    invalid = [s for s in resolved if s not in SCOPES]
    if invalid:
        raise ValueError(
            f"invalid scope(s) {sorted(invalid)}; valid scopes: {sorted(SCOPES)}"
        )
    return resolved


def _generate_key(tenant: str) -> tuple[str, str]:
    """Return ``(plaintext, prefix)`` for a fresh key belonging to ``tenant``."""
    if not isinstance(tenant, str) or not tenant:
        raise ValueError("tenant must be a non-empty string")
    if "_" in tenant:
        raise ValueError("tenant must not contain '_' (reserved as key separator)")
    raw = secrets.token_bytes(_RANDOM_BYTES)
    rand = base64.b32encode(raw).decode("ascii").rstrip("=").lower()
    plaintext = f"{_KEY_SCHEME}_{tenant}_{rand}"
    return plaintext, plaintext[:KEY_PREFIX_LEN]


def create_key(
    tenant: str,
    scopes: Iterable[str],
    *,
    conn_factory: ConnFactory | None = None,
) -> tuple[str, dict[str, Any]]:
    """Create an API key for ``tenant`` with ``scopes``.

    The plaintext key is returned **once** (first tuple element) and is never
    persisted — only its prefix and Argon2id hash are stored in ``api_keys``.

    Args:
        tenant: Owning tenant (must not contain ``_``).
        scopes: One or more of :data:`SCOPES`.
        conn_factory: Optional ``(tenant, *, bypass_rls=False) -> contextmanager``
            used instead of :func:`stratum.db.connection`.

    Returns:
        ``(plaintext, record)`` where ``record`` holds ``key_id``, ``tenant``,
        ``key_prefix``, ``scopes`` (sorted list) and ``created_at``. The record
        never contains the plaintext or the hash.

    Raises:
        ValueError: For an unknown scope or an invalid tenant.
    """
    normalised = _validate_scopes(scopes)
    plaintext, prefix = _generate_key(tenant)
    key_hash = _HASHER.hash(plaintext)

    sql = """
    INSERT INTO api_keys (tenant_id, key_prefix, key_hash, scopes)
    VALUES (%s, %s, %s, %s)
    RETURNING key_id, created_at
    """
    params = (tenant, prefix, key_hash, normalised)
    with _connect(conn_factory, tenant) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
        conn.commit()

    key_id = str(row["key_id"]) if row else ""
    created_at = row.get("created_at") if row else None
    record: dict[str, Any] = {
        "key_id": key_id,
        "tenant": tenant,
        "key_prefix": prefix,
        "scopes": sorted(normalised),
        "created_at": created_at,
    }
    return plaintext, record


def revoke_key(
    key_id: str,
    *,
    conn_factory: ConnFactory | None = None,
) -> bool:
    """Revoke the key ``key_id`` by stamping ``revoked_at``.

    Revocation is a tenant-scoped write on ``api_keys``; the RLS policy restricts
    the UPDATE to the caller's tenant. A revoked key is rejected by
    :func:`verify_key` from then on (the row is kept for audit purposes).

    Args:
        key_id: The ``api_keys.key_id`` (UUID string) to revoke.
        conn_factory: Optional ``(tenant, *, bypass_rls=False) -> contextmanager``
            used instead of :func:`stratum.db.connection`. The lookup needs the
            owning tenant first, so it is resolved on a ``bypass_rls`` connection
            (as in :func:`verify_key`), then the UPDATE runs in that tenant scope.

    Returns:
        ``True`` if a (previously unrevoked) row was revoked, ``False`` if no
        such key exists or it was already revoked.
    """
    with _connect(conn_factory, None, bypass_rls=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT tenant_id, revoked_at FROM api_keys WHERE key_id = %s", (key_id,))
        row = cur.fetchone()
        if row is None or row.get("revoked_at") is not None:
            return False
        tenant = row["tenant_id"]

    sql = "UPDATE api_keys SET revoked_at = now() WHERE key_id = %s AND revoked_at IS NULL"
    with _connect(conn_factory, tenant) as conn, conn.cursor() as cur:
        cur.execute(sql, (key_id,))
        updated = cur.rowcount or 0
        conn.commit()
    return updated >= 1


def verify_key(
    plaintext: str,
    *,
    conn_factory: ConnFactory | None = None,
) -> AuthContext | None:
    """Resolve ``plaintext`` to an :class:`AuthContext`, or ``None``.

    Looks the key up by its prefix (on a ``bypass_rls`` connection, since the
    tenant is not yet known), then verifies the Argon2id hash. Revoked keys and
    malformed keys yield ``None``.
    """
    if not isinstance(plaintext, str) or len(plaintext) < KEY_PREFIX_LEN:
        return None
    prefix = plaintext[:KEY_PREFIX_LEN]

    sql = """
    SELECT key_id, tenant_id, key_hash, scopes, revoked_at
    FROM api_keys
    WHERE key_prefix = %s
    """
    with _connect(conn_factory, None, bypass_rls=True) as conn, conn.cursor() as cur:
        cur.execute(sql, (prefix,))
        rows = cur.fetchall() or []

    verified_any = False
    for row in rows:
        if row.get("revoked_at") is not None:
            continue
        verified_any = True
        try:
            if _HASHER.verify(row["key_hash"], plaintext):
                return AuthContext(
                    tenant=row["tenant_id"],
                    scopes=set(row.get("scopes") or ()),
                    key_id=str(row["key_id"]),
                )
        except (VerificationError, InvalidHashError):
            # A malformed stored hash must not abort verification of the whole
            # prefix group: skip this row and keep checking the others (B4).
            continue

    if not verified_any:
        # Constant-time behaviour (B5): an unknown prefix (or one whose only
        # rows are revoked) must cost the same as a known one, so run a full
        # Argon2id verify against a throw-away hash and discard the result.
        try:
            _HASHER.verify(_dummy_hash(), plaintext)
        except (VerificationError, InvalidHashError):
            pass
    return None


def require_scope(ctx: AuthContext, scope: str) -> None:
    """Raise :class:`ScopeError` unless ``ctx`` grants ``scope``."""
    if not ctx.has_scope(scope):
        raise ScopeError(
            f"key {ctx.key_id or '<unknown>'} lacks scope {scope!r} "
            f"(has {sorted(ctx.scopes)})"
        )


def log_audit(
    tenant: str,
    actor: str,
    action: str,
    *,
    target_uri: str | None = None,
    detail: Mapping[str, Any] | None = None,
    conn_factory: ConnFactory | None = None,
) -> None:
    """Append an entry to ``audit_log``.

    Args:
        tenant: Tenant the action was performed for (RLS scopes the row).
        actor: Who acted (e.g. the ``key_id`` or a component name).
        action: Verb describing the operation (e.g. ``ADD``, ``FORGET``).
        target_uri: Optional affected ``stratum://`` URI.
        detail: Optional structured detail persisted as ``jsonb``.
        conn_factory: Optional connection factory override.
    """
    sql = """
    INSERT INTO audit_log (tenant_id, actor, action, target_uri, detail)
    VALUES (%s, %s, %s, %s, %s)
    """
    params = (
        tenant,
        actor,
        action,
        target_uri,
        Jsonb(dict(detail)) if detail is not None else None,
    )
    with _connect(conn_factory, tenant) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        conn.commit()
