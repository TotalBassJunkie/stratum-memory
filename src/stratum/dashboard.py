"""Stratum-Memory operator dashboard (read-mostly UI over the live store).

A small, dependency-free console served by the MCP HTTP transport:

* ``GET  /``                     — the single-page UI (also the server root)
* ``GET  /dashboard``            — the single-page UI (self-contained HTML/CSS/JS)
* ``GET  /dashboard/api/tenants``    — selectable tenants + the active one
* ``GET  /dashboard/api/overview``  — health + counts + recent events
* ``GET  /dashboard/api/areas``     — top-level areas with counts
* ``GET  /dashboard/api/list``      — browse memory files/chunks under a prefix
* ``GET  /dashboard/api/get``       — read one URI at level l0/l1/l2
* ``GET  /dashboard/api/search``    — hybrid/vector/graph/kv query (needs embeddings)
* ``GET  /dashboard/api/facts``     — bi-temporal facts (+ summary KPIs)
* ``GET  /dashboard/api/entities``  — graph entities with their fact counts
* ``GET  /dashboard/api/jobs``      — the job queue (Task Center)
* ``GET  /dashboard/api/staging``   — staged candidates (pending/approved/rejected)
* ``GET  /dashboard/api/events``    — the append-only memory_events journal
* ``POST /dashboard/api/promote``   — release a staged candidate (scope: promote)
* ``POST /dashboard/api/reflect``   — enqueue a reflect run (scope: promote)

All API routes reuse the MCP server's authentication: when
``STRATUM_REQUIRE_API_KEY`` is set, they require ``Authorization: Bearer stk_...``
with a ``read`` scope (``promote`` for the two write actions). The HTML shell
itself is public so the browser can prompt for a token.

The three query-style routes ``/search``, ``/promote`` and ``/reflect`` return
HTTP ``200`` with a ``{"error": ...}`` body when the underlying action fails
(e.g. no embedding backend, an unknown candidate, a dead queue). This is
deliberate MCP parity: a caller can distinguish "the request was understood but
the operation failed" from an auth/validation error without parsing status
codes, mirroring how the matching MCP tools report failures.

The HTML/CSS/JS is embedded (a single string) so it ships inside the wheel with
no package-data configuration and no build step — it must run on a Pi with no
Node toolchain.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response

from . import db

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Data access (read-mostly; the dashboard is a single-operator admin surface)
# --------------------------------------------------------------------------- #


def _json_safe(value: Any) -> Any:
    """Convert psycopg/DB types into JSON-serialisable values.

    Postgres returns real ``datetime``/``date``/``UUID``/``Decimal`` objects;
    ``JSONResponse`` cannot serialise those (it only knows str/int/float/bool/
    None/list/dict). Normalise recursively so every row can be returned as-is.
    """
    import datetime as _dt
    import uuid as _uuid
    from decimal import Decimal

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        return value.isoformat()
    if isinstance(value, (_uuid.UUID,)):
        return str(value)
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    return str(value)


def _q(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    """Run an admin read (RLS bypassed) and return JSON-safe dict rows."""
    from psycopg.rows import dict_row

    with db.connection(bypass_rls=True) as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return [{k: _json_safe(v) for k, v in row.items()} for row in cur.fetchall()]


def _as_int(value: Any, default: int, *, lo: int | None = None, hi: int | None = None) -> int:
    """Parse an integer query parameter defensively.

    A malformed ``?limit=abc`` must not raise (which would be a 500): fall back
    to ``default`` and clamp into ``[lo, hi]`` when bounds are given.
    """
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    if lo is not None:
        n = max(lo, n)
    if hi is not None:
        n = min(hi, n)
    return n


def _escape_like(value: str) -> str:
    """Escape ``\\``, ``%`` and ``_`` in a user-supplied ``ILIKE`` prefix (B10).

    A prefix is a *literal* substring match: without escaping, a ``%`` or ``_``
    in the prefix would act as a wildcard (``%`` matches everything, ``_`` any
    single character). The backslash itself must be escaped first so it stays
    the escape character. Pair the result with ``ILIKE %s ESCAPE '\\'`` (the
    ESCAPE character is a literal backslash).
    """
    return (
        value.replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )


#: Tables the dashboard may count. The table name is interpolated into SQL
#: (identifiers cannot be parameterised), so it is validated against this
#: allow-list to make the "never interpolate user input" invariant explicit.
_COUNTABLE_TABLES = frozenset(
    {"files", "chunks", "facts", "entities", "memory_events", "staging_candidates", "jobs"}
)


def _count(table: str, tenant: str | None = None) -> int:
    if table not in _COUNTABLE_TABLES:
        raise ValueError(f"unknown table for count: {table!r}")
    if tenant is None:
        rows = _q(f"SELECT count(*) AS n FROM {table}")
    else:
        rows = _q(f"SELECT count(*) AS n FROM {table} WHERE tenant_id = %s", (tenant,))
    return int(rows[0]["n"]) if rows else 0


def _count_staging_pending(tenant: str) -> int:
    """Count only *pending* staged candidates (status is parameterised)."""
    rows = _q(
        "SELECT count(*) AS n FROM staging_candidates "
        "WHERE tenant_id = %s AND status = %s",
        (tenant, "pending"),
    )
    return int(rows[0]["n"]) if rows else 0


def _overview(tenant: str) -> dict[str, Any]:
    from .observability import health

    try:
        h = health()
    except Exception as exc:  # noqa: BLE001
        h = {"status": "degraded", "error": str(exc)}
    counts = {
        "files": _count("files", tenant),
        "chunks": _count("chunks", tenant),
        "facts": _count("facts", tenant),
        "entities": _count("entities", tenant),
        "events": _count("memory_events", tenant),
        "staging_pending": _count_staging_pending(tenant),
        "jobs": _count("jobs", tenant),
    }
    recent = _q(
        "SELECT op, target_uri, created_at FROM memory_events "
        "WHERE tenant_id = %s ORDER BY created_at DESC LIMIT 12",
        (tenant,),
    )
    return {"health": h, "counts": counts, "recent_events": recent}


def _areas(tenant: str) -> list[dict[str, Any]]:
    # A stratum URI is ``stratum://<bereich>/<tenant>/<path>``; the top-level
    # area is the bereich (docs/facts/entities/…), i.e. the *third* segment
    # (``split_part`` is 1-based). Using segment 4 would report the first path
    # segment under the tenant instead of the namespace.
    rows = _q(
        "SELECT split_part(uri, '/', 3) AS area, count(*) AS n "
        "FROM files WHERE tenant_id = %s GROUP BY area ORDER BY n DESC",
        (tenant,),
    )
    return rows


def _list(tenant: str, prefix: str | None, limit: int) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 500))
    if prefix:
        # B10: the prefix is a literal match — escape LIKE metacharacters so a
        # user-typed ``%``/``_``/``\`` cannot turn into a wildcard.
        return _q(
            "SELECT uri, l0, size_bytes, updated_at, git_commit FROM files "
            "WHERE tenant_id = %s AND uri ILIKE %s ESCAPE '\\' "
            "ORDER BY updated_at DESC LIMIT %s",
            (tenant, _escape_like(prefix) + "%", limit),
        )
    return _q(
        "SELECT uri, l0, size_bytes, updated_at, git_commit FROM files "
        "WHERE tenant_id = %s ORDER BY updated_at DESC LIMIT %s",
        (tenant, limit),
    )


def _get(tenant: str, uri: str, level: str) -> dict[str, Any]:
    # B3: normalise the requested level. ``?level=L0``/``L3``/garbage must not
    # silently fall through to L1 via the ``else`` branch; only l0/l1/l2 are
    # valid, anything else becomes l1.
    level = (level or "l1").lower()
    if level not in {"l0", "l1", "l2"}:
        level = "l1"
    rows = _q(
        "SELECT uri, l0, l1, size_bytes, updated_at, content_hash FROM files "
        "WHERE tenant_id = %s AND uri = %s",
        (tenant, uri),
    )
    if rows:
        row = rows[0]
        content = row.get("l0") if level == "l0" else row.get("l1")
        if level == "l2":
            # L2 is the vault file itself; read via the filesystem store. The
            # import and the read are both guarded so a missing/misconfigured
            # vault degrades to an explanatory string instead of a 500.
            try:
                from .config import get_settings
                from .filesystem import FilesystemStore

                content = FilesystemStore(get_settings()).read(
                    uri, level="l2", tenant=tenant
                )["content"]
            except Exception as exc:  # noqa: BLE001
                content = f"(L2 unavailable: {exc})"
        return {"kind": "file", **row, "content": content}
    facts = _q(
        "SELECT f.fact_id, f.predicate, f.object_text, f.valid_from, f.valid_to, "
        "f.confidence, f.source_uri, e.name AS subject "
        "FROM facts f LEFT JOIN entities e ON e.entity_id = f.subject_entity_id "
        "WHERE f.tenant_id = %s AND f.source_uri = %s ORDER BY f.created_at DESC LIMIT 50",
        (tenant, uri),
    )
    if facts:
        return {"kind": "facts", "uri": uri, "facts": facts}
    return {"error": "not_found", "uri": uri}


def _staging(tenant: str, status: str, limit: int) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 200))
    return _q(
        "SELECT candidate_id, kind, source_uri, confidence, status, reason, created_at "
        "FROM staging_candidates WHERE tenant_id = %s AND status = %s "
        "ORDER BY created_at DESC LIMIT %s",
        (tenant, status, limit),
    )


def _events(tenant: str, limit: int) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 500))
    return _q(
        "SELECT event_id, op, target_uri, actor, created_at FROM memory_events "
        "WHERE tenant_id = %s ORDER BY created_at DESC LIMIT %s",
        (tenant, limit),
    )


def _tenants() -> list[dict[str, Any]]:
    """List selectable tenants (id + display name) for the switcher."""
    return _q("SELECT tenant_id, name FROM tenants ORDER BY tenant_id")


def _facts(tenant, *, subject, predicate, limit):
    """Browse bi-temporal facts (Stratum-only: validity + transaction windows)."""
    limit = max(1, min(limit, 500))
    where = ["f.tenant_id = %s"]
    params = [tenant]
    if subject:
        where.append("e.name ILIKE %s ESCAPE '\\'")
        params.append(_escape_like(subject) + "%")
    if predicate:
        where.append("f.predicate ILIKE %s ESCAPE '\\'")
        params.append(_escape_like(predicate) + "%")
    params.append(limit)
    return _q(
        "SELECT f.fact_id, e.name AS subject, f.predicate, f.object_text, "
        "f.valid_from, f.valid_to, f.tx_from, f.tx_to, f.confidence, f.source_uri, "
        "(f.valid_to IS NULL AND f.tx_to IS NULL) AS is_open "
        "FROM facts f LEFT JOIN entities e ON e.entity_id = f.subject_entity_id "
        "WHERE " + " AND ".join(where) + " ORDER BY f.created_at DESC LIMIT %s",
        tuple(params),
    )


def _entities(tenant, *, q, limit):
    """List entities (subjects/objects) with their fact counts."""
    limit = max(1, min(limit, 500))
    base = (
        "SELECT e.entity_id, e.name, e.type, e.canonical_uri, "
        "(SELECT count(*) FROM facts f WHERE f.subject_entity_id = e.entity_id) AS fact_count "
        "FROM entities e WHERE e.tenant_id = %s "
    )
    if q:
        return _q(base + "AND e.name ILIKE %s ESCAPE '\\' ORDER BY e.name LIMIT %s",
                  (tenant, _escape_like(q) + "%", limit))
    return _q(base + "ORDER BY e.name LIMIT %s", (tenant, limit))


def _jobs(limit, tenant=None):
    """Task Center: the job queue (kind/status/attempts), newest first.

    When ``tenant`` is given the queue is scoped to that tenant
    (``WHERE tenant_id = %s``); ``tenant=None`` reads the global queue (used
    only by an explicitly admin/global caller). The ``/dashboard/api/jobs``
    route always passes the effective request tenant, so a non-admin key can
    never see another tenant's jobs.
    """
    limit = max(1, min(limit, 500))
    if tenant is None:
        return _q(
            "SELECT job_id, tenant_id, kind, status, attempts, max_attempts, "
            "run_after, locked_by, last_error, created_at FROM jobs "
            "ORDER BY created_at DESC LIMIT %s",
            (limit,),
        )
    return _q(
        "SELECT job_id, tenant_id, kind, status, attempts, max_attempts, "
        "run_after, locked_by, last_error, created_at FROM jobs "
        "WHERE tenant_id = %s ORDER BY created_at DESC LIMIT %s",
        (tenant, limit),
    )


def _facts_summary(tenant):
    """Home KPI: open vs. total facts, distinct subjects."""
    rows = _q(
        "SELECT count(*) AS total, "
        "count(*) FILTER (WHERE valid_to IS NULL AND tx_to IS NULL) AS open, "
        "count(DISTINCT subject_entity_id) AS subjects "
        "FROM facts WHERE tenant_id = %s",
        (tenant,),
    )
    r = rows[0] if rows else {"total": 0, "open": 0, "subjects": 0}
    return {"total": int(r["total"]), "open": int(r["open"]), "subjects": int(r["subjects"])}


# --------------------------------------------------------------------------- #
# Route registration
# --------------------------------------------------------------------------- #


def register_dashboard(
    mcp: Any,
    *,
    tenant_for: Callable[[Request], str] | None = None,
    ensure_scope: Callable[[Request, str], Response | None] | None = None,
    tenants_for: Callable[[Request], list[dict[str, Any]]] | None = None,
) -> None:
    """Register the dashboard routes on a FastMCP instance.

    Args:
        mcp: The ``FastMCP`` server (provides ``custom_route``).
        tenant_for: Resolves the *effective* tenant for a request. The callback
            owns the ``?tenant=`` override and its authorization (a non-admin
            key must not switch away from its own tenant), defaulting to the
            ``default`` tenant.
        ensure_scope: Returns a ``Response`` to short-circuit when the request
            lacks ``scope`` (used when the key gate is on), else ``None``.
        tenants_for: Optional callback returning the tenants to offer in the
            switcher (already authorization-filtered by the caller).
    """

    def _tenant(request: Request) -> str:
        """Resolve the effective tenant, failing open to ``default``.

        Semantics: when ``tenant_for`` raises, this falls back to ``"default"``
        rather than 500ing the request. That is intentional fail-open for the
        *open local mode* (no API key gate): a broken auth context must not lock
        the operator out of a local dashboard. When the key gate is active the
        scope check still runs first (``_gate``), so an unauthenticated request
        is denied before this fallback can expose anything; the fallback then
        only ever applies to an already-authorised caller. The exception is
        logged (with the request path) so a misconfigured ``tenant_for`` is
        visible in the server log instead of failing silently.
        """
        if tenant_for is not None:
            try:
                return tenant_for(request) or "default"
            except Exception:  # noqa: BLE001 - fail open to "default", but log it
                logger.warning(
                    "dashboard tenant resolution failed for %s; falling back to 'default'",
                    request.url.path,
                    exc_info=True,
                )
                return "default"
        return "default"

    def _gate(request: Request, scope: str) -> Response | None:
        if ensure_scope is None:
            return None
        return ensure_scope(request, scope)

    def _read_route(build: Callable[[], Any]) -> Response:
        """Run a GET read route and never let a DB error escape as a 500 crash.

        B3: a raw ``psycopg``/connection failure used to bubble out of the
        endpoint as an unhandled exception (a bare 500 with a stack trace). Each
        read route now wraps its data access and returns a JSON error body with
        ``500`` so the UI (and any caller) gets a structured response.
        """
        try:
            return JSONResponse(build())
        except Exception as exc:  # noqa: BLE001 - surface as a JSON 500, never a crash
            return JSONResponse({"error": str(exc)}, status_code=500)

    @mcp.custom_route("/dashboard", methods=["GET"])
    async def _dashboard(_request: Request) -> Response:  # pragma: no cover - static
        return HTMLResponse(_DASHBOARD_HTML)

    @mcp.custom_route("/", methods=["GET"])
    async def _root(_request: Request) -> Response:  # pragma: no cover - redirect
        return HTMLResponse(_DASHBOARD_HTML)

    def _resolve_tenants(request: Request) -> list[dict[str, Any]]:
        if tenants_for is not None:
            return tenants_for(request)
        return _tenants()

    @mcp.custom_route("/dashboard/api/tenants", methods=["GET"])
    async def _api_tenants(request: Request) -> Response:  # pragma: no cover - io
        if (deny := _gate(request, "read")) is not None:
            return deny
        return _read_route(
            lambda: {"items": _resolve_tenants(request), "current": _tenant(request)}
        )

    @mcp.custom_route("/dashboard/api/overview", methods=["GET"])
    async def _api_overview(request: Request) -> Response:  # pragma: no cover - io
        if (deny := _gate(request, "read")) is not None:
            return deny
        return _read_route(lambda: _overview(_tenant(request)))

    @mcp.custom_route("/dashboard/api/areas", methods=["GET"])
    async def _api_areas(request: Request) -> Response:  # pragma: no cover - io
        if (deny := _gate(request, "read")) is not None:
            return deny
        return _read_route(lambda: {"areas": _areas(_tenant(request))})

    @mcp.custom_route("/dashboard/api/list", methods=["GET"])
    async def _api_list(request: Request) -> Response:  # pragma: no cover - io
        if (deny := _gate(request, "read")) is not None:
            return deny
        qp = request.query_params
        return _read_route(
            lambda: {
                "items": _list(
                    _tenant(request),
                    qp.get("prefix"),
                    _as_int(qp.get("limit", "100"), 100),
                )
            }
        )

    @mcp.custom_route("/dashboard/api/facts", methods=["GET"])
    async def _api_facts(request: Request) -> Response:  # pragma: no cover - io
        if (deny := _gate(request, "read")) is not None:
            return deny
        qp = request.query_params
        return _read_route(
            lambda: {
                "items": _facts(
                    _tenant(request),
                    subject=qp.get("subject") or None,
                    predicate=qp.get("predicate") or None,
                    limit=_as_int(qp.get("limit", "100"), 100),
                ),
                "summary": _facts_summary(_tenant(request)),
            }
        )

    @mcp.custom_route("/dashboard/api/entities", methods=["GET"])
    async def _api_entities(request: Request) -> Response:  # pragma: no cover - io
        if (deny := _gate(request, "read")) is not None:
            return deny
        qp = request.query_params
        return _read_route(
            lambda: {
                "items": _entities(
                    _tenant(request),
                    q=qp.get("q") or None,
                    limit=_as_int(qp.get("limit", "100"), 100),
                )
            }
        )

    @mcp.custom_route("/dashboard/api/jobs", methods=["GET"])
    async def _api_jobs(request: Request) -> Response:  # pragma: no cover - io
        if (deny := _gate(request, "read")) is not None:
            return deny
        return _read_route(
            lambda: {
                "items": _jobs(
                    _as_int(request.query_params.get("limit", "100"), 100),
                    tenant=_tenant(request),
                )
            }
        )

    @mcp.custom_route("/dashboard/api/get", methods=["GET"])
    async def _api_get(request: Request) -> Response:  # pragma: no cover - io
        if (deny := _gate(request, "read")) is not None:
            return deny
        qp = request.query_params
        return _read_route(
            lambda: _get(_tenant(request), qp.get("uri", ""), qp.get("level", "l1"))
        )

    @mcp.custom_route("/dashboard/api/search", methods=["GET"])
    async def _api_search(request: Request) -> Response:  # pragma: no cover - io
        if (deny := _gate(request, "read")) is not None:
            return deny
        qp = request.query_params
        try:
            from .config import get_settings
            from .query import QueryEngine

            engine = QueryEngine(get_settings())
            results = engine.query(
                _tenant(request),
                qp.get("q", ""),
                mode=qp.get("mode", "hybrid"),
                k=_as_int(qp.get("k", "8"), 8, lo=1, hi=50),
                level=qp.get("level", "l1"),
                scope=qp.get("scope") or None,
            )
            # B1: engine.query returns raw DB types (datetime/UUID/Decimal);
            # JSONResponse cannot serialise them, so normalise before encoding.
            return JSONResponse({"results": _json_safe(results)})
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"error": str(exc), "results": []}, status_code=200)

    @mcp.custom_route("/dashboard/api/staging", methods=["GET"])
    async def _api_staging(request: Request) -> Response:  # pragma: no cover - io
        if (deny := _gate(request, "read")) is not None:
            return deny
        qp = request.query_params
        return _read_route(
            lambda: {
                "items": _staging(
                    _tenant(request),
                    qp.get("status", "pending"),
                    _as_int(qp.get("limit", "50"), 50),
                )
            }
        )

    @mcp.custom_route("/dashboard/api/events", methods=["GET"])
    async def _api_events(request: Request) -> Response:  # pragma: no cover - io
        if (deny := _gate(request, "read")) is not None:
            return deny
        limit = _as_int(request.query_params.get("limit", "50"), 50)
        return _read_route(lambda: {"items": _events(_tenant(request), limit)})

    @mcp.custom_route("/dashboard/api/promote", methods=["POST"])
    async def _api_promote(request: Request) -> Response:  # pragma: no cover - io
        if (deny := _gate(request, "promote")) is not None:
            return deny
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        if not isinstance(body, dict):
            body = {}
        candidate_id = body.get("candidate_id", "")
        if not candidate_id:
            return JSONResponse({"error": "candidate_id required"}, status_code=400)
        try:
            from .config import get_settings
            from .curator import Curator

            # B2: mirror the MCP tool's audit parity — pass an actor so the
            # ``memory_events`` row is attributed (``actor`` must not be NULL).
            result = Curator(get_settings()).promote(
                _tenant(request), candidate_id, actor="dashboard"
            )
            return JSONResponse({"ok": True, "result": result})
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"error": str(exc)}, status_code=200)

    @mcp.custom_route("/dashboard/api/reflect", methods=["POST"])
    async def _api_reflect(request: Request) -> Response:  # pragma: no cover - io
        if (deny := _gate(request, "promote")) is not None:
            return deny
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        if not isinstance(body, dict):
            body = {}
        try:
            from .config import get_settings
            from .worker import JobQueue

            settings = get_settings()
            # B2: mirror the MCP tool's budget validation. The budget is optional
            # (default from Settings) and clamped to ``reflect_max_tokens``.
            budget_tokens = _as_int(
                body.get("budget_tokens", settings.reflect_budget_tokens),
                settings.reflect_budget_tokens,
                lo=1,
                hi=settings.reflect_max_tokens,
            )
            job_id = JobQueue(settings).enqueue(
                _tenant(request),
                "reflect",
                {
                    "tenant": _tenant(request),
                    "scope": body.get("scope"),
                    "budget_tokens": budget_tokens,
                    "async": True,
                },
            )
            return JSONResponse({"ok": True, "job_id": job_id})
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"error": str(exc)}, status_code=200)


# --------------------------------------------------------------------------- #
# The UI (single self-contained document; no build step, no CDN)
# --------------------------------------------------------------------------- #

_DASHBOARD_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Stratum Studio</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><rect width='32' height='32' rx='7' fill='%230B1220'/><rect x='7' y='7' width='18' height='3.4' rx='1.7' fill='%233B4A63'/><rect x='6' y='12' width='20' height='3.4' rx='1.7' fill='%2322D3EE'/><rect x='5' y='17' width='22' height='3.4' rx='1.7' fill='%230EA5E9'/><rect x='4' y='22' width='24' height='3.4' rx='1.7' fill='%23F59E0B'/></svg>">
<style>
  :root{
    --bg:#0B1220; --panel:#111c33; --panel-2:#0e1830; --line:#1e293b; --line-2:#24344d;
    --text:#E8EEF7; --dim:#9fb0c9; --faint:#7c8ba6;
    --accent:#F59E0B; --accent-2:#22D3EE; --ok:#34d399; --warn:#fbbf24; --bad:#f87171;
    --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
    --sans:system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
    --r:12px; --r-sm:8px;
    --sidebar:236px;
  }
  *{box-sizing:border-box}
  html,body{margin:0;height:100%}
  body{background:var(--bg);color:var(--text);font-family:var(--sans);font-size:14px;
       -webkit-font-smoothing:antialiased;display:grid;grid-template-columns:var(--sidebar) 1fr;
       grid-template-rows:56px 1fr;grid-template-areas:"brand topbar" "nav main";height:100vh;overflow:hidden}
  @media (max-width:720px){body{grid-template-columns:1fr;grid-template-areas:"topbar" "main"}}
  a{color:inherit}
  /* brand */
  .brand{grid-area:brand;display:flex;align-items:center;gap:10px;padding:0 16px;border-right:1px solid var(--line);border-bottom:1px solid var(--line)}
  .brand .mark{width:24px;height:24px;flex:none}
  .brand b{font-weight:700;letter-spacing:-.2px;font-size:15px}
  .brand small{color:var(--dim);font-size:9.5px;letter-spacing:.24em;text-transform:uppercase;display:block;margin-top:1px}
  @media (max-width:720px){.brand{display:none}}
  /* topbar */
  .topbar{grid-area:topbar;display:flex;align-items:center;gap:10px;padding:0 16px;border-bottom:1px solid var(--line);
          background:linear-gradient(180deg,#0d1730,#0B1220);min-width:0}
  .chip{display:inline-flex;align-items:center;gap:8px;border:1px solid var(--line);border-radius:999px;padding:5px 12px;color:var(--dim);font-size:12.5px;white-space:nowrap;background:var(--panel-2)}
  .chip .dot{width:8px;height:8px;border-radius:50%;background:var(--faint)}
  .chip .dot.ok{background:var(--ok)}.chip .dot.bad{background:var(--bad)}.chip .dot.warn{background:var(--warn)}
  .chip select{background:transparent;border:0;color:var(--text);font-family:var(--mono);max-width:170px;padding:1px}
  .chip select:focus-visible{outline:2px solid var(--accent-2);outline-offset:2px;border-radius:5px}
  .acct{display:inline-flex;align-items:center;gap:9px;border:1px solid var(--line);border-radius:999px;padding:4px 12px 4px 4px;background:var(--panel-2);white-space:nowrap}
  .acct .av{width:26px;height:26px;border-radius:50%;background:linear-gradient(140deg,#0EA5E9,#F59E0B);display:grid;place-items:center;font-weight:700;color:#08111f;font-size:12px}
  .acct small{display:block;color:var(--faint);font-size:10px;line-height:1}
  .spacer{flex:1}
  button,select,input{font:inherit;color:var(--text);background:var(--panel);border:1px solid var(--line);border-radius:var(--r-sm);padding:7px 11px}
  button{cursor:pointer;transition:transform .06s ease,border-color .15s ease,background .15s ease}
  button:hover{border-color:var(--line-2)} button:active{transform:translateY(1px)}
  button.primary{background:var(--accent);color:#1b1300;border-color:transparent;font-weight:600}
  button.ghost{background:transparent}
  input::placeholder{color:var(--faint)}
  :focus-visible{outline:2px solid var(--accent-2);outline-offset:2px}
  /* nav */
  nav{grid-area:nav;border-right:1px solid var(--line);padding:12px 10px 28px;overflow:auto;background:linear-gradient(180deg,#0c1529,#0B1220)}
  @media (max-width:720px){nav{position:fixed;left:0;top:56px;bottom:0;width:var(--sidebar);background:var(--panel-2);transform:translateX(-100%);transition:transform .18s ease;z-index:30}
    nav.open{transform:none}}
  nav .grp{color:var(--faint);font-size:10px;letter-spacing:.18em;text-transform:uppercase;margin:14px 10px 6px;font-weight:600}
  nav .grp:first-child{margin-top:4px}
  nav a{display:flex;align-items:center;gap:10px;padding:8px 11px;border-radius:var(--r-sm);color:var(--dim);text-decoration:none;cursor:pointer;font-size:13px;line-height:1}
  nav a .ic{width:16px;height:16px;flex:none;opacity:.85}
  nav a:hover{background:#15233f;color:var(--text)}
  nav a.active{background:#15233f;color:var(--text);box-shadow:inset 2px 0 0 var(--accent)}
  nav a .k{margin-left:auto;font-family:var(--mono);font-size:11px;color:var(--faint)}
  /* main */
  main{grid-area:main;overflow:auto;padding:22px 26px 72px;min-width:0}
  .view{display:none;animation:fade .18s ease}
  .view.active{display:block}
  @keyframes fade{from{opacity:0;transform:translateY(3px)}to{opacity:1;transform:none}}
  @media (prefers-reduced-motion:reduce){.view{animation:none}button{transition:none}}
  .pagehead{margin:0 0 18px}
  h1{font-size:19px;margin:0 0 3px;letter-spacing:-.2px}
  .sub{color:var(--dim);margin:0;font-size:13px;max-width:70ch}
  .grid{display:grid;gap:14px}
  .kpis{grid-template-columns:repeat(auto-fit,minmax(158px,1fr))}
  .kpi{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);padding:14px 16px;position:relative;overflow:hidden}
  .kpi:before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--accent-2);opacity:.55}
  .kpi.amber:before{background:var(--accent)}.kpi.ok:before{background:var(--ok)}
  .kpi .n{font-family:var(--mono);font-size:26px;font-weight:600;letter-spacing:-.5px}
  .kpi .l{color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.13em;margin-top:4px}
  .kpi .d{color:var(--faint);font-size:11.5px;margin-top:6px}
  .panel{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);overflow:hidden}
  .panel h2{margin:0;padding:12px 15px;font-size:12px;text-transform:uppercase;letter-spacing:.14em;color:var(--dim);border-bottom:1px solid var(--line);font-weight:600;display:flex;align-items:center;gap:10px}
  .panel h2 .k{margin-left:auto;font-family:var(--mono);color:var(--faint);font-size:11px;text-transform:none;letter-spacing:0}
  .panel .body{padding:13px 15px}
  .tw{overflow:auto;max-width:100%}
  table{width:100%;border-collapse:collapse;font-size:12.5px;min-width:420px}
  th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}
  th{color:var(--faint);font-weight:600;font-size:10.5px;text-transform:uppercase;letter-spacing:.1em;position:sticky;top:0;background:var(--panel)}
  tr:last-child td{border-bottom:0}
  tr.row:hover td{background:#15233f}
  .mono{font-family:var(--mono)} .dim{color:var(--dim)} .faint{color:var(--faint)}
  .tag{display:inline-block;font-family:var(--mono);font-size:10.5px;padding:2px 8px;border:1px solid var(--line);border-radius:999px;color:var(--dim)}
  .tag.amber{color:var(--accent);border-color:#4a3608}.tag.cyan{color:var(--accent-2);border-color:#0e4b57}
  .tag.ok{color:var(--ok);border-color:#124d38}.tag.bad{color:var(--bad);border-color:#5a2020}
  .bar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:15px}
  .bar input[type=search],.bar input[type=text]{flex:1;min-width:200px}
  pre{background:var(--panel-2);border:1px solid var(--line);border-radius:var(--r-sm);padding:13px;overflow:auto;font-family:var(--mono);font-size:12px;line-height:1.55;white-space:pre-wrap;word-break:break-word;margin:0}
  .empty{color:var(--faint);padding:22px 6px;text-align:center}
  .err{color:var(--bad)} .clickable{cursor:pointer} .clickable:hover{color:var(--accent-2)}
  .toast{position:fixed;right:20px;bottom:20px;background:var(--panel);border:1px solid var(--line);
         border-left:3px solid var(--accent);border-radius:var(--r-sm);padding:11px 15px;font-size:12.5px;
         box-shadow:0 14px 34px rgba(0,0,0,.5);opacity:0;transform:translateY(8px);transition:opacity .2s,transform .2s;pointer-events:none;z-index:50}
  .toast.show{opacity:1;transform:none}
  .tabs{display:flex;gap:6px;margin-bottom:12px}
  .tabs button{background:transparent;color:var(--dim)} .tabs button.active{color:var(--text);border-color:var(--accent)}
  .hamb{display:none}@media (max-width:720px){.hamb{display:inline-flex}}
  .split{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:14px}
  .muted{color:var(--dim)}
  .statrow{display:flex;gap:18px;flex-wrap:wrap}
  .statrow .s{min-width:120px}
  .statrow .s b{display:block;font-family:var(--mono);font-size:20px}
  .statrow .s span{color:var(--faint);font-size:11px;text-transform:uppercase;letter-spacing:.12em}
</style>
</head>
<body>
  <div class="brand">
    <svg class="mark" viewBox="0 0 256 256" aria-hidden="true"><rect width="256" height="256" rx="56" fill="#111c33"/><polygon points="128,84 204,124 128,164 52,124" fill="#F59E0B"/><polygon points="128,120 200,158 128,196 56,158" fill="#22D3EE"/><polygon points="128,150 192,184 128,218 64,184" fill="#0EA5E9"/><polygon points="128,180 184,210 128,240 72,210" fill="#3B4A63"/></svg>
    <div><b>Stratum</b><small>Studio</small></div>
  </div>

  <header class="topbar">
    <button class="hamb ghost" id="hamb" aria-label="Toggle navigation" aria-controls="nav" aria-expanded="false">≡</button>
    <span class="chip" role="status" aria-live="polite"><span class="dot" id="hdot"></span><span id="hstatus">connecting…</span></span>
    <label class="chip" title="Active tenant"><span class="dim">tenant</span>
      <select id="htenant" aria-label="Active tenant"></select>
    </label>
    <span class="chip" id="hqueue" title="Queued jobs">queue 0</span>
    <span class="spacer"></span>
    <button class="ghost" id="refresh" title="Reload">refresh</button>
    <button class="ghost" id="tokenbtn" title="Set API key">key</button>
    <span class="acct" title="Account">
      <span class="av" id="hav">D</span>
      <span><b id="hacct" style="font-size:12.5px">default</b><small id="hacctsub">Account</small></span>
    </span>
  </header>

  <nav id="nav" aria-label="Primary">
    <div class="grp">Workspace</div>
    <a data-view="home" class="active" tabindex="0" aria-current="page"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M3 11l9-7 9 7v9a1 1 0 0 1-1 1h-5v-6h-6v6H4a1 1 0 0 1-1-1z"/></svg>Home</a>
    <a data-view="retrieval" tabindex="0"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>Retrieval</a>
    <a data-view="facts" tabindex="0"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M4 6h16M4 12h16M4 18h10"/></svg>Facts <span class="k" id="n-facts">–</span></a>
    <a data-view="entities" tabindex="0"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><circle cx="12" cy="7" r="3"/><path d="M5 21v-2a5 5 0 0 1 5-5h4a5 5 0 0 1 5 5v2"/></svg>Entities</a>
    <a data-view="memories" tabindex="0"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M6 3h9l4 4v14H6z"/><path d="M14 3v5h5"/></svg>Memories <span class="k" id="n-areas">–</span></a>
    <a data-view="staging" tabindex="0"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M3 7h18M3 12h18M3 17h18"/></svg>Staging <span class="k" id="n-staging">–</span></a>

    <div class="grp">Activity</div>
    <a data-view="tasks" tabindex="0"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="4" y="4" width="16" height="16" rx="3"/><path d="M8 12l2.5 2.5L16 9"/></svg>Task Center</a>
    <a data-view="events" tabindex="0"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M4 5h16M4 10h16M4 15h11"/></svg>Activity Log</a>
    <a data-view="ops" tabindex="0"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><circle cx="12" cy="12" r="3"/><path d="M12 3v3M12 18v3M3 12h3M18 12h3M5.6 5.6l2.1 2.1M16.3 16.3l2.1 2.1M18.4 5.6l-2.1 2.1M7.7 16.3l-2.1 2.1"/></svg>Operations</a>
  </nav>

  <main>
    <section class="view active" id="v-home">
      <div class="pagehead"><h1>Home</h1><p class="sub">Live state of the active store. All counters are scoped to the selected tenant.</p></div>
      <div class="grid kpis" id="kpis"></div>
      <div class="grid" style="grid-template-columns:1fr;margin-top:14px">
        <div class="panel"><h2>Recent activity <span class="k" id="hometenant"></span></h2><div class="body" id="recent"></div></div>
      </div>
    </section>

    <section class="view" id="v-retrieval">
      <div class="pagehead"><h1>Retrieval</h1><p class="sub">Hybrid retrieval across memories, bi-temporal facts and KV. Vector/hybrid modes need the embedding backend.</p></div>
      <div class="bar">
        <input type="search" id="q" placeholder="query…" autocomplete="off" aria-label="Query">
        <select id="qmode" aria-label="Mode"><option>hybrid</option><option>vector</option><option>graph</option><option>kv</option></select>
        <select id="qlevel" aria-label="Level"><option>l0</option><option selected>l1</option><option>l2</option></select>
        <input id="qk" type="number" value="8" min="1" max="50" style="width:74px" aria-label="k">
        <button class="primary" id="doq">Search</button>
      </div>
      <div class="panel"><div class="body" id="qres"><div class="empty">Enter a query.</div></div></div>
    </section>

    <section class="view" id="v-facts">
      <div class="pagehead"><h1>Facts</h1><p class="sub">Bi-temporal facts — validity window (world time) and transaction window (system time). Stratum-only.</p></div>
      <div class="grid kpis" id="factkpis" style="margin-bottom:14px"></div>
      <div class="bar">
        <input id="fsubj" placeholder="subject contains…" aria-label="Subject">
        <input id="fpred" placeholder="predicate contains…" aria-label="Predicate">
        <button id="dofacts">Filter</button>
      </div>
      <div class="panel"><div class="body" id="facts"><div class="empty">Loading…</div></div></div>
    </section>

    <section class="view" id="v-entities">
      <div class="pagehead"><h1>Entities</h1><p class="sub">Subjects and objects in the graph, with their fact counts.</p></div>
      <div class="bar"><input id="entq" placeholder="name contains…" aria-label="Entity name"><button id="doents">Filter</button></div>
      <div class="panel"><div class="body" id="entities"><div class="empty">Loading…</div></div></div>
    </section>

    <section class="view" id="v-memories">
      <div class="pagehead"><h1>Memories</h1><p class="sub">Memory files by area. Open a URI to read it at L0 / L1 / L2.</p></div>
      <div class="bar">
        <input id="prefix" placeholder="uri prefix (optional), e.g. stratum://docs/default/openviking/memories" style="flex:1">
        <button id="dobrowse">Load</button>
      </div>
      <div class="panel"><div class="body" id="browse"><div class="empty">Load a prefix.</div></div></div>
      <div class="panel" id="reader-panel" style="margin-top:14px;display:none">
        <h2 id="reader-title">Document</h2><div class="body">
          <div class="tabs" id="reader-tabs"><button data-lvl="l0">L0</button><button data-lvl="l1" class="active">L1</button><button data-lvl="l2">L2</button></div>
          <pre id="reader"></pre>
        </div>
      </div>
    </section>

    <section class="view" id="v-staging">
      <div class="pagehead"><h1>Staging</h1><p class="sub">Candidates awaiting promotion through the Curator gate (7 criteria). Stratum-only.</p></div>
      <div class="bar"><select id="ststatus" aria-label="Status"><option>pending</option><option>approved</option><option>rejected</option></select><button id="dostage">Load</button></div>
      <div class="panel"><div class="body" id="staging"><div class="empty">Load candidates.</div></div></div>
    </section>

    <section class="view" id="v-tasks">
      <div class="pagehead"><h1>Task Center</h1><p class="sub">The job queue (ingest / curate / reflect), claimed with SKIP LOCKED.</p></div>
      <div class="panel"><div class="body" id="tasks"><div class="empty">Loading…</div></div></div>
    </section>

    <section class="view" id="v-events">
      <div class="pagehead"><h1>Activity Log</h1><p class="sub">The append-only journal: one row per write decision (ADD/UPDATE/NOOP/PROMOTE/ARCHIVE/FORGET).</p></div>
      <div class="panel"><div class="body" id="events"><div class="empty">Loading…</div></div></div>
    </section>

    <section class="view" id="v-ops">
      <div class="pagehead"><h1>Operations</h1><p class="sub">Maintenance actions and service endpoints.</p></div>
      <div class="split">
        <div class="panel"><h2>Reflect</h2><div class="body">
          <p class="dim" style="margin-top:0">Enqueue a budgeted reflect run — condenses facts into synthesis candidates (never active writes).</p>
          <button class="primary" id="doreflect">Enqueue reflect</button>
        </div></div>
        <div class="panel"><h2>Endpoints</h2><div class="body" id="endpoints"></div></div>
      </div>
    </section>
  </main>

  <div class="toast" id="toast" role="status" aria-live="polite" aria-atomic="true"></div>

<script>
const state={token:localStorage.getItem("stratum_token")||"", tenant:localStorage.getItem("stratum_tenant")||"default"};
const $=(s,r=document)=>r.querySelector(s);
const $$=(s,r=document)=>[...r.querySelectorAll(s)];
function auth(){return state.token?{"Authorization":"Bearer "+state.token}:{};}
function withTenant(path){return state.tenant?(path+(path.includes("?")?"&":"?")+"tenant="+encodeURIComponent(state.tenant)):path;}
async function api(path,opts={}){
  const res=await fetch(withTenant(path),{...opts,headers:{"Accept":"application/json",...(opts.headers||{}),...auth()}});
  if(res.status===401||res.status===403){throw new Error("AUTH");}
  return res.json();
}
function toast(m){const t=$("#toast");t.textContent=m;t.classList.add("show");clearTimeout(t._t);t._t=setTimeout(()=>t.classList.remove("show"),2400);}
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const num=n=>Number(n||0).toLocaleString("en-US");
const fmtNum=v=>{const n=Number(v);return esc(Number.isFinite(n)?n.toFixed(3):"—");};
const short=s=>{s=String(s??"");return s.length>64?s.slice(0,61)+"…":s;};
let authPrompted=false;

async function loadOverview(){
  try{
    const d=await api("dashboard/api/overview");const h=d.health||{},c=d.counts||{};
    const ok=h.status==="ok";
    $("#hdot").className="dot "+(ok?"ok":"bad");$("#hstatus").textContent=ok?"healthy":(h.status||"degraded");
    $("#hstatus").parentElement.title=JSON.stringify(h);
    $("#hqueue").textContent="queue "+num(c.jobs);
    $("#hacct").textContent=state.tenant;$("#hav").textContent=(state.tenant||"d").slice(0,1).toUpperCase();
    $("#hacctsub").textContent="Account";
    $("#n-facts").textContent=num(c.facts);$("#n-staging").textContent=num(c.staging_pending);
    $("#hometenant").textContent=state.tenant;
    const kpis=[["Files",c.files,"Memory documents"],["Chunks",c.chunks,"Embedded passages"],["Facts",c.facts,"Bi-temporal"],["Entities",c.entities,"Graph nodes"],["Staging",c.staging_pending,"Awaiting curator"],["Events",c.events,"Journal rows"]];
    $("#kpis").innerHTML=kpis.map(([l,n,d2],i)=>`<div class="kpi ${i===2?'amber':''}"><div class="n">${num(n)}</div><div class="l">${l}</div><div class="d">${d2}</div></div>`).join("");
    renderEvents($("#recent"),d.recent_events);
  }catch(e){handleErr(e);}
}
function opClass(op){return op==="ADD"?"ok":op==="FORGET"||op==="DELETE"?"bad":op==="UPDATE"||op==="PROMOTE"?"amber":"cyan";}
function renderEvents(host,rows){
  if(!rows||!rows.length){host.innerHTML='<div class="empty">No events yet.</div>';return;}
  host.innerHTML='<div class="tw"><table><thead><tr><th>op</th><th>target</th><th>when</th></tr></thead><tbody>'+
    rows.map(r=>`<tr class="row"><td><span class="tag ${opClass(r.op)}">${esc(r.op)}</span></td><td class="mono dim">${esc(r.target_uri||"")}</td><td class="faint mono">${esc(r.created_at||"")}</td></tr>`).join("")+"</tbody></table></div>";
}

async function doSearch(){
  const q=$("#q").value.trim();if(!q)return;
  $("#qres").innerHTML='<div class="empty">Searching…</div>';
  try{
    const d=await api(`dashboard/api/search?q=${encodeURIComponent(q)}&mode=${$("#qmode").value}&level=${$("#qlevel").value}&k=${$("#qk").value}`);
    if(d.error){$("#qres").innerHTML=`<div class="err">${esc(d.error)}</div>`;return;}
    const rows=d.results||[];
    if(!rows.length){$("#qres").innerHTML='<div class="empty">No matches.</div>';return;}
    $("#qres").innerHTML=rows.map(r=>`<div style="padding:11px 0;border-bottom:1px solid var(--line)">
        <div><span class="tag cyan mono">${fmtNum(r.score)}</span>
        <span class="mono dim clickable" data-open="${esc(r.uri)}">${esc(r.uri)}</span></div>
        <div style="margin-top:6px">${esc((r.content||"").slice(0,340))}</div></div>`).join("");
  }catch(e){handleErr(e);}
}

async function loadFacts(){
  $("#facts").innerHTML='<div class="empty">Loading…</div>';
  try{
    const p=new URLSearchParams();
    if($("#fsubj").value.trim())p.set("subject",$("#fsubj").value.trim());
    if($("#fpred").value.trim())p.set("predicate",$("#fpred").value.trim());
    const d=await api("dashboard/api/facts?"+p.toString());
    const s=d.summary||{};
    $("#factkpis").innerHTML=[["Total facts",s.total],["Open facts",s.open],["Subjects",s.subjects]]
      .map(([l,n])=>`<div class="kpi"><div class="n">${num(n)}</div><div class="l">${l}</div></div>`).join("");
    const rows=d.items||[];
    if(!rows.length){$("#facts").innerHTML='<div class="empty">No facts.</div>';return;}
    $("#facts").innerHTML='<div class="tw"><table><thead><tr><th>subject</th><th>predicate</th><th>object</th><th>valid</th><th>status</th></tr></thead><tbody>'+
      rows.map(r=>`<tr class="row"><td>${esc(r.subject||"—")}</td><td class="mono dim">${esc(r.predicate)}</td>
        <td>${esc(short(r.object_text||""))}</td>
        <td class="faint mono">${esc((r.valid_from||"").slice(0,10))} → ${r.valid_to?esc(String(r.valid_to).slice(0,10)):"open"}</td>
        <td>${r.is_open?'<span class="tag ok">open</span>':'<span class="tag">closed</span>'}</td></tr>`).join("")+"</tbody></table></div>";
  }catch(e){handleErr(e);}
}

async function loadEntities(){
  $("#entities").innerHTML='<div class="empty">Loading…</div>';
  try{
    const q=$("#entq").value.trim();
    const d=await api("dashboard/api/entities"+(q?"?q="+encodeURIComponent(q):""));
    const rows=d.items||[];
    if(!rows.length){$("#entities").innerHTML='<div class="empty">No entities.</div>';return;}
    $("#entities").innerHTML='<div class="tw"><table><thead><tr><th>name</th><th>type</th><th>facts</th><th>uri</th></tr></thead><tbody>'+
      rows.map(r=>`<tr class="row"><td>${esc(r.name)}</td><td class="dim">${esc(r.type||"")}</td>
        <td class="mono">${num(r.fact_count)}</td><td class="mono faint clickable" data-open="${esc(r.canonical_uri||"")}">${esc(short(r.canonical_uri||""))}</td></tr>`).join("")+"</tbody></table></div>";
  }catch(e){handleErr(e);}
}

async function doBrowse(){
  $("#browse").innerHTML='<div class="empty">Loading…</div>';
  try{
    const p=$("#prefix").value.trim();
    const d=await api(`dashboard/api/list?limit=100${p?"&prefix="+encodeURIComponent(p):""}`);
    const rows=d.items||[];
    if(!rows.length){$("#browse").innerHTML='<div class="empty">Nothing found.</div>';return;}
    $("#browse").innerHTML='<div class="tw"><table><thead><tr><th>uri</th><th>L0</th><th>size</th><th>updated</th></tr></thead><tbody>'+
      rows.map(r=>`<tr class="row"><td class="mono dim clickable" data-open="${esc(r.uri)}">${esc(short(r.uri))}</td>
        <td>${esc((r.l0||"").slice(0,90))}</td><td class="mono faint">${num(r.size_bytes)}</td>
        <td class="faint mono">${esc(r.updated_at||"")}</td></tr>`).join("")+"</tbody></table></div>";
  }catch(e){handleErr(e);}
}

async function loadAreas(){
  try{
    const d=await api("dashboard/api/areas");const areas=d.areas||[];
    $("#n-areas").textContent=num(areas.length);
  }catch(e){/* the badge stays at its placeholder on failure */}
}

let current=null;
async function openUri(uri,level="l1"){
  if(!uri)return;
  current=uri;$("#reader-panel").style.display="";$("#reader-title").textContent=uri;
  $$("#reader-tabs button").forEach(b=>b.classList.toggle("active",b.dataset.lvl===level));
  $("#reader").textContent="Loading…";
  try{
    const d=await api(`dashboard/api/get?uri=${encodeURIComponent(uri)}&level=${level}`);
    if(d.error){$("#reader").textContent=d.error;return;}
    if(d.kind==="file"){$("#reader").textContent=d.content||"(empty)";}
    else if(d.kind==="facts"){$("#reader").textContent=(d.facts||[]).map(f=>`${f.subject||"?"} · ${f.predicate} → ${f.object_text||""}  [${f.valid_from||""} .. ${f.valid_to||"open"}]`).join("\n");}
    else{$("#reader").textContent=JSON.stringify(d,null,2);}
  }catch(e){handleErr(e);}
}

async function doStaging(){
  $("#staging").innerHTML='<div class="empty">Loading…</div>';
  try{
    const s=$("#ststatus").value;
    const d=await api(`dashboard/api/staging?status=${s}&limit=100`);
    const rows=d.items||[];
    if(!rows.length){$("#staging").innerHTML='<div class="empty">No candidates.</div>';return;}
    $("#staging").innerHTML='<div class="tw"><table><thead><tr><th>kind</th><th>source</th><th>conf</th><th>created</th>'+(s==="pending"?"<th></th>":"<th>reason</th>")+'</tr></thead><tbody>'+
      rows.map(r=>`<tr class="row"><td><span class="tag amber">${esc(r.kind)}</span></td>
        <td class="mono dim">${esc(short(r.source_uri||""))}</td><td class="mono">${fmtNum(r.confidence)}</td>
        <td class="faint mono">${esc(r.created_at||"")}</td>
        ${s==="pending"?`<td><button data-promote="${esc(r.candidate_id)}">promote</button></td>`:`<td class="dim">${esc(r.reason||"")}</td>`}</tr>`).join("")+"</tbody></table></div>";
  }catch(e){handleErr(e);}
}
async function doPromote(id){
  try{const d=await api("dashboard/api/promote",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({candidate_id:id})});
    toast(d.error?("error: "+d.error):"promoted");doStaging();}catch(e){handleErr(e);}
}

async function loadTasks(){
  $("#tasks").innerHTML='<div class="empty">Loading…</div>';
  try{
    const d=await api("dashboard/api/jobs?limit=100");
    const rows=d.items||[];
    if(!rows.length){$("#tasks").innerHTML='<div class="empty">No jobs.</div>';return;}
    $("#tasks").innerHTML='<div class="tw"><table><thead><tr><th>kind</th><th>tenant</th><th>status</th><th>attempts</th><th>created</th></tr></thead><tbody>'+
      rows.map(r=>`<tr class="row"><td class="mono">${esc(r.kind)}</td><td class="dim">${esc(r.tenant_id)}</td>
        <td><span class="tag ${r.status==='done'?'ok':r.status==='failed'?'bad':'cyan'}">${esc(r.status)}</span></td>
        <td class="mono">${r.attempts}/${r.max_attempts}</td><td class="faint mono">${esc(r.created_at||"")}</td></tr>`).join("")+"</tbody></table></div>";
  }catch(e){handleErr(e);}
}

async function loadEvents(){
  try{const d=await api("dashboard/api/events?limit=200");renderEvents($("#events"),d.items);
    if(!(d.items||[]).length)$("#events").innerHTML='<div class="empty">No events.</div>';}catch(e){handleErr(e);}
}

async function doReflect(){
  try{const d=await api("dashboard/api/reflect",{method:"POST",headers:{"Content-Type":"application/json"},body:"{}"});
    toast(d.ok?("reflect job "+String(d.job_id).slice(0,8)):("error: "+(d.error||"")));loadTasks();}catch(e){handleErr(e);}
}

function endpoints(){const base=location.origin;
  $("#endpoints").innerHTML='<div class="tw"><table>'+[
    ["MCP (streamable HTTP)",base+"/mcp"],["Health",base+"/health"],["Metrics",base+"/metrics"],
    ["Dashboard API",base+"/dashboard/api/overview"]].map(([l,u])=>`<tr><td class="dim">${l}</td><td class="mono clickable" data-href="${u}">${u}</td></tr>`).join("")+"</table></div>";
}

async function loadTenants(){
  const sel=$("#htenant");
  try{
    const d=await api("dashboard/api/tenants");const items=d.items||[];
    if(!items.length){sel.innerHTML=`<option>${esc(state.tenant||"default")}</option>`;return;}
    sel.innerHTML=items.map(t=>`<option value="${esc(t.tenant_id)}"${t.tenant_id===state.tenant?" selected":""}>${esc(t.name||t.tenant_id)}</option>`).join("");
    if(!items.some(t=>t.tenant_id===state.tenant)){const cur=d.current||items[0].tenant_id;state.tenant=cur;localStorage.setItem("stratum_tenant",cur);sel.value=cur;}
  }catch(e){sel.innerHTML=`<option>${esc(state.tenant||"default")}</option>`;}
}
function switchTenant(v){
  state.tenant=v||"default";localStorage.setItem("stratum_tenant",state.tenant);
  $("#browse").dataset.done="";$("#browse").innerHTML='<div class="empty">Load a prefix.</div>';$("#reader-panel").style.display="none";
  toast("tenant: "+state.tenant);reload();
}

function handleErr(e){if(e.message==="AUTH"){if(!authPrompted){authPrompted=true;askToken();}else{toast("unauthorized — set an API key");}}else{toast("error: "+e.message);}}
function askToken(){
  const t=prompt("API key (Bearer token, stk_…). Leave empty for local/no-key mode.",state.token);
  authPrompted=false;if(t===null)return;
  state.token=t.trim();
  if(state.token)localStorage.setItem("stratum_token",state.token);else localStorage.removeItem("stratum_token");
  toast("key saved — reloading");reload();
}
function reload(){
  loadOverview();
  if(view==="events")loadEvents();
  if(view==="ops")endpoints();
  if(view==="staging")doStaging();
  if(view==="memories"){doBrowse();loadAreas();}
  if(view==="facts")loadFacts();
  if(view==="entities")loadEntities();
  if(view==="tasks")loadTasks();
  if(view==="retrieval"&&$("#q").value.trim())doSearch();
}

let view="home";
function show(v){
  view=v;
  $$("nav a").forEach(a=>{const on=a.dataset.view===v;a.classList.toggle("active",on);if(on)a.setAttribute("aria-current","page");else a.removeAttribute("aria-current");});
  $$(".view").forEach(s=>s.classList.toggle("active",s.id==="v-"+v));
  closeNav();
  if(v==="events")loadEvents();
  if(v==="ops")endpoints();
  if(v==="staging")doStaging();
  if(v==="facts")loadFacts();
  if(v==="entities")loadEntities();
  if(v==="tasks")loadTasks();
  if(v==="memories"&&!$("#browse").dataset.done){doBrowse();$("#browse").dataset.done="1";}
}
function refresh(){reload();}
function closeNav(){const n=$("#nav");n.classList.remove("open");$("#hamb").setAttribute("aria-expanded","false");}
function toggleNav(){const open=$("#nav").classList.toggle("open");$("#hamb").setAttribute("aria-expanded",open?"true":"false");}

document.addEventListener("click",e=>{
  const nav=e.target.closest("nav a");if(nav){show(nav.dataset.view);return;}
  const open=e.target.closest("[data-open]");if(open&&open.dataset.open){openUri(open.dataset.open);show("memories");return;}
  const pro=e.target.closest("[data-promote]");if(pro){doPromote(pro.dataset.promote);return;}
  const href=e.target.closest("[data-href]");if(href){window.open(href.dataset.href,"_blank");return;}
  const tab=e.target.closest("#reader-tabs button");if(tab&&current){openUri(current,tab.dataset.lvl);return;}
});
$("#doq").onclick=doSearch;$("#q").addEventListener("keydown",e=>{if(e.key==="Enter")doSearch();});
$("#dobrowse").onclick=doBrowse;$("#dofacts").onclick=loadFacts;$("#doents").onclick=loadEntities;
$("#dostage").onclick=doStaging;$("#ststatus").onchange=doStaging;$("#refresh").onclick=refresh;$("#tokenbtn").onclick=askToken;
$("#doreflect").onclick=doReflect;$("#hamb").onclick=toggleNav;
$("#prefix").addEventListener("keydown",e=>{if(e.key==="Enter")doBrowse();});
$("#fsubj").addEventListener("keydown",e=>{if(e.key==="Enter")loadFacts();});
$("#fpred").addEventListener("keydown",e=>{if(e.key==="Enter")loadFacts();});
$("#entq").addEventListener("keydown",e=>{if(e.key==="Enter")loadEntities();});
$("#htenant").addEventListener("change",e=>switchTenant(e.target.value));
document.addEventListener("keydown",e=>{if(e.key==="Escape")closeNav();});

loadTenants();loadOverview();setInterval(loadOverview,30000);
</script>
</body>
</html>"""
