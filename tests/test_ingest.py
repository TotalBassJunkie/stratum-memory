"""Tests for the ECL ingestion pipeline + episode store (WP-07).

Nothing here touches the network or a live database:

* :class:`FakeLLM` is a deterministic stand-in for :class:`stratum.llm.LLMClient`
  (fixed-dimension embeddings, schema-bound JSON facts, metered usage);
* :class:`FakeStore` implements the KV + ``add_chunk`` surface, plus the
  optional ``has_content_hash`` hook, backed by plain dicts;
* :class:`FakeConnFactory` records every ``staging_candidates`` insert;
* the vault root is ``tmp_path`` via ``Settings(kb_path=...)``.
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from stratum.config import Settings
from stratum.ingest import (
    DEFERRED_FACTS_ONLY,
    DEFERRED_FULL,
    STATUS_DEFERRED,
    ExtractedSource,
    Extractor,
    IngestPipeline,
    SessionStore,
    UnsupportedSource,
    _sha256,  # noqa: PLC2701 - dedup helper asserted directly
)
from stratum.vector import chunk_text

# --------------------------------------------------------------------------- fakes


class _Usage:
    """Minimal ``Usage`` look-alike (only ``total_tokens`` is read by the pipeline)."""

    def __init__(self, total_tokens: int = 0) -> None:
        self.total_tokens = total_tokens

    def as_dict(self) -> dict[str, int]:
        return {"total_tokens": self.total_tokens}


class FakeLLM:
    """Deterministic, offline LLM/embedding/fetch double with token metering."""

    def __init__(
        self,
        *,
        dim: int = 1024,
        facts_per_chunk: int = 1,
        embed_cost: int = 10,
        chat_cost: int = 20,
        fetch_body: bytes | None = None,
        fetch_status: int = 200,
    ) -> None:
        self.dim = dim
        self.facts_per_chunk = facts_per_chunk
        self.embed_cost = embed_cost
        self.chat_cost = chat_cost
        self.fetch_body = fetch_body if fetch_body is not None else b""
        self.fetch_status = fetch_status
        self.embed_calls = 0
        self.chat_calls = 0
        self.fetch_calls = 0
        self.embedded_texts: list[str] = []
        self._total = 0

    def usage(self) -> _Usage:
        return _Usage(self._total)

    def _vector(self, text: str) -> list[float]:
        seed = sum(text.encode("utf-8")) % 97
        return [((index + seed) % 11) / 11.0 for index in range(self.dim)]

    async def embed(self, texts: Any, *, model: str | None = None) -> list[list[float]]:
        if isinstance(texts, str):
            inputs = [texts]
        else:
            inputs = list(texts)
        self.embed_calls += 1
        self.embedded_texts.extend(inputs)
        self._total += self.embed_cost * len(inputs)
        return [self._vector(text) for text in inputs]

    async def chat(self, messages: Any, **kwargs: Any) -> Any:
        self.chat_calls += 1
        self._total += self.chat_cost
        facts = [
            {
                "subject": "chunk",
                "predicate": f"asserts_{index}",
                "object": f"value_{index}",
                "object_text": f"value_{index}",
                "valid_from": "2024-01-01T00:00:00Z",
                "valid_to": None,
                "confidence": 0.9,
            }
            for index in range(self.facts_per_chunk)
        ]
        return type("ChatResult", (), {"content": {"facts": facts}})()

    async def fetch(self, url: str, **kwargs: Any) -> tuple[int, bytes]:
        self.fetch_calls += 1
        return self.fetch_status, self.fetch_body


class FakeStore:
    """In-memory KV + chunk store (mirrors the surface the pipeline uses)."""

    def __init__(self) -> None:
        self.kv: dict[tuple[str, str], dict[str, Any]] = {}
        self.chunks: list[dict[str, Any]] = []
        self._hashes: set[tuple[str, str]] = set()

    def has_content_hash(self, tenant: str, digest: str) -> bool:
        return (tenant, digest) in self._hashes

    def add_chunk(
        self,
        tenant: str,
        *,
        source_uri: str,
        ordinal: int,
        text: str,
        embedding: list[float],
        content_hash: str | None = None,
    ) -> dict[str, Any]:
        digest = content_hash or _sha256(text)
        if (tenant, digest) in self._hashes:
            return {"chunk_id": f"c{len(self.chunks)}", "skipped": True}
        self._hashes.add((tenant, digest))
        self.chunks.append(
            {
                "tenant": tenant,
                "source_uri": source_uri,
                "ordinal": ordinal,
                "text": text,
                "embedding": embedding,
                "content_hash": digest,
            }
        )
        return {"chunk_id": f"c{len(self.chunks) - 1}", "skipped": False}

    def kv_get(self, tenant: str, key: str) -> dict[str, Any] | None:
        value = self.kv.get((tenant, key))
        return None if value is None else dict(value)

    def kv_set(self, tenant: str, key: str, value: dict[str, Any], **_: Any) -> None:
        self.kv[(tenant, key)] = dict(value)


class _FakeCursor:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn
        self._rows: list[Any] = []

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def execute(self, sql: str, params: tuple = ()) -> None:
        normalized = re.sub(r"\s+", " ", sql).strip()
        shared = self._conn.shared
        if normalized.startswith("INSERT INTO staging_candidates"):
            tenant, kind, payload, source_uri, confidence, status, ingest_id = params
            candidate_id = f"cand-{len(shared)}"
            shared.append(
                {
                    "candidate_id": candidate_id,
                    "tenant": tenant,
                    "kind": kind,
                    "payload": payload.obj if hasattr(payload, "obj") else payload,
                    "source_uri": source_uri,
                    "confidence": confidence,
                    "status": status,
                    "ingest_id": ingest_id,
                }
            )
            self._conn.last = {"candidate_id": candidate_id}
        elif normalized.startswith("SELECT 1 FROM chunks"):
            self._conn.last = None
        elif normalized.startswith("SELECT candidate_id"):
            self._rows = [
                row for row in shared if self._matches_markers(row, normalized, params)
            ]
        elif normalized.startswith("UPDATE staging_candidates"):
            tenant, candidate_id = params
            for row in shared:
                if row["tenant"] == tenant and row["candidate_id"] == candidate_id:
                    row["status"] = "approved"
        else:  # pragma: no cover - guards against an unexpected statement shape
            raise AssertionError(f"unsupported SQL: {normalized}")

    @staticmethod
    def _matches_markers(row: dict[str, Any], normalized: str, params: tuple) -> bool:
        if row["tenant"] != params[0] or row["status"] != params[1]:
            return False
        index = 2
        if "source_uri = %s" in normalized:
            if row["source_uri"] != params[index]:
                return False
            index += 1
        if "ingest_id = %s" in normalized:
            if row["ingest_id"] != params[index]:
                return False
        return True

    def fetchone(self) -> Any:
        return self._conn.last

    def fetchall(self) -> list[Any]:
        return list(self._rows)


class _FakeConn:
    def __init__(self, shared: list[dict[str, Any]]) -> None:
        self.shared = shared
        self.last: Any = None
        self.commits = 0

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1


class FakeConnFactory:
    """Collects all ``staging_candidates`` rows written through it.

    One shared list backs every connection so a later connection (e.g. the
    ``resume_facts`` lookup) sees rows staged by an earlier one.
    """

    def __init__(self) -> None:
        self.candidates: list[dict[str, Any]] = []

    @contextmanager
    def __call__(self, tenant: str | None = None, **_kwargs: object):
        yield _FakeConn(self.candidates)


class FakeFs:
    """Records episode writes (no real filesystem / git)."""

    def __init__(self) -> None:
        self.writes: list[dict[str, Any]] = []

    def write(self, uri: str, content: str, *, tenant: str, metadata: Any = None, **_: Any) -> dict:
        self.writes.append({"uri": uri, "content": content, "tenant": tenant, "metadata": metadata})
        return {"uri": uri, "content": content}


# ----------------------------------------------------------------------- fixtures


def _long_doc() -> str:
    """Distinct, non-repeating long text so every chunk hashes differently."""
    return " ".join(f"wort{index:04d}" for index in range(2000))


def _two_chunk_doc() -> str:
    """~5400 chars: > one 3200-char window, <= 6000, i.e. exactly 2 chunks."""
    return " ".join(f"wort{index:04d}" for index in range(600))


def _pipeline(
    *,
    llm: FakeLLM,
    store: FakeStore,
    budget_settings: int = 20000,
    conn: FakeConnFactory | None = None,
    extractor: Extractor | None = None,
) -> tuple[IngestPipeline, FakeConnFactory]:
    settings = Settings(reflect_budget_tokens=budget_settings, db_password=None, db_password_file=None)
    conn = conn or FakeConnFactory()
    pipeline = IngestPipeline(
        settings,
        llm=llm,  # type: ignore[arg-type]
        store=store,
        fs=FakeFs(),
        graph=object(),
        conn_factory=conn,
        extractor=extractor,
    )
    return pipeline, conn


# ------------------------------------------------------------------------- Extract


async def test_extract_markdown_produces_chunks():
    extractor = Extractor()
    text = "# Titel\n\nEin Absatz über Stratum-Memory.\n"
    result = await extractor.extract(text, source_uri="stratum://docs/default/notes")

    assert isinstance(result, ExtractedSource)
    assert result.source_uri == "stratum://docs/default/notes"
    assert result.text == text.strip()
    assert result.chunks == chunk_text(text.strip())
    assert len(result.chunks) >= 1


async def test_extract_long_text_splits_into_multiple_chunks():
    extractor = Extractor()
    text = _long_doc()  # ~10k chars >> 3200 char target => multiple chunks
    result = await extractor.extract(text, source_uri="stratum://docs/default/long")
    assert len(result.chunks) > 1


async def test_extract_json_session_becomes_transcript():
    extractor = Extractor()
    session = {
        "messages": [
            {"role": "user", "content": "Wie läuft die Ingestion?"},
            {"role": "assistant", "content": "Extract, Cognify, Load."},
        ]
    }
    result = await extractor.extract(session, source_uri="stratum://episodes/default/2026/10/s1")

    assert "user: Wie läuft die Ingestion?" in result.text
    assert "assistant: Extract, Cognify, Load." in result.text
    assert result.chunks == chunk_text(result.text)


async def test_extract_json_session_string_is_detected():
    extractor = Extractor()
    raw = '{"messages": [{"role": "user", "content": "hallo"}]}'
    assert extractor.detect_source_type(raw) == "json"
    result = await extractor.extract(raw, source_uri="stratum://episodes/default/2026/10/s2")
    assert "user: hallo" in result.text


async def test_extract_url_uses_fetch_and_strips_html():
    llm = FakeLLM(fetch_body=b"<html><body><h1>Hi</h1><p>Text &amp; mehr</p></body></html>")
    extractor = Extractor(llm=llm)  # type: ignore[arg-type]
    result = await extractor.extract("https://example.com/doc", source_uri="stratum://docs/default/web")

    assert llm.fetch_calls == 1
    assert "<h1>" not in result.text
    assert "Hi" in result.text
    assert "Text & mehr" in result.text


async def test_extract_pdf_without_parser_raises_unsupported():
    extractor = Extractor()
    with pytest.raises(UnsupportedSource):
        await extractor.extract(b"%PDF-1.7 ...", source_uri="stratum://docs/default/report.pdf")


async def test_extract_pdf_with_parser_adapter():
    extractor = Extractor(parser=lambda data: f"PDF bytes={len(data)}")
    result = await extractor.extract(
        b"%PDF-1.7 body", source_uri="stratum://docs/default/report", filename="report.pdf"
    )
    assert result.text == "PDF bytes=13"


async def test_extract_docx_detected_by_magic_bytes():
    extractor = Extractor()
    with pytest.raises(UnsupportedSource):
        await extractor.extract(b"PK\x03\x04docx", source_uri="stratum://docs/default/x")


# -------------------------------------------------------------------------- Ingest


async def test_ingest_two_chunks_embeds_each_and_stages_facts():
    llm = FakeLLM(facts_per_chunk=2)
    store = FakeStore()
    pipeline, conn = _pipeline(llm=llm, store=store)
    text = _two_chunk_doc()  # exactly 2 chunks

    result = await pipeline.ingest(
        text, tenant="default", source_uri="stratum://docs/default/notes", kind="doc"
    )

    assert result.chunks_new == len(chunk_text(text.strip()))
    assert result.chunks_new == 2
    assert len(store.chunks) == result.chunks_new

    # one staged fact candidate per extracted fact
    assert result.facts_staged == result.chunks_new * 2
    fact_rows = [row for row in conn.candidates if row["kind"] == "fact"]
    assert len(fact_rows) == result.facts_staged
    assert all(row["status"] == "pending" for row in conn.candidates)
    assert all(row["ingest_id"] == result.ingest_id for row in conn.candidates)
    assert result.truncated is False
    # one embed per chunk plus one fact-text embed per extracted fact.
    assert llm.embed_calls == result.chunks_new + result.facts_staged


async def test_ingest_fact_payload_matches_curator_contract():
    """F2: facts carry object_text + object (alias) + a fact embedding + keys."""
    llm = FakeLLM(facts_per_chunk=2)
    store = FakeStore()
    pipeline, conn = _pipeline(llm=llm, store=store)

    result = await pipeline.ingest(
        "ein kurzer dokumenttext",
        tenant="default",
        source_uri="stratum://docs/default/notes",
        kind="doc",
    )

    assert result.facts_staged == 2
    facts = [row for row in conn.candidates if row["kind"] == "fact"]
    assert facts, "expected staged fact candidates"
    for row in facts:
        payload = row["payload"]
        assert set(
            {
                "subject",
                "predicate",
                "object",
                "object_text",
                "valid_from",
                "valid_to",
                "confidence",
                "embedding",
                "subject_entity_id",
            }
        ) <= set(payload)
        # object is an exact alias of object_text.
        assert payload["object"] == payload["object_text"]
        assert isinstance(payload["embedding"], list)
        assert payload["embedding"]
        assert payload["subject_entity_id"] is None


async def test_extract_facts_embeds_the_fact_text():
    """The fact embedding is embed(subject + " " + predicate + " " + object)."""
    llm = FakeLLM(facts_per_chunk=1)
    store = FakeStore()
    pipeline, _ = _pipeline(llm=llm, store=store)

    await pipeline._extract_facts(  # noqa: SLF001 - asserting the embed input
        "chunk body", source_uri="stratum://docs/default/notes"
    )

    assert "chunk asserts_0 value_0" in llm.embedded_texts


async def test_ingest_chunk_dedup_second_run_writes_nothing():
    llm = FakeLLM()
    store = FakeStore()
    pipeline, conn = _pipeline(llm=llm, store=store)
    text = _long_doc()

    first = await pipeline.ingest(
        text, tenant="default", source_uri="stratum://docs/default/notes", kind="doc"
    )
    assert first.chunks_new > 0
    embeds_after_first = llm.embed_calls

    second = await pipeline.ingest(
        text, tenant="default", source_uri="stratum://docs/default/notes", kind="doc"
    )

    assert second.chunks_new == 0
    assert second.ingest_id != first.ingest_id  # new id per call
    assert llm.embed_calls == embeds_after_first  # no LLM call on duplicate chunks
    assert len(store.chunks) == first.chunks_new
    assert not [row for row in conn.candidates if row["ingest_id"] == second.ingest_id]


async def test_ingest_budget_truncates_and_defers_rest():
    # embed(100) + fact-embed(100) + chat(100): a 250-token budget leaves chunk 0
    # fully cognified (300 used after facts) and stops before chunk 1.
    llm = FakeLLM(facts_per_chunk=1, embed_cost=100, chat_cost=100)
    store = FakeStore()
    pipeline, conn = _pipeline(llm=llm, store=store)
    text = _long_doc()

    result = await pipeline.ingest(
        text,
        tenant="default",
        source_uri="stratum://docs/default/notes",
        kind="doc",
        budget_tokens=250,
    )

    assert result.truncated is True
    assert result.chunks_new == 1  # only the first chunk was embedded
    assert result.facts_staged == 1
    # undeveloped chunks are recorded as ``deferred`` (not dropped, not pending).
    deferred = [row for row in conn.candidates if row["payload"].get("deferred") is True]
    assert len(deferred) >= 1
    assert all(row["status"] == STATUS_DEFERRED for row in deferred)
    # nothing deferred is left in the Curator's promote queue.
    assert all(row["status"] == "pending" for row in conn.candidates if row["kind"] == "fact"
               and row["payload"].get("deferred") is not True)


async def test_ingest_budget_after_embed_marks_current_chunk_facts_only():
    """F1: the chunk embedded just before the budget cut is NOT lost."""
    # embed(100) alone exhausts the 100-token budget: chunk 0 is embedded, then
    # the budget guard fires *before* its fact extraction.
    llm = FakeLLM(facts_per_chunk=1, embed_cost=100, chat_cost=100)
    store = FakeStore()
    pipeline, conn = _pipeline(llm=llm, store=store)
    text = _long_doc()

    result = await pipeline.ingest(
        text,
        tenant="default",
        source_uri="stratum://docs/default/notes",
        kind="doc",
        budget_tokens=100,
    )

    assert result.truncated is True
    assert result.chunks_new == 1
    assert result.facts_staged == 0  # chunk 0's fact pass was cut off

    markers = [row for row in conn.candidates if row["payload"].get("deferred") is True]
    assert markers
    # the embedded current chunk is a facts-only marker carrying its vector...
    facts_only = [r for r in markers if r["payload"]["deferred_stage"] == DEFERRED_FACTS_ONLY]
    assert len(facts_only) == 1
    assert facts_only[0]["payload"]["embedding"]
    assert facts_only[0]["payload"]["chunk_ordinal"] == facts_only[0]["payload"]["ordinal"]
    assert facts_only[0]["status"] == STATUS_DEFERRED
    # ...and every following chunk is deferred whole (no vector yet).
    full = [r for r in markers if r["payload"]["deferred_stage"] == DEFERRED_FULL]
    assert full
    assert all(r["payload"].get("embedding") is None for r in full)


async def test_resume_facts_recovers_deferred_facts_only_marker():
    """F1/F3: resume_facts re-cognifies a facts-only marker (no re-embedding)."""
    llm = FakeLLM(facts_per_chunk=2, embed_cost=100, chat_cost=100)
    store = FakeStore()
    pipeline, conn = _pipeline(llm=llm, store=store)
    text = _long_doc()

    first = await pipeline.ingest(
        text,
        tenant="default",
        source_uri="stratum://docs/default/notes",
        kind="doc",
        budget_tokens=100,  # chunk 0 embed only: its facts are cut off
    )
    assert first.facts_staged == 0
    embeds_after_ingest = llm.embed_calls
    chunks_after_ingest = len(store.chunks)

    result = await pipeline.resume_facts(
        "default",
        source_uri="stratum://docs/default/notes",
        ingest_id=first.ingest_id,
    )

    # chunk 0 (facts_only) plus every whole-deferred chunk are all recovered.
    assert result.resumed >= 2
    assert result.still_deferred == 0
    assert result.facts_staged == result.resumed * 2  # 2 facts per chunk
    # the recovered facts are promotable (pending) and match the WP-08 contract.
    recovered = [
        row for row in conn.candidates if row["kind"] == "fact" and row["status"] == "pending"
    ]
    assert len(recovered) == result.facts_staged
    assert all(row["payload"]["object_text"] == row["payload"]["object"] for row in recovered)
    assert all(row["payload"]["embedding"] for row in recovered)
    # no resume marker is left behind, and none sits in the promote queue.
    assert not [row for row in conn.candidates if row["status"] == STATUS_DEFERRED]
    # the whole-deferred chunks were embedded now (chunk 0 was already embedded).
    assert len(store.chunks) > chunks_after_ingest
    assert llm.embed_calls > embeds_after_ingest
    # a second resume run finds nothing to do.
    again = await pipeline.resume_facts("default", source_uri="stratum://docs/default/notes")
    assert again.resumed == 0
    assert again.facts_staged == 0
    assert again.still_deferred == 0


async def test_ingest_no_fact_extraction_for_other_kinds():
    llm = FakeLLM(facts_per_chunk=3)
    store = FakeStore()
    pipeline, conn = _pipeline(llm=llm, store=store)

    result = await pipeline.ingest(
        "kurzer text",
        tenant="default",
        source_uri="stratum://docs/default/note",
        kind="entity",
    )

    assert result.facts_staged == 0
    assert llm.chat_calls == 0
    assert conn.candidates == []


# --------------------------------------------------------------------- SessionStore


def test_session_store_append_and_read_roundtrip(tmp_path: Path):
    settings = Settings(kb_path=str(tmp_path), db_password=None, db_password_file=None)
    store = FakeStore()
    fs = FakeFs()
    session = SessionStore(settings, store=store, fs=fs)

    first = session.append("default", "sess-1", "user", "Hallo", metadata={"lang": "de"})
    second = session.append("default", "sess-1", "assistant", "Hi!")

    assert first["turn"]["index"] == 0
    assert second["turn"]["index"] == 1
    assert first["uri"].startswith("stratum://episodes/default/")
    assert first["uri"].endswith("/sess-1")
    assert first["turn"]["metadata"] == {"lang": "de"}

    turns = session.read("default", "sess-1")
    assert [turn["role"] for turn in turns] == ["user", "assistant"]
    assert turns[1]["content"] == "Hi!"

    # the episode document was (re)written once per append
    assert len(fs.writes) == 2
    assert fs.writes[-1]["uri"] == second["uri"]
    assert "assistant" in fs.writes[-1]["content"]


def test_session_store_read_unknown_session_is_empty():
    settings = Settings(db_password=None, db_password_file=None)
    session = SessionStore(settings, store=FakeStore(), fs=FakeFs())
    assert session.read("default", "missing") == []


def test_session_store_episode_uri_is_fixed_once_per_session(tmp_path: Path):
    """F4: a session spanning a month boundary keeps one stable episode URI."""
    settings = Settings(kb_path=str(tmp_path), db_password=None, db_password_file=None)
    store = FakeStore()
    fs = FakeFs()
    session = SessionStore(settings, store=store, fs=fs)

    session.append("default", "sess-2", "user", "Tag eins")
    first_uri = fs.writes[0]["uri"]
    assert "/2026/10/sess-2" in first_uri  # today: 2026-10-11

    # the month rolls over: the cached URI must win over the current month.
    monkeypatched = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    with mock.patch("stratum.ingest.datetime") as fake_datetime:
        fake_datetime.now.return_value = monkeypatched
        second = session.append("default", "sess-2", "assistant", "Tag zwei")

    assert second["uri"] == first_uri
    assert fs.writes[-1]["uri"] == first_uri  # no stale Nov file for the same session
    assert store.kv_get("default", "episode_uri:sess-2") == {"uri": first_uri}
    # a *different* session first appended in November correctly lands in Nov.
    assert session.episode_uri("default", "sess-3", now=monkeypatched).count("/2026/11/") == 1


# ------------------------------------------------------------------------ F5 media
async def test_extract_accepts_media_type_and_filename():
    extractor = Extractor()
    # A bytes source with an explicit media_type resolves to markdown/text.
    result = await extractor.extract(
        b"# Titel\n\nInhalt",
        source_uri="stratum://docs/default/notes",
        media_type="text/markdown",
        filename="notes.md",
    )
    assert "# Titel" in result.text


async def test_ingest_forwards_media_type_and_filename():
    """F5: ingest() passes media_type/filename through to extract()."""
    extractor = Extractor(parser=lambda data: f"PDF bytes={len(data)}")
    llm = FakeLLM()
    store = FakeStore()
    pipeline, _ = _pipeline(llm=llm, store=store, extractor=extractor)

    result = await pipeline.ingest(
        b"%PDF-1.7 body",
        tenant="default",
        source_uri="stratum://docs/default/report",
        kind="doc",
        media_type="application/pdf",
        filename="report.pdf",
    )

    assert result.chunks_new >= 1
    assert "PDF bytes=13" in store.chunks[0]["text"]
