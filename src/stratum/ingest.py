"""ECL ingestion pipeline + episode/session store (WP-07).

Implements the Extract-Cognify-Load pipeline of ``MASTER-PLAN.md`` §7 and the
session episode store:

* **Extract** — :class:`Extractor` detects the source type from MIME/extension
  and normalises the source into text, then splits it into chunks with
  :func:`stratum.vector.chunk_text`.
* **Cognify** — :class:`IngestPipeline` embeds every *new* chunk (level-1
  ``content_hash`` dedup means duplicate chunks are skipped **without** an LLM
  call) and, for ``kind`` ``fact``/``doc``, extracts bi-temporal facts via a
  schema-constrained chat completion. A per-run ``budget_tokens`` cap stops the
  run early; the remainder is recorded for a later run and the result carries
  ``truncated=True``.
* **Load** — promotable candidates are written to ``staging_candidates`` with
  ``status='pending'``. Chunks a budget cut short additionally get a
  ``status='deferred'`` resume marker (see :meth:`IngestPipeline.resume_facts`),
  so no fact is lost and the Curator never promotes a marker by accident. The
  pipeline never promotes anything directly; promotion is the Curator's job
  (WP-08).

Design rules:

* Every database access goes through the injected ``conn_factory`` whose
  signature mirrors :func:`stratum.db.connection`; the default is the real,
  tenant-scoped RLS connection. Tests inject an in-memory fake, so the module is
  exercisable without Postgres.
* The LLM client is injected so the pipeline can be driven by a deterministic
  fake in tests; no network access happens unless a real client is passed.
* Both ``llm.chat``/``llm.embed``/``llm.fetch`` are coroutines, hence
  :meth:`Extractor.extract` and :meth:`IngestPipeline.ingest` are ``async``.
"""

from __future__ import annotations

import hashlib
import html as _html
import json
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from psycopg.types.json import Jsonb

from .config import Settings, get_settings
from .db import connection as _default_connection
from .llm import LLMClient
from .uri import build_uri
from .vector import chunk_text

__all__ = [
    "DEFERRED_FACTS_ONLY",
    "DEFERRED_FULL",
    "DOCX",
    "JSON_SESSION",
    "MARKDOWN",
    "PDF",
    "STATUS_DEFERRED",
    "TEXT",
    "URL",
    "ExtractedSource",
    "Extractor",
    "IngestPipeline",
    "IngestResult",
    "ResumeFactsResult",
    "SessionStore",
    "UnsupportedSource",
]

# --------------------------------------------------------------------- source kinds

MARKDOWN = "markdown"
TEXT = "text"
JSON_SESSION = "json"
URL = "url"
PDF = "pdf"
DOCX = "docx"

#: Media type (lower-case, parameters stripped) -> source kind.
_MIME_MAP: dict[str, str] = {
    "text/markdown": MARKDOWN,
    "text/x-markdown": MARKDOWN,
    "text/plain": TEXT,
    "text/html": TEXT,
    "application/json": JSON_SESSION,
    "application/pdf": PDF,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": DOCX,
}

#: File extension (lower-case, with dot) -> source kind.
_EXT_MAP: dict[str, str] = {
    ".md": MARKDOWN,
    ".markdown": MARKDOWN,
    ".txt": TEXT,
    ".text": TEXT,
    ".html": TEXT,
    ".htm": TEXT,
    ".json": JSON_SESSION,
    ".pdf": PDF,
    ".docx": DOCX,
}

#: Kinds for which bi-temporal fact extraction runs during Cognify.
_FACT_KINDS = frozenset({"fact", "doc", "dok"})

#: ``staging_candidates.status`` for a resume marker: not promotable until a
#: later :meth:`IngestPipeline.resume_facts` run re-cognifies it (so the Curator,
#: which only promotes ``pending`` candidates, never picks a marker up).
STATUS_DEFERRED = "deferred"

#: ``payload['deferred_stage']`` — the chunk was embedded, but its fact
#: extraction is still outstanding. :meth:`IngestPipeline.resume_facts` runs the
#: fact pass only; it must not re-embed (the chunk already lives in ``chunks``).
DEFERRED_FACTS_ONLY = "facts_only"

#: ``payload['deferred_stage']`` — the chunk was deferred whole (embedding +
#: fact extraction). A full re-``ingest`` of the same source re-embeds it, then
#: extracts its facts.
DEFERRED_FULL = "full"

_SCRIPT_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t]+")

_INSERT_CANDIDATE_SQL = (
    "INSERT INTO staging_candidates "
    "(tenant_id, kind, payload, source_uri, confidence, status, ingest_id) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s) "
    "RETURNING candidate_id"
)

_FACT_SCHEMA: dict[str, Any] = {
    "name": "fact_extraction",
    "schema": {
        "type": "object",
        "properties": {
            "facts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "subject": {"type": "string"},
                        "predicate": {"type": "string"},
                        "object": {"type": "string"},
                        "object_text": {"type": ["string", "null"]},
                        "valid_from": {"type": ["string", "null"]},
                        "valid_to": {"type": ["string", "null"]},
                        "confidence": {"type": "number"},
                    },
                    "required": ["subject", "predicate", "object"],
                },
            }
        },
        "required": ["facts"],
    },
}

_FACT_SYSTEM_PROMPT = (
    "You extract durable, bi-temporal facts from one text chunk. "
    "Return ONLY JSON of the shape {\"facts\": [ ... ]}. Each fact has the keys "
    "subject, predicate, object, object_text, valid_from, valid_to and confidence. "
    "``object`` and ``object_text`` carry the same literal object value. "
    "Use ISO-8601 for valid_from/valid_to (or null when unknown) and a "
    "confidence in [0, 1]. Never invent facts that are not supported by the text."
)


class UnsupportedSource(ValueError):
    """Raised when a source cannot be normalised to text.

    Typical causes: a PDF/DOCX source without a ``parser`` adapter, an
    unparseable JSON session, or a URL fetch without an LLM client.
    """


def _sha256(text: str) -> str:
    """Return the hex SHA-256 digest of ``text`` (UTF-8) for level-1 dedup."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _normalise(text: str) -> str:
    """Normalise line endings and trim the outer whitespace of ``text``."""
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def _row_get(row: Any, key: str, index: int) -> Any:
    """Read a column from a row that is either a mapping or a positional sequence."""
    if isinstance(row, Mapping):
        return row.get(key)
    return row[index]


def _row_payload(row: Any) -> dict[str, Any]:
    """Return a candidate row's ``payload`` as a plain dict.

    Accepts a mapping row (``dict_row``), a positional row, or a payload stored
    as a JSON string; anything unexpected yields ``{}``.
    """
    raw = row.get("payload") if isinstance(row, Mapping) else row[3]
    if isinstance(raw, Mapping):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {}


def _strip_html(html_text: str) -> str:
    """Roughly strip HTML tags/scripts and unescape entities.

    This is a lightweight normaliser, not a parser: ``<script>``/``<style>``
    blocks are dropped, block-level tags become newlines, remaining tags are
    removed, and HTML entities are unescaped.
    """
    text = _SCRIPT_RE.sub(" ", html_text)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</(?:p|div|h[1-6]|li|tr|section|article)\s*>", "\n", text, flags=re.IGNORECASE)
    text = _TAG_RE.sub("", text)
    text = _html.unescape(text)
    text = _WS_RE.sub(" ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return _normalise(text)


def _content_to_text(content: Any) -> str:
    """Flatten a chat message ``content`` (string or multimodal parts) to text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, Mapping):
                parts.append(str(part.get("text", "")))
            else:
                parts.append(str(part))
        return " ".join(p for p in parts if p)
    return str(content)


def _session_to_text(messages: Sequence[Any]) -> str:
    """Render a ``{messages:[{role,content}]}`` session as a plain transcript."""
    lines: list[str] = []
    for message in messages:
        if not isinstance(message, Mapping):
            continue
        role = str(message.get("role", "unknown"))
        content = _content_to_text(message.get("content"))
        lines.append(f"{role}: {content}")
    return "\n".join(lines)


def _looks_like_session_json(text: str) -> bool:
    """Return ``True`` when ``text`` parses as a ``{"messages": [...]}`` object."""
    stripped = text.lstrip()
    if not stripped.startswith("{"):
        return False
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return False
    return isinstance(data, Mapping) and isinstance(data.get("messages"), list)


@dataclass(frozen=True)
class ExtractedSource:
    """Result of extracting a source: normalised text plus its chunks.

    Attributes:
        text: The normalised, LLM-ready text of the whole source.
        source_uri: The ``stratum://`` URI the source is attributed to.
        chunks: Overlapping chunks of ``text`` (see
            :func:`stratum.vector.chunk_text`).
    """

    text: str
    source_uri: str
    chunks: list[str] = field(default_factory=list)


class Extractor:
    """Detect a source type and normalise it into text + chunks.

    Args:
        llm: Optional :class:`stratum.llm.LLMClient`; required for URL sources
            (``llm.fetch`` is the only permitted outbound HTTP path).
        parser: Optional PDF/DOCX adapter ``bytes -> str``. When absent, a
            PDF/DOCX source raises :class:`UnsupportedSource` instead of
            silently producing garbage.
    """

    def __init__(
        self,
        *,
        llm: LLMClient | None = None,
        parser: Callable[[bytes], str] | None = None,
    ) -> None:
        self._llm = llm
        self._parser = parser

    # ------------------------------------------------------------- detection

    def detect_source_type(
        self,
        source: Any,
        *,
        media_type: str | None = None,
        filename: str | None = None,
    ) -> str:
        """Classify ``source`` as one of the supported source kinds.

        Resolution order: explicit ``media_type``, then an explicit ``filename``
        (or the name of a :class:`pathlib.Path` source), then the shape of the
        source itself (URL string, session JSON, PDF/DOCX magic bytes).
        """
        if media_type:
            mime = media_type.split(";")[0].strip().lower()
            if mime in _MIME_MAP:
                return _MIME_MAP[mime]

        name = filename
        if name is None and isinstance(source, Path):
            name = source.name
        if name is not None:
            ext = Path(name).suffix.lower()
            if ext in _EXT_MAP:
                return _EXT_MAP[ext]

        if isinstance(source, Mapping):
            return JSON_SESSION
        if isinstance(source, (bytes, bytearray)):
            head = bytes(source[:4])
            if head.startswith(b"%PDF"):
                return PDF
            if head.startswith(b"PK\x03\x04"):
                return DOCX
            return TEXT
        if isinstance(source, str):
            if source.lstrip().startswith(("http://", "https://")):
                return URL
            if _looks_like_session_json(source):
                return JSON_SESSION
            return MARKDOWN
        return TEXT

    # ------------------------------------------------------------- extraction

    async def extract(
        self,
        source: Any,
        *,
        source_uri: str,
        media_type: str | None = None,
        filename: str | None = None,
    ) -> ExtractedSource:
        """Normalise ``source`` into text and split it into chunks.

        Args:
            source: A ``str`` (Markdown/text, session JSON or URL), ``bytes``
                (PDF/DOCX/etc.), a :class:`pathlib.Path`, or a mapping holding a
                ``messages`` list.
            source_uri: Attribution URI recorded on the extracted source.
            media_type: Optional MIME type (overrides shape detection).
            filename: Optional file name whose extension is used for detection.

        Returns:
            An :class:`ExtractedSource`. ``chunks`` is empty for empty input.

        Raises:
            UnsupportedSource: for a source that cannot be turned into text
                (PDF/DOCX without a parser, unparseable session JSON, or a URL
                without an LLM client / with a failing fetch).
        """
        kind = self.detect_source_type(source, media_type=media_type, filename=filename)
        text = await self._load_text(
            source, kind, source_uri=source_uri, media_type=media_type, filename=filename
        )
        text = _normalise(text)
        return ExtractedSource(text=text, source_uri=source_uri, chunks=chunk_text(text))

    async def _load_text(
        self,
        source: Any,
        kind: str,
        *,
        source_uri: str,
        media_type: str | None,
        filename: str | None,
    ) -> str:
        if kind == URL:
            return await self._load_url(source)
        if kind == JSON_SESSION:
            return self._load_session(source)
        if kind in (PDF, DOCX):
            return self._load_binary(source, kind, filename=filename)
        return self._load_plain(source)

    def _read_bytes(self, source: Any) -> bytes:
        """Coerce a byte-oriented source (``bytes``/``Path``) to raw bytes."""
        if isinstance(source, (bytes, bytearray)):
            return bytes(source)
        if isinstance(source, Path):
            return source.read_bytes()
        raise UnsupportedSource(
            "PDF/DOCX sources require bytes or a path, "
            f"got {type(source).__name__}"
        )

    def _load_plain(self, source: Any) -> str:
        if isinstance(source, Path):
            return source.read_text(encoding="utf-8")
        if isinstance(source, (bytes, bytearray)):
            return bytes(source).decode("utf-8", errors="replace")
        if isinstance(source, str):
            return source
        raise UnsupportedSource(f"cannot read plain text from {type(source).__name__}")

    def _load_session(self, source: Any) -> str:
        if isinstance(source, Mapping):
            data: Any = source
        elif isinstance(source, Path):
            data = json.loads(source.read_text(encoding="utf-8"))
        elif isinstance(source, (bytes, bytearray)):
            data = json.loads(bytes(source).decode("utf-8", errors="replace"))
        elif isinstance(source, str):
            try:
                data = json.loads(source)
            except (ValueError, TypeError) as exc:
                raise UnsupportedSource(f"invalid JSON session: {exc}") from exc
        else:  # pragma: no cover - defensive
            raise UnsupportedSource(f"cannot read session from {type(source).__name__}")

        if not isinstance(data, Mapping) or not isinstance(data.get("messages"), list):
            raise UnsupportedSource(
                "JSON session must be an object with a 'messages' list of "
                "{role, content} objects"
            )
        return _session_to_text(data["messages"])

    async def _load_url(self, source: Any) -> str:
        if self._llm is None:
            raise UnsupportedSource("URL sources require an LLMClient (llm.fetch)")
        if not isinstance(source, str):
            raise UnsupportedSource(f"URL source must be a string, got {type(source).__name__}")
        status, body = await self._llm.fetch(source)
        if status >= 400:
            raise UnsupportedSource(f"failed to fetch {source!r}: HTTP {status}")
        text = body.decode("utf-8", errors="replace")
        if "<" in text and ">" in text:
            return _strip_html(text)
        return text

    def _load_binary(self, source: Any, kind: str, *, filename: str | None) -> str:
        if self._parser is None:
            label = filename or kind.upper()
            raise UnsupportedSource(
                f"{label} requires a parser adapter "
                "(Extractor(parser=Callable[[bytes], str]))"
            )
        return self._parser(self._read_bytes(source))


@dataclass(frozen=True)
class IngestResult:
    """Outcome of one :meth:`IngestPipeline.ingest` run.

    Attributes:
        ingest_id: UUID minted for this run (idempotency / provenance key).
        chunks_new: Number of chunks newly written (duplicates excluded).
        facts_staged: Number of fact candidates written to staging.
        truncated: ``True`` when the token budget stopped the run early.
        tokens_used: Total LLM tokens consumed during this run.
    """

    ingest_id: str
    chunks_new: int
    facts_staged: int
    truncated: bool
    tokens_used: int
    candidate_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ResumeFactsResult:
    """Outcome of one :meth:`IngestPipeline.resume_facts` run.

    Attributes:
        resumed: Number of facts-only-deferred markers picked up (chunks whose
            fact extraction this run re-attempted).
        facts_staged: Number of fact candidates written by this run.
        still_deferred: Number of markers that stayed ``deferred`` because the
            token budget ran out again.
        tokens_used: Total LLM tokens consumed during this run.
    """

    resumed: int
    facts_staged: int
    still_deferred: int
    tokens_used: int


class IngestPipeline:
    """Extract-Cognify-Load pipeline writing candidates to ``staging_candidates``.

    Args:
        settings: Configuration source; defaults to :func:`get_settings`. The
            default per-run budget is ``settings.reflect_budget_tokens``.
        llm: LLM client used for embeddings and fact extraction (injected).
        store: Vector/KV store (``MemoryStore`` or a compatible fake) providing
            ``add_chunk`` and, optionally, ``has_content_hash``.
        fs: Filesystem store. Reserved for the Curator hand-off / doc landing
            (WP-08); the pipeline's Load stage only writes staging candidates.
        graph: Bi-temporal graph. Reserved for the entity-resolution and
            promotion stages (WP-08); not written here.
        conn_factory: Callable with the signature of :func:`stratum.db.connection`
            used for the dedup lookup and the staging insert. Defaults to the
            real tenant-scoped connection.
        parser: Optional PDF/DOCX adapter forwarded to the internal
            :class:`Extractor`.
        extractor: Optional pre-built :class:`Extractor` (overrides ``parser``).
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        llm: LLMClient,
        store: Any,
        fs: Any,
        graph: Any,
        conn_factory: Callable[..., AbstractContextManager[Any]] | None = None,
        parser: Callable[[bytes], str] | None = None,
        extractor: Extractor | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._llm = llm
        self._store = store
        self._fs = fs
        self._graph = graph
        self._connect: Callable[..., AbstractContextManager[Any]] = (
            conn_factory or _default_connection
        )
        self._extractor = extractor or Extractor(llm=llm, parser=parser)

    # ------------------------------------------------------------------ ingest

    async def ingest(
        self,
        source: Any,
        *,
        tenant: str,
        source_uri: str,
        kind: str = "doc",
        budget_tokens: int | None = None,
        media_type: str | None = None,
        filename: str | None = None,
        skip_facts: bool = False,
    ) -> IngestResult:
        """Run Extract -> Cognify -> Load for one source.

        Args:
            source: Anything :meth:`Extractor.extract` accepts.
            tenant: Tenant id (RLS scope) for every DB access.
            source_uri: Attribution URI for chunks and candidates.
            kind: Candidate kind (``fact``/``doc``/``entity``/... ). Fact
                extraction runs only for ``fact``/``doc``.
            budget_tokens: Hard per-run token ceiling; defaults to
                ``settings.reflect_budget_tokens``. When exceeded, the remainder
                is recorded as ``deferred`` resume markers and ``truncated`` is
                set.
            media_type: Optional MIME type forwarded to
                :meth:`Extractor.extract` (overrides shape detection).
            filename: Optional file name forwarded to
                :meth:`Extractor.extract`; its extension aids source detection.
            skip_facts: When ``True`` the Cognify fact-extraction step is skipped
                entirely: only chunks + embeddings are written (no chat LLM
                calls). Used for a fast, cheap, fully searchable bulk import.

        Returns:
            An :class:`IngestResult`.
        """
        ingest_id = str(uuid.uuid4())
        budget = self.settings.reflect_budget_tokens if budget_tokens is None else budget_tokens
        start_tokens = self._llm.usage().total_tokens
        if skip_facts:
            kind = "chunk"

        extracted = await self._extractor.extract(
            source, source_uri=source_uri, media_type=media_type, filename=filename
        )
        chunks = extracted.chunks

        # Extract -> level-1 dedup happens inside the loop (see below): a chunk
        # whose content_hash already exists — from a previous run *or from an
        # earlier chunk of this same run* — is skipped with no LLM call at all.
        pending: list[tuple[int, str, str]] = [
            (ordinal, chunk, _sha256(chunk)) for ordinal, chunk in enumerate(chunks)
        ]

        chunks_new = 0
        facts_staged = 0
        truncated = False
        candidate_ids: list[str] = []
        # (ordinal, chunk, digest, embedding, facts_only) — see the Load stage.
        deferred: list[tuple[int, str, str, list[float] | None, bool]] = []

        for position, (ordinal, chunk, digest) in enumerate(pending):
            # Level-1 dedup: duplicate chunk (this run or a prior one) -> no work.
            if self._hash_present(tenant, digest):
                continue

            used = self._llm.usage().total_tokens - start_tokens
            if used >= budget:
                truncated = True
                deferred = [(o, c, d, None, False) for o, c, d in pending[position:]]
                break

            # --- Cognify: embedding per new chunk
            vectors = await self._llm.embed([chunk])
            if not vectors:
                raise UnsupportedSource("embedding backend returned no vector")
            embedding = vectors[0]
            self._store.add_chunk(
                tenant,
                source_uri=source_uri,
                ordinal=ordinal,
                text=chunk,
                embedding=embedding,
                content_hash=digest,
            )
            chunks_new += 1

            used = self._llm.usage().total_tokens - start_tokens
            if used >= budget:
                # Budget spent on the embedding: THIS chunk is already embedded,
                # so only its fact extraction is outstanding (``facts_only``) —
                # a plain re-ingest would skip it via the chunk dedup and lose
                # its facts forever. Every following chunk is deferred whole.
                truncated = True
                current = (ordinal, chunk, digest, embedding, True)
                rest = [(o, c, d, None, False) for o, c, d in pending[position + 1 :]]
                deferred = [current, *rest]
                break

            # --- Cognify: bi-temporal fact extraction (LLM-bound)
            if kind in _FACT_KINDS:
                facts = await self._extract_facts(chunk, source_uri=source_uri)
                for fact in facts:
                    candidate_ids.append(
                        self._stage_candidate(
                            tenant,
                            kind="fact",
                            payload=fact,
                            source_uri=source_uri,
                            confidence=float(fact.get("confidence", 1.0) or 0.0),
                            ingest_id=ingest_id,
                        )
                    )
                    facts_staged += 1

                used = self._llm.usage().total_tokens - start_tokens
                if used >= budget:
                    truncated = True
                    deferred = [
                        (o, c, d, None, False) for o, c, d in pending[position + 1 :]
                    ]
                    break

        # --- Load: record every deferred chunk as a ``deferred`` resume marker.
        # Only ``fact``/``doc`` runs produce facts, so only they need a resume
        # marker; other kinds are intentionally chunk-only and are not staged.
        if kind in _FACT_KINDS:
            for ordinal, chunk, digest, embedding, facts_only in deferred:
                stage = DEFERRED_FACTS_ONLY if facts_only else DEFERRED_FULL
                # The embedded vector is required to re-run just the fact pass
                # without another (heavy) embedding call; it is ``None`` for a
                # whole-deferred chunk.
                marker: dict[str, Any] = {
                    "deferred": True,
                    "deferred_stage": stage,
                    "chunk_ordinal": ordinal,
                    "ordinal": ordinal,
                    "text": chunk,
                    "content_hash": digest,
                    "embedding": list(embedding) if embedding else None,
                    "source_uri": source_uri,
                    "ingest_id": ingest_id,
                }
                self._stage_candidate(
                    tenant,
                    kind="fact",
                    payload=marker,
                    source_uri=source_uri,
                    confidence=0.0,
                    ingest_id=ingest_id,
                    status=STATUS_DEFERRED,
                )

        tokens_used = self._llm.usage().total_tokens - start_tokens
        return IngestResult(
            ingest_id=ingest_id,
            chunks_new=chunks_new,
            facts_staged=facts_staged,
            truncated=truncated,
            tokens_used=tokens_used,
            candidate_ids=candidate_ids,
        )

    # ----------------------------------------------------------- resume facts

    async def resume_facts(
        self,
        tenant: str,
        *,
        source_uri: str | None = None,
        ingest_id: str | None = None,
        budget_tokens: int | None = None,
    ) -> ResumeFactsResult:
        """Re-cognify ``status='deferred'`` markers — the pickup path for a cut run.

        A chunk deferred *after* its embedding was written (``deferred_stage ==
        'facts_only'``) can never be reached by a plain re-``ingest`` (level-1
        chunk dedup skips it), so this is its only recovery path: its fact
        extraction runs again with no re-embedding. A chunk deferred *whole*
        (``deferred_stage == 'full'``) is embedded now, its facts extracted, and
        its chunk written — completing what the truncated run left undone.

        Neither stage re-emits the source chunks into ``staging_candidates``; it
        only recovers the **facts**. Every marker consumed is marked
        ``approved`` (never ``pending``), so the Curator never validates a
        marker, and a later run does not process it again.

        Args:
            tenant: Tenant id (RLS scope).
            source_uri: When given, only markers for that source are resumed.
            ingest_id: When given, only markers from that ingest run are resumed.
            budget_tokens: Hard per-run token ceiling; defaults to
                ``settings.reflect_budget_tokens``. When exhausted, remaining
                markers stay ``deferred`` (counted in ``still_deferred``).

        Returns:
            A :class:`ResumeFactsResult`.
        """
        budget = self.settings.reflect_budget_tokens if budget_tokens is None else budget_tokens
        start_tokens = self._llm.usage().total_tokens
        markers = self._deferred_markers(tenant, source_uri=source_uri, ingest_id=ingest_id)

        resumed = facts_staged = still_deferred = 0
        seen_hashes: set[str] = set()
        for marker in markers:
            used = self._llm.usage().total_tokens - start_tokens
            if used >= budget:
                still_deferred += 1
                continue

            payload = marker["payload"]
            stage = payload.get("deferred_stage")
            chunk = str(payload.get("text") or payload.get("chunk") or "")
            digest = payload.get("content_hash")
            marker_uri = str(payload.get("source_uri") or marker.get("source_uri") or "")

            if stage == DEFERRED_FULL and isinstance(digest, str) and digest not in seen_hashes:
                # The chunk was deferred whole: it was never written. Embed and
                # write it now (unless a concurrent/previous run already did).
                seen_hashes.add(digest)
                if not self._hash_present(tenant, digest):
                    vectors = await self._llm.embed([chunk])
                    if vectors:
                        self._store.add_chunk(
                            tenant,
                            source_uri=marker_uri,
                            ordinal=int(payload.get("ordinal") or 0),
                            text=chunk,
                            embedding=vectors[0],
                            content_hash=digest,
                        )

            facts = await self._extract_facts(chunk, source_uri=marker_uri)
            for fact in facts:
                self._stage_candidate(
                    tenant,
                    kind="fact",
                    payload=fact,
                    source_uri=marker_uri,
                    confidence=float(fact.get("confidence", 1.0) or 0.0),
                    ingest_id=str(marker.get("ingest_id") or ""),
                )
                facts_staged += 1
            self._mark_resumed(tenant, marker["candidate_id"])
            resumed += 1

        tokens_used = self._llm.usage().total_tokens - start_tokens
        return ResumeFactsResult(
            resumed=resumed,
            facts_staged=facts_staged,
            still_deferred=still_deferred,
            tokens_used=tokens_used,
        )

    # ----------------------------------------------------------------- helpers

    async def _extract_facts(self, chunk: str, *, source_uri: str) -> list[dict[str, Any]]:
        """Extract bi-temporal facts from ``chunk`` via a schema-bound chat call.

        Each returned fact carries the WP-08/Curator payload contract:
        ``subject``, ``predicate``, ``object`` *and* its alias ``object_text``
        (identical value), ``valid_from``, ``valid_to``, ``confidence`` and a
        fact ``embedding`` (the ``llm.embed`` vector of
        ``subject + " " + predicate + " " + object``, or ``null`` when empty).
        """
        messages = [
            {"role": "system", "content": _FACT_SYSTEM_PROMPT},
            {"role": "user", "content": f"Source: {source_uri}\n\n{chunk}"},
        ]
        result = await self._llm.chat(messages, json_schema=_FACT_SCHEMA)
        content = result.content
        if isinstance(content, str):
            try:
                content = json.loads(content)
            except ValueError:
                return []
        if not isinstance(content, Mapping):
            return []
        facts = content.get("facts")
        if not isinstance(facts, list):
            return []
        normalised: list[dict[str, Any]] = []
        for fact in facts:
            if not isinstance(fact, Mapping):
                continue
            confidence = fact.get("confidence", 1.0)
            try:
                confidence = min(1.0, max(0.0, float(confidence)))
            except (TypeError, ValueError):
                confidence = 1.0
            # ``object_text`` is the canonical key; ``object`` is its alias. The
            # LLM may return either (or both); prefer the non-empty one.
            object_text = fact.get("object_text") or fact.get("object")
            subject = fact.get("subject")
            predicate = fact.get("predicate")
            fact_text = " ".join(
                part.strip() for part in (str(subject or ""), str(predicate or ""), str(object_text or ""))
                if part and str(part).strip()
            )
            embedding: list[float] | None = None
            if fact_text:
                vectors = await self._llm.embed([fact_text])
                embedding = list(vectors[0]) if vectors else None
            normalised.append(
                {
                    "subject": subject,
                    "predicate": predicate,
                    "object_text": object_text,
                    "object": object_text,
                    "valid_from": fact.get("valid_from"),
                    "valid_to": fact.get("valid_to"),
                    "confidence": confidence,
                    "embedding": embedding,
                    "subject_entity_id": fact.get("subject_entity_id"),
                }
            )
        return normalised

    def _hash_present(self, tenant: str, digest: str) -> bool:
        """Return ``True`` when a chunk with ``digest`` already exists.

        Prefers a ``has_content_hash`` hook on the injected store (keeps the
        store the single source of truth); otherwise queries the ``chunks`` table
        directly through the injected connection (the real ``MemoryStore``
        exposes no such lookup).
        """
        checker = getattr(self._store, "has_content_hash", None)
        if callable(checker):
            return bool(checker(tenant, digest))
        with self._connect(tenant) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM chunks WHERE tenant_id = %s AND content_hash = %s LIMIT 1",
                (tenant, digest),
            )
            return cur.fetchone() is not None

    def _stage_candidate(
        self,
        tenant: str,
        *,
        kind: str,
        payload: Mapping[str, Any],
        source_uri: str,
        confidence: float,
        ingest_id: str,
        status: str = "pending",
    ) -> str:
        """Insert one ``staging_candidates`` row and return its ``candidate_id``.

        ``pending`` by default. The id is surfaced (B7) so a caller such as the
        MCP ``auto_promote`` path can promote **exactly** the rows this run
        staged instead of the whole tenant's pending backlog.
        """
        with self._connect(tenant) as conn, conn.cursor() as cur:
            cur.execute(
                _INSERT_CANDIDATE_SQL,
                (tenant, kind, Jsonb(dict(payload)), source_uri, confidence, status, ingest_id),
            )
            row = cur.fetchone()
            conn.commit()
        return str(_row_get(row, "candidate_id", 0))

    def _deferred_markers(
        self,
        tenant: str,
        *,
        source_uri: str | None = None,
        ingest_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return the ``status='deferred'`` resume markers awaiting re-cognify.

        Both stages are returned: ``'facts_only'`` (already embedded) and
        ``'full'`` (deferred whole). An optional ``source_uri``/``ingest_id``
        narrows the set. Ordered by ``created_at`` so resume runs are
        deterministic.
        """
        clauses = ["tenant_id = %s", "status = %s"]
        params: list[Any] = [tenant, STATUS_DEFERRED]
        if source_uri is not None:
            clauses.append("source_uri = %s")
            params.append(source_uri)
        if ingest_id is not None:
            clauses.append("ingest_id = %s")
            params.append(ingest_id)
        sql = (
            "SELECT candidate_id, tenant_id, kind, payload, source_uri, confidence, "
            "status, ingest_id, created_at "
            "FROM staging_candidates "
            f"WHERE {' AND '.join(clauses)} ORDER BY created_at"
        )
        with self._connect(tenant) as conn, conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            rows = cur.fetchall()
        markers: list[dict[str, Any]] = []
        for row in rows:
            payload = _row_payload(row)
            if payload.get("deferred_stage") not in (DEFERRED_FACTS_ONLY, DEFERRED_FULL):
                continue
            markers.append(
                {
                    "candidate_id": str(_row_get(row, "candidate_id", 0)),
                    "payload": payload,
                    "source_uri": _row_get(row, "source_uri", 4),
                    "ingest_id": _row_get(row, "ingest_id", 7),
                }
            )
        return markers

    def _mark_resumed(self, tenant: str, candidate_id: str) -> None:
        """Mark a resume marker ``approved`` so it is not processed again.

        A resume marker is never a real fact candidate, so marking it
        ``approved`` (rather than leaving it ``pending``) keeps it out of the
        Curator's promote queue too.
        """
        sql = (
            "UPDATE staging_candidates SET status = 'approved', reason = 'resumed' "
            "WHERE tenant_id = %s AND candidate_id = %s"
        )
        with self._connect(tenant) as conn, conn.cursor() as cur:
            cur.execute(sql, (tenant, candidate_id))
            conn.commit()


class SessionStore:
    """Append-only episode store for chat/agent sessions.

    Turns are kept as a JSONB list under ``kv`` key ``episode:<session_id>`` and
    mirrored to a Markdown episode file via the injected
    :class:`stratum.filesystem.FilesystemStore`.

    Args:
        settings: Configuration source; defaults to :func:`get_settings`.
        store: KV store (``MemoryStore`` or compatible fake) with
            ``kv_get``/``kv_set``.
        fs: Filesystem store used to write/update the episode document.
        conn_factory: Callable with the signature of
            :func:`stratum.db.connection`; reserved for direct episode indexing
            (the KV path goes through ``store``).
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        store: Any,
        fs: Any,
        conn_factory: Callable[..., AbstractContextManager[Any]] | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._store = store
        self._fs = fs
        self._connect: Callable[..., AbstractContextManager[Any]] = (
            conn_factory or _default_connection
        )

    @staticmethod
    def episode_uri(tenant: str, session_id: str, *, now: datetime | None = None) -> str:
        """Return the episode URI ``stratum://episodes/<tenant>/<yyyy>/<mm>/<id>``."""
        moment = now or datetime.now(UTC)
        return build_uri("episodes", tenant, f"{moment:%Y}/{moment:%m}/{session_id}")

    def _resolve_episode_uri(self, tenant: str, session_id: str, stored: Mapping[str, Any]) -> str:
        """Return the session's episode URI, fixed once and reused thereafter.

        The month component is derived from the *first* append (its instant is
        cached under ``kv`` key ``episode_uri:<session_id>``) so a session that
        spans a month boundary keeps writing to one episode file instead of
        orphaning the earlier month's file and leaving a stale duplicate behind.
        """
        cached = stored.get("uri")
        if isinstance(cached, str) and cached:
            return cached
        uri = self.episode_uri(tenant, session_id)
        self._store.kv_set(tenant, f"episode_uri:{session_id}", {"uri": uri})
        return uri

    def append(
        self,
        tenant: str,
        session_id: str,
        role: str,
        content: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Append one turn to a session and refresh its episode document.

        Returns:
            ``{"uri": str, "turn": dict}`` where ``turn`` carries ``index``,
            ``role``, ``content``, ``metadata`` and an ISO-8601 ``ts``.
        """
        now = datetime.now(UTC)
        key = f"episode:{session_id}"

        stored = self._store.kv_get(tenant, key) or {}
        uri = self._resolve_episode_uri(tenant, session_id, stored)
        turns: list[dict[str, Any]] = list(stored.get("turns") or [])
        turn: dict[str, Any] = {
            "index": len(turns),
            "role": role,
            "content": content,
            "metadata": dict(metadata or {}),
            "ts": now.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        }
        turns.append(turn)
        self._store.kv_set(tenant, key, {"uri": uri, "session_id": session_id, "turns": turns})

        self._fs.write(
            uri,
            self._render_episode(session_id, turns),
            tenant=tenant,
            metadata={"title": session_id, "tags": ["episode"]},
        )
        return {"uri": uri, "turn": turn}

    def read(self, tenant: str, session_id: str) -> list[dict[str, Any]]:
        """Return the stored turn list for ``session_id`` (empty when unknown)."""
        stored = self._store.kv_get(tenant, f"episode:{session_id}") or {}
        return list(stored.get("turns") or [])

    @staticmethod
    def _render_episode(session_id: str, turns: Sequence[Mapping[str, Any]]) -> str:
        """Render the episode file body (Markdown transcript)."""
        lines = [f"# Episode {session_id}", ""]
        for turn in turns:
            lines.append(f"## {turn.get('role', 'unknown')}")
            lines.append("")
            lines.append(str(turn.get("content", "")))
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"
