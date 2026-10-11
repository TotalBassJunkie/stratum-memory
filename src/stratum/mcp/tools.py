"""Agent tool API for Stratum-Memory (WP-11, ``MASTER-PLAN.md`` §6).

This module implements the ten MCP tools as **pure, testable Python functions**.
It performs only thin orchestration over the existing modules and is deliberately
free of any MCP import, network access or database at import time: every
collaborator (``llm``/``store``/``graph``/``fs``/``curator``/``query``/``auth``
and friends) is supplied through a :class:`ToolContext`, so the whole surface can
be exercised with in-memory fakes and without a running server or Postgres.

Design:

* **One context, one call.** Each tool takes a :class:`ToolContext` first and its
  business arguments keyword-only. The context carries the authenticated
  :class:`~stratum.auth.AuthContext`, the :class:`~stratum.auth.Authorizer`, the
  shared settings and the injected collaborators.
* **Scope gate first.** Every tool resolves its required scope from
  :data:`SCOPE_FOR_TOOL` and calls :meth:`Authorizer.authorize` *before* touching
  any collaborator, so a missing scope fails fast with ``FORBIDDEN``.
* **Structured errors, never exceptions.** Failures are returned as
  ``{"error": {"code": <CODE>, "message": <str>}}`` with ``code`` from
  :class:`ErrorCode` (``NOT_FOUND``/``FORBIDDEN``/``VALIDATION``/``CONFLICT``/
  ``BUDGET_EXCEEDED``/``INTERNAL``). Internally a :class:`ToolError` is raised and
  the :func:`_guard` decorator converts it (and any unexpected exception) to that
  shape.
* **Success shape mirrors §6** — each tool returns the structured dict documented
  in ``MASTER-PLAN.md`` §6.

The MCP adapter lives in :mod:`stratum.mcp.server`; it registers thin closures
over these functions so the tool signatures exposed to clients carry only the
public arguments.
"""

from __future__ import annotations

import functools
import inspect
import logging
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import Any

from ..auth import AuthContext, Authorizer, ScopeError
from ..config import Settings, get_settings
from ..filesystem import make_l0
from ..uri import UriError, parse_uri

__all__ = [
    "ALLOWED_WRITE_KINDS",
    "ErrorCode",
    "SCOPE_FOR_TOOL",
    "TOOL_NAMES",
    "ToolContext",
    "ToolError",
    "error_result",
    "stratum_archive",
    "stratum_forget",
    "stratum_get",
    "stratum_health",
    "stratum_link",
    "stratum_promote",
    "stratum_query",
    "stratum_reflect",
    "stratum_session_append",
    "stratum_write",
]

logger = logging.getLogger("stratum.mcp.tools")


# --------------------------------------------------------------------------- #
# Error contract
# --------------------------------------------------------------------------- #


class ErrorCode:
    """The stable error codes of the tool API (``MASTER-PLAN.md`` §6)."""

    NOT_FOUND = "NOT_FOUND"
    FORBIDDEN = "FORBIDDEN"
    VALIDATION = "VALIDATION"
    CONFLICT = "CONFLICT"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    INTERNAL = "INTERNAL"


class ToolError(Exception):
    """Internal carrier for a structured tool failure.

    Raised inside the tool bodies and converted to the public error dict by
    :func:`_guard`, so callers never see an exception for a normal failure.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def as_dict(self) -> dict[str, dict[str, str]]:
        """Return the public error representation."""
        return {"error": {"code": self.code, "message": self.message}}


def error_result(code: str, message: str) -> dict[str, dict[str, str]]:
    """Build the public error dict ``{"error": {"code", "message"}}``."""
    return {"error": {"code": code, "message": message}}


# --------------------------------------------------------------------------- #
# Scope map
# --------------------------------------------------------------------------- #

#: Required scope per tool (``MASTER-PLAN.md`` §6 / §11).
#:
#: * ``read``    — ``query`` / ``get`` / ``health``
#: * ``write``   — ``write`` / ``session_append`` / ``link``
#: * ``promote`` — ``promote`` / ``archive`` / ``forget`` / ``reflect``
SCOPE_FOR_TOOL: dict[str, str] = {
    "write": "write",
    "session_append": "write",
    "link": "write",
    "query": "read",
    "get": "read",
    "health": "read",
    "promote": "promote",
    "archive": "promote",
    "forget": "promote",
    "reflect": "promote",
}

#: The ten tool names, in registration order.
TOOL_NAMES: tuple[str, ...] = (
    "stratum_write",
    "stratum_query",
    "stratum_get",
    "stratum_promote",
    "stratum_archive",
    "stratum_forget",
    "stratum_link",
    "stratum_session_append",
    "stratum_reflect",
    "stratum_health",
)

#: Accepted ``kind`` values for :func:`stratum_write` (§6).
ALLOWED_WRITE_KINDS: frozenset[str] = frozenset({"doc", "fact", "episode", "skill"})

#: Accepted query modes / levels (mirrors :mod:`stratum.query`).
_QUERY_MODES: tuple[str, ...] = ("hybrid", "vector", "graph", "kv")
_QUERY_LEVELS: tuple[str, ...] = ("l0", "l1", "l2")


# --------------------------------------------------------------------------- #
# Context
# --------------------------------------------------------------------------- #


@dataclass
class ToolContext:
    """Everything a tool call needs, injected for testability.

    Only ``auth``/``authorizer``/``settings`` are always relevant; the
    collaborators are optional and constructed lazily or replaced by fakes in
    tests.

    Attributes:
        auth: The authenticated :class:`~stratum.auth.AuthContext` for the call.
        authorizer: The :class:`~stratum.auth.Authorizer` enforcing scope+tenant.
        settings: Configuration source; defaults to :func:`get_settings`.
        llm: Embedding/chat client (``LLMClient`` or a fake).
        store: Vector/KV store with ``log_event`` (``MemoryStore`` or a fake).
        graph: Bi-temporal graph (``TemporalGraph`` or a fake).
        fs: Filesystem vault (``FilesystemStore`` or a fake).
        curator: The Curator gate (``promote`` / ``promote_batch``).
        query: The query engine (``QueryEngine`` or a fake).
        sessions: Session/episode store (``SessionStore`` or a fake).
        ingest: Ingestion pipeline (``IngestPipeline`` or a fake); built from the
            other collaborators when omitted.
        queue: Job queue (``JobQueue`` or a fake) used by ``reflect``.
        conn_factory: ``(tenant, *, bypass_rls=False) -> contextmanager`` used by
            the default hard-delete; defaults to :func:`stratum.db.connection`.
        health_fn: Zero-argument readiness probe; defaults to
            :func:`stratum.observability.health`.
        hard_delete: ``(tenant, uri) -> deleted_refs | dict`` used by a hard
            forget; defaults to :func:`_default_hard_delete`. May return an int
            or a mapping carrying ``deleted_refs`` and a ``git_commit``.
        fs_delete: ``(tenant, uri) -> git_commit | None`` used by the default
            hard delete to remove the vault file with a Git commit; defaults to
            :func:`_default_fs_delete` over ``ctx.fs``.
        vector_delete: ``(tenant, uri) -> int`` used by the default hard delete
            to remove embeddings; defaults to :func:`_default_vector_delete`
            over ``ctx.store``.
        events: Optional event sink ``(**kwargs) -> event_id`` used when ``store``
            exposes no ``log_event``.
    """

    auth: AuthContext | None = None
    authorizer: Authorizer | None = None
    settings: Settings | None = None
    llm: Any = None
    store: Any = None
    graph: Any = None
    fs: Any = None
    curator: Any = None
    query: Any = None
    sessions: Any = None
    ingest: Any = None
    queue: Any = None
    conn_factory: Callable[..., AbstractContextManager[Any]] | None = None
    health_fn: Callable[[], Mapping[str, Any]] | None = None
    hard_delete: Callable[[str, str], Any] | None = None
    fs_delete: Callable[[str, str], Any] | None = None
    vector_delete: Callable[[str, str], Any] | None = None
    events: Callable[..., str] | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------ factory

    @classmethod
    def build(
        cls,
        settings: Settings | None = None,
        *,
        auth: AuthContext | None = None,
        conn_factory: Callable[..., AbstractContextManager[Any]] | None = None,
    ) -> ToolContext:
        """Wire a context to the real Stratum modules (lazy; no I/O at build).

        Collaborators are constructed but not connected: ``LLMClient``,
        ``MemoryStore``, ``TemporalGraph``, ``FilesystemStore``, ``Curator``,
        ``QueryEngine``, ``SessionStore``, ``IngestPipeline`` and ``JobQueue`` all
        defer their database/network work to the first call. When ``auth`` is
        omitted a bootstrap ``admin`` context for tenant ``default`` is used; real
        key-based authentication is layered on by the transport (a later WP).
        """
        from ..curator import Curator
        from ..db import connection as db_connection
        from ..filesystem import FilesystemStore
        from ..graph import TemporalGraph
        from ..ingest import IngestPipeline, SessionStore
        from ..llm import LLMClient
        from ..query import QueryEngine
        from ..vector import MemoryStore
        from ..worker import JobQueue

        resolved_settings = settings or get_settings()
        connect = conn_factory or db_connection

        llm = LLMClient(resolved_settings)
        store = MemoryStore(resolved_settings)
        graph = TemporalGraph(resolved_settings, conn_factory=connect)
        fs = FilesystemStore(resolved_settings, conn_factory=connect)
        curator = Curator(resolved_settings, store=store, graph=graph, conn_factory=connect)
        query = QueryEngine(
            resolved_settings, llm=llm, store=store, graph=graph, fs=fs, conn_factory=connect
        )
        sessions = SessionStore(resolved_settings, store=store, fs=fs, conn_factory=connect)
        ingest = IngestPipeline(
            resolved_settings, llm=llm, store=store, fs=fs, graph=graph, conn_factory=connect
        )
        queue = JobQueue(resolved_settings, conn_factory=connect)

        if auth is None:
            auth = AuthContext(tenant="default", scopes={"admin"}, key_id="bootstrap")

        return cls(
            auth=auth,
            authorizer=Authorizer(),
            settings=resolved_settings,
            llm=llm,
            store=store,
            graph=graph,
            fs=fs,
            curator=curator,
            query=query,
            sessions=sessions,
            ingest=ingest,
            queue=queue,
            conn_factory=connect,
        )


# --------------------------------------------------------------------------- #
# Guard + helpers
# --------------------------------------------------------------------------- #


def _guard(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap a tool body so failures become structured error dicts.

    :class:`ToolError` maps to its code; any other exception maps to
    ``INTERNAL`` (and is logged) so the transport never sees a traceback.
    """
    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def async_wrapper(ctx: ToolContext, *args: Any, **kwargs: Any) -> Any:
            try:
                return await fn(ctx, *args, **kwargs)
            except ToolError as exc:
                _record_tool_error(fn.__name__)
                return exc.as_dict()
            except Exception as exc:  # noqa: BLE001 - the API must return, not raise
                _log_internal(fn.__name__, exc)
                _record_tool_error(fn.__name__)
                return error_result(ErrorCode.INTERNAL, "internal error")

        return async_wrapper

    @functools.wraps(fn)
    def wrapper(ctx: ToolContext, *args: Any, **kwargs: Any) -> Any:
        try:
            return fn(ctx, *args, **kwargs)
        except ToolError as exc:
            _record_tool_error(fn.__name__)
            return exc.as_dict()
        except Exception as exc:  # noqa: BLE001 - the API must return, not raise
            _log_internal(fn.__name__, exc)
            _record_tool_error(fn.__name__)
            return error_result(ErrorCode.INTERNAL, "internal error")

    return wrapper


def _log_internal(tool: str, exc: Exception) -> None:
    """Log an unexpected tool failure (details) without leaking them to clients.

    The structured detail (exception type and message) goes to the logs only —
    via :func:`stratum.observability.log_event` — so the client always receives
    the generic ``INTERNAL`` message and never internal specifics (m3).
    """
    logger.exception("tool %s failed", tool)
    try:
        from ..observability import log_event  # noqa: PLC0415 - lazy import

        log_event(
            logger,
            "tool internal error",
            level=logging.ERROR,
            tool=tool,
            detail=f"{type(exc).__name__}: {exc}",
        )
    except Exception:  # noqa: BLE001 - logging must never mask the original error
        pass


def _record_tool_error(tool: str) -> None:
    """Increment ``stratum_tool_errors_total{tool=...}`` (B20c), best-effort."""
    try:
        from ..observability import default_metrics  # noqa: PLC0415 - lazy import

        default_metrics().inc("stratum_tool_errors_total", tool=tool)
    except Exception:  # noqa: BLE001 - metering must never break the call path
        pass


def _feed_llm_gauge(ctx: ToolContext) -> None:
    """Publish the cumulative LLM token count as a gauge (B20c), best-effort."""
    llm = ctx.llm
    usage = getattr(llm, "usage", None)
    if not callable(usage):
        return
    try:
        total = int(usage().total_tokens)
        from ..observability import default_metrics  # noqa: PLC0415 - lazy import

        default_metrics().set_gauge("stratum_llm_total_tokens", total)
    except Exception:  # noqa: BLE001 - metering must never break the call path
        pass


def _audit(
    ctx: ToolContext,
    tenant: str,
    action: str,
    *,
    target_uri: str | None = None,
    detail: Mapping[str, Any] | None = None,
) -> None:
    """Append a tenant-scoped ``audit_log`` row (B20a), best-effort.

    ``log_audit`` writes through the injected connection factory; a failure is
    swallowed so the primary tool result is never lost to an audit-trail hiccup
    (the ``memory_events`` journal remains the authoritative record). Skipped when
    no factory is injected, so a purely in-memory tool context (unit tests) never
    reaches for the real pool.
    """
    if ctx.conn_factory is None:
        return
    try:
        from ..auth import log_audit  # noqa: PLC0415 - lazy import

        log_audit(
            tenant,
            _actor(ctx) or "unknown",
            action,
            target_uri=target_uri,
            detail=detail,
            conn_factory=ctx.conn_factory,
        )
    except Exception:  # noqa: BLE001 - audit is best-effort on the request path
        pass


def _parse(uri: str) -> Any:
    """Parse ``uri`` or raise a ``VALIDATION`` :class:`ToolError`."""
    if not isinstance(uri, str) or not uri.strip():
        raise ToolError(ErrorCode.VALIDATION, "uri must be a non-empty string")
    try:
        return parse_uri(uri)
    except UriError as exc:
        raise ToolError(ErrorCode.VALIDATION, str(exc)) from exc


def _actor(ctx: ToolContext) -> str | None:
    """Return the acting identity (the API key id) for events/audit."""
    return ctx.auth.key_id if ctx.auth is not None else None


def _authorize(ctx: ToolContext, tool: str, uri: str | None = None) -> str:
    """Enforce the scope+tenant gate for ``tool``; return the acting tenant.

    The scope comes from :data:`SCOPE_FOR_TOOL`. The tenant is derived from
    ``uri`` when given (so a non-admin key cannot act on another tenant's URI),
    otherwise from the authenticated context.
    """
    scope = SCOPE_FOR_TOOL.get(tool)
    if scope is None:  # pragma: no cover - defensive, all tools are mapped
        raise ToolError(ErrorCode.INTERNAL, f"unknown tool {tool!r}")

    auth_ctx = ctx.auth
    if auth_ctx is None:
        raise ToolError(ErrorCode.FORBIDDEN, "no authentication context")

    tenant = _parse(uri).tenant if uri is not None else auth_ctx.tenant
    authorizer = ctx.authorizer or Authorizer()
    try:
        authorizer.authorize(auth_ctx, scope=scope, tenant=tenant)
    except (ScopeError, PermissionError) as exc:
        raise ToolError(ErrorCode.FORBIDDEN, str(exc)) from exc
    except ValueError as exc:  # unknown scope -> programming error
        raise ToolError(ErrorCode.INTERNAL, str(exc)) from exc
    return tenant


def _log_event(
    ctx: ToolContext,
    tenant: str,
    op: str,
    *,
    target_uri: str | None = None,
    before: Any | None = None,
    after: Any | None = None,
) -> str:
    """Append a ``memory_events`` row via the store (or the ``events`` sink)."""
    actor = _actor(ctx)
    store = ctx.store
    if store is not None and hasattr(store, "log_event"):
        return str(
            store.log_event(
                tenant,
                op=op,
                target_uri=target_uri,
                before=before,
                after=after,
                actor=actor,
            )
        )
    if ctx.events is not None:
        return str(
            ctx.events(
                tenant=tenant,
                op=op,
                target_uri=target_uri,
                before=before,
                after=after,
                actor=actor,
            )
        )
    raise ToolError(ErrorCode.INTERNAL, "no event store configured")


def _default_hard_delete(ctx: ToolContext, tenant: str, uri: str) -> dict[str, Any]:
    """Delete rows, vectors, graph rows and the file for ``uri`` (hard forget).

    The whole deletion runs in **one transaction**, in dependency order so the
    no-CASCADE foreign keys (``facts.subject_entity_id`` → ``entities``,
    ``edges.fact_id`` → ``facts``, ``edges.src/dst_entity_id`` → ``entities``)
    are never violated:

    1. ``edges`` — those backed by a fact of ``uri`` **or** touching an entity
       whose ``canonical_uri`` is ``uri``;
    2. ``facts`` — those sourced from ``uri`` or belonging to such an entity;
    3. ``entities`` — the entity whose ``canonical_uri`` is ``uri``;
    4. ``chunks`` / ``files`` / ``uri_registry`` — the row catalogue of ``uri``.

    Embeddings are dropped with their rows (the ``*_embedding_idx`` only indexes
    them; the vector account is tallied separately through
    :func:`_default_vector_delete`). The vault file is then removed through
    :func:`_default_fs_delete`, which commits ``forget: <uri>``.

    Returns:
        ``{"deleted_refs": int, "git_commit": str | None}``.
    """
    from ..db import connection as db_connection  # noqa: PLC0415 - lazy import

    connect = ctx.conn_factory or db_connection
    # ``uri`` is a path-style civic address; an entity's canonical URI may
    # correspond to one of several equivalent shapes (``fact_uri``/``entity_uri``
    # built by the Curator use a leading ``stratum://``), so match on a small
    # canonical set and on the ``stratum://`` suffix.
    _parse(uri)
    stripped = uri[len("stratum://"):] if uri.startswith("stratum://") else uri
    candidates = {uri, stripped, f"stratum://{stripped}"}

    # Each statement is (SQL, params); executed in order, inside one transaction.
    # ``candidates`` is the entity/URI identity set; ``facts_of_uri`` is the set
    # of facts sourced from or incident to (subject *or* object) the URI's entity.
    facts_of_uri = (
        "SELECT fact_id FROM facts f WHERE f.tenant_id = %s AND "
        "(f.source_uri = ANY(%s) OR f.subject_entity_id IN "
        " (SELECT entity_id FROM entities WHERE tenant_id = %s AND canonical_uri = ANY(%s)) "
        " OR f.object_entity_id IN "
        " (SELECT entity_id FROM entities WHERE tenant_id = %s AND canonical_uri = ANY(%s)))"
    )
    statements: tuple[tuple[str, tuple[Any, ...]], ...] = (
        # 1. edges: fact-backed by one of the URI's facts, or touching its entity.
        (
            "DELETE FROM edges WHERE tenant_id = %s AND "
            f"(fact_id IN ({facts_of_uri}) "
            "OR src_entity_id IN (SELECT entity_id FROM entities WHERE tenant_id = %s "
            " AND canonical_uri = ANY(%s)) "
            "OR dst_entity_id IN (SELECT entity_id FROM entities WHERE tenant_id = %s "
            " AND canonical_uri = ANY(%s)))",
            (
                tenant,
                tenant, list(candidates), tenant, list(candidates), tenant, list(candidates),
                tenant, list(candidates),
                tenant, list(candidates),
            ),
        ),
        # 2a. preserve the self-referential ``superseded_by`` FK: any fact still
        # pointing at a soon-to-be-deleted fact is detached first.
        (
            "UPDATE facts SET superseded_by = NULL WHERE tenant_id = %s "
            f"AND superseded_by IN ({facts_of_uri})",
            (
                tenant,
                tenant, list(candidates), tenant, list(candidates), tenant, list(candidates),
            ),
        ),
        # 2b. facts: sourced from the URI or incident to its entity.
        (
            "DELETE FROM facts WHERE tenant_id = %s AND "
            "(source_uri = ANY(%s) OR subject_entity_id IN "
            " (SELECT entity_id FROM entities WHERE tenant_id = %s AND canonical_uri = ANY(%s)) "
            " OR object_entity_id IN "
            " (SELECT entity_id FROM entities WHERE tenant_id = %s AND canonical_uri = ANY(%s)))",
            (
                tenant, list(candidates), tenant, list(candidates),
                tenant, list(candidates),
            ),
        ),
        # 3. entities: the URI's own entity.
        (
            "DELETE FROM entities WHERE tenant_id = %s AND canonical_uri = ANY(%s)",
            (tenant, list(candidates)),
        ),
        # 4. rows keyed directly by the URI.
        ("DELETE FROM chunks WHERE tenant_id = %s AND source_uri = %s", (tenant, uri)),
        ("DELETE FROM files WHERE tenant_id = %s AND uri = %s", (tenant, uri)),
        ("DELETE FROM uri_registry WHERE uri = %s", (uri,)),
        # 5. B8: derived state that also belongs to the URI's right-to-be-forgotten
        #    set — pending/decided staging candidates, episode KV episodes whose
        #    stored ``uri`` is this URI (the key is ``episode:<session>``, so the
        #    payload's ``uri`` is the reliable join), and reflect syntheses scoped
        #    to the URI. Without these a hard forget left resurrectable residue.
        (
            "DELETE FROM staging_candidates WHERE tenant_id = %s AND source_uri = %s",
            (tenant, uri),
        ),
        (
            "DELETE FROM kv WHERE tenant_id = %s AND value->>'uri' = %s",
            (tenant, uri),
        ),
        (
            "DELETE FROM syntheses WHERE tenant_id = %s AND scope_uri = %s",
            (tenant, uri),
        ),
    )
    deleted = 0
    with connect(tenant) as conn, conn.cursor() as cur:
        for sql, params in statements:
            cur.execute(sql, params)
            deleted += int(getattr(cur, "rowcount", 0) or 0)
        conn.commit()

    deleted += _run_vector_delete(ctx, tenant, uri)
    git_commit = _run_fs_delete(ctx, tenant, uri)
    return {"deleted_refs": deleted, "git_commit": git_commit}


def _run_vector_delete(ctx: ToolContext, tenant: str, uri: str) -> int:
    """Run the injected ``vector_delete`` hook, else the default best-effort."""
    hook = ctx.vector_delete
    if hook is not None:
        result = hook(tenant, uri)
        return int(result) if isinstance(result, int) else 0
    return _default_vector_delete(ctx, tenant, uri)


def _run_fs_delete(ctx: ToolContext, tenant: str, uri: str) -> str | None:
    """Run the injected ``fs_delete`` hook, else the default vault removal."""
    hook = ctx.fs_delete
    if hook is not None:
        result = hook(tenant, uri)
        return str(result) if result else None
    return _default_fs_delete(ctx, tenant, uri)


def _default_fs_delete(ctx: ToolContext, tenant: str, uri: str) -> str | None:
    """Remove the vault file for ``uri`` with a Git commit (``forget: <uri>``).

    Delegates to the injected filesystem store's ``delete`` when it offers one
    (the production :class:`~stratum.filesystem.FilesystemStore` path); otherwise
    removes the file behind :meth:`FilesystemStore.path_for` best-effort. Returns
    the new commit hash, or ``None``.
    """
    fs = ctx.fs
    if fs is None:
        return None
    deleter = getattr(fs, "delete", None)
    if callable(deleter):
        try:
            result = deleter(uri, tenant=tenant)
        except (OSError, ValueError, TypeError):  # pragma: no cover - best-effort
            return None
        if isinstance(result, Mapping):
            commit = result.get("git_commit")
            return str(commit) if commit else None
        # FilesystemStore.delete returns the commit hash directly (str | None).
        if isinstance(result, str) and result:
            return result
        return None
    path_for = getattr(fs, "path_for", None)
    if not callable(path_for):
        return None
    try:
        path = path_for(uri)
        if path.is_file():
            path.unlink()
    except (OSError, ValueError):  # pragma: no cover - best-effort file removal
        return None
    return None


def _default_vector_delete(ctx: ToolContext, tenant: str, uri: str) -> int:
    """Drop the embeddings associated with ``uri`` via the store, if supported.

    The ``facts``/``files``/``chunks`` rows carry the vectors and are removed by
    :func:`_default_hard_delete`; a store exposing a ``delete_by_source``-style
    hook is asked to clean any additional vector/KV state it owns. Returns the
    number of extra references removed (``0`` when unsupported).
    """
    store = ctx.store
    if store is None:
        return 0
    hook = getattr(store, "delete_by_source", None)
    if not callable(hook):
        return 0
    try:
        result = hook(tenant, uri)
    except (OSError, ValueError, TypeError):  # pragma: no cover - best-effort
        return 0
    if isinstance(result, int):
        return result
    if isinstance(result, Mapping):
        value = result.get("deleted_refs") or result.get("count")
        return int(value) if isinstance(value, int) else 0
    return 0


def _get_ingest(ctx: ToolContext) -> Any:
    """Return the ingestion pipeline, building one from the context if needed."""
    if ctx.ingest is not None:
        return ctx.ingest
    if ctx.llm is None or ctx.store is None or ctx.fs is None or ctx.graph is None:
        raise ToolError(ErrorCode.INTERNAL, "ingestion pipeline is not configured")
    from ..ingest import IngestPipeline  # noqa: PLC0415 - lazy import on purpose

    ctx.ingest = IngestPipeline(
        ctx.settings or get_settings(),
        llm=ctx.llm,
        store=ctx.store,
        fs=ctx.fs,
        graph=ctx.graph,
        conn_factory=ctx.conn_factory,
    )
    return ctx.ingest


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #


@_guard
async def stratum_write(
    ctx: ToolContext,
    *,
    uri: str,
    content: str,
    kind: str,
    metadata: Mapping[str, Any] | None = None,
    auto_promote: bool = False,
) -> dict[str, Any]:
    """Write content through ingestion + staging (§6 ``stratum_write``).

    Runs the ECL pipeline (chunk → embed → stage candidates) and, when
    ``auto_promote`` is set, immediately hands the staged candidates to the
    Curator. Returns ``{uri, status, l0, candidate_id, ingest_id, ...}``.

    Args:
        uri: Target ``stratum://`` URI (its tenant is the RLS scope).
        content: Markdown/JSON body to ingest.
        kind: One of :data:`ALLOWED_WRITE_KINDS`.
        metadata: Optional extra inputs (``tags``, ``source_uri``, ...); echoed
            back in the result.
        auto_promote: When ``True`` the Curator runs on the staged candidates and
            ``status`` becomes ``"promoted"``; otherwise ``"staged"``.
    """
    _authorize(ctx, "write", uri)
    parsed = _parse(uri)
    if kind not in ALLOWED_WRITE_KINDS:
        raise ToolError(
            ErrorCode.VALIDATION,
            f"invalid kind {kind!r}; allowed: {sorted(ALLOWED_WRITE_KINDS)}",
        )
    if not isinstance(content, str):
        raise ToolError(ErrorCode.VALIDATION, "content must be a string")

    tenant = parsed.tenant
    ingest = _get_ingest(ctx)
    extra_metadata = dict(metadata or {})
    result = await _ingest_with_metadata(
        ingest,
        content,
        tenant=tenant,
        source_uri=uri,
        kind=kind,
        metadata=extra_metadata,
    )

    candidate_id = getattr(result, "candidate_id", None)
    candidate_ids = list(getattr(result, "candidate_ids", None) or [])
    if candidate_id and candidate_id not in candidate_ids:
        candidate_ids.append(candidate_id)
    status = "staged"
    promote: dict[str, Any] | None = None
    if auto_promote:
        # B1: auto-promote hand the staged candidates to the Curator, so it needs
        # the ``promote`` scope *in addition* to ``write``. Checking it here —
        # before any Curator access — makes a write-only key fail fast.
        _authorize(ctx, "promote", uri)
        promote = _promote_staged(ctx, tenant, candidate_ids=candidate_ids)
        status = "promoted"

    _audit(
        ctx,
        tenant,
        "WRITE",
        target_uri=uri,
        detail={"kind": kind, "status": status, "ingest_id": getattr(result, "ingest_id", None)},
    )
    _feed_llm_gauge(ctx)

    return {
        "uri": uri,
        "status": status,
        "l0": make_l0(content),
        "candidate_id": candidate_id or (candidate_ids[0] if candidate_ids else None),
        "candidate_ids": candidate_ids,
        "ingest_id": getattr(result, "ingest_id", None),
        "chunks_new": getattr(result, "chunks_new", None),
        "facts_staged": getattr(result, "facts_staged", None),
        "truncated": getattr(result, "truncated", None),
        "metadata": extra_metadata,
        "promote": promote,
    }


async def _ingest_with_metadata(
    ingest: Any,
    content: str,
    *,
    tenant: str,
    source_uri: str,
    kind: str,
    metadata: Mapping[str, Any],
) -> Any:
    """Call ``ingest.ingest`` forwarding ``metadata`` when the pipeline accepts it.

    The §6 ``metadata`` object (``tags``, ``valid_from``, ``source_uri``, ...) is
    forwarded as a keyword argument when the pipeline supports it and is ignored
    otherwise, so the write call never crashes on an older pipeline (m1). Use
    :func:`_accepts_metadata` to decide, keeping one call site.
    """
    if metadata and _accepts_metadata(ingest):
        return await ingest.ingest(
            content, tenant=tenant, source_uri=source_uri, kind=kind, metadata=metadata
        )
    return await ingest.ingest(content, tenant=tenant, source_uri=source_uri, kind=kind)


def _accepts_metadata(ingest: Any) -> bool:
    """Return ``True`` when ``ingest.ingest`` declares a ``metadata`` parameter."""
    try:
        params = inspect.signature(ingest.ingest).parameters
    except (TypeError, ValueError):  # pragma: no cover - builtins / C callables
        return False
    if "metadata" in params:
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


def _promote_staged(
    ctx: ToolContext, tenant: str, *, candidate_ids: list[str] | None = None
) -> dict[str, Any]:
    """Promote **exactly** the candidates this write staged (B7).

    The write's :class:`~stratum.ingest.IngestResult` now carries ``candidate_ids``;
    promoting only those ids avoids the old behaviour of sweeping the tenant's
    entire ``pending`` backlog. A single id yields the per-candidate result
    shape; several are promoted one by one and aggregated. Only when the write
    produced *no* ids does it fall back to the tenant-wide
    :meth:`Curator.promote_batch`.
    """
    curator = ctx.curator
    if curator is None:
        raise ToolError(ErrorCode.INTERNAL, "curator is not configured")
    ids = [cid for cid in (candidate_ids or []) if cid]
    if ids and hasattr(curator, "promote"):
        if len(ids) == 1:
            try:
                result = curator.promote(tenant, ids[0], actor=_actor(ctx))
            except LookupError as exc:
                raise ToolError(ErrorCode.NOT_FOUND, str(exc)) from exc
            return _as_result_dict(result, candidate_id=ids[0])
        approved = rejected = 0
        results: list[dict[str, Any]] = []
        for cid in ids:
            try:
                outcome = curator.promote(tenant, cid, actor=_actor(ctx))
            except LookupError as exc:
                rejected += 1
                results.append({"candidate_id": cid, "status": "NOT_FOUND", "reason": str(exc)})
                continue
            outcome = _as_result_dict(outcome, candidate_id=cid)
            results.append(outcome)
            if outcome.get("status") == "approved":
                approved += 1
            else:
                rejected += 1
        return {
            "processed": len(ids),
            "approved": approved,
            "rejected": rejected,
            "candidate_ids": ids,
            "results": results,
        }
    if hasattr(curator, "promote_batch"):
        result = curator.promote_batch(tenant)
        return _as_result_dict(result)
    if hasattr(curator, "promote"):
        raise ToolError(
            ErrorCode.INTERNAL,
            "cannot auto-promote: the write produced no candidate ids",
        )
    raise ToolError(ErrorCode.INTERNAL, "curator exposes neither promote nor promote_batch")


def _as_result_dict(result: Any, *, candidate_id: str | None = None) -> dict[str, Any]:
    """Normalise a curator result to a mapping (optionally tagging the id)."""
    if isinstance(result, Mapping):
        normalised = dict(result)
    else:  # pragma: no cover - curator results are mappings in practice
        normalised = {"result": result}
    if candidate_id is not None:
        normalised.setdefault("candidate_id", candidate_id)
    return normalised


@_guard
def stratum_query(
    ctx: ToolContext,
    *,
    query: str,
    scope: str | None = None,
    mode: str = "hybrid",
    k: int = 8,
    level: str = "l0",
    as_of_valid: Any | None = None,
    as_of_tx: Any | None = None,
    filters: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Hybrid search with time and level control (§6 ``stratum_query``).

    Delegates to :meth:`stratum.query.QueryEngine.query` and wraps the hit list as
    ``{"results": [...], "count": int, "mode", "level"}``.
    """
    # ``scope`` is a path *prefix*, not a resource URI: the engine always reads
    # under the auth tenant, so the scope only narrows the result set and never
    # widens tenant access. Authorise against the auth tenant, not the prefix.
    _authorize(ctx, "query")
    if not isinstance(query, str) or not query.strip():
        raise ToolError(ErrorCode.VALIDATION, "query must be a non-empty string")
    if mode not in _QUERY_MODES:
        raise ToolError(ErrorCode.VALIDATION, f"invalid mode {mode!r}; allowed: {_QUERY_MODES}")
    if level not in _QUERY_LEVELS:
        raise ToolError(ErrorCode.VALIDATION, f"invalid level {level!r}; allowed: {_QUERY_LEVELS}")
    if not isinstance(k, int) or k < 1:
        raise ToolError(ErrorCode.VALIDATION, f"k must be an integer >= 1, got {k!r}")
    if ctx.query is None:
        raise ToolError(ErrorCode.INTERNAL, "query engine is not configured")

    tenant = ctx.auth.tenant if ctx.auth is not None else None
    if tenant is None:
        raise ToolError(ErrorCode.FORBIDDEN, "no tenant in authentication context")

    try:
        results = ctx.query.query(
            tenant,
            query,
            scope=scope,
            mode=mode,
            k=k,
            level=level,
            as_of_valid=as_of_valid,
            as_of_tx=as_of_tx,
            filters=dict(filters or {}),
        )
    except ValueError as exc:
        raise ToolError(ErrorCode.VALIDATION, str(exc)) from exc

    hits = list(results or [])
    return {"results": hits, "count": len(hits), "mode": mode, "level": level}


@_guard
def stratum_get(ctx: ToolContext, *, uri: str, level: str = "l1") -> dict[str, Any]:
    """Direct read of one URI at ``level`` (§6 ``stratum_get``).

    Serves the document from the filesystem vault
    (:meth:`stratum.filesystem.FilesystemStore.read`), which owns the L0–L2
    representations; a missing document is a ``NOT_FOUND``.
    """
    tenant = _authorize(ctx, "get", uri)
    if level not in _QUERY_LEVELS:
        raise ToolError(ErrorCode.VALIDATION, f"invalid level {level!r}; allowed: {_QUERY_LEVELS}")
    if ctx.fs is None:
        raise ToolError(ErrorCode.INTERNAL, "filesystem store is not configured")
    try:
        document = ctx.fs.read(uri, level=level, tenant=tenant)
    except FileNotFoundError as exc:
        raise ToolError(ErrorCode.NOT_FOUND, f"no memory at {uri!r}") from exc
    except ValueError as exc:
        raise ToolError(ErrorCode.VALIDATION, str(exc)) from exc

    metadata = dict(document.get("metadata") or {})
    return {
        "uri": document.get("uri", uri),
        "level": document.get("level", level),
        "content": document.get("content", ""),
        "metadata": metadata,
        "valid_from": metadata.get("valid_from"),
        "valid_to": metadata.get("valid_to"),
    }


@_guard
def stratum_promote(
    ctx: ToolContext,
    *,
    candidate_id: str,
    override_reason: str | None = None,
) -> dict[str, Any]:
    """Release one staging candidate into the active store (§6 ``stratum_promote``).

    Delegates to :meth:`stratum.curator.Curator.promote`; an unknown candidate is
    a ``NOT_FOUND``. ``override_reason`` is recorded on the result (the Curator's
    validation itself is never bypassed).
    """
    _authorize(ctx, "promote")
    if not isinstance(candidate_id, str) or not candidate_id.strip():
        raise ToolError(ErrorCode.VALIDATION, "candidate_id must be a non-empty string")
    if ctx.curator is None:
        raise ToolError(ErrorCode.INTERNAL, "curator is not configured")

    tenant = ctx.auth.tenant if ctx.auth is not None else None
    if tenant is None:
        raise ToolError(ErrorCode.FORBIDDEN, "no tenant in authentication context")

    try:
        result = ctx.curator.promote(tenant, candidate_id, actor=_actor(ctx))
    except LookupError as exc:
        raise ToolError(ErrorCode.NOT_FOUND, str(exc)) from exc

    result = dict(result or {})
    raw_status = str(result.get("status", ""))
    status = "promoted" if raw_status == "approved" else raw_status
    _audit(
        ctx,
        tenant,
        "PROMOTE",
        target_uri=result.get("uri"),
        detail={"candidate_id": candidate_id, "status": status},
    )
    _feed_llm_gauge(ctx)
    return {
        "uri": result.get("uri"),
        "status": status,
        "event_id": result.get("event_id"),
        "action": result.get("action"),
        "reason": result.get("reason"),
        "candidate_id": candidate_id,
        "override_reason": override_reason,
    }


@_guard
def stratum_archive(ctx: ToolContext, *, uri: str, reason: str) -> dict[str, Any]:
    """Archive a URI from the active store, keeping history (§6 ``stratum_archive``).

    A soft, non-destructive operation: it appends an ``ARCHIVE``
    ``memory_events`` row and never deletes rows, vectors or the file.
    """
    tenant = _authorize(ctx, "archive", uri)
    _parse(uri)
    if not isinstance(reason, str) or not reason.strip():
        raise ToolError(ErrorCode.VALIDATION, "reason is required")
    event_id = _log_event(
        ctx, tenant, "ARCHIVE", target_uri=uri, after={"reason": reason}
    )
    return {"uri": uri, "status": "archived", "event_id": event_id, "reason": reason}


@_guard
def stratum_forget(
    ctx: ToolContext,
    *,
    uri: str,
    hard: bool = False,
    reason: str,
) -> dict[str, Any]:
    """Right-to-be-forgotten removal (§6 ``stratum_forget``).

    ``hard=False`` (soft) only archives — no physical deletion — while
    ``hard=True`` deletes the rows, vectors, graph rows and the vault file and
    counts the deleted references. Either way a ``FORGET`` ``memory_events`` row
    is written.
    """
    tenant = _authorize(ctx, "forget", uri)
    _parse(uri)
    if not isinstance(reason, str) or not reason.strip():
        raise ToolError(ErrorCode.VALIDATION, "reason is required")

    mode = "hard" if hard else "soft"
    deleted = 0
    git_commit: str | None = None
    if hard:
        handler = ctx.hard_delete
        outcome = handler(tenant, uri) if handler is not None else _default_hard_delete(ctx, tenant, uri)
        deleted, git_commit = _hard_delete_outcome(outcome)

    event_id = _log_event(
        ctx,
        tenant,
        "FORGET",
        target_uri=uri,
        after={
            "mode": mode,
            "reason": reason,
            "deleted_refs": deleted,
            "git_commit": git_commit,
        },
    )
    _audit(
        ctx,
        tenant,
        "FORGET",
        target_uri=uri,
        detail={"mode": mode, "reason": reason, "deleted_refs": deleted},
    )
    _feed_llm_gauge(ctx)
    return {
        "uri": uri,
        "mode": mode,
        "deleted_refs": deleted,
        "git_commit": git_commit,
        "event_id": event_id,
        "reason": reason,
    }


def _hard_delete_outcome(outcome: Any) -> tuple[int, str | None]:
    """Normalise a hard-delete result to ``(deleted_refs, git_commit)``.

    Accepts a plain integer (tests/simple hooks) or a mapping carrying
    ``deleted_refs`` and an optional ``git_commit``.
    """
    if isinstance(outcome, Mapping):
        refs = outcome.get("deleted_refs")
        deleted = int(refs) if isinstance(refs, int) else 0
        commit = outcome.get("git_commit")
        return deleted, str(commit) if commit else None
    if isinstance(outcome, int):
        return outcome, None
    return 0, None


@_guard
def stratum_link(
    ctx: ToolContext,
    *,
    src_uri: str,
    dst_uri: str,
    rel_type: str,
    valid_from: Any | None = None,
    valid_to: Any | None = None,
    confidence: float = 1.0,
) -> dict[str, Any]:
    """Set an explicit bi-temporal graph edge (§6 ``stratum_link``).

    Resolves/creates the two endpoint entities, writes a fact for the relation
    and a directed edge backed by that fact. Returns ``{edge_id, fact_id, ...}``.
    """
    _authorize(ctx, "link", src_uri)
    _authorize(ctx, "link", dst_uri)
    _parse(src_uri)
    _parse(dst_uri)
    if not isinstance(rel_type, str) or not rel_type.strip():
        raise ToolError(ErrorCode.VALIDATION, "rel_type must be a non-empty string")
    if ctx.graph is None:
        raise ToolError(ErrorCode.INTERNAL, "temporal graph is not configured")

    tenant = _parse(src_uri).tenant
    src_name = _endpoint_name(src_uri)
    dst_name = _endpoint_name(dst_uri)

    src_id = ctx.graph.upsert_entity(tenant, name=src_name, canonical_uri=src_uri)
    dst_id = ctx.graph.upsert_entity(tenant, name=dst_name, canonical_uri=dst_uri)

    fact_id = ctx.graph.add_fact(
        tenant,
        subject_entity_id=src_id,
        predicate=rel_type,
        object_entity_id=dst_id,
        valid_from=valid_from,
        valid_to=valid_to,
        confidence=float(confidence),
        source_uri=src_uri,
    )
    edge_id = ctx.graph.add_edge(
        tenant,
        src_entity_id=src_id,
        dst_entity_id=dst_id,
        rel_type=rel_type,
        valid_from=valid_from,
        valid_to=valid_to,
        fact_id=fact_id,
    )
    return {
        "edge_id": edge_id,
        "fact_id": fact_id,
        "src_uri": src_uri,
        "dst_uri": dst_uri,
        "rel_type": rel_type,
    }


@_guard
def stratum_session_append(
    ctx: ToolContext,
    *,
    session_id: str,
    role: str,
    content: str,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Append one turn to an episode/session (§6 ``stratum_session_append``).

    Delegates to :meth:`stratum.ingest.SessionStore.append` and returns
    ``{uri, turn, session_id}`` where ``turn`` is the new turn index.
    """
    _authorize(ctx, "session_append")
    if not isinstance(session_id, str) or not session_id.strip():
        raise ToolError(ErrorCode.VALIDATION, "session_id must be a non-empty string")
    if not isinstance(role, str) or not role.strip():
        raise ToolError(ErrorCode.VALIDATION, "role must be a non-empty string")
    if ctx.sessions is None:
        raise ToolError(ErrorCode.INTERNAL, "session store is not configured")

    tenant = ctx.auth.tenant if ctx.auth is not None else None
    if tenant is None:
        raise ToolError(ErrorCode.FORBIDDEN, "no tenant in authentication context")

    stored = ctx.sessions.append(
        tenant, session_id, role, content, dict(metadata or {})
    )
    stored = dict(stored or {})
    turn = stored.get("turn")
    if isinstance(turn, Mapping):
        turn_index = turn.get("index")
    else:
        turn_index = turn
    return {"uri": stored.get("uri"), "turn": turn_index, "session_id": session_id}


@_guard
def stratum_reflect(
    ctx: ToolContext,
    *,
    scope: str | None = None,
    budget_tokens: int = 20000,
    async_: bool = True,
) -> dict[str, Any]:
    """Trigger a budgeted synthesis run (§6 ``stratum_reflect``).

    Validates the token budget against ``Settings.reflect_max_tokens`` and
    enqueues a ``reflect`` job. Returns ``{job_id, scope, estimated_tokens}``.
    """
    tenant = _authorize(ctx, "reflect")
    settings = ctx.settings or get_settings()
    if not isinstance(budget_tokens, int) or budget_tokens < 1:
        raise ToolError(ErrorCode.VALIDATION, f"budget_tokens must be >= 1, got {budget_tokens!r}")
    if budget_tokens > settings.reflect_max_tokens:
        raise ToolError(
            ErrorCode.BUDGET_EXCEEDED,
            f"budget_tokens {budget_tokens} exceeds the maximum "
            f"{settings.reflect_max_tokens}",
        )
    if ctx.queue is None:
        raise ToolError(ErrorCode.INTERNAL, "job queue is not configured")

    resolved_scope = scope or tenant
    job_id = ctx.queue.enqueue(
        tenant,
        "reflect",
        {"scope": resolved_scope, "budget_tokens": budget_tokens, "async": async_},
    )
    return {
        "job_id": str(job_id),
        "scope": resolved_scope,
        "estimated_tokens": budget_tokens,
    }


@_guard
def stratum_health(ctx: ToolContext) -> dict[str, Any]:
    """Readiness probe (§6 ``stratum_health``).

    Delegates to the injected probe (default :func:`stratum.observability.health`)
    and flattens the result to ``{status, db, git, llm, queued_jobs}``.
    """
    _authorize(ctx, "health")
    if ctx.health_fn is not None:
        raw: Mapping[str, Any] = ctx.health_fn()
    else:
        from ..observability import health as observability_health  # noqa: PLC0415

        raw = observability_health()

    return {
        "status": raw.get("status", "degraded"),
        "db": _sub_ok(raw.get("db")),
        "git": _sub_ok(raw.get("git")),
        "llm": _sub_ok(raw.get("llm")),
        "queued_jobs": _queued(raw.get("jobs")),
        "version": raw.get("version"),
    }


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def _endpoint_name(uri: str) -> str:
    """Derive a human-readable endpoint name from a URI's final segment."""
    return _parse(uri).path.rsplit("/", 1)[-1]


def _sub_ok(value: Any) -> bool:
    """Normalise a health sub-result to a boolean ``ok`` flag."""
    if isinstance(value, Mapping):
        return bool(value.get("ok"))
    return bool(value)


def _queued(value: Any) -> int | None:
    """Extract the queued-jobs count from a health sub-result."""
    if isinstance(value, Mapping):
        queued = value.get("queued")
        return int(queued) if isinstance(queued, int) else None
    return value if isinstance(value, int) else None
