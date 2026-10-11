"""Tests for the LLM/embedding client abstraction (no real network access)."""

from __future__ import annotations

import json

import httpx
import pytest

from stratum.config import Settings
from stratum.llm import DEFAULT_TIMEOUT, ChatResult, LLMClient, LLMError


def _settings(**overrides) -> Settings:
    """Build isolated settings that ignore ambient environment variables."""
    base = {
        "llm_base_url": "https://api.test/v1",
        "llm_api_key": "test-key",
        "llm_api_key_file": None,
        "llm_chat_model": "test-chat",
        "llm_embed_model": "test-embed",
    }
    base.update(overrides)
    return Settings(**base)


_CLIENT_KWARGS = {"rate_limit_attempts", "max_attempts", "max_backoff", "backoff_base", "cache_size"}


def _client(handler, **overrides) -> LLMClient:
    transport = httpx.MockTransport(handler)
    client_kw = {k: overrides.pop(k) for k in list(overrides) if k in _CLIENT_KWARGS}
    client_kw.setdefault("backoff_base", 0.0)
    return LLMClient(_settings(**overrides), transport=transport, **client_kw)


def _chat_response(content: str, *, model: str = "test-chat") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": model,
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
        },
    )


async def test_chat_returns_content_and_tokens():
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return _chat_response("hello world")

    client = _client(handler)
    result = await client.chat([{"role": "user", "content": "hi"}], max_tokens=42)

    assert isinstance(result, ChatResult)
    assert result.content == "hello world"
    assert result.prompt_tokens == 7
    assert result.completion_tokens == 3
    assert result.total_tokens == 10
    assert result.model == "test-chat"
    assert result.cached is False

    assert seen["url"] == "https://api.test/v1/chat/completions"
    assert seen["auth"] == "Bearer test-key"
    body = seen["body"]
    assert body["model"] == "test-chat"
    assert body["temperature"] == 0.0
    assert body["max_tokens"] == 42

    usage = client.usage()
    assert usage.calls == 1
    assert usage.total_tokens == 10
    assert usage.cache_hits == 0

    await client.aclose()


async def test_chat_json_schema_parses_and_rejects_invalid():
    payload = {"name": "Ada", "age": 36}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["response_format"]["type"] == "json_schema"
        assert body["response_format"]["json_schema"]["name"] == "person"
        return _chat_response(json.dumps(payload))

    schema = {
        "name": "person",
        "schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "age": {"type": "integer"}},
        },
    }
    client = _client(handler)
    result = await client.chat([{"role": "user", "content": "extract"}], json_schema=schema)
    assert result.content == payload

    async def bad_handler(request: httpx.Request) -> httpx.Response:
        return _chat_response("not-json{")

    bad_client = _client(bad_handler)
    with pytest.raises(LLMError):
        await bad_client.chat([{"role": "user", "content": "x"}], json_schema=schema)

    await client.aclose()
    await bad_client.aclose()


async def test_cache_hit_avoids_second_network_call():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return _chat_response("cached-answer")

    client = _client(handler)
    messages = [{"role": "user", "content": "same question"}]

    first = await client.chat(messages)
    second = await client.chat(messages)

    assert calls["n"] == 1
    assert first.cached is False
    assert second.cached is True
    assert second.content == "cached-answer"
    assert second.total_tokens == first.total_tokens

    usage = client.usage()
    assert usage.calls == 1
    assert usage.cache_hits == 1

    # A different message must trigger a fresh call.
    await client.chat([{"role": "user", "content": "different"}])
    assert calls["n"] == 2

    await client.aclose()


async def test_embed_preserves_input_order():
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://api.test/v1/embeddings"
        body = json.loads(request.content)
        assert body["input"] == ["a", "b", "c"]
        assert body["model"] == "test-embed"
        # Intentionally out of order to prove the client sorts by ``index``.
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 2, "embedding": [2.0, 2.0]},
                    {"index": 0, "embedding": [0.0, 0.0]},
                    {"index": 1, "embedding": [1.0, 1.0]},
                ],
                "usage": {"prompt_tokens": 6, "total_tokens": 6},
            },
        )

    client = _client(handler)
    vectors = await client.embed(["a", "b", "c"])
    assert vectors == [[0.0, 0.0], [1.0, 1.0], [2.0, 2.0]]

    # Second identical call is served from cache.
    vectors_again = await client.embed(["a", "b", "c"])
    assert vectors_again == vectors
    assert client.usage().cache_hits == 1
    assert client.usage().calls == 1

    await client.aclose()


async def test_embed_sends_dimensions_and_raises_on_mismatched_fallback():
    """B16: a 400 without ``dimensions`` is retried, but a fallback whose vector
    length != the requested ``dims`` must raise (never cache a bad dimension)."""
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        if "dimensions" in body:
            # Provider that rejects the optional parameter.
            return httpx.Response(400, json={"error": {"message": "unknown parameter dimensions"}})
        # Fallback returns a native dimension that does NOT match 1024.
        return httpx.Response(
            200,
            json={"data": [{"index": 0, "embedding": [0.1, 0.2]}], "usage": {}},
        )

    client = _client(handler, embedding_dim=1024)
    # First attempt carried dimensions=1024, the fallback retry did not.
    with pytest.raises(LLMError) as excinfo:
        await client.embed(["x"])
    assert seen[0].get("dimensions") == 1024
    assert "dimensions" not in seen[1]
    assert "dimension mismatch" in str(excinfo.value)
    # Nothing was cached (a re-call must hit the network again).
    with pytest.raises(LLMError):
        await client.embed(["x"])
    assert len(seen) == 4
    await client.aclose()


async def test_embed_fallback_with_matching_dimension_succeeds():
    """A fallback that still returns the requested dimension is accepted+cached."""
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        if "dimensions" in body:
            return httpx.Response(400, json={"error": {"message": "bad dimensions"}})
        vector = [0.5] * 4
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": vector}], "usage": {}})

    client = _client(handler, embedding_dim=4)
    vectors = await client.embed(["x"])
    assert vectors == [[0.5, 0.5, 0.5, 0.5]]
    # The verified fallback result is cached: a repeat is a cache hit, no network.
    again = await client.embed(["x"])
    assert again == vectors
    assert len(seen) == 2
    assert client.usage().cache_hits == 1
    await client.aclose()


async def test_embed_includes_dimensions_when_supported():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.0]}], "usage": {}})

    client = _client(handler, embedding_dim=1024)
    await client.embed(["x"])
    assert seen["dimensions"] == 1024
    await client.aclose()


def test_client_recreated_across_event_loops():
    """The same client must be usable from sequential asyncio.run() calls.

    Reproduces the Pi bug: QueryEngine wraps each async call in its own
    asyncio.run(); a cached AsyncClient bound to the first loop then raised
    "Event is bound to a different event loop".
    """
    import asyncio

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": [{"index": 0, "embedding": [1.0]}], "usage": {}},
        )

    client = _client(handler)
    counter = {"n": 0}

    def one_run() -> list[list[float]]:
        counter["n"] += 1
        return asyncio.run(client.embed([f"loop-{counter['n']}"]))

    # Two independent event loops, same client instance: must not raise.
    first = one_run()
    second = one_run()
    assert first == [[1.0]] and second == [[1.0]]
    assert client.usage().calls == 2


async def test_retry_on_500_then_success():
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 3:
            return httpx.Response(500, text="server error")
        return _chat_response("recovered")

    client = _client(handler)
    result = await client.chat([{"role": "user", "content": "retry me"}])

    assert attempts["n"] == 3
    assert result.content == "recovered"
    assert client.usage().calls == 1

    await client.aclose()


async def test_retry_on_429_exhausts_to_llmerror():
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(429, text="rate limited")

    # Rate limits get more attempts than transient 5xx (shared/free providers).
    client = _client(handler, rate_limit_attempts=3)
    with pytest.raises(LLMError) as excinfo:
        await client.chat([{"role": "user", "content": "x"}])

    assert attempts["n"] == 3
    assert excinfo.value.status == 429

    await client.aclose()


async def test_429_retries_more_than_5xx_and_honours_retry_after():
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 5:
            return httpx.Response(429, headers={"Retry-After": "1"}, text="slow down")
        return _chat_response("finally")

    client = _client(handler)  # default rate_limit_attempts (8)
    result = await client.chat([{"role": "user", "content": "x"}])
    assert result.content == "finally"
    assert attempts["n"] == 5
    await client.aclose()


async def test_5xx_exhausts_at_max_attempts_not_rate_limit_budget():
    """B9: a 5xx uses the small ``max_attempts`` budget (3), never the 429 one."""
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(503, text="unavailable")

    client = _client(handler)  # max_attempts defaults to 3, rate_limit_attempts to 8
    with pytest.raises(LLMError) as excinfo:
        await client.chat([{"role": "user", "content": "x"}])

    assert attempts["n"] == 3
    assert excinfo.value.status == 503
    await client.aclose()


async def test_429_and_5xx_use_their_own_budgets():
    """B9: with max_attempts=2 / rate_limit_attempts=5, 5xx stops at 2, 429 at 5."""
    calls = {"n": 0}

    def fivexx(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500, text="boom")

    client = _client(fivexx, max_attempts=2, rate_limit_attempts=5)
    with pytest.raises(LLMError):
        await client.chat([{"role": "user", "content": "x"}])
    assert calls["n"] == 2
    await client.aclose()

    calls["n"] = 0

    def rate(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429, text="slow")

    client = _client(rate, max_attempts=2, rate_limit_attempts=5)
    with pytest.raises(LLMError):
        await client.chat([{"role": "user", "content": "x"}])
    assert calls["n"] == 5
    await client.aclose()


async def test_fetch_returns_status_and_bytes():
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, content=b"<html>ok</html>")

    client = _client(handler)
    status, payload = await client.fetch(
        "https://example.com/page", method="POST", body={"q": "x"}
    )

    assert status == 200
    assert payload == b"<html>ok</html>"
    assert seen["url"] == "https://example.com/page"
    assert seen["body"] == {"q": "x"}

    await client.aclose()


async def test_fetch_returns_4xx_without_raising():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, content=b"nope")

    client = _client(handler)
    status, payload = await client.fetch("https://example.com/missing")

    assert status == 404
    assert payload == b"nope"

    await client.aclose()


async def test_missing_api_key_raises_llmerror():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("network must not be reached without an API key")

    client = _client(handler, llm_api_key=None, llm_api_key_file=None)

    with pytest.raises(LLMError):
        await client.chat([{"role": "user", "content": "x"}])

    with pytest.raises(LLMError):
        await client.embed(["x"])

    # B2 regression: the empty-input shortcut must not bypass the auth check.
    with pytest.raises(LLMError):
        await client.embed([])

    await client.aclose()


async def test_cache_key_separates_plain_and_json_schema_calls():
    """B1 regression: identical messages, plain vs. json_schema must not share a cache entry."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        body = json.loads(request.content)
        if body.get("response_format", {}).get("type") == "json_schema":
            return _chat_response(json.dumps({"name": "Ada"}))
        return _chat_response("plain-answer")

    client = _client(handler)
    messages = [{"role": "user", "content": "same question"}]
    schema = {
        "name": "person",
        "schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
        },
    }

    plain = await client.chat(messages)
    structured = await client.chat(messages, json_schema=schema)

    assert calls["n"] == 2  # two distinct network calls, no cross-contamination
    assert plain.content == "plain-answer"
    assert isinstance(structured.content, dict)
    assert structured.content == {"name": "Ada"}
    assert structured.cached is False

    # Each variant is then served from its own cache entry, still without calls.
    plain_again = await client.chat(messages)
    structured_again = await client.chat(messages, json_schema=schema)
    assert calls["n"] == 2
    assert plain_again.cached is True
    assert plain_again.content == "plain-answer"
    assert structured_again.cached is True
    assert isinstance(structured_again.content, dict)
    assert structured_again.content == {"name": "Ada"}

    usage = client.usage()
    assert usage.calls == 2
    assert usage.cache_hits == 2

    await client.aclose()


async def test_cache_key_separates_temperature_and_max_tokens():
    """B1 regression: temperature/max_tokens variants get distinct cache entries."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        body = json.loads(request.content)
        return _chat_response(f"t={body['temperature']},m={body.get('max_tokens')}")

    client = _client(handler)
    messages = [{"role": "user", "content": "variants"}]

    base = await client.chat(messages, temperature=0.0, max_tokens=10)
    hotter = await client.chat(messages, temperature=0.7, max_tokens=10)
    longer = await client.chat(messages, temperature=0.0, max_tokens=20)

    assert calls["n"] == 3
    assert base.content == "t=0.0,m=10"
    assert hotter.content == "t=0.7,m=10"
    assert longer.content == "t=0.0,m=20"
    assert base.cached is False
    assert hotter.cached is False
    assert longer.cached is False

    # The exact repeat of one variant is a cache hit, the others stay untouched.
    base_again = await client.chat(messages, temperature=0.0, max_tokens=10)
    assert calls["n"] == 3
    assert base_again.cached is True
    assert base_again.content == "t=0.0,m=10"
    assert client.usage().cache_hits == 1

    await client.aclose()


async def test_no_retry_on_4xx_error():
    """B3a: 4xx responses fail fast — exactly one attempt, no retry loop."""
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(400, text="bad request")

    client = _client(handler)
    with pytest.raises(LLMError) as excinfo:
        await client.chat([{"role": "user", "content": "x"}])

    assert attempts["n"] == 1
    assert excinfo.value.status == 400

    await client.aclose()


async def test_default_timeout_is_60_seconds():
    """B3b: the client defaults to a 60 s per-request timeout."""
    assert DEFAULT_TIMEOUT == 60.0

    client = LLMClient(
        _settings(), transport=httpx.MockTransport(lambda request: httpx.Response(200))
    )
    assert client._client().timeout == httpx.Timeout(DEFAULT_TIMEOUT)

    await client.aclose()
