"""FastMCP transport adapter for the Stratum-Memory tool API (WP-11).

This module is the **only** place in the package that imports the ``mcp``
library, so ``import stratum.mcp.tools`` never needs it and the pure tool surface
stays testable without a server. :func:`build_server` creates a
:class:`mcp.server.fastmcp.FastMCP` instance and registers the ten tools with
clean public signatures (each forwarding to the matching function in
:mod:`stratum.mcp.tools`). :func:`main` selects the transport from
:class:`stratum.config.Settings`:

* ``stdio``            — ``mcp.run()`` (the client starts the process),
* ``streamable-http``  — ``mcp.run(transport="streamable-http")`` with the host
  and port from Settings (host ``0.0.0.0`` inside a container).

Two operational HTTP routes are registered alongside the MCP endpoint
(``WP-17``):

* ``GET /health``  — the readiness probe (:func:`stratum.observability.health`)
  as JSON; ``200`` when ``status == "ok"``, else ``503``,
* ``GET /metrics`` — the Prometheus text exposition
  (:func:`stratum.observability.metrics_response`), content type
  ``text/plain; version=0.0.4``.

Both are public (no key required): they are monitoring probes.

Authentication is **bootstrap by default** and **optionally key-gated**. When
``STRATUM_REQUIRE_API_KEY`` is unset or false (the default, suitable for local
stdio operation) the server runs with a bootstrap ``admin`` context for tenant
``"default"`` — the transparent local mode. When ``STRATUM_REQUIRE_API_KEY`` is
truthy, the streamable-HTTP transport requires an ``Authorization: Bearer
stk_...`` header: the tenant and scopes are resolved with
:func:`stratum.auth.verify_key` and bound to the tool calls; a missing header
yields ``401`` and an unverifiable token ``403``. The gate applies to the
``/mcp`` endpoint only.

Nothing here connects to the network or database at import time: :func:`main`
builds the :class:`~stratum.mcp.tools.ToolContext` only when it actually runs.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from contextvars import ContextVar
from dataclasses import replace
from typing import Any

from mcp.server.fastmcp import FastMCP
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from ..auth import AuthContext, Authorizer
from ..config import Settings, get_settings
from .tools import (
    TOOL_NAMES,
    ToolContext,
    stratum_archive,
    stratum_forget,
    stratum_get,
    stratum_health,
    stratum_link,
    stratum_promote,
    stratum_query,
    stratum_reflect,
    stratum_session_append,
    stratum_write,
)

__all__ = ["TOOL_NAMES", "build_server", "main"]

logger = logging.getLogger("stratum.mcp.server")

#: Per-request authenticated identity, bound by the optional key gate (see
#: :class:`_ApiKeyAuthMiddleware`) and read by :func:`_call` for every tool. Not
#: set in the default bootstrap/stdio mode, where the fixed ``ctx.auth`` applies.
_CURRENT_AUTH: ContextVar[AuthContext | None] = ContextVar(
    "stratum_mcp_auth", default=None
)


def _require_api_key() -> bool:
    """Return whether the bearer-token gate is enabled.

    Reads ``STRATUM_REQUIRE_API_KEY`` directly (the setting is not part of
    :class:`stratum.config.Settings`). Anything other than empty/``0``/``false``/
    ``no``/``off`` counts as enabled; the default is disabled so local stdio
    operation works without keys.
    """
    raw = os.environ.get("STRATUM_REQUIRE_API_KEY", "0")
    return str(raw).strip().lower() not in {"", "0", "false", "no", "off"}


def _bearer_token(header: str | None) -> str | None:
    """Extract the token from an ``Authorization: Bearer <token>`` header."""
    if not header:
        return None
    parts = header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1].strip()
    return token or None


def _verify_bearer(token: str) -> AuthContext | None:
    """Resolve a bearer token to an :class:`AuthContext` via ``verify_key``.

    Never raises: a database/verification failure is treated as an unverifiable
    token (fail-closed → ``403``) rather than a server error.
    """
    from ..auth import verify_key  # noqa: PLC0415 - lazy import on purpose

    try:
        return verify_key(token)
    except Exception:  # noqa: BLE001 - fail closed, never 500 on a bad token
        logger.exception("api key verification failed")
        return None


def _effective_ctx(ctx: ToolContext) -> ToolContext:
    """Return the context for a call, honouring a per-request key gate.

    When the optional key gate is active, :class:`_ApiKeyAuthMiddleware` binds the
    resolved :class:`AuthContext` to :data:`_CURRENT_AUTH` for the duration of the
    request; the tool then runs under that identity instead of the bootstrap one.
    Outside a gated request the bootstrap ``ctx`` is returned unchanged.
    """
    override = _CURRENT_AUTH.get()
    if override is None or override is ctx.auth:
        return ctx
    return replace(ctx, auth=override)


class _ApiKeyAuthMiddleware:
    """Optional pure-ASGI bearer-token gate for the ``/mcp`` endpoint (WP-17).

    Installed only when ``STRATUM_REQUIRE_API_KEY`` is truthy. Requests to the MCP
    path must carry ``Authorization: Bearer stk_...``:

    * no/malformed header → ``401`` (with ``WWW-Authenticate: Bearer``),
    * a token that :func:`stratum.auth.verify_key` cannot resolve → ``403``.

    On success the :class:`AuthContext` is bound to :data:`_CURRENT_AUTH` for the
    request so tool calls run under that tenant/scope. Non-HTTP scopes (lifespan)
    and paths other than the MCP endpoint (``/health``, ``/metrics``) pass through
    untouched — the probes stay public.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        mcp_path: str = "/mcp",
        verify: Any | None = None,
    ) -> None:
        self.app = app
        self.mcp_path = mcp_path
        self._verify = verify or _verify_bearer

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") != self.mcp_path:
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        token = _bearer_token(headers.get("authorization"))
        if token is None:
            response = JSONResponse(
                {"error": "missing bearer token"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return

        auth = self._verify(token)
        if auth is None:
            response = JSONResponse(
                {"error": "invalid or revoked api key"}, status_code=403
            )
            await response(scope, receive, send)
            return

        var_token = _CURRENT_AUTH.set(auth)
        try:
            await self.app(scope, receive, send)
        finally:
            _CURRENT_AUTH.reset(var_token)


def build_server(
    ctx: ToolContext,
    *,
    name: str = "stratum-memory",
    settings: Settings | None = None,
    instructions: str | None = None,
) -> FastMCP:
    """Create a :class:`FastMCP` instance with the ten Stratum tools registered.

    Also registers the two public operational routes ``GET /health`` and
    ``GET /metrics`` (WP-17).

    Args:
        ctx: The :class:`~stratum.mcp.tools.ToolContext` carrying the injected
            collaborators and the authentication context. Each registered tool
            binds it, so the exposed signatures carry only the public arguments.
        name: Server name reported to clients.
        settings: Configuration source; its ``mcp_host``/``mcp_port`` become the
            HTTP bind address for the ``streamable-http`` transport.
        instructions: Optional server instructions for clients.

    Returns:
        The configured :class:`FastMCP` server (not yet running).
    """
    resolved = settings or ctx.settings or get_settings()
    mcp = FastMCP(
        name,
        instructions=instructions,
        host=resolved.mcp_host,
        port=int(resolved.mcp_port),
    )

    # Operational HTTP probes (WP-17): real endpoints, public (no key needed).
    # Mapped to the observability module so the docs match the live server.
    @mcp.custom_route("/health", methods=["GET"])
    async def _health_route(_request: Request) -> Response:  # pragma: no cover - trivial
        from ..observability import health as _health  # noqa: PLC0415 - lazy

        report = _health()
        status = 200 if report.get("status") == "ok" else 503
        return JSONResponse(report, status_code=status)

    @mcp.custom_route("/metrics", methods=["GET"])
    async def _metrics_route(_request: Request) -> Response:  # pragma: no cover - trivial
        from ..observability import metrics_response  # noqa: PLC0415 - lazy

        body, content_type = metrics_response()
        # Set the header verbatim so the content type is exactly the Prometheus
        # exposition type (``Response(media_type=...)`` would append ``charset``).
        response = Response(content=body)
        response.headers["content-type"] = content_type
        return response

    @mcp.tool()
    async def stratum_write(
        uri: str,
        content: str,
        kind: str,
        metadata: Mapping[str, Any] | None = None,
        auto_promote: bool = False,
    ) -> dict[str, Any]:
        """Write Markdown/JSON through ingestion + staging (optionally auto-promote)."""
        return await _call(
            ctx,
            "stratum_write",
            uri=uri,
            content=content,
            kind=kind,
            metadata=dict(metadata) if metadata else None,
            auto_promote=auto_promote,
        )

    @mcp.tool()
    def stratum_query(
        query: str,
        scope: str | None = None,
        mode: str = "hybrid",
        k: int = 8,
        level: str = "l0",
        as_of_valid: str | None = None,
        as_of_tx: str | None = None,
        filters: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Hybrid search over the memory layers with time and level control."""
        return _call(
            ctx,
            "stratum_query",
            query=query,
            scope=scope,
            mode=mode,
            k=k,
            level=level,
            as_of_valid=as_of_valid,
            as_of_tx=as_of_tx,
            filters=dict(filters) if filters else None,
        )

    @mcp.tool()
    def stratum_get(uri: str, level: str = "l1") -> dict[str, Any]:
        """Read one memory URI directly at level l0/l1/l2."""
        return _call(ctx, "stratum_get", uri=uri, level=level)

    @mcp.tool()
    def stratum_promote(
        candidate_id: str, override_reason: str | None = None
    ) -> dict[str, Any]:
        """Release a staging candidate into the active store."""
        return _call(
            ctx,
            "stratum_promote",
            candidate_id=candidate_id,
            override_reason=override_reason,
        )

    @mcp.tool()
    def stratum_archive(uri: str, reason: str) -> dict[str, Any]:
        """Archive a URI (soft, keeps history) and journal an ARCHIVE event."""
        return _call(ctx, "stratum_archive", uri=uri, reason=reason)

    @mcp.tool()
    def stratum_forget(uri: str, hard: bool = False, reason: str = "") -> dict[str, Any]:
        """Right-to-be-forgotten removal; ``hard`` deletes rows/vectors/graph/file."""
        return _call(ctx, "stratum_forget", uri=uri, hard=hard, reason=reason)

    @mcp.tool()
    def stratum_link(
        src_uri: str,
        dst_uri: str,
        rel_type: str,
        valid_from: str | None = None,
        valid_to: str | None = None,
        confidence: float = 1.0,
    ) -> dict[str, Any]:
        """Create an explicit bi-temporal edge between two URIs."""
        return _call(
            ctx,
            "stratum_link",
            src_uri=src_uri,
            dst_uri=dst_uri,
            rel_type=rel_type,
            valid_from=valid_from,
            valid_to=valid_to,
            confidence=confidence,
        )

    @mcp.tool()
    def stratum_session_append(
        session_id: str,
        role: str,
        content: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Append one turn to an episode/session transcript."""
        return _call(
            ctx,
            "stratum_session_append",
            session_id=session_id,
            role=role,
            content=content,
            metadata=dict(metadata) if metadata else None,
        )

    @mcp.tool()
    def stratum_reflect(
        scope: str | None = None,
        budget_tokens: int = 20000,
        async_: bool = True,
    ) -> dict[str, Any]:
        """Trigger a budgeted synthesis run (returns a job id)."""
        return _call(
            ctx,
            "stratum_reflect",
            scope=scope,
            budget_tokens=budget_tokens,
            async_=async_,
        )

    @mcp.tool()
    def stratum_health() -> dict[str, Any]:
        """Readiness probe: status, db, git, llm and queued jobs."""
        return _call(ctx, "stratum_health")

    return mcp


def _call(ctx: ToolContext, tool: str, **kwargs: Any) -> dict[str, Any]:
    """Invoke a pure tool by name and return its result dict.

    The context is resolved per call (:func:`_effective_ctx`) so that, when the
    optional key gate is active, the tool runs under the request's authenticated
    tenant/scopes rather than the bootstrap identity.
    """
    funcs: dict[str, Any] = {
        "stratum_write": stratum_write,
        "stratum_query": stratum_query,
        "stratum_get": stratum_get,
        "stratum_promote": stratum_promote,
        "stratum_archive": stratum_archive,
        "stratum_forget": stratum_forget,
        "stratum_link": stratum_link,
        "stratum_session_append": stratum_session_append,
        "stratum_reflect": stratum_reflect,
        "stratum_health": stratum_health,
    }
    return funcs[tool](_effective_ctx(ctx), **kwargs)


def main(argv: list[str] | None = None) -> int:
    """Run the MCP server using the transport configured in ``Settings``.

    Reads ``Settings.mcp_transport`` (``stdio`` or ``streamable-http``) plus
    ``mcp_host``/``mcp_port`` for HTTP. The tool context is wired to the real
    Stratum modules here (never at import time).

    Auth is bootstrap-by-default: a fixed ``admin`` context for tenant
    ``"default"`` (transparent local mode). When ``STRATUM_REQUIRE_API_KEY`` is
    truthy, the streamable-HTTP transport additionally requires a valid
    ``Authorization: Bearer stk_...`` header on ``/mcp`` (see
    :class:`_ApiKeyAuthMiddleware`).

    Args:
        argv: Unused; accepted so the function can be a console entry point.

    Returns:
        Process exit code (``0`` after a clean shutdown).
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="stratum.mcp", description="Stratum-Memory MCP server."
    )
    parser.add_argument("--transport", choices=("stdio", "streamable-http"), default=None)
    args = parser.parse_args(argv)

    # Structured JSON logs on the process root logger (B20b): the /health and
    # /metrics routes and every request log then emit one JSON object per line.
    from ..observability import setup_logging  # noqa: PLC0415 - lazy import

    setup_logging()

    settings = get_settings()
    transport = args.transport or settings.mcp_transport

    ctx = _runtime_context(settings)

    if transport == "stdio":
        server = build_server(ctx, settings=settings)
        server.run()
        return 0

    # Streamable-HTTP: bind host/port from Settings (0.0.0.0 in a container).
    # Built here (not via ``server.run``) so the optional key gate can be
    # installed on the concrete ASGI app before uvicorn serves it.
    server = build_server(ctx, settings=settings)
    _run_streamable_http(server)
    return 0


def _run_streamable_http(server: FastMCP) -> None:  # pragma: no cover - server loop
    """Serve ``server`` over streamable-HTTP, applying the optional key gate.

    The app is :meth:`FastMCP.streamable_http_app`; when
    :func:`_require_api_key` is true, :class:`_ApiKeyAuthMiddleware` is added on
    the ``/mcp`` path (host/port come from the server's Settings).
    """
    import uvicorn

    app = server.streamable_http_app()
    if _require_api_key():
        logger.info("STRATUM_REQUIRE_API_KEY is set: requiring Bearer tokens on /mcp")
        app.add_middleware(
            _ApiKeyAuthMiddleware,
            mcp_path=getattr(server.settings, "streamable_http_path", "/mcp"),
        )
    config = uvicorn.Config(
        app,
        host=server.settings.host,
        port=server.settings.port,
        log_level=str(server.settings.log_level).lower(),
    )
    uvicorn.Server(config).run()


def _runtime_context(settings: Settings) -> ToolContext:
    """Build the runtime :class:`ToolContext` (real modules; bootstrap auth).

    Kept separate so tests can build a context from fakes without ever touching
    the live collaborators. The bootstrap ``admin`` context for tenant
    ``"default"`` is the transparent default; when ``STRATUM_REQUIRE_API_KEY`` is
    set, :class:`_ApiKeyAuthMiddleware` binds a per-request :class:`AuthContext`
    that :func:`_effective_ctx` substitutes for it on every tool call.
    """
    bootstrap = AuthContext(tenant="default", scopes={"admin"}, key_id="bootstrap")
    ctx = ToolContext.build(settings, auth=bootstrap)
    ctx.authorizer = Authorizer()
    return ctx


if __name__ == "__main__":  # pragma: no cover - exercised via the module CLI
    raise SystemExit(main())
