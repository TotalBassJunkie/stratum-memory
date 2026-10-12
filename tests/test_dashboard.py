"""Tests for the operator dashboard (:mod:`stratum.dashboard`).

Everything runs **without Postgres, without the network and without the real
vault**. A minimal ``FakeFastMCP`` records every ``custom_route`` registration so
the exact route table is pinned, and the pure data helpers are exercised through
a recording ``_q`` stub (so the SQL is inspected, not executed). The route
endpoints are driven directly with a hand-built Starlette ``Request``.

Pinned here:

* ``register_dashboard`` registers exactly the eleven documented routes,
* the SQL builders never interpolate user input (``prefix``/``uri``/``status``
  are always ``%s`` parameters; bare table names are allow-listed),
* ``limit`` clamping for ``_list``/``_staging``/``_events`` (and the defensive
  ``?limit=`` query parsing that never 500s on garbage),
* the ``_overview`` / ``_areas`` / ``_get`` shapes (incl. the not-found fallback
  and the L2 "unavailable" degradation),
* the ``/_tenant`` resolution (auth tenant, default fallback, raising callback),
* the ``ensure_scope`` gate (403 when the scope is missing, ``None`` when it is
  present) and that the HTML shell stays public,
* the ``/search`` error path returns ``{"error":…, "results":[]}`` with 200,
* both POST routes survive a missing / non-JSON / non-object body,
* the auth middleware protects ``/mcp`` and ``/dashboard/api*`` but leaves
  ``/dashboard``, ``/health`` and ``/metrics`` public.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from stratum import dashboard
from stratum.dashboard import (
    _areas,
    _as_int,
    _count,
    _count_staging_pending,
    _events,
    _get,
    _list,
    _overview,
    _staging,
    register_dashboard,
)

# --------------------------------------------------------------------------- #
# Fakes / helpers
# --------------------------------------------------------------------------- #


class FakeFastMCP:
    """A ``FastMCP``-shaped object that only records ``custom_route`` calls.

    ``custom_route`` is a decorator factory on the real server; here it stores
    the path, methods and endpoint so a test can assert the route table and then
    invoke an endpoint directly.
    """

    def __init__(self) -> None:
        self.routes: list[dict[str, Any]] = []

    def custom_route(self, path: str, methods: list[str] | None = None, **_kwargs: Any):
        def decorator(fn: Any) -> Any:
            self.routes.append(
                {"path": path, "methods": list(methods or ["GET"]), "endpoint": fn}
            )
            return fn

        return decorator

    def route(self, path: str, method: str | None = None) -> dict[str, Any]:
        """Return the single recorded route at ``path`` (asserting uniqueness)."""
        if method is not None:
            matches = [
                r for r in self.routes if r["path"] == path and method in r["methods"]
            ]
        else:
            matches = [r for r in self.routes if r["path"] == path]
        assert len(matches) == 1, f"expected exactly one route for {path}/{method}: {matches}"
        return matches[0]


def make_request(
    query: str = "",
    *,
    method: str = "GET",
    body: bytes = b"",
    content_type: str = "application/json",
) -> Any:
    """Build a minimal Starlette ``Request`` (no server, no sockets)."""
    from starlette.requests import Request

    scope = {
        "type": "http",
        "method": method,
        "path": "/",
        "headers": [(b"content-type", content_type.encode())],
        "query_string": query.encode(),
    }

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


def call(endpoint: Any, request: Any) -> Any:
    """Drive an async route endpoint to completion and return its response."""
    return asyncio.run(endpoint(request))


def body_of(response: Any) -> dict[str, Any]:
    return json.loads(response.body)


def capture_q(monkeypatch: pytest.MonkeyPatch, rows: Any = None) -> list[tuple[str, tuple]]:
    """Patch ``dashboard._q`` with a recorder; return the ``(sql, params)`` log."""
    log: list[tuple[str, tuple]] = []

    def fake_q(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        log.append((sql, tuple(params)))
        if rows is None:
            return []
        return list(rows) if isinstance(rows, list) else [rows]

    monkeypatch.setattr(dashboard, "_q", fake_q)
    return log


# --------------------------------------------------------------------------- #
# Route registration
# --------------------------------------------------------------------------- #

EXPECTED_ROUTES = {
    ("/dashboard", ("GET",)),
    ("/", ("GET",)),
    ("/dashboard/api/tenants", ("GET",)),
    ("/dashboard/api/overview", ("GET",)),
    ("/dashboard/api/areas", ("GET",)),
    ("/dashboard/api/list", ("GET",)),
    ("/dashboard/api/get", ("GET",)),
    ("/dashboard/api/search", ("GET",)),
    ("/dashboard/api/staging", ("GET",)),
    ("/dashboard/api/events", ("GET",)),
    ("/dashboard/api/promote", ("POST",)),
    ("/dashboard/api/reflect", ("POST",)),
}


def test_register_dashboard_registers_exactly_the_expected_routes():
    fake = FakeFastMCP()
    register_dashboard(fake)
    registered = {(r["path"], tuple(r["methods"])) for r in fake.routes}
    assert registered == EXPECTED_ROUTES
    assert len(fake.routes) == 12


def test_register_dashboard_is_safe_without_callbacks():
    """Both callbacks are optional; registration must not require them."""
    fake = FakeFastMCP()
    register_dashboard(fake, tenant_for=None, ensure_scope=None)
    assert len(fake.routes) == 12


# --------------------------------------------------------------------------- #
# Pure helpers: SQL construction / injection safety
# --------------------------------------------------------------------------- #


def test_count_without_tenant_has_no_where(monkeypatch: pytest.MonkeyPatch):
    log = capture_q(monkeypatch, [{"n": 7}])
    assert _count("files") == 7
    sql, params = log[0]
    assert sql == "SELECT count(*) AS n FROM files"
    assert params == ()


def test_count_with_tenant_parameterises_the_tenant(monkeypatch: pytest.MonkeyPatch):
    log = capture_q(monkeypatch, [{"n": 3}])
    assert _count("files", "t1") == 3
    sql, params = log[0]
    assert "WHERE tenant_id = %s" in sql
    assert params == ("t1",)
    assert "t1" not in sql  # tenant is never interpolated


def test_count_returns_zero_on_no_rows(monkeypatch: pytest.MonkeyPatch):
    capture_q(monkeypatch, [])
    assert _count("chunks", "t1") == 0


@pytest.mark.parametrize(
    "hostile",
    [
        "files; DROP TABLE files",
        "files WHERE 1=1 --",
        "pg_catalog.pg_tables",
        "files)",
        "",
    ],
)
def test_count_rejects_non_allowlisted_table_names(
    monkeypatch: pytest.MonkeyPatch, hostile: str
):
    """The only interpolated identifier comes from a fixed allow-list."""
    log = capture_q(monkeypatch, [{"n": 1}])
    with pytest.raises(ValueError):
        _count(hostile)
    assert log == []  # nothing reached the database


def test_count_accepts_every_allowlisted_table(monkeypatch: pytest.MonkeyPatch):
    capture_q(monkeypatch, [{"n": 0}])
    for table in sorted(dashboard._COUNTABLE_TABLES):
        assert _count(table, "t1") == 0


def test_count_staging_pending_is_status_scoped(monkeypatch: pytest.MonkeyPatch):
    log = capture_q(monkeypatch, [{"n": 4}])
    assert _count_staging_pending("t1") == 4
    sql, params = log[0]
    assert "status = %s" in sql
    assert params == ("t1", "pending")


def test_list_without_prefix_parameterises_only_tenant_and_limit(
    monkeypatch: pytest.MonkeyPatch,
):
    log = capture_q(monkeypatch, [])
    _list("t1", None, 25)
    sql, params = log[0]
    assert "ILIKE" not in sql
    assert params == ("t1", 25)


def test_list_with_prefix_parameterises_the_prefix(monkeypatch: pytest.MonkeyPatch):
    """A prefix with SQL metacharacters is passed as a bound parameter.

    B10: LIKE metacharacters are escaped, so ``%``/``_``/``\\`` in the prefix are
    matched literally rather than as wildcards.
    """
    log = capture_q(monkeypatch, [])
    hostile = "stratum://docs/t1/x%'; DROP TABLE files; --"
    _list("t1", hostile, 10)
    sql, params = log[0]
    assert "ILIKE %s" in sql
    assert "ESCAPE" in sql
    assert hostile not in sql
    # The raw ``%`` is escaped to ``\%`` and only the trailing ``%`` stays a
    # wildcard; the value is still a bound parameter (never interpolated).
    assert params == ("t1", "stratum://docs/t1/x\\%'; DROP TABLE files; --%", 10)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("plain", "plain"),
        ("a%b", "a\\%b"),
        ("a_b", "a\\_b"),
        ("a\\b", "a\\\\b"),
        ("%_\\", "\\%\\_\\\\"),
        ("stratum://docs/t1/openviking/memories", "stratum://docs/t1/openviking/memories"),
    ],
)
def test_escape_like_escapes_wildcards(raw: str, expected: str):
    """B10: ``_escape_like`` neutralises ``%``/``_``/``\\`` for a literal match."""
    assert dashboard._escape_like(raw) == expected


def test_list_prefix_wildcard_is_neutralised(monkeypatch: pytest.MonkeyPatch):
    """A ``%`` in the prefix must not turn into a match-everything wildcard (B10)."""
    log = capture_q(monkeypatch, [])
    _list("t1", "stratum://docs/t1/%", 10)
    sql, params = log[0]
    assert "ESCAPE" in sql
    # The user's ``%`` is escaped; only our appended ``%`` remains a wildcard.
    assert params[1] == "stratum://docs/t1/\\%%"


@pytest.mark.parametrize(
    ("requested", "expected"),
    [(0, 1), (-5, 1), (1, 1), (250, 250), (9999, 500)],
)
def test_list_limit_is_clamped(
    monkeypatch: pytest.MonkeyPatch, requested: int, expected: int
):
    log = capture_q(monkeypatch, [])
    _list("t1", None, requested)
    assert log[0][1][-1] == expected


@pytest.mark.parametrize(
    ("requested", "expected"), [(0, 1), (-1, 1), (50, 50), (10_000, 200)]
)
def test_staging_limit_is_clamped(
    monkeypatch: pytest.MonkeyPatch, requested: int, expected: int
):
    log = capture_q(monkeypatch, [])
    _staging("t1", "pending", requested)
    sql, params = log[0]
    assert "status = %s" in sql
    assert params == ("t1", "pending", expected)


@pytest.mark.parametrize(
    ("requested", "expected"), [(0, 1), (-9, 1), (120, 120), (10_000, 500)]
)
def test_events_limit_is_clamped(
    monkeypatch: pytest.MonkeyPatch, requested: int, expected: int
):
    log = capture_q(monkeypatch, [])
    _events("t1", requested)
    assert log[0][1] == ("t1", expected)


def test_get_parameterises_tenant_and_uri(monkeypatch: pytest.MonkeyPatch):
    log = capture_q(monkeypatch, [])  # both reads return no rows
    result = _get("t1", "stratum://docs/t1/x'; DROP --", "l1")
    assert result == {"error": "not_found", "uri": "stratum://docs/t1/x'; DROP --"}
    first_sql, first_params = log[0]
    assert "uri = %s" in first_sql
    assert first_params == ("t1", "stratum://docs/t1/x'; DROP --")


def test_areas_reports_the_bereich_segment(monkeypatch: pytest.MonkeyPatch):
    """A stratum URI is ``stratum://<bereich>/<tenant>/<path>``: area = segment 3."""
    log = capture_q(monkeypatch, [{"area": "docs", "n": 2}])
    assert _areas("t1") == [{"area": "docs", "n": 2}]
    sql, params = log[0]
    assert "split_part(uri, '/', 3)" in sql
    assert params == ("t1",)


# --------------------------------------------------------------------------- #
# _overview shape
# --------------------------------------------------------------------------- #


def test_overview_structure_with_stubbed_q(monkeypatch: pytest.MonkeyPatch):
    import stratum.observability as obs

    monkeypatch.setattr(obs, "health", lambda: {"status": "ok", "db": {"ok": True}})

    def fake_q(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        if "count(*)" in sql:
            return [{"n": 5}]
        return [{"op": "ADD", "target_uri": "stratum://docs/t1/a", "created_at": "2026-01-01"}]

    monkeypatch.setattr(dashboard, "_q", fake_q)

    out = _overview("t1")

    assert set(out) == {"health", "counts", "recent_events"}
    assert out["health"]["status"] == "ok"
    assert set(out["counts"]) == {
        "files",
        "chunks",
        "facts",
        "entities",
        "events",
        "staging_pending",
        "jobs",
    }
    assert all(v == 5 for v in out["counts"].values())
    assert out["recent_events"][0]["op"] == "ADD"


def test_overview_degrades_when_health_raises(monkeypatch: pytest.MonkeyPatch):
    import stratum.observability as obs

    def boom() -> dict[str, Any]:
        raise RuntimeError("db down")

    monkeypatch.setattr(obs, "health", boom)
    monkeypatch.setattr(dashboard, "_q", lambda sql, params=(): [{"n": 0}])

    out = _overview("t1")
    assert out["health"]["status"] == "degraded"
    assert "db down" in out["health"]["error"]


# --------------------------------------------------------------------------- #
# _get fallbacks
# --------------------------------------------------------------------------- #


def test_get_not_found_returns_error(monkeypatch: pytest.MonkeyPatch):
    capture_q(monkeypatch, [])
    assert _get("t1", "stratum://docs/t1/missing", "l1") == {
        "error": "not_found",
        "uri": "stratum://docs/t1/missing",
    }


def test_get_returns_l1_from_the_file_row(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        dashboard,
        "_q",
        lambda sql, params=(): [
            {"uri": "stratum://docs/t1/a", "l0": "L0", "l1": "L1", "content_hash": "h"}
        ]
        if "FROM files" in sql
        else [],
    )
    out = _get("t1", "stratum://docs/t1/a", "l1")
    assert out["kind"] == "file"
    assert out["content"] == "L1"


def test_get_l2_uses_the_filesystem_store(monkeypatch: pytest.MonkeyPatch):
    import stratum.filesystem as fs_mod

    monkeypatch.setattr(
        dashboard,
        "_q",
        lambda sql, params=(): [
            {"uri": "stratum://docs/t1/a", "l0": "L0", "l1": "L1"}
        ]
        if "FROM files" in sql
        else [],
    )

    class FakeFS:
        def __init__(self, *_a: Any, **_k: Any) -> None:
            pass

        def read(self, uri: str, *, level: str = "l1", tenant: str) -> dict[str, Any]:
            return {"content": "FULL BODY"}

    monkeypatch.setattr(fs_mod, "FilesystemStore", FakeFS)
    out = _get("t1", "stratum://docs/t1/a", "l2")
    assert out["content"] == "FULL BODY"


def test_get_l2_missing_file_degrades_without_crashing(monkeypatch: pytest.MonkeyPatch):
    import stratum.filesystem as fs_mod

    monkeypatch.setattr(
        dashboard,
        "_q",
        lambda sql, params=(): [
            {"uri": "stratum://docs/t1/a", "l0": "L0", "l1": "L1"}
        ]
        if "FROM files" in sql
        else [],
    )

    class BoomFS:
        def __init__(self, *_a: Any, **_k: Any) -> None:
            pass

        def read(self, uri: str, *, level: str = "l1", tenant: str) -> dict[str, Any]:
            raise FileNotFoundError(uri)

    monkeypatch.setattr(fs_mod, "FilesystemStore", BoomFS)
    out = _get("t1", "stratum://docs/t1/a", "l2")
    assert out["kind"] == "file"
    assert out["content"].startswith("(L2 unavailable:")


def test_get_l2_store_construction_failure_degrades(monkeypatch: pytest.MonkeyPatch):
    """Even a failing store *import/construction* must not crash ``_get``."""
    import stratum.filesystem as fs_mod

    monkeypatch.setattr(
        dashboard,
        "_q",
        lambda sql, params=(): [{"uri": "stratum://docs/t1/a", "l0": "L0", "l1": "L1"}]
        if "FROM files" in sql
        else [],
    )

    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("no vault configured")

    monkeypatch.setattr(fs_mod, "FilesystemStore", boom)
    out = _get("t1", "stratum://docs/t1/a", "l2")
    assert out["kind"] == "file"
    assert "no vault configured" in out["content"]


def test_get_returns_facts_when_no_file_matches(monkeypatch: pytest.MonkeyPatch):
    def fake_q(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        if "FROM files" in sql:
            return []
        return [{"fact_id": "f1", "predicate": "likes", "subject": "Ada"}]

    monkeypatch.setattr(dashboard, "_q", fake_q)
    out = _get("t1", "stratum://docs/t1/a", "l1")
    assert out["kind"] == "facts"
    assert out["facts"][0]["fact_id"] == "f1"


# --------------------------------------------------------------------------- #
# _as_int defensive parsing
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "default", "lo", "hi", "expected"),
    [
        ("10", 8, None, None, 10),
        ("abc", 8, None, None, 8),
        ("", 8, None, None, 8),
        (None, 8, None, None, 8),
        ("abc", 8, 1, 50, 8),
        ("0", 8, 1, 50, 1),
        ("999", 8, 1, 50, 50),
        ("-3", 8, 1, 50, 1),
    ],
)
def test_as_int_is_defensive(raw: Any, default: int, lo: Any, hi: Any, expected: int):
    assert _as_int(raw, default, lo=lo, hi=hi) == expected


# --------------------------------------------------------------------------- #
# _tenant resolution
# --------------------------------------------------------------------------- #


def _list_route_with_tenant(tenant_for: Any) -> Any:
    fake = FakeFastMCP()
    register_dashboard(fake, tenant_for=tenant_for)
    return fake.route("/dashboard/api/list")["endpoint"]


def test_tenant_defaults_when_no_callback(monkeypatch: pytest.MonkeyPatch):
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        dashboard, "_list", lambda tenant, prefix, limit: captured.update(tenant=tenant) or []
    )
    endpoint = _list_route_with_tenant(None)
    call(endpoint, make_request(""))
    assert captured["tenant"] == "default"


def test_tenant_uses_the_callback_value(monkeypatch: pytest.MonkeyPatch):
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        dashboard, "_list", lambda tenant, prefix, limit: captured.update(tenant=tenant) or []
    )
    endpoint = _list_route_with_tenant(lambda request: "tenant-42")
    call(endpoint, make_request(""))
    assert captured["tenant"] == "tenant-42"


def test_tenant_falls_back_on_falsy_value(monkeypatch: pytest.MonkeyPatch):
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        dashboard, "_list", lambda tenant, prefix, limit: captured.update(tenant=tenant) or []
    )
    endpoint = _list_route_with_tenant(lambda request: "")
    call(endpoint, make_request(""))
    assert captured["tenant"] == "default"


def test_tenant_falls_back_when_callback_raises(monkeypatch: pytest.MonkeyPatch):
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        dashboard, "_list", lambda tenant, prefix, limit: captured.update(tenant=tenant) or []
    )

    def boom(request: Any) -> str:
        raise RuntimeError("no auth context")

    endpoint = _list_route_with_tenant(boom)
    call(endpoint, make_request(""))
    assert captured["tenant"] == "default"


# --------------------------------------------------------------------------- #
# Gate behaviour (ensure_scope)
# --------------------------------------------------------------------------- #


def _deny_gate(request: Any, scope: str) -> Any:
    from starlette.responses import JSONResponse

    return JSONResponse({"error": "forbidden"}, status_code=403)


def test_gate_denies_when_gate_callback_returns_a_response(monkeypatch: pytest.MonkeyPatch):
    def explode(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("data helper must not run when the gate denies")

    monkeypatch.setattr(dashboard, "_list", explode)
    fake = FakeFastMCP()
    register_dashboard(fake, ensure_scope=_deny_gate)
    response = call(fake.route("/dashboard/api/list")["endpoint"], make_request(""))
    assert response.status_code == 403


def test_gate_passes_when_callback_returns_none(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(dashboard, "_list", lambda tenant, prefix, limit: [])
    fake = FakeFastMCP()
    register_dashboard(fake, ensure_scope=lambda request, scope: None)
    response = call(fake.route("/dashboard/api/list")["endpoint"], make_request(""))
    assert response.status_code == 200


@pytest.mark.parametrize(
    "path",
    ["/dashboard/api/overview", "/dashboard/api/areas", "/dashboard/api/get",
     "/dashboard/api/search", "/dashboard/api/staging", "/dashboard/api/events"],
)
def test_every_read_route_is_gated(
    monkeypatch: pytest.MonkeyPatch, path: str
):
    """All read routes call the gate before any data access."""
    fake = FakeFastMCP()
    register_dashboard(fake, ensure_scope=_deny_gate)
    response = call(fake.route(path)["endpoint"], make_request(""))
    assert response.status_code == 403


@pytest.mark.parametrize("path", ["/dashboard/api/promote", "/dashboard/api/reflect"])
def test_both_write_routes_are_gated(monkeypatch: pytest.MonkeyPatch, path: str):
    fake = FakeFastMCP()
    register_dashboard(fake, ensure_scope=_deny_gate)
    response = call(fake.route(path)["endpoint"], make_request(method="POST", body=b"{}"))
    assert response.status_code == 403


def test_html_shell_is_public_even_with_a_denying_gate():
    fake = FakeFastMCP()
    register_dashboard(fake, ensure_scope=_deny_gate)
    for path in ("/dashboard", "/"):
        response = call(fake.route(path)["endpoint"], make_request(""))
        assert response.status_code == 200
        assert b"Stratum" in response.body


# --------------------------------------------------------------------------- #
# HTML shell content
# --------------------------------------------------------------------------- #


def test_html_shell_has_the_core_elements():
    html = dashboard._DASHBOARD_HTML
    assert 'id="kpis"' in html
    assert "dashboard/api/overview" in html
    assert 'class="mark"' in html  # the logo SVG
    assert "<svg" in html
    assert 'id="hstatus"' in html


# --------------------------------------------------------------------------- #
# /search error path
# --------------------------------------------------------------------------- #


def test_search_provider_error_returns_200_with_empty_results(monkeypatch: pytest.MonkeyPatch):
    import stratum.query as query_mod

    class BoomEngine:
        def __init__(self, *_a: Any, **_k: Any) -> None:
            raise RuntimeError("no embedding backend")

    monkeypatch.setattr(query_mod, "QueryEngine", BoomEngine)
    fake = FakeFastMCP()
    register_dashboard(fake)
    response = call(fake.route("/dashboard/api/search")["endpoint"], make_request("q=hello"))
    assert response.status_code == 200
    payload = body_of(response)
    assert payload["results"] == []
    assert payload["error"]


def test_search_success_returns_results(monkeypatch: pytest.MonkeyPatch):
    import stratum.query as query_mod

    class FakeEngine:
        def __init__(self, *_a: Any, **_k: Any) -> None:
            pass

        def query(self, tenant: str, query: str, **kwargs: Any) -> list[dict[str, Any]]:
            return [{"uri": "stratum://docs/t1/a", "score": 0.5}]

    monkeypatch.setattr(query_mod, "QueryEngine", FakeEngine)
    fake = FakeFastMCP()
    register_dashboard(fake)
    response = call(fake.route("/dashboard/api/search")["endpoint"], make_request("q=hello&k=3"))
    assert response.status_code == 200
    assert body_of(response)["results"][0]["uri"] == "stratum://docs/t1/a"


def test_search_tolerates_bad_k_parameter(monkeypatch: pytest.MonkeyPatch):
    import stratum.query as query_mod

    seen: dict[str, Any] = {}

    class FakeEngine:
        def __init__(self, *_a: Any, **_k: Any) -> None:
            pass

        def query(self, tenant: str, query: str, **kwargs: Any) -> list[dict[str, Any]]:
            seen.update(kwargs)
            return []

    monkeypatch.setattr(query_mod, "QueryEngine", FakeEngine)
    fake = FakeFastMCP()
    register_dashboard(fake)
    response = call(fake.route("/dashboard/api/search")["endpoint"], make_request("q=x&k=abc"))
    assert response.status_code == 200
    assert seen["k"] == 8  # default, not a crash


# --------------------------------------------------------------------------- #
# /list + /events query parsing never 500s on garbage
# --------------------------------------------------------------------------- #


def test_list_route_tolerates_bad_limit(monkeypatch: pytest.MonkeyPatch):
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        dashboard,
        "_list",
        lambda tenant, prefix, limit: captured.update(limit=limit) or [],
    )
    fake = FakeFastMCP()
    register_dashboard(fake)
    response = call(fake.route("/dashboard/api/list")["endpoint"], make_request("limit=nonsense"))
    assert response.status_code == 200
    assert captured["limit"] == 100


def test_events_route_tolerates_bad_limit(monkeypatch: pytest.MonkeyPatch):
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        dashboard, "_events", lambda tenant, limit: captured.update(limit=limit) or []
    )
    fake = FakeFastMCP()
    register_dashboard(fake)
    response = call(fake.route("/dashboard/api/events")["endpoint"], make_request("limit=abc"))
    assert response.status_code == 200
    assert captured["limit"] == 50


# --------------------------------------------------------------------------- #
# POST routes: missing / invalid / non-object bodies
# --------------------------------------------------------------------------- #


def _patch_curator(monkeypatch: pytest.MonkeyPatch, result: dict[str, Any]) -> dict[str, Any]:
    import stratum.curator as curator_mod

    calls: dict[str, Any] = {}

    class FakeCurator:
        def __init__(self, *_a: Any, **_k: Any) -> None:
            pass

        def promote(self, tenant: str, candidate_id: str, **_k: Any) -> dict[str, Any]:
            calls.update(tenant=tenant, candidate_id=candidate_id)
            return result

    monkeypatch.setattr(curator_mod, "Curator", FakeCurator)
    return calls


def test_promote_requires_candidate_id(monkeypatch: pytest.MonkeyPatch):
    _patch_curator(monkeypatch, {"status": "approved"})
    fake = FakeFastMCP()
    register_dashboard(fake)
    response = call(
        fake.route("/dashboard/api/promote")["endpoint"],
        make_request(method="POST", body=b"{}"),
    )
    assert response.status_code == 400
    assert body_of(response)["error"] == "candidate_id required"


@pytest.mark.parametrize("raw_body", [b"", b"{not json", b"[1,2,3]", b"\"just a string\""])
def test_promote_survives_missing_or_non_object_body(
    monkeypatch: pytest.MonkeyPatch, raw_body: bytes
):
    _patch_curator(monkeypatch, {"status": "approved"})
    fake = FakeFastMCP()
    register_dashboard(fake)
    response = call(
        fake.route("/dashboard/api/promote")["endpoint"],
        make_request(method="POST", body=raw_body),
    )
    # A non-object body normalises to {} → the 400 validation path, never a 500.
    assert response.status_code == 400


def test_promote_happy_path(monkeypatch: pytest.MonkeyPatch):
    calls = _patch_curator(monkeypatch, {"status": "approved", "action": "ADD"})
    fake = FakeFastMCP()
    register_dashboard(fake)
    response = call(
        fake.route("/dashboard/api/promote")["endpoint"],
        make_request(method="POST", body=json.dumps({"candidate_id": "cand-1"}).encode()),
    )
    assert response.status_code == 200
    assert body_of(response)["ok"] is True
    assert calls["candidate_id"] == "cand-1"


def test_promote_curator_error_is_200_with_error(monkeypatch: pytest.MonkeyPatch):
    import stratum.curator as curator_mod

    class BoomCurator:
        def __init__(self, *_a: Any, **_k: Any) -> None:
            pass

        def promote(self, *_a: Any, **_k: Any) -> dict[str, Any]:
            raise LookupError("candidate not found")

    monkeypatch.setattr(curator_mod, "Curator", BoomCurator)
    fake = FakeFastMCP()
    register_dashboard(fake)
    response = call(
        fake.route("/dashboard/api/promote")["endpoint"],
        make_request(method="POST", body=json.dumps({"candidate_id": "x"}).encode()),
    )
    assert response.status_code == 200
    assert "candidate not found" in body_of(response)["error"]


def _patch_jobqueue(monkeypatch: pytest.MonkeyPatch, job_id: str = "job-1") -> dict[str, Any]:
    import stratum.worker as worker_mod

    calls: dict[str, Any] = {}

    class FakeQueue:
        def __init__(self, *_a: Any, **_k: Any) -> None:
            pass

        def enqueue(self, tenant: str, kind: str, payload: Any = None, **_k: Any) -> str:
            calls.update(tenant=tenant, kind=kind, payload=payload)
            return job_id

    monkeypatch.setattr(worker_mod, "JobQueue", FakeQueue)
    return calls


@pytest.mark.parametrize("raw_body", [b"", b"{not json", b"[1,2,3]"])
def test_reflect_survives_missing_or_non_object_body(
    monkeypatch: pytest.MonkeyPatch, raw_body: bytes
):
    _patch_jobqueue(monkeypatch)
    fake = FakeFastMCP()
    register_dashboard(fake)
    response = call(
        fake.route("/dashboard/api/reflect")["endpoint"],
        make_request(method="POST", body=raw_body),
    )
    assert response.status_code == 200
    assert body_of(response)["ok"] is True


def test_reflect_failure_is_200_with_error(monkeypatch: pytest.MonkeyPatch):
    import stratum.worker as worker_mod

    class BoomQueue:
        def __init__(self, *_a: Any, **_k: Any) -> None:
            pass

        def enqueue(self, *_a: Any, **_k: Any) -> str:
            raise RuntimeError("queue unavailable")

    monkeypatch.setattr(worker_mod, "JobQueue", BoomQueue)
    fake = FakeFastMCP()
    register_dashboard(fake)
    response = call(
        fake.route("/dashboard/api/reflect")["endpoint"],
        make_request(method="POST", body=b"{}"),
    )
    assert response.status_code == 200
    assert "queue unavailable" in body_of(response)["error"]


# --------------------------------------------------------------------------- #
# Auth middleware: which paths are protected
# --------------------------------------------------------------------------- #


def _middleware():
    pytest.importorskip("mcp")
    from stratum.mcp.server import _ApiKeyAuthMiddleware

    async def inner(scope: Any, receive: Any, send: Any) -> None:  # pragma: no cover
        await send({"type": "http.response.start", "status": 200, "headers": []})

    return _ApiKeyAuthMiddleware(inner, mcp_path="/mcp", verify=lambda token: None)


@pytest.mark.parametrize(
    "path",
    ["/mcp", "/dashboard/api", "/dashboard/api/overview", "/dashboard/api/promote"],
)
def test_middleware_protects_mcp_and_dashboard_api(path: str):
    mw = _middleware()
    assert mw._is_protected(path) is True


@pytest.mark.parametrize("path", ["/dashboard", "/", "/health", "/metrics", "/other"])
def test_middleware_leaves_shell_probes_and_other_paths_public(path: str):
    mw = _middleware()
    assert mw._is_protected(path) is False


def test_middleware_401_on_dashboard_api_without_token():
    pytest.importorskip("mcp")
    mw = _middleware()
    scope = {"type": "http", "path": "/dashboard/api/overview", "headers": []}
    start = _drive(mw, scope)
    assert start["status"] == 401


def test_middleware_passes_health_through_to_the_app():
    pytest.importorskip("mcp")
    mw = _middleware()
    scope = {"type": "http", "path": "/health", "headers": []}
    start = _drive(mw, scope)
    assert start["status"] == 200  # the inner app ran


def _drive(app: Any, scope: dict[str, Any]) -> dict[str, Any]:
    captured: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request"}

    async def send(message: dict[str, Any]) -> None:
        captured.append(message)

    asyncio.run(app(scope, receive, send))
    return captured[0]

# ------------------------------------------------------------- JSON safety (live bug)

def test_json_safe_normalises_db_types():
    """Postgres returns datetime/UUID/Decimal; JSONResponse cannot serialise them.

    Regression: /dashboard/api/overview and /events returned HTTP 500 with
    "Object of type datetime is not JSON serializable".
    """
    import datetime
    import json
    import uuid
    from decimal import Decimal

    from stratum.dashboard import _json_safe

    row = {
        "created_at": datetime.datetime(2026, 10, 11, 12, 0, tzinfo=datetime.UTC),
        "event_id": uuid.uuid4(),
        "confidence": Decimal("1.5"),
        "target_uri": "stratum://docs/default/x",
        "n": None,
        "nested": [datetime.date(2026, 1, 1), uuid.UUID(int=0)],
    }
    out = _json_safe(row)
    # Must be json-serialisable (this is the exact failure mode).
    encoded = json.dumps(out)
    assert "2026-10-11" in encoded
    assert isinstance(out["event_id"], str)
    assert isinstance(out["confidence"], float)
    assert out["n"] is None
    assert isinstance(out["nested"][0], str)


def test_search_results_with_datetime_are_json_safe(monkeypatch: pytest.MonkeyPatch):
    """B1: a result carrying a raw DB ``datetime`` must serialise, not 500.

    ``engine.query`` returns psycopg row dicts whose ``valid_from``/``valid_to``
    are real ``datetime`` objects; ``JSONResponse`` raises ``TypeError`` on them,
    which the error path used to swallow into an empty result. The route must
    normalise the rows before encoding.
    """
    import datetime

    import stratum.query as query_mod

    class FakeEngine:
        def __init__(self, *_a: Any, **_k: Any) -> None:
            pass

        def query(self, tenant: str, query: str, **kwargs: Any) -> list[dict[str, Any]]:
            return [
                {
                    "uri": "stratum://docs/t1/a",
                    "score": 0.5,
                    "valid_from": datetime.datetime(2026, 10, 11, 12, 0, tzinfo=datetime.UTC),
                    "valid_to": None,
                }
            ]

    monkeypatch.setattr(query_mod, "QueryEngine", FakeEngine)
    fake = FakeFastMCP()
    register_dashboard(fake)
    response = call(fake.route("/dashboard/api/search")["endpoint"], make_request("q=hello"))
    assert response.status_code == 200
    payload = body_of(response)  # json.loads on the raw body: would raise if unsafe
    assert "error" not in payload
    row = payload["results"][0]
    assert row["valid_from"] == "2026-10-11T12:00:00+00:00"
    assert row["valid_to"] is None


# ------------------------------------------------- GET routes: DB error -> JSON 500 (B3)


def _boom_q(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    raise RuntimeError("db down")


@pytest.mark.parametrize(
    "path",
    [
        "/dashboard/api/overview",
        "/dashboard/api/areas",
        "/dashboard/api/list",
        "/dashboard/api/get",
        "/dashboard/api/staging",
        "/dashboard/api/events",
    ],
)
def test_read_routes_return_json_500_on_db_error(
    monkeypatch: pytest.MonkeyPatch, path: str
):
    """B3: a DB failure on a GET read route is a structured JSON 500, not a crash."""
    monkeypatch.setattr(dashboard, "_q", _boom_q)
    fake = FakeFastMCP()
    register_dashboard(fake)
    response = call(fake.route(path)["endpoint"], make_request("uri=stratum://docs/t1/a"))
    assert response.status_code == 500
    payload = body_of(response)
    assert "db down" in payload["error"]


# ----------------------------------------------- promote/reflect audit + budget (B2)


def test_promote_passes_dashboard_actor(monkeypatch: pytest.MonkeyPatch):
    """B2: the dashboard promote sets ``actor="dashboard"`` (non-NULL audit)."""
    import stratum.curator as curator_mod

    seen: dict[str, Any] = {}

    class FakeCurator:
        def __init__(self, *_a: Any, **_k: Any) -> None:
            pass

        def promote(self, tenant: str, candidate_id: str, **kwargs: Any) -> dict[str, Any]:
            seen.update(tenant=tenant, candidate_id=candidate_id, **kwargs)
            return {"status": "approved"}

    monkeypatch.setattr(curator_mod, "Curator", FakeCurator)
    fake = FakeFastMCP()
    register_dashboard(fake)
    response = call(
        fake.route("/dashboard/api/promote")["endpoint"],
        make_request(method="POST", body=json.dumps({"candidate_id": "c1"}).encode()),
    )
    assert response.status_code == 200
    assert seen["actor"] == "dashboard"


def test_reflect_enqueues_budgeted_payload(monkeypatch: pytest.MonkeyPatch):
    """B2: ``/reflect`` mirrors the tool's budget validation and payload."""
    import stratum.worker as worker_mod

    calls: dict[str, Any] = {}

    class FakeQueue:
        def __init__(self, *_a: Any, **_k: Any) -> None:
            pass

        def enqueue(self, tenant: str, kind: str, payload: Any = None, **_k: Any) -> str:
            calls.update(tenant=tenant, kind=kind, payload=payload)
            return "job-7"

    monkeypatch.setattr(worker_mod, "JobQueue", FakeQueue)
    fake = FakeFastMCP()
    register_dashboard(fake)
    response = call(
        fake.route("/dashboard/api/reflect")["endpoint"],
        make_request(method="POST", body=json.dumps({"budget_tokens": 1234}).encode()),
    )
    assert response.status_code == 200
    assert calls["payload"]["budget_tokens"] == 1234
    assert calls["payload"]["tenant"] == "default"


def test_reflect_defaults_and_clamps_budget(monkeypatch: pytest.MonkeyPatch):
    """B2: an absent budget uses the Settings default; an over-max one is clamped."""
    import stratum.worker as worker_mod
    from stratum.config import Settings

    settings = Settings()
    seen: list[Any] = []

    class FakeQueue:
        def __init__(self, *_a: Any, **_k: Any) -> None:
            pass

        def enqueue(self, tenant: str, kind: str, payload: Any = None, **_k: Any) -> str:
            seen.append(payload["budget_tokens"])
            return "job-x"

    monkeypatch.setattr(worker_mod, "JobQueue", FakeQueue)
    fake = FakeFastMCP()
    register_dashboard(fake)
    endpoint = fake.route("/dashboard/api/reflect")["endpoint"]
    call(endpoint, make_request(method="POST", body=b"{}"))
    assert seen[-1] == settings.reflect_budget_tokens
    call(
        endpoint,
        make_request(
            method="POST", body=json.dumps({"budget_tokens": 10_000_000}).encode()
        ),
    )
    assert seen[-1] == settings.reflect_max_tokens


# ------------------------------------ server.py wiring: real _dash_gate/_dash_tenant (B7)


def _mcp_ctx() -> Any:
    from stratum.auth import AuthContext
    from stratum.config import Settings
    from stratum.mcp.tools import ToolContext

    return ToolContext(
        auth=AuthContext(tenant="bootstrap-tenant", scopes={"admin"}, key_id="bootstrap"),
        settings=Settings(),
    )


def _dashboard_routes_from_server(monkeypatch: pytest.MonkeyPatch, *, require_key: bool):
    """Build the real server and return its dashboard route endpoints.

    ``build_server`` wires ``_dash_gate``/``_dash_tenant`` to the live
    ``_CURRENT_AUTH`` context var; the routes are registered on the FastMCP app,
    so they can be driven directly to test the real closures.
    """
    pytest.importorskip("mcp")
    from stratum.mcp import server as server_mod

    monkeypatch.setattr(server_mod, "_require_api_key", lambda: require_key)
    server = server_mod.build_server(_mcp_ctx())
    return {
        getattr(r, "path", None): r.endpoint
        for r in server.streamable_http_app().routes
        if getattr(r, "path", None)
    }


def test_dash_tenant_uses_bound_request_auth(monkeypatch: pytest.MonkeyPatch):
    """The real ``_dash_tenant`` closure reads the tenant from the request auth."""
    pytest.importorskip("mcp")
    from stratum.auth import AuthContext
    from stratum.mcp import server as server_mod

    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        dashboard, "_list", lambda tenant, prefix, limit: captured.update(tenant=tenant) or []
    )
    routes = _dashboard_routes_from_server(monkeypatch, require_key=False)
    token = server_mod._CURRENT_AUTH.set(AuthContext(tenant="t-42", scopes={"read"}))
    try:
        response = call(routes["/dashboard/api/list"], make_request(""))
    finally:
        server_mod._CURRENT_AUTH.reset(token)
    assert response.status_code == 200
    assert captured["tenant"] == "t-42"


def test_dash_gate_allows_when_open_mode(monkeypatch: pytest.MonkeyPatch):
    """With the key gate off the real ``_dash_gate`` never denies."""
    monkeypatch.setattr(dashboard, "_list", lambda tenant, prefix, limit: [])
    routes = _dashboard_routes_from_server(monkeypatch, require_key=False)
    response = call(routes["/dashboard/api/list"], make_request(""))
    assert response.status_code == 200


def test_dash_gate_denies_without_scope(monkeypatch: pytest.MonkeyPatch):
    """With the key gate on, a bound auth lacking the scope gets a 403."""
    pytest.importorskip("mcp")
    from stratum.auth import AuthContext
    from stratum.mcp import server as server_mod

    def explode(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("data helper must not run when the gate denies")

    monkeypatch.setattr(dashboard, "_list", explode)
    routes = _dashboard_routes_from_server(monkeypatch, require_key=True)
    token = server_mod._CURRENT_AUTH.set(AuthContext(tenant="t", scopes={"read"}))
    try:
        response = call(routes["/dashboard/api/promote"], make_request(method="POST", body=b"{}"))
    finally:
        server_mod._CURRENT_AUTH.reset(token)
    assert response.status_code == 403


def test_dash_gate_allows_with_scope(monkeypatch: pytest.MonkeyPatch):
    """With the key gate on, a bound auth holding the scope passes the gate."""
    pytest.importorskip("mcp")
    from stratum.auth import AuthContext
    from stratum.mcp import server as server_mod

    routes = _dashboard_routes_from_server(monkeypatch, require_key=True)
    monkeypatch.setattr(dashboard, "_list", lambda tenant, prefix, limit: [])
    token = server_mod._CURRENT_AUTH.set(AuthContext(tenant="t", scopes={"read"}))
    try:
        response = call(routes["/dashboard/api/list"], make_request(""))
    finally:
        server_mod._CURRENT_AUTH.reset(token)
    assert response.status_code == 200

# ------------------------------------------------ tenant switcher (override auth)

def test_tenant_override_allowed_for_admin(monkeypatch: pytest.MonkeyPatch):
    """An admin key may switch to another tenant via ?tenant=."""
    pytest.importorskip("mcp")
    from stratum.auth import AuthContext
    from stratum.mcp import server as server_mod

    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        dashboard, "_list", lambda tenant, prefix, limit: captured.update(tenant=tenant) or []
    )
    routes = _dashboard_routes_from_server(monkeypatch, require_key=False)
    token = server_mod._CURRENT_AUTH.set(
        AuthContext(tenant="mine", scopes={"read", "admin"})
    )
    try:
        response = call(routes["/dashboard/api/list"], make_request("tenant=other"))
    finally:
        server_mod._CURRENT_AUTH.reset(token)
    assert response.status_code == 200
    assert captured["tenant"] == "other"


def test_tenant_override_denied_for_non_admin(monkeypatch: pytest.MonkeyPatch):
    """A non-admin key stays scoped to its own tenant even with ?tenant=."""
    pytest.importorskip("mcp")
    from stratum.auth import AuthContext
    from stratum.mcp import server as server_mod

    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        dashboard, "_list", lambda tenant, prefix, limit: captured.update(tenant=tenant) or []
    )
    routes = _dashboard_routes_from_server(monkeypatch, require_key=False)
    token = server_mod._CURRENT_AUTH.set(AuthContext(tenant="mine", scopes={"read"}))
    try:
        response = call(routes["/dashboard/api/list"], make_request("tenant=other"))
    finally:
        server_mod._CURRENT_AUTH.reset(token)
    assert response.status_code == 200
    assert captured["tenant"] == "mine"  # NOT escalated to "other"
