"""Database access layer: connection pool and tenant-scoped RLS sessions."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

import psycopg
from psycopg.adapt import Loader
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import Settings

_POOL: ConnectionPool | None = None


class _StrUuidLoader(Loader):
    """Load ``uuid`` columns as ``str`` so every module returns the same type.

    Without this, psycopg returns ``uuid.UUID`` for ``SELECT`` results while the
    public API (``add_fact``/``enqueue``/...) returns ``str``; callers that mix
    the two (e.g. ``{f1, f2}`` set comparisons or dict keys) then break. The
    project's documented contract is plain strings for all identifiers.
    """

    def load(self, data: bytes) -> str:
        return bytes(data).decode()


# Register once at import time; applies to every connection in the pool.
psycopg.adapters.register_loader("uuid", _StrUuidLoader)


def _reset_connection(conn: psycopg.Connection) -> None:
    """Pool reset callback: clear per-tenant GUCs before the connection is reused.

    Defense in depth on top of transaction-local (``is_local=true``) GUCs.
    """
    with conn.cursor() as cur:
        cur.execute("RESET stratum.tenant")
        cur.execute("RESET stratum.bypass_rls")
    conn.commit()


def init_pool(settings: Settings) -> ConnectionPool:
    global _POOL
    if _POOL is None:
        _POOL = ConnectionPool(
            conninfo=settings.dsn(),
            min_size=1,
            max_size=8,
            kwargs={"row_factory": dict_row},
            reset=_reset_connection,
            open=True,
        )
    return _POOL


def get_pool() -> ConnectionPool:
    if _POOL is None:
        from .config import get_settings

        return init_pool(get_settings())
    return _POOL


def close_pool() -> None:
    global _POOL
    if _POOL is not None:
        _POOL.close()
        _POOL = None


@contextmanager
def connection(tenant: str | None = None, *, bypass_rls: bool = False) -> Iterator[psycopg.Connection]:
    """Yield a connection with RLS context set for the given tenant.

    The app role (stratum_app) is not the table owner, so RLS applies.
    ``bypass_rls`` sets a GUC used by the policies for admin/migration paths.
    """
    pool = get_pool()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            # Transaction-local GUCs (is_local=true): they vanish on commit/rollback and are
            # additionally cleared by the pool reset callback, so a reused pooled connection
            # can never leak another tenant's context (MASTER-PLAN §11).
            if bypass_rls:
                cur.execute("SELECT set_config('stratum.bypass_rls', 'on', true)")
            if tenant is not None:
                cur.execute(
                    "SELECT set_config('stratum.tenant', %s, true)", (tenant,)
                )
        yield conn


def fetch_all(sql: str, params: tuple | dict = (), tenant: str | None = None) -> list[dict[str, Any]]:
    with connection(tenant) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def execute(sql: str, params: tuple | dict = (), tenant: str | None = None) -> None:
    with connection(tenant) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        conn.commit()
