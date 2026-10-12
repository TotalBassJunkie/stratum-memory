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
<html lang="en" translate="no">
<head>
<meta charset="utf-8">
<meta name="google" content="notranslate">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Stratum</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'><rect width='64' height='64' rx='13' fill='%2307080c'/><polygon points='32,14 52,25 32,36 12,25' fill='%23ff9e64'/><polygon points='32,26 50,36 32,46 14,36' fill='%237aa2f7'/><polygon points='32,36 46,44 32,52 18,44' fill='%23ff4d5e'/></svg>">
<style>
  :root{
    --rail:244px; --top:40px;
    --bg:#07080c; --ink:#e6e8ef; --dim:#7d8399; --line:#1b1f2b;
    --panel:#0d1017e6; --accent:#ff9e64; --brand:#ff4d5e; --brandglow:#ff2d4088;
    --hud-cool:#7aa2f7;
    --mono:ui-monospace,SFMono-Regular,Consolas,Menlo,monospace;
  }
  *{box-sizing:border-box}
  html,body{height:100%;margin:0}
  body{background:var(--bg);color:var(--ink);overflow:hidden;font:13px/1.5 ui-sans-serif,system-ui,"Segoe UI",Roboto,sans-serif;
       display:grid;grid-template-columns:var(--rail) 1fr;grid-template-rows:100%}
  @media (max-width:820px){body{grid-template-columns:1fr}}
  button,select,input{font:inherit;color:var(--ink)}
  button{cursor:pointer}
  a{color:inherit}
  /* ---- rail ---- */
  #rail{grid-column:1;display:flex;flex-direction:column;min-height:0;border-right:1px solid var(--line);background:#0a0c12}
  @media (max-width:820px){#rail{position:fixed;left:0;top:0;bottom:0;width:var(--rail);transform:translateX(-100%);transition:transform .18s ease;z-index:30}
    #rail.open{transform:none}}
  #railbrand{padding:14px 16px 10px}
  #railbrand b{font-size:15px;letter-spacing:.16em;color:var(--brand);text-shadow:0 0 4px var(--brand),0 0 14px var(--brandglow),0 0 34px var(--brandglow)}
  #railbrand span{color:var(--dim);font-size:15px;letter-spacing:.16em}
  #railbrand small{display:block;color:var(--dim);font-size:10px;letter-spacing:.07em;margin-top:3px}
  #nav{padding:6px 8px 10px;display:flex;flex-direction:column;gap:2px;flex:none}
  #nav .grp{color:#5d6377;font-size:9px;letter-spacing:.16em;text-transform:uppercase;font-weight:600;margin:9px 10px 3px}
  #nav button{display:flex;align-items:center;gap:9px;width:100%;padding:8px 10px;border:1px solid transparent;border-radius:8px;background:none;color:var(--dim);font-size:12px;text-align:left;cursor:pointer}
  #nav button .ic{width:17px;text-align:center;flex:none;font-size:13px}
  #nav button .k{margin-left:auto;font-family:var(--mono);font-size:10px;color:#5d6377}
  #nav button:hover{background:#12151f;color:var(--ink)}
  #nav button[aria-current="page"]{background:#171b26;color:var(--ink);border-color:#39405a}
  #raildock{flex:1;min-height:0;display:flex;flex-direction:column;border-top:1px solid var(--line);background:#0b0d13}
  #raildock .dockhead{padding:8px 12px;font-size:9px;letter-spacing:.16em;color:var(--dim);font-weight:600;border-bottom:1px solid var(--line);flex:none;text-transform:uppercase}
  #dockbody{flex:1;min-height:0;overflow:auto;padding:9px 10px}
  /* ---- main ---- */
  #main{grid-column:2;display:flex;flex-direction:column;min-width:0;min-height:0}
  #topbar{flex:none;height:var(--top);display:flex;align-items:center;gap:8px;padding:0 12px;border-bottom:1px solid var(--line);background:#0a0c12}
  #pagetitle{font-size:10px;letter-spacing:.16em;color:var(--dim);font-weight:600;text-transform:uppercase}
  #topbar .grow{flex:1}
  #topbar button{padding:5px 10px;border-radius:7px;font-size:11px;background:#12151d;border:1px solid var(--line);color:var(--ink)}
  #topbar button:hover{border-color:#39405a}
  .chip{display:inline-flex;align-items:center;gap:7px;padding:4px 10px;border:1px solid var(--line);border-radius:999px;background:#0d1017;color:var(--dim);font-size:11px;white-space:nowrap}
  .chip .dot{width:7px;height:7px;border-radius:50%;background:#5d6377}
  .chip .dot.ok{background:#9ece6a;box-shadow:0 0 6px #9ece6a88}
  .chip .dot.bad{background:#f7768e}
  .chip select{background:transparent;border:0;color:var(--ink);font-family:var(--mono);max-width:150px;font-size:11px}
  #hamb{display:none}@media (max-width:820px){#hamb{display:inline-flex}}
  #pages{flex:1;position:relative;min-height:0}
  .page{position:absolute;inset:0;display:none;overflow:auto}
  .page.on{display:block}
  /* ---- overview (JARVIS-style tiles) ---- */
  .ov{padding:20px 22px 34px;max-width:1500px;display:flex;flex-direction:column;gap:14px}
  .ov-hello{display:flex;align-items:flex-end;gap:14px}
  .ov-hello h1{margin:0;font-size:21px;font-weight:600;letter-spacing:.01em}
  .ov-hello .sub{color:var(--dim);font-size:11.5px;margin-top:3px}
  .ov-hello button{margin-left:auto}
  .act{padding:7px 13px;border-radius:8px;font-size:12px;background:#12151d;border:1px solid var(--line);color:var(--ink)}
  .act.go{background:var(--accent);border-color:var(--accent);color:#14100a;font-weight:600}
  .act:hover{border-color:#39405a}
  .ov-ask{display:flex;gap:8px}
  .ov-ask input{flex:1;padding:11px 13px;border-radius:10px;color:var(--ink);background:#10131b;border:1px solid var(--line);font-size:13px}
  .ov-ask input:focus{outline:none;border-color:#39405a}
  .ov-cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px}
  .ov-card{background:linear-gradient(160deg,#11141d 0%,#0c0e15 70%);border:1px solid var(--line);border-radius:12px;padding:13px 15px;text-align:left;color:var(--ink);cursor:pointer}
  .ov-card:hover{border-color:#39405a}
  .ov-card .k{font-size:9px;letter-spacing:.16em;text-transform:uppercase;color:var(--dim);font-weight:600}
  .ov-card .v{font-family:var(--mono);font-size:26px;font-weight:600;margin-top:6px;letter-spacing:-.5px}
  .ov-card .d{color:#5d6377;font-size:10.5px;margin-top:3px}
  .tile{background:linear-gradient(160deg,#11141d 0%,#0c0e15 70%);border:1px solid var(--line);border-radius:12px;padding:12px 14px;min-width:0}
  .tile-h{font-size:9px;letter-spacing:.16em;color:var(--dim);font-weight:600;text-transform:uppercase;display:flex;gap:8px;align-items:baseline}
  .tile-h small{letter-spacing:0;text-transform:none;font-size:10.5px;color:#5d6377}
  .tile-b{margin-top:9px}
  .ov-two{display:grid;grid-template-columns:1fr 1fr;gap:12px}
  .ov-wide{grid-template-columns:1.35fr 1fr}
  @media (max-width:1000px){.ov-two,.ov-wide{grid-template-columns:1fr}}
  .ov-side{display:flex;flex-direction:column;gap:12px}
  .kv{display:flex;justify-content:space-between;gap:10px;padding:5px 0;border-bottom:1px solid var(--line);font-size:12px}
  .kv:last-child{border-bottom:0}
  .kv b{font-family:var(--mono);font-weight:500;font-variant-numeric:tabular-nums}
  .kv span{color:var(--dim)}
  .mini{font-size:11.5px;color:var(--dim);line-height:1.5}
  .mini b{color:var(--ink);font-weight:500}
  /* bars (per-day chart) */
  .chart{margin-top:10px}
  .chart svg{display:block;width:100%;height:96px}
  .chart-x{display:flex;justify-content:space-between;margin-top:6px;font-size:10px;color:#5d6377;font-variant-numeric:tabular-nums}
  .chart .none{color:var(--dim);font-size:11.5px}
  /* tables */
  .tw{overflow:auto;max-width:100%}
  table{width:100%;border-collapse:collapse;font-size:12px;min-width:420px}
  th,td{text-align:left;padding:7px 9px;border-bottom:1px solid var(--line);vertical-align:top}
  th{color:#5d6377;font-weight:600;font-size:10px;text-transform:uppercase;letter-spacing:.1em}
  tr:last-child td{border-bottom:0}
  tr.row:hover td{background:#12151f}
  .mono{font-family:var(--mono)}.dim{color:var(--dim)}.faint{color:#5d6377}
  .tag{display:inline-block;font-family:var(--mono);font-size:10px;padding:1.5px 7px;border:1px solid var(--line);border-radius:999px;color:var(--dim)}
  .tag.amber{color:var(--accent);border-color:#4a3418}.tag.cyan{color:var(--hud-cool);border-color:#22305a}
  .tag.ok{color:#9ece6a;border-color:#2b4318}.tag.bad{color:#f7768e;border-color:#4a1d24}
  .bar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:0 0 12px}
  .bar input[type=search],.bar input[type=text]{flex:1;min-width:180px;padding:7px 10px;border-radius:8px;background:#10131b;border:1px solid var(--line)}
  .bar select{padding:7px 9px;border-radius:8px;background:#10131b;border:1px solid var(--line)}
  .bar button{padding:7px 12px;border-radius:8px;background:#12151d;border:1px solid var(--line)}
  pre{background:#0b0d14;border:1px solid var(--line);border-radius:10px;padding:13px;overflow:auto;font-family:var(--mono);font-size:12px;line-height:1.55;white-space:pre-wrap;word-break:break-word;margin:0}
  .empty{color:var(--dim);padding:18px 4px;text-align:center;font-size:12px}
  .err{color:#f7768e}.clickable{cursor:pointer}.clickable:hover{color:var(--accent)}
  .sect{padding:20px 22px 40px;max-width:1500px;display:flex;flex-direction:column;gap:14px}
  .sect h1{margin:0;font-size:19px;font-weight:600}
  .sect .sub{color:var(--dim);font-size:12px;margin-top:2px}
  .toast{position:fixed;right:18px;bottom:18px;background:#0d1017;border:1px solid var(--line);border-left:3px solid var(--accent);border-radius:9px;padding:10px 14px;font-size:12.5px;box-shadow:0 14px 34px rgba(0,0,0,.6);opacity:0;transform:translateY(8px);transition:opacity .2s,transform .2s;pointer-events:none;z-index:50}
  .toast.show{opacity:1;transform:none}
  :focus-visible{outline:2px solid var(--hud-cool);outline-offset:2px}
  @media (prefers-reduced-motion:reduce){.toast{transition:none}}
</style>
</head>
<body>
  <aside id="rail">
    <div id="railbrand"><b>STRATUM</b> <span>MEMORY</span><small>context database</small></div>
    <nav id="nav"></nav>
    <div id="raildock">
      <div class="dockhead">Quick query</div>
      <div id="dockbody">
        <input id="dockq" placeholder="ask the store…" style="width:100%;padding:8px 10px;border-radius:8px;background:#10131b;border:1px solid var(--line)">
        <div id="dockhits" style="margin-top:8px"></div>
      </div>
    </div>
  </aside>

  <div id="main">
    <div id="topbar">
      <button id="hamb" aria-label="Toggle navigation">≡</button>
      <span id="pagetitle">Overview</span>
      <span class="grow"></span>
      <span class="chip"><span class="dot" id="hdot"></span><span id="hstatus">connecting…</span></span>
      <label class="chip" title="Active tenant"><span class="dim">tenant</span><select id="htenant" aria-label="Active tenant"></select></label>
      <span class="chip" id="hqueue">queue 0</span>
      <button id="refresh">refresh</button>
      <button id="keybtn">key</button>
    </div>
    <div id="pages"></div>
  </div>

  <div class="toast" id="toast" role="status" aria-live="polite"></div>

<script>
const S={token:localStorage.getItem("stratum_token")||"",tenant:localStorage.getItem("stratum_tenant")||"default"};
const $=(s,r=document)=>r.querySelector(s);
const $$=(s,r=document)=>[...r.querySelectorAll(s)];
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const num=n=>Number(n||0).toLocaleString("en-US");
const fmtNum=v=>{const n=Number(v);return esc(Number.isFinite(n)?n.toFixed(3):"—");};
const short=s=>{s=String(s??"");return s.length>70?s.slice(0,67)+"…":s;};
function auth(){return S.token?{"Authorization":"Bearer "+S.token}:{};}
function withTenant(p){return S.tenant?(p+(p.includes("?")?"&":"?")+"tenant="+encodeURIComponent(S.tenant)):p;}
async function api(p,o={}){const r=await fetch(withTenant(p),{...o,headers:{"Accept":"application/json",...(o.headers||{}),...auth()}});if(r.status===401||r.status===403)throw new Error("AUTH");return r.json();}
let authPrompted=false;
function toast(m){const t=$("#toast");t.textContent=m;t.classList.add("show");clearTimeout(t._t);t._t=setTimeout(()=>t.classList.remove("show"),2400);}
function handleErr(e){if(e.message==="AUTH"){if(!authPrompted){authPrompted=true;askToken();}else toast("unauthorized — set a key");}else toast("error: "+e.message);}
function askToken(){const t=prompt("API key (Bearer token, stk_…). Empty = local/no-key mode.",S.token);authPrompted=false;if(t===null)return;S.token=t.trim();if(S.token)localStorage.setItem("stratum_token",S.token);else localStorage.removeItem("stratum_token");toast("key saved");render();}

/* ---------------- nav model (JARVIS rail) ---------------- */
const PAGES=[
  {id:"overview",t:"Overview",ic:"◉",grp:"Workspace"},
  {id:"retrieval",t:"Retrieval",ic:"⌕"},
  {id:"facts",t:"Facts",ic:"≣"},
  {id:"entities",t:"Entities",ic:"☰"},
  {id:"memories",t:"Memories",ic:"▤"},
  {id:"staging",t:"Staging",ic:"⧉"},
  {id:"tasks",t:"Task Center",ic:"⊞",grp:"Activity"},
  {id:"events",t:"Activity Log",ic:"≡"},
  {id:"ops",t:"Operations",ic:"⚙"},
];
let page="overview", counts={};
function buildNav(){
  const nav=$("#nav");nav.innerHTML="";
  let last="";
  for(const p of PAGES){
    if(p.grp&&p.grp!==last){const g=document.createElement("div");g.className="grp";g.textContent=p.grp;nav.appendChild(g);last=p.grp;}
    const b=document.createElement("button");b.dataset.p=p.id;
    b.innerHTML=`<span class="ic">${p.ic}</span><span class="tx">${p.t}</span>${p.id==="facts"?'<span class="k" id="k-facts">–</span>':p.id==="memories"?'<span class="k" id="k-mem">–</span>':p.id==="staging"?'<span class="k" id="k-stg">–</span>':""}`;
    b.onclick=()=>show(p.id);nav.appendChild(b);
  }
}
function buildPages(){
  const host=$("#pages");
  host.innerHTML=`
  <section class="page on" id="pg-overview"><div class="ov" id="ov"></div></section>
  <section class="page" id="pg-retrieval"><div class="sect" id="ov2-retr"></div></section>
  <section class="page" id="pg-facts"><div class="sect" id="ov2-facts"></div></section>
  <section class="page" id="pg-entities"><div class="sect" id="ov2-ent"></div></section>
  <section class="page" id="pg-memories"><div class="sect" id="ov2-mem"></div></section>
  <section class="page" id="pg-staging"><div class="sect" id="ov2-stg"></div></section>
  <section class="page" id="pg-tasks"><div class="sect" id="ov2-tasks"></div></section>
  <section class="page" id="pg-events"><div class="sect" id="ov2-ev"></div></section>
  <section class="page" id="pg-ops"><div class="sect" id="ov2-ops"></div></section>`;
}
function show(id){page=id;
  $$("#nav button").forEach(b=>{const on=b.dataset.p===id;if(on)b.setAttribute("aria-current","page");else b.removeAttribute("aria-current");});
  $$(".page").forEach(s=>s.classList.toggle("on",s.id==="pg-"+id));
  $("#pagetitle").textContent=(PAGES.find(p=>p.id===id)||{t:id}).t;
  $("#rail").classList.remove("open");
  const el=$(("#ov2-"+id.slice(0,4)) );
  ({retrieval:renderRetrieval,facts:renderFacts,entities:renderEntities,memories:renderMemories,staging:renderStaging,tasks:renderTasks,events:renderEvents,ops:renderOps,overview:render}[id]||render)();
}

/* ---------------- overview ---------------- */
async function render(){
  const ov=$("#ov");ov.innerHTML='<div class="empty">Loading…</div>';
  try{
    const d=await api("dashboard/api/overview");const h=d.health||{},c=d.counts||{};counts=c;
    const ok=h.status==="ok";
    $("#hdot").className="dot "+(ok?"ok":"bad");$("#hstatus").textContent=ok?"healthy":(h.status||"degraded");
    $("#hqueue").textContent="queue "+num(c.jobs);
    const kf=$("#k-facts"),km=$("#k-mem"),ks=$("#k-stg");
    if(kf)kf.textContent=num(c.facts);if(km)km.textContent=num(c.files);if(ks)ks.textContent=num(c.staging_pending);
    const cards=[
      ["Files",c.files,"memory documents"],["Chunks",c.chunks,"embedded passages"],
      ["Facts",c.facts,"bi-temporal"],["Entities",c.entities,"graph nodes"],
      ["Staging",c.staging_pending,"awaiting curator"],["Events",c.events,"journal rows"],
    ];
    ov.innerHTML=`
      <div class="ov-hello"><div><h1 id="ov-greet">Stratum</h1><div class="sub" id="ov-sub"></div></div>
        <button class="act" id="ov-refresh">Refresh</button></div>
      <form class="ov-ask" id="ov-ask"><input id="ov-asktext" autocomplete="off" placeholder="Search the store — opens in Retrieval."><button class="act go" type="submit">Go</button></form>
      <div class="ov-cards">${cards.map(([k,v,dd])=>`<button class="ov-card" data-go="${k.toLowerCase()}"><div class="k">${k}</div><div class="v">${num(v)}</div><div class="d">${dd}</div></button>`).join("")}</div>
      <div class="ov-two">
        <div class="tile"><div class="tile-h">Recent activity <small id="ov-actn"></small></div><div class="tile-b" id="ov-recent"></div></div>
        <div class="tile"><div class="tile-h">Store <small>this tenant</small></div><div class="tile-b" id="ov-store"></div></div>
      </div>
      <div class="ov-wide ov-two">
        <div class="tile"><div class="tile-h">Memory areas <small>by bereich</small></div><div class="tile-b" id="ov-areas"></div></div>
        <div class="ov-side">
          <div class="tile"><div class="tile-h">Health</div><div class="tile-b" id="ov-health"></div></div>
          <div class="tile"><div class="tile-h">Endpoints</div><div class="tile-b" id="ov-endpoints"></div></div>
        </div>
      </div>`;
    $("#ov-sub").textContent=new Date().toLocaleDateString(undefined,{weekday:"long",day:"numeric",month:"long",year:"numeric"})+" · tenant "+S.tenant;
    $("#ov-refresh").onclick=render;
    $("#ov-ask").onsubmit=e=>{e.preventDefault();const q=$("#ov-asktext").value.trim();if(q){$("#dockq").value=q;show("retrieval");doSearch();}};
    $$(".ov-card").forEach(b=>b.onclick=()=>{const g=b.dataset.go;if(g==="files"||g==="chunks")show("memories");else if(g==="facts")show("facts");else if(g==="entities")show("entities");else if(g==="staging")show("staging");else show("events");});
    renderRecent(d.recent_events);
    $("#ov-store").innerHTML=kvList([["Files",c.files],["Chunks",c.chunks],["Facts",c.facts],["Entities",c.entities],["Staging",c.staging_pending],["Jobs",c.jobs]]);
    $("#ov-health").innerHTML=kvList([["status",h.status||"?"],["db",h.db?"ok":"down"],["git",h.git?"ok":"off"],["llm",h.llm?"ok":"off"],["queued",c.jobs]]);
    endpointsInto($("#ov-endpoints"));
    loadAreas();
  }catch(e){
    if(e.message==="AUTH"){ov.innerHTML='<div class="empty">Set an API key to load data — <b class="clickable" id="ov-key">enter key</b></div>';const k=$("#ov-key");if(k)k.onclick=askToken;return;}
    ov.innerHTML=`<div class="err">${esc(e.message)}</div>`;
  }
}
function kvList(pairs){return pairs.map(([k,v])=>`<div class="kv"><span>${esc(k)}</span><b>${esc(v)}</b></div>`).join("");}
function renderRecent(rows){
  const el=$("#ov-recent");if(!el)return;
  if(!rows||!rows.length){el.innerHTML='<div class="mini">No events yet.</div>';return;}
  $("#ov-actn").textContent=rows.length+" recent";
  el.innerHTML=rows.map(r=>`<div class="kv"><span><span class="tag ${opClass(r.op)}">${esc(r.op)}</span> <span class="mono dim">${esc(short(r.target_uri||""))}</span></span><b class="faint">${esc((r.created_at||"").slice(11,19))}</b></div>`).join("");
}
function opClass(op){return op==="ADD"?"ok":op==="FORGET"||op==="DELETE"?"bad":op==="UPDATE"||op==="PROMOTE"?"amber":"cyan";}
async function loadAreas(){try{const d=await api("dashboard/api/areas");const a=d.areas||[];const el=$("#ov-areas");if(!el)return;if(!a.length){el.innerHTML='<div class="mini">No files yet.</div>';return;}el.innerHTML=kvList(a.map(x=>[x.area||"(root)",x.n]));}catch(e){}}
function endpointsInto(el){const b=location.origin;el.innerHTML=kvList([["/mcp",b+"/mcp"],["/health",b+"/health"],["/metrics",b+"/metrics"],["/dashboard",b+"/dashboard"]]);}

/* ---------------- sub pages ---------------- */
function renderRetrieval(){
  $("#ov2-retr").innerHTML=`
    <div><h1>Retrieval</h1><div class="sub">Hybrid search across memories, bi-temporal facts and KV.</div></div>
    <div class="bar"><input type="search" id="q" placeholder="query…" autocomplete="off">
      <select id="qmode"><option>hybrid</option><option>vector</option><option>graph</option><option>kv</option></select>
      <select id="qlevel"><option>l0</option><option selected>l1</option><option>l2</option></select>
      <input id="qk" type="number" value="8" min="1" max="50" style="width:74px">
      <button class="act go" id="doq">Search</button></div>
    <div class="tile"><div class="tile-b" id="qres"><div class="empty">Enter a query.</div></div></div>`;
  $("#doq").onclick=doSearch;$("#q").addEventListener("keydown",e=>{if(e.key==="Enter")doSearch();});
}
async function doSearch(){
  const q=$("#q").value.trim();if(!q)return;$("#qres").innerHTML='<div class="empty">Searching…</div>';
  try{const d=await api(`dashboard/api/search?q=${encodeURIComponent(q)}&mode=${$("#qmode").value}&level=${$("#qlevel").value}&k=${$("#qk").value}`);
    if(d.error){$("#qres").innerHTML=`<div class="err">${esc(d.error)}</div>`;return;}
    const rows=d.results||[];if(!rows.length){$("#qres").innerHTML='<div class="empty">No matches.</div>';return;}
    $("#qres").innerHTML=rows.map(r=>`<div style="padding:10px 0;border-bottom:1px solid var(--line)"><div><span class="tag cyan mono">${fmtNum(r.score)}</span> <span class="mono dim clickable" data-open="${esc(r.uri)}">${esc(short(r.uri))}</span></div><div style="margin-top:5px">${esc((r.content||"").slice(0,320))}</div></div>`).join("");
  }catch(e){handleErr(e);}
}
function renderFacts(){
  $("#ov2-facts").innerHTML=`
    <div><h1>Facts</h1><div class="sub">Bi-temporal facts — validity window (world time) and transaction window (system time).</div></div>
    <div class="ov-cards" id="factkpis"></div>
    <div class="bar"><input id="fsubj" placeholder="subject contains…"><input id="fpred" placeholder="predicate contains…"><button class="act" id="dofacts">Filter</button></div>
    <div class="tile"><div class="tile-b" id="facts"><div class="empty">Loading…</div></div></div>`;
  $("#dofacts").onclick=loadFacts;$("#fsubj").addEventListener("keydown",e=>{if(e.key==="Enter")loadFacts();});$("#fpred").addEventListener("keydown",e=>{if(e.key==="Enter")loadFacts();});
  loadFacts();
}
async function loadFacts(){
  $("#facts").innerHTML='<div class="empty">Loading…</div>';
  try{const p=new URLSearchParams();if($("#fsubj").value.trim())p.set("subject",$("#fsubj").value.trim());if($("#fpred").value.trim())p.set("predicate",$("#fpred").value.trim());
    const d=await api("dashboard/api/facts?"+p);const s=d.summary||{};
    $("#factkpis").innerHTML=[["Total facts",s.total],["Open facts",s.open],["Subjects",s.subjects]].map(([k,v])=>`<div class="ov-card" style="cursor:default"><div class="k">${k}</div><div class="v">${num(v)}</div></div>`).join("");
    const rows=d.items||[];if(!rows.length){$("#facts").innerHTML='<div class="empty">No facts.</div>';return;}
    $("#facts").innerHTML='<div class="tw"><table><thead><tr><th>subject</th><th>predicate</th><th>object</th><th>valid</th><th>status</th></tr></thead><tbody>'+
      rows.map(r=>`<tr class="row"><td>${esc(r.subject||"—")}</td><td class="mono dim">${esc(r.predicate)}</td><td>${esc(short(r.object_text||""))}</td>
      <td class="faint mono">${esc((r.valid_from||"").slice(0,10))} → ${r.valid_to?esc(String(r.valid_to).slice(0,10)):"open"}</td>
      <td>${r.is_open?'<span class="tag ok">open</span>':'<span class="tag">closed</span>'}</td></tr>`).join("")+"</tbody></table></div>";
  }catch(e){handleErr(e);}
}
function renderEntities(){
  $("#ov2-ent").innerHTML=`<div><h1>Entities</h1><div class="sub">Subjects and objects in the graph, with their fact counts.</div></div>
    <div class="bar"><input id="entq" placeholder="name contains…"><button class="act" id="doents">Filter</button></div>
    <div class="tile"><div class="tile-b" id="entities"><div class="empty">Loading…</div></div></div>`;
  $("#doents").onclick=loadEntities;$("#entq").addEventListener("keydown",e=>{if(e.key==="Enter")loadEntities();});loadEntities();
}
async function loadEntities(){
  $("#entities").innerHTML='<div class="empty">Loading…</div>';
  try{const q=$("#entq").value.trim();const d=await api("dashboard/api/entities"+(q?"?q="+encodeURIComponent(q):""));const rows=d.items||[];
    if(!rows.length){$("#entities").innerHTML='<div class="empty">No entities.</div>';return;}
    $("#entities").innerHTML='<div class="tw"><table><thead><tr><th>name</th><th>type</th><th>facts</th><th>uri</th></tr></thead><tbody>'+
      rows.map(r=>`<tr class="row"><td>${esc(r.name)}</td><td class="dim">${esc(r.type||"")}</td><td class="mono">${num(r.fact_count)}</td>
      <td class="mono faint clickable" data-open="${esc(r.canonical_uri||"")}">${esc(short(r.canonical_uri||""))}</td></tr>`).join("")+"</tbody></table></div>";
  }catch(e){handleErr(e);}
}
function renderMemories(){
  $("#ov2-mem").innerHTML=`<div><h1>Memories</h1><div class="sub">Memory files by area. Open a URI to read it at L0 / L1 / L2.</div></div>
    <div class="bar"><input id="prefix" placeholder="uri prefix (optional)" style="flex:1"><button class="act" id="dobrowse">Load</button></div>
    <div class="tile"><div class="tile-b" id="browse"><div class="empty">Load a prefix.</div></div></div>
    <div class="tile" id="reader-panel" style="display:none"><div class="tile-h" id="reader-title">Document</div><div class="tile-b">
      <div class="bar"><button data-lvl="l0">L0</button><button data-lvl="l1">L1</button><button data-lvl="l2">L2</button></div>
      <pre id="reader"></pre></div></div>`;
  $("#dobrowse").onclick=doBrowse;$("#prefix").addEventListener("keydown",e=>{if(e.key==="Enter")doBrowse();});
  $$("#reader-panel [data-lvl]").forEach(b=>b.onclick=()=>{if(current)openUri(current,b.dataset.lvl);});
  doBrowse();
}
let current=null;
async function doBrowse(){
  $("#browse").innerHTML='<div class="empty">Loading…</div>';
  try{const p=$("#prefix").value.trim();const d=await api(`dashboard/api/list?limit=100${p?"&prefix="+encodeURIComponent(p):""}`);const rows=d.items||[];
    if(!rows.length){$("#browse").innerHTML='<div class="empty">Nothing found.</div>';return;}
    $("#browse").innerHTML='<div class="tw"><table><thead><tr><th>uri</th><th>L0</th><th>size</th><th>updated</th></tr></thead><tbody>'+
      rows.map(r=>`<tr class="row"><td class="mono dim clickable" data-open="${esc(r.uri)}">${esc(short(r.uri))}</td><td>${esc((r.l0||"").slice(0,90))}</td>
      <td class="mono faint">${num(r.size_bytes)}</td><td class="faint mono">${esc(r.updated_at||"")}</td></tr>`).join("")+"</tbody></table></div>";
  }catch(e){handleErr(e);}
}
async function openUri(uri,level="l1"){
  if(!uri)return;current=uri;$("#reader-panel").style.display="";$("#reader-title").textContent=uri;$("#reader").textContent="Loading…";
  try{const d=await api(`dashboard/api/get?uri=${encodeURIComponent(uri)}&level=${level}`);
    if(d.error){$("#reader").textContent=d.error;return;}
    if(d.kind==="file")$("#reader").textContent=d.content||"(empty)";
    else if(d.kind==="facts")$("#reader").textContent=(d.facts||[]).map(f=>`${f.subject||"?"} · ${f.predicate} → ${f.object_text||""}  [${f.valid_from||""} .. ${f.valid_to||"open"}]`).join("\n");
    else $("#reader").textContent=JSON.stringify(d,null,2);
  }catch(e){handleErr(e);}
}
function renderStaging(){
  $("#ov2-stg").innerHTML=`<div><h1>Staging</h1><div class="sub">Candidates awaiting promotion through the Curator gate (7 criteria).</div></div>
    <div class="bar"><select id="ststatus"><option>pending</option><option>approved</option><option>rejected</option></select><button class="act" id="dostage">Load</button></div>
    <div class="tile"><div class="tile-b" id="staging"><div class="empty">Loading…</div></div></div>`;
  $("#dostage").onclick=doStaging;$("#ststatus").onchange=doStaging;doStaging();
}
async function doStaging(){
  $("#staging").innerHTML='<div class="empty">Loading…</div>';
  try{const s=$("#ststatus").value;const d=await api(`dashboard/api/staging?status=${s}&limit=100`);const rows=d.items||[];
    if(!rows.length){$("#staging").innerHTML='<div class="empty">No candidates.</div>';return;}
    $("#staging").innerHTML='<div class="tw"><table><thead><tr><th>kind</th><th>source</th><th>conf</th><th>created</th>'+(s==="pending"?"<th></th>":"<th>reason</th>")+'</tr></thead><tbody>'+
      rows.map(r=>`<tr class="row"><td><span class="tag amber">${esc(r.kind)}</span></td><td class="mono dim">${esc(short(r.source_uri||""))}</td>
      <td class="mono">${fmtNum(r.confidence)}</td><td class="faint mono">${esc(r.created_at||"")}</td>
      ${s==="pending"?`<td><button class="act" data-promote="${esc(r.candidate_id)}">promote</button></td>`:`<td class="dim">${esc(r.reason||"")}</td>`}</tr>`).join("")+"</tbody></table></div>";
  }catch(e){handleErr(e);}
}
async function doPromote(id){try{const d=await api("dashboard/api/promote",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({candidate_id:id})});toast(d.error?("error: "+d.error):"promoted");doStaging();}catch(e){handleErr(e);}}
function renderTasks(){
  $("#ov2-tasks").innerHTML=`<div><h1>Task Center</h1><div class="sub">The job queue (ingest / curate / reflect), claimed with SKIP LOCKED.</div></div><div class="tile"><div class="tile-b" id="tasks"><div class="empty">Loading…</div></div></div>`;
  loadTasks();
}
async function loadTasks(){
  try{const d=await api("dashboard/api/jobs?limit=100");const rows=d.items||[];
    if(!rows.length){$("#tasks").innerHTML='<div class="empty">No jobs.</div>';return;}
    $("#tasks").innerHTML='<div class="tw"><table><thead><tr><th>kind</th><th>tenant</th><th>status</th><th>attempts</th><th>created</th></tr></thead><tbody>'+
      rows.map(r=>`<tr class="row"><td class="mono">${esc(r.kind)}</td><td class="dim">${esc(r.tenant_id)}</td>
      <td><span class="tag ${r.status==='done'?'ok':r.status==='failed'?'bad':'cyan'}">${esc(r.status)}</span></td>
      <td class="mono">${r.attempts}/${r.max_attempts}</td><td class="faint mono">${esc(r.created_at||"")}</td></tr>`).join("")+"</tbody></table></div>";
  }catch(e){handleErr(e);}
}
function renderEvents(){
  $("#ov2-ev").innerHTML=`<div><h1>Activity Log</h1><div class="sub">The append-only journal — one row per write decision.</div></div><div class="tile"><div class="tile-b" id="events"><div class="empty">Loading…</div></div></div>`;
  loadEvents();
}
async function loadEvents(){
  try{const d=await api("dashboard/api/events?limit=200");const rows=d.items||[];
    if(!rows.length){$("#events").innerHTML='<div class="empty">No events.</div>';return;}
    $("#events").innerHTML='<div class="tw"><table><thead><tr><th>op</th><th>target</th><th>actor</th><th>when</th></tr></thead><tbody>'+
      rows.map(r=>`<tr class="row"><td><span class="tag ${opClass(r.op)}">${esc(r.op)}</span></td><td class="mono dim">${esc(short(r.target_uri||""))}</td>
      <td class="dim">${esc(r.actor||"")}</td><td class="faint mono">${esc(r.created_at||"")}</td></tr>`).join("")+"</tbody></table></div>";
  }catch(e){handleErr(e);}
}
function renderOps(){
  $("#ov2-ops").innerHTML=`<div><h1>Operations</h1><div class="sub">Maintenance actions and service endpoints.</div></div>
    <div class="ov-two"><div class="tile"><div class="tile-h">Reflect</div><div class="tile-b"><div class="mini">Enqueue a budgeted reflect run — condenses facts into synthesis candidates (never active writes).</div>
      <button class="act go" id="doreflect" style="margin-top:10px">Enqueue reflect</button></div></div>
      <div class="tile"><div class="tile-h">Endpoints</div><div class="tile-b" id="ops-endpoints"></div></div></div>`;
  endpointsInto($("#ops-endpoints"));$("#doreflect").onclick=doReflect;
}
async function doReflect(){try{const d=await api("dashboard/api/reflect",{method:"POST",headers:{"Content-Type":"application/json"},body:"{}"});toast(d.ok?("reflect job "+String(d.job_id).slice(0,8)):"error: "+(d.error||""));}catch(e){handleErr(e);}}

/* ---------------- rail quick query ---------------- */
async function dockQuery(){
  const q=$("#dockq").value.trim();if(!q)return;$("#dockhits").innerHTML='<div class="mini">…</div>';
  try{const d=await api(`dashboard/api/search?q=${encodeURIComponent(q)}&mode=hybrid&k=5&level=l0`);const rows=d.results||[];
    $("#dockhits").innerHTML=rows.length?rows.map(r=>`<button class="clickable" data-open="${esc(r.uri)}" style="display:block;width:100%;text-align:left;padding:5px 6px;border:0;border-radius:5px;background:none;color:var(--ink);font:inherit;font-size:11px" title="${esc(r.uri)}"><b class="mono" style="color:var(--hud-cool)">${fmtNum(r.score)}</b> ${esc(short(r.uri))}</button>`).join(""):'<div class="mini">No matches.</div>';
  }catch(e){$("#dockhits").innerHTML=`<div class="err">${esc(e.message)}</div>`;}
}
async function loadTenants(){const sel=$("#htenant");try{const d=await api("dashboard/api/tenants");const items=d.items||[];
  if(!items.length){sel.innerHTML=`<option>${esc(S.tenant)}</option>`;return;}
  sel.innerHTML=items.map(t=>`<option value="${esc(t.tenant_id)}"${t.tenant_id===S.tenant?" selected":""}>${esc(t.name||t.tenant_id)}</option>`).join("");
  if(!items.some(t=>t.tenant_id===S.tenant)){const c=d.current||items[0].tenant_id;S.tenant=c;localStorage.setItem("stratum_tenant",c);sel.value=c;}
}catch(e){sel.innerHTML=`<option>${esc(S.tenant)}</option>`;}}
function switchTenant(v){S.tenant=v||"default";localStorage.setItem("stratum_tenant",S.tenant);toast("tenant: "+S.tenant);render();}

document.addEventListener("click",e=>{
  const open=e.target.closest("[data-open]");if(open&&open.dataset.open){if(page!=="memories")show("memories");openUri(open.dataset.open);return;}
  const pro=e.target.closest("[data-promote]");if(pro){doPromote(pro.dataset.promote);return;}
});
$("#refresh").onclick=render;$("#keybtn").onclick=askToken;
$("#hamb").onclick=()=>$("#rail").classList.toggle("open");
$("#htenant").addEventListener("change",e=>switchTenant(e.target.value));
$("#dockq").addEventListener("keydown",e=>{if(e.key==="Enter")dockQuery();});

buildNav();buildPages();loadTenants();render();setInterval(render,30000);
</script>
</body>
</html>"""
