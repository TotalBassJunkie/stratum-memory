"""LLM / embedding client abstraction over external OpenAI-compatible APIs.

This module talks to a FreeLLMAPI-compatible HTTP endpoint that exposes the
OpenAI surface (``POST /chat/completions`` and ``POST /embeddings``). It is the
only place in Stratum-Memory that is allowed to perform outbound LLM calls.

All configuration is read from :class:`stratum.config.Settings`
(``llm_base_url``, ``resolved_llm_api_key()``, ``llm_chat_model``,
``llm_embed_model``); no secrets are hard-coded here.

Cross-cutting concerns implemented in one place:

* retry with exponential backoff (3 attempts, only on 5xx / 429 / network errors),
* a 60 s request timeout,
* an in-memory LRU cache keyed by SHA-256 over ``model``, ``messages``/``texts``
  and all response-relevant arguments (``json_schema``, ``temperature``,
  ``max_tokens``),
* simple cost metering via :meth:`LLMClient.usage`.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections import OrderedDict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

import httpx
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt

from .config import Settings, get_settings

__all__ = ["ChatResult", "LLMClient", "LLMError", "Usage", "DEFAULT_TIMEOUT", "DEFAULT_CACHE_SIZE"]

DEFAULT_TIMEOUT = 60.0
DEFAULT_CACHE_SIZE = 512
DEFAULT_MAX_ATTEMPTS = 3


class LLMError(Exception):
    """Raised for any LLM/embedding/fetch failure.

    ``status`` is the HTTP status code when the failure came from an HTTP
    response, or ``None`` for configuration and network/transport errors.
    """

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class _RetryableStatus(Exception):
    """Internal signal for a **rate-limit** HTTP status (429).

    429 is treated separately from the shorter transient budget: free/shared
    providers ask for multi-second waits, so a 429 gets
    ``rate_limit_attempts`` attempts — never fewer than ``max_attempts``.
    """

    def __init__(self, status: int, message: str, retry_after: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.retry_after = retry_after


class _RetryableTransient(Exception):
    """Internal signal for a transient error (5xx / exhausted network retries).

    ``status`` is the HTTP status for a 5xx, or ``None`` for a transport error
    that ran out of attempts. Bounded by ``max_attempts`` (not the larger
    429 budget).
    """

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(slots=True)
class ChatResult:
    """Result of a chat completion call.

    ``content`` holds the assistant message as a string, or the parsed JSON
    object when the call was made with ``json_schema``.
    """

    content: Any
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    model: str = ""
    cached: bool = False


@dataclass(slots=True)
class Usage:
    """Cumulative cost-metering counters for one :class:`LLMClient`."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    calls: int = 0
    cache_hits: int = 0

    def as_dict(self) -> dict[str, int]:
        """Return the counters as a plain dict (for logging / health payloads)."""
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "calls": self.calls,
            "cache_hits": self.cache_hits,
        }


def _canonical(value: Any) -> str:
    """Deterministic JSON encoding used for cache keys."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def _sha256(*parts: Any) -> str:
    h = hashlib.sha256()
    for part in parts:
        h.update(_canonical(part).encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


class LLMClient:
    """Async client for a FreeLLMAPI-compatible chat + embedding endpoint.

    The client is testable without a server: pass an ``httpx.AsyncBaseTransport``
    (e.g. ``httpx.MockTransport``) via ``transport``.

    Args:
        settings: Configuration source; defaults to :func:`get_settings`.
        transport: Optional httpx transport (used for offline tests).
        timeout: Per-request timeout in seconds (default 60 s).
        cache_size: Maximum number of cached responses (LRU, default 512).
        max_attempts: Total attempts per request (default 3).
        backoff_base: Base multiplier for exponential backoff in seconds.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        cache_size: int = DEFAULT_CACHE_SIZE,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        backoff_base: float = 0.1,
        rate_limit_attempts: int = 8,
        max_backoff: float = 60.0,
    ) -> None:
        self.settings = settings or get_settings()
        self._base_url = self.settings.llm_base_url.rstrip("/")
        self._transport = transport
        self._timeout = timeout
        self._cache_size = cache_size
        self._max_attempts = max_attempts
        self._backoff_base = backoff_base
        self._rate_limit_attempts = max(max_attempts, rate_limit_attempts)
        self._max_backoff = max_backoff
        self._cache: OrderedDict[str, Any] = OrderedDict()
        self._usage = Usage()
        self._http: httpx.AsyncClient | None = None
        self._http_loop: Any = None

    # ------------------------------------------------------------------ setup

    def _client(self) -> httpx.AsyncClient:
        """Return an AsyncClient bound to the *current* event loop.

        Callers may drive this client from different loops over the client's
        lifetime (the sync QueryEngine wraps each call in its own
        ``asyncio.run``). An httpx AsyncClient — and its internal locks — is
        bound to the loop that first used it, so reuse across loops raises
        "bound to a different event loop". We therefore recreate the client
        whenever the running loop changes.
        """
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if self._http is None or self._http_loop is not running:
            # The old client (if any) is bound to a *different* loop that is no
            # longer running; its connection pool can never be awaited again, so
            # detach it and close it best-effort to avoid leaking sockets (B10).
            # ``aclose`` cannot be awaited on the current loop for a foreign-loop
            # client, so this is explicitly best-effort: if it cannot run, the
            # reference is dropped and the GC reclaims it (a ResourceWarning may
            # be emitted — documented here rather than silently ignored).
            old = self._http
            if old is not None and running is None:
                # Only closeable when no loop is running; on a *different* live
                # loop the old client cannot be awaited here, so the reference is
                # dropped and reclaimed by the GC (a ResourceWarning may be
                # emitted — documented rather than silently swallowed).
                try:
                    asyncio.run(old.aclose())
                except Exception:  # noqa: BLE001 - cleanup must never break a call
                    pass
            self._http = httpx.AsyncClient(transport=self._transport, timeout=self._timeout)
            self._http_loop = running
        return self._http

    def _auth_headers(self) -> dict[str, str]:
        key = self.settings.resolved_llm_api_key()
        if not key:
            raise LLMError("No LLM API key configured (llm_api_key / llm_api_key_file)")
        return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    def _require_base_url(self) -> str:
        if not self._base_url:
            raise LLMError("No LLM base URL configured (llm_base_url)")
        return self._base_url

    async def aclose(self) -> None:
        """Close the underlying HTTP connection pool."""
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def __aenter__(self) -> LLMClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ------------------------------------------------------------------ cache

    def _cache_get(self, key: str) -> Any | None:
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        return None

    def _cache_put(self, key: str, value: Any) -> None:
        self._cache[key] = value
        self._cache.move_to_end(key)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)

    # ------------------------------------------------------------- transport

    def _retry_wait(self, retry_state: Any) -> float:
        """Wait strategy: honour Retry-After on 429, else exponential backoff."""
        exc = retry_state.outcome.exception() if retry_state.outcome else None
        if isinstance(exc, _RetryableStatus) and exc.retry_after:
            try:
                return min(self._max_backoff, float(exc.retry_after))
            except (TypeError, ValueError):
                pass
        attempt = max(0, int(getattr(retry_state, "attempt_number", 1)) - 1)
        return min(self._max_backoff, self._backoff_base * (2**attempt))

    @staticmethod
    def _classify(status: int, message: str, retry_after: str | None) -> Exception:
        """Map an HTTP failure to the right retry class.

        A 429 is a rate limit (larger attempt budget); a 5xx is a transient
        server error (bounded by ``max_attempts``). They are distinct exception
        *types* so tenacity can stop each class at its own budget.
        """
        if status == 429:
            return _RetryableStatus(status, message, retry_after=retry_after)
        return _RetryableTransient(message, status=status)

    async def _send(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        json_body: Any | None = None,
        content: bytes | None = None,
    ) -> httpx.Response:
        """Send one request with retry/backoff on 5xx, 429 and network errors.

        Two retry budgets, deliberately distinct (B9):

        * **429 (rate limit)** — common with free/shared providers and often
          asking for a multi-second wait; retried up to
          ``self._rate_limit_attempts`` (default 8), honouring ``Retry-After``.
        * **5xx / network** — transient, retried up to ``self._max_attempts``
          (default 3). A 5xx therefore never borrows the larger 429 budget.

        The smaller ``max_attempts`` budget is implemented as the *outer* tenacity
        loop; a 429 raised inside it would terminate the outer loop early, so
        429s are instead re-issued by an inner per-attempt loop that owns its own
        ``rate_limit_attempts`` budget. The two loops nest cleanly: the inner
        loop only ever returns a response or re-raises a 429 once its budget is
        spent, which the outer loop then surfaces as a final failure.
        """
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(self._max_attempts),
            wait=self._retry_wait,
            retry=retry_if_exception_type((_RetryableTransient, httpx.TransportError)),
            reraise=True,
        ):
            with attempt:
                return await self._send_once(
                    method, url, headers=headers, json_body=json_body, content=content
                )
        raise LLMError("unreachable")  # pragma: no cover - tenacity always returns or raises

    async def _send_once(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        json_body: Any | None = None,
        content: bytes | None = None,
    ) -> httpx.Response:
        """One logical request, retrying 429s on their own larger budget."""
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(self._rate_limit_attempts),
            wait=self._retry_wait,
            retry=retry_if_exception_type(_RetryableStatus),
            reraise=True,
        ):
            with attempt:
                response = await self._client().request(
                    method, url, headers=dict(headers or {}), json=json_body, content=content
                )
                if response.status_code == 429 or response.status_code >= 500:
                    raise self._classify(
                        response.status_code,
                        f"HTTP {response.status_code}: {response.text[:200]}",
                        response.headers.get("retry-after"),
                    )
                return response
        raise LLMError("unreachable")  # pragma: no cover - tenacity always returns or raises

    @staticmethod
    def _wrap_transport_error(exc: Exception) -> LLMError:
        if isinstance(exc, _RetryableStatus):
            return LLMError(exc.message, status=exc.status)
        if isinstance(exc, _RetryableTransient):
            return LLMError(exc.message, status=exc.status)
        return LLMError(f"transport error: {exc}")

    # ------------------------------------------------------------------- chat

    async def chat(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        model: str | None = None,
        temperature: float = 0.0,
        json_schema: Mapping[str, Any] | None = None,
        max_tokens: int | None = None,
    ) -> ChatResult:
        """Call ``POST {base_url}/chat/completions``.

        Args:
            messages: OpenAI-style chat messages.
            model: Model override; defaults to ``Settings.llm_chat_model``.
            temperature: Sampling temperature (default 0.0 for determinism).
            json_schema: When given, request structured output via
                ``response_format={"type": "json_schema", "json_schema": ...}``
                and parse the returned content as JSON. Invalid JSON raises
                :class:`LLMError`.
            max_tokens: Optional completion token cap.

        Returns:
            A :class:`ChatResult`; ``cached`` is ``True`` on a cache hit.
        """
        headers = self._auth_headers()
        base_url = self._require_base_url()
        model = model or self.settings.llm_chat_model

        cache_key = _sha256("chat", model, list(messages), json_schema, temperature, max_tokens)
        cached = self._cache_get(cache_key)
        if cached is not None:
            self._usage.cache_hits += 1
            return replace(cached, cached=True)

        payload: dict[str, Any] = {
            "model": model,
            "messages": list(messages),
            "temperature": temperature,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if json_schema is not None:
            payload["response_format"] = {"type": "json_schema", "json_schema": dict(json_schema)}

        try:
            response = await self._send(
                "POST", f"{base_url}/chat/completions", headers=headers, json_body=payload
            )
        except httpx.TransportError as exc:
            raise self._wrap_transport_error(exc) from exc
        except (_RetryableStatus, _RetryableTransient) as exc:
            raise self._wrap_transport_error(exc) from exc

        if response.status_code >= 400:
            raise LLMError(
                f"chat failed: HTTP {response.status_code}: {response.text[:200]}",
                status=response.status_code,
            )

        try:
            data = response.json()
            choice = data["choices"][0]["message"]
            content = choice.get("content")
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"malformed chat response: {exc}", status=response.status_code) from exc

        if content is None:
            raise LLMError("chat response contained no content", status=response.status_code)

        if json_schema is not None:
            if not isinstance(content, str):
                raise LLMError("expected JSON string content for json_schema request")
            try:
                content = json.loads(content)
            except json.JSONDecodeError as exc:
                raise LLMError(f"invalid JSON in chat response: {exc}") from exc

        usage = data.get("usage") or {}
        result = ChatResult(
            content=content,
            prompt_tokens=int(usage.get("prompt_tokens", 0) or 0),
            completion_tokens=int(usage.get("completion_tokens", 0) or 0),
            total_tokens=int(usage.get("total_tokens", 0) or 0),
            model=str(data.get("model", model)),
            cached=False,
        )
        self._usage.calls += 1
        self._usage.prompt_tokens += result.prompt_tokens
        self._usage.completion_tokens += result.completion_tokens
        self._usage.total_tokens += result.total_tokens
        self._cache_put(cache_key, result)
        return result

    # ------------------------------------------------------------------ embed

    async def embed(
        self,
        texts: str | Iterable[str],
        *,
        model: str | None = None,
        dimensions: int | None = None,
    ) -> list[list[float]]:
        """Call ``POST {base_url}/embeddings`` and return vectors in input order.

        ``dimensions`` (defaults to ``Settings.embedding_dim``) is sent so the
        provider returns vectors matching the database column (e.g. 1024). If the
        provider rejects the parameter, the request is retried without it — the
        caller then sees the provider's native dimension.
        """
        # Embeddings may use a dedicated endpoint (e.g. local Ollama) so bulk
        # embedding does not compete with the chat provider's rate limits.
        embed_base = self.settings.llm_embed_base_url.rstrip("/")
        if embed_base:
            base_url = embed_base
            key = self.settings.resolved_llm_embed_api_key()
            headers = {"Content-Type": "application/json"}
            if key:
                headers["Authorization"] = f"Bearer {key}"
        else:
            base_url = self._require_base_url()
            headers = self._auth_headers()
        model = model or self.settings.llm_embed_model
        dims = self.settings.embedding_dim if dimensions is None else dimensions

        if isinstance(texts, str):
            inputs = [texts]
        else:
            inputs = list(texts)
        if not inputs:
            return []

        cache_key = _sha256("embed", model, inputs, dims)
        cached = self._cache_get(cache_key)
        if cached is not None:
            self._usage.cache_hits += 1
            return [list(vector) for vector in cached]

        payload: dict[str, Any] = {"model": model, "input": inputs}
        if dims:
            payload["dimensions"] = dims
        try:
            response = await self._send(
                "POST", f"{base_url}/embeddings", headers=headers, json_body=payload
            )
        except httpx.TransportError as exc:
            raise self._wrap_transport_error(exc) from exc
        except (_RetryableStatus, _RetryableTransient) as exc:
            raise self._wrap_transport_error(exc) from exc

        # Some providers reject the optional `dimensions` parameter; retry once
        # without it so embedding still works (caller gets native dimension).
        fell_back = False
        if response.status_code == 400 and "dimensions" in payload:
            fell_back = True
            payload.pop("dimensions", None)
            try:
                response = await self._send(
                    "POST", f"{base_url}/embeddings", headers=headers, json_body=payload
                )
            except httpx.TransportError as exc:
                raise self._wrap_transport_error(exc) from exc
            except (_RetryableStatus, _RetryableTransient) as exc:
                raise self._wrap_transport_error(exc) from exc

        if response.status_code >= 400:
            raise LLMError(
                f"embed failed: HTTP {response.status_code}: {response.text[:200]}",
                status=response.status_code,
            )

        try:
            data = response.json()
            rows = sorted(data["data"], key=lambda row: row.get("index", 0))
            vectors = [list(row["embedding"]) for row in rows]
        except (ValueError, KeyError, TypeError) as exc:
            raise LLMError(f"malformed embeddings response: {exc}", status=response.status_code) from exc

        # B16: after a fallback the provider rejected `dimensions`; if it then
        # returns a *different* length the vectors cannot be trusted anywhere the
        # DB column is dimensioned, so raise instead of poisoning the cache with
        # them. Only verified vectors are cached (the success path below).
        if fell_back and dims and (not vectors or len(vectors[0]) != dims):
            got = len(vectors[0]) if vectors else 0
            raise LLMError(
                f"embedding dimension mismatch: requested {dims}, got {got} "
                "(provider rejected `dimensions`)",
                status=response.status_code,
            )

        usage = data.get("usage") or {}
        self._usage.calls += 1
        self._usage.prompt_tokens += int(usage.get("prompt_tokens", 0) or 0)
        self._usage.total_tokens += int(usage.get("total_tokens", 0) or 0)
        self._cache_put(cache_key, vectors)
        return [list(vector) for vector in vectors]

    # ------------------------------------------------------------------ fetch

    async def fetch(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: Mapping[str, str] | None = None,
        body: Any | None = None,
    ) -> tuple[int, bytes]:
        """Fetch an arbitrary URL and return ``(status_code, raw_bytes)``.

        Used for URL ingestion. Retries on 5xx/429/network errors; 4xx responses
        are returned to the caller instead of raising. ``body`` may be a mapping
        (sent as JSON) or ``bytes``/``str`` (sent verbatim).
        """
        json_body: Any | None = None
        content: bytes | None = None
        if isinstance(body, (bytes, bytearray)):
            content = bytes(body)
        elif isinstance(body, str):
            content = body.encode("utf-8")
        elif body is not None:
            json_body = body

        try:
            response = await self._send(
                method.upper(), url, headers=headers, json_body=json_body, content=content
            )
        except httpx.TransportError as exc:
            raise self._wrap_transport_error(exc) from exc
        except (_RetryableStatus, _RetryableTransient) as exc:
            raise self._wrap_transport_error(exc) from exc

        self._usage.calls += 1
        return response.status_code, response.content

    # ------------------------------------------------------------------ usage

    def usage(self) -> Usage:
        """Return a snapshot of the accumulated cost-metering counters."""
        return replace(self._usage)
