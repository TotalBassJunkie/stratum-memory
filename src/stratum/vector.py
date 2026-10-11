"""Vector/KV layer: pgvector similarity, KV store, add-only dedup (WP-05).

This module owns three of the four storage layers' primitives from
``MASTER-PLAN.md``:

* **KV** — small, fast key/value state per tenant (``kv`` table),
* **Vector** — cosine similarity search over facts and chunks (``halfvec``),
* **Add-only-Dedup** — the ``ADD``/``UPDATE``/``DELETE``/``NOOP`` classifier
  (ADR-0005, MASTER-PLAN §8).

Design rules:

* Every database access goes through :func:`stratum.db.connection` with the
  tenant set, so Row Level Security applies (never the owner role here).
* ``memory_events`` is append-only; :meth:`MemoryStore.log_event` only ever
  ``INSERT``s.
* Candidate decisions never silently ``ADD`` when the nearest active fact is
  above the dedup threshold — ADD is forbidden there (ADR-0005).

``cosine_similarity`` is pure Python (no numpy required) so the module stays
importable on a bare Pi install.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Mapping, Sequence
from difflib import SequenceMatcher
from typing import Any

from psycopg.types.json import Jsonb

from .config import Settings, get_settings
from .db import connection

__all__ = [
    "VALID_OPS",
    "MemoryStore",
    "chunk_text",
    "cosine_similarity",
]

#: Allowed ``memory_events.op`` values (append-only journal; MASTER-PLAN §5).
VALID_OPS = frozenset(
    {"ADD", "UPDATE", "DELETE", "NOOP", "PROMOTE", "ARCHIVE", "FORGET", "REFLECT"}
)

#: Two object texts count as a restatement ("practically equal") above this
#: normalised character ratio — combined with identical number/decision tokens.
_NOOP_RATIO = 0.98

#: Rough characters-per-token estimate used by :func:`chunk_text` (~4 chars/token).
_CHARS_PER_TOKEN = 4

_WS_RE = re.compile(r"\s+")

#: Number tokens (integers/decimals, optional sign), e.g. ``10.0.0.5`` yields
#: ``{"10.0.0.5"}`` and ``30`` yields ``{"30"}``. Used to detect edits that
#: change a value while leaving the surrounding wording identical.
_NUMBER_TOKEN_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")

#: Wording that flips a statement's meaning when added/removed/swapped even if
#: the numbers stay the same (e.g. "not", "enabled", "true").
_DECISION_WORD_RE = re.compile(
    r"\b(?:not|no|never|none|enabled|disabled|true|false|on|off|"
    r"yes|y|n|allow(?:ed)?|deny|denied|forbid(?:den)?|activate[d]?)\b"
)


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Return the cosine similarity of two vectors in ``[-1.0, 1.0]``.

    Zero vectors (either side) have undefined direction and yield ``0.0``
    instead of raising. Vectors of different length raise :class:`ValueError`,
    as do vectors containing non-finite components (``NaN`` / ``inf``), which
    would otherwise poison the score (e.g. ``NaN`` silently comparing as
    ``1.0``).
    """
    if len(a) != len(b):
        raise ValueError(f"vector length mismatch: {len(a)} != {len(b)}")
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for x, y in zip(a, b, strict=True):
        fx = float(x)
        fy = float(y)
        if not (math.isfinite(fx) and math.isfinite(fy)):
            raise ValueError(f"non-finite vector component: {x!r}, {y!r}")
        x, y = fx, fy
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    sim = dot / (math.sqrt(norm_a) * math.sqrt(norm_b))
    # Guard against floating-point overshoot just outside [-1, 1].
    return max(-1.0, min(1.0, sim))


def _vector_literal(embedding: Sequence[float]) -> str:
    """Render an embedding as a pgvector literal, e.g. ``[0.1,0.2]``."""
    return "[" + ",".join(repr(float(x)) for x in embedding) + "]"


def _row_get(row: Any, key: str, index: int) -> Any:
    """Read a column from a row that may be a dict (dict_row) or a sequence."""
    if isinstance(row, Mapping):
        return row[key]
    return row[index]


def _normalise_text(text: str) -> str:
    """Lowercase, trim and collapse whitespace for equality comparison."""
    return _WS_RE.sub(" ", text.strip().lower())


def _object_similarity(a: str, b: str) -> float:
    """Normalised similarity of two object texts in ``[0.0, 1.0]``.

    Equality after normalisation short-circuits to ``1.0``; otherwise a
    character-level ratio is used. Used only to distinguish ``NOOP`` (object
    practically unchanged) from ``UPDATE`` (object changed).
    """
    na = _normalise_text(a)
    nb = _normalise_text(b)
    if na == nb:
        return 1.0
    if not na or not nb:
        return 0.0
    return SequenceMatcher(None, na, nb).ratio()


def _number_tokens(text: str) -> frozenset[str]:
    """Return the multiset-as-frozenset of number tokens in ``text``."""
    return frozenset(_NUMBER_TOKEN_RE.findall(_normalise_text(text)))


def _decision_words(text: str) -> frozenset[str]:
    """Return the set of meaning-flipping words present in ``text``."""
    return frozenset(_DECISION_WORD_RE.findall(_normalise_text(text)))


def _is_meaningful_noop(candidate: str, existing: str) -> bool:
    """Decide whether a candidate object leaves the nearest fact unchanged.

    ``NOOP`` (leave the stored fact alone) is only safe when the candidate is
    a genuine restatement, never when it silently edits a value. It requires:

    * normalised equality (verbatim restatement), **or**
    * the *same* number-token multiset **and** the *same* decision-word set
      (no negations / flags swapped) **and** a high character ratio
      (``>= 0.98``).

    Anything else — a changed IP/port, a changed number, a flipped flag — is a
    real change and must be classified ``UPDATE``.
    """
    na = _normalise_text(candidate)
    nb = _normalise_text(existing)
    if na == nb:
        return True
    if _number_tokens(na) != _number_tokens(nb):
        return False
    if _decision_words(na) != _decision_words(nb):
        return False
    return _object_similarity(na, nb) >= _NOOP_RATIO


def _candidate_object_text(candidate: Any) -> str | None:
    """Extract the object text from a candidate (str or mapping)."""
    if candidate is None:
        return None
    if isinstance(candidate, str):
        return candidate
    if isinstance(candidate, Mapping):
        for key in ("object_text", "object", "text"):
            value = candidate.get(key)
            if isinstance(value, str):
                return value
    return None


def chunk_text(text: str, *, target_tokens: int = 800, overlap: int = 100) -> list[str]:
    """Split ``text`` into overlapping chunks of ~``target_tokens`` tokens.

    Tokens are estimated at ``~4 characters`` each, so no tokenizer is needed.
    Consecutive chunks overlap by ``overlap`` tokens; the resulting list covers
    the entire input in order. Empty input yields an empty list.
    """
    if not text:
        return []
    target_chars = max(1, target_tokens * _CHARS_PER_TOKEN)
    overlap_chars = max(0, overlap * _CHARS_PER_TOKEN)
    if overlap_chars >= target_chars:
        # Keep progress guaranteed: never let overlap swallow the whole window.
        overlap_chars = target_chars // 2
    step = target_chars - overlap_chars

    if len(text) <= target_chars:
        return [text]

    chunks: list[str] = []
    start = 0
    length = len(text)
    while start < length:
        end = min(start + target_chars, length)
        chunks.append(text[start:end])
        if end >= length:
            break
        start += step
    return chunks


class MemoryStore:
    """Tenant-scoped vector/KV operations and the add-only dedup classifier.

    Args:
        settings: Configuration source; defaults to :func:`get_settings`.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    # ------------------------------------------------------------------- KV

    def kv_get(self, tenant: str, key: str) -> dict[str, Any] | None:
        """Return the stored value for ``key``, or ``None`` if missing/expired.

        Expired rows (``expires_at`` in the past) are treated as absent; the
        column is filtered in SQL so no cleanup pass is required here.
        """
        with connection(tenant) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT value FROM kv WHERE tenant_id = %s AND key = %s "
                "AND (expires_at IS NULL OR expires_at > now())",
                (tenant, key),
            )
            row = cur.fetchone()
        if row is None:
            return None
        value = _row_get(row, "value", 0)
        return value if value is None else dict(value)

    def kv_set(
        self,
        tenant: str,
        key: str,
        value: Mapping[str, Any],
        expires_at: Any | None = None,
    ) -> None:
        """Upsert ``value`` for ``key`` (optionally with an expiry timestamp)."""
        with connection(tenant) as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO kv (tenant_id, key, value, expires_at) "
                "VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (tenant_id, key) DO UPDATE "
                "SET value = EXCLUDED.value, expires_at = EXCLUDED.expires_at",
                (tenant, key, Jsonb(dict(value)), expires_at),
            )
            conn.commit()

    # --------------------------------------------------------------- events

    def log_event(
        self,
        tenant: str,
        *,
        op: str,
        target_uri: str | None = None,
        before: Any | None = None,
        after: Any | None = None,
        actor: str | None = None,
    ) -> str:
        """Append one row to the append-only ``memory_events`` journal.

        Args:
            tenant: Tenant id (RLS scope).
            op: One of :data:`VALID_OPS`; anything else raises ``ValueError``.
            target_uri: URI of the affected object, if any.
            before: Pre-image (JSON-serialisable), stored as ``jsonb``.
            after: Post-image (JSON-serialisable), stored as ``jsonb``.
            actor: Who caused the change (agent / user id).

        Returns:
            The new ``event_id`` as a string.
        """
        if op not in VALID_OPS:
            raise ValueError(f"invalid op {op!r}; allowed: {sorted(VALID_OPS)}")
        with connection(tenant) as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO memory_events "
                "(tenant_id, op, target_uri, before, after, actor) "
                "VALUES (%s, %s, %s, %s, %s, %s) RETURNING event_id",
                (
                    tenant,
                    op,
                    target_uri,
                    None if before is None else Jsonb(before),
                    None if after is None else Jsonb(after),
                    actor,
                ),
            )
            row = cur.fetchone()
            conn.commit()
        return str(_row_get(row, "event_id", 0))

    # --------------------------------------------------------------- search

    def _vector_cast(self) -> str:
        """pgvector type cast matching the configured embedding storage type."""
        return "vector" if self.settings.vector_type == "vector" else "halfvec"

    def find_similar_facts(
        self,
        tenant: str,
        embedding: Sequence[float],
        *,
        threshold: float,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Return the nearest active facts at or above ``threshold``.

        Uses pgvector cosine distance (``<=>``) over the ``facts.embedding``
        column. Only open facts (``valid_to IS NULL AND tx_to IS NULL``) are
        considered. Each result has ``fact_id``, ``object_text`` and
        ``similarity``.
        """
        vec = _vector_literal(embedding)
        cast = self._vector_cast()
        with connection(tenant) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT fact_id, object_text, "
                f"1 - (embedding <=> %s::{cast}) AS similarity "
                "FROM facts "
                "WHERE tenant_id = %s AND embedding IS NOT NULL "
                "AND valid_to IS NULL AND tx_to IS NULL "
                f"AND 1 - (embedding <=> %s::{cast}) >= %s "
                f"ORDER BY embedding <=> %s::{cast} "
                "LIMIT %s",
                (vec, tenant, vec, threshold, vec, limit),
            )
            rows = cur.fetchall()
        return [
            {
                "fact_id": str(_row_get(row, "fact_id", 0)),
                "object_text": _row_get(row, "object_text", 1),
                "similarity": float(_row_get(row, "similarity", 2)),
            }
            for row in rows
        ]

    def search_chunks(
        self,
        tenant: str,
        embedding: Sequence[float],
        *,
        k: int = 8,
        scope: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return the ``k`` nearest chunks, optionally limited to a URI prefix.

        ``scope`` is matched as a prefix over ``source_uri`` (so a
        ``stratum://docs/default/projects`` scope covers everything below it).
        Each result has ``chunk_id``, ``source_uri``, ``ordinal``, ``text`` and
        ``similarity``.
        """
        vec = _vector_literal(embedding)
        cast = self._vector_cast()
        sql = (
            "SELECT chunk_id, source_uri, ordinal, text, "
            f"1 - (embedding <=> %s::{cast}) AS similarity "
            "FROM chunks "
            "WHERE tenant_id = %s AND embedding IS NOT NULL"
        )
        params: list[Any] = [vec, tenant]
        if scope:
            # ``starts_with`` is an exact prefix match (unlike LIKE, where ``_``
            # would be a wildcard — and URI segments may contain ``_``).
            sql += " AND starts_with(source_uri, %s)"
            params.append(scope)
        sql += f" ORDER BY embedding <=> %s::{cast} LIMIT %s"
        params.extend([vec, k])

        with connection(tenant) as conn, conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            rows = cur.fetchall()
        return [
            {
                "chunk_id": str(_row_get(row, "chunk_id", 0)),
                "source_uri": _row_get(row, "source_uri", 1),
                "ordinal": int(_row_get(row, "ordinal", 2)),
                "text": _row_get(row, "text", 3),
                "similarity": float(_row_get(row, "similarity", 4)),
            }
            for row in rows
        ]

    # ------------------------------------------------------- dedup decision

    def decide(
        self,
        tenant: str,
        *,
        candidate: Any,
        embedding: Sequence[float],
        threshold: float | None = None,
        force_delete: bool = False,
    ) -> str:
        """Classify a candidate against the nearest active fact (ADR-0005).

        Rules (MASTER-PLAN §8 / ADR-0005):

        * similarity ``>= threshold`` ⇒ **ADD is forbidden** → ``NOOP`` when the
          object text is a genuine restatement (see :func:`_is_meaningful_noop`),
          otherwise ``UPDATE``;
        * similarity ``< threshold`` ⇒ ``ADD``;
        * ``force_delete=True`` ⇒ ``DELETE`` (explicit caller intent).

        ``threshold`` must lie in ``[0.0, 1.0]``; anything else raises
        :class:`ValueError` (a value above ``1.0`` would make every candidate an
        ``ADD`` and silently defeat the dedup gate).

        Returns one of ``"ADD"``, ``"UPDATE"``, ``"DELETE"``, ``"NOOP"``.
        """
        effective_threshold = self.settings.dedup_threshold if threshold is None else threshold

        if force_delete:
            return "DELETE"

        if not 0.0 <= effective_threshold <= 1.0:
            raise ValueError(
                f"threshold must be in [0.0, 1.0], got {effective_threshold!r}"
            )

        matches = self.find_similar_facts(
            tenant, embedding, threshold=effective_threshold, limit=1
        )
        if not matches:
            return "ADD"

        nearest = matches[0]
        similarity = float(nearest.get("similarity", 0.0))
        if similarity < effective_threshold:
            # Below the gate the candidate is a genuinely new fact.
            return "ADD"

        candidate_object = _candidate_object_text(candidate)
        existing_object = nearest.get("object_text") or ""
        if candidate_object is not None and _is_meaningful_noop(
            candidate_object, str(existing_object)
        ):
            return "NOOP"
        return "UPDATE"

    # ---------------------------------------------------------------- chunks

    def add_chunk(
        self,
        tenant: str,
        *,
        source_uri: str,
        ordinal: int,
        text: str,
        embedding: Sequence[float],
        content_hash: str | None = None,
    ) -> dict[str, Any]:
        """Insert a document chunk, applying level-1 content-hash dedup.

        If a chunk with the same ``content_hash`` already exists for the tenant,
        nothing is written and the existing ``chunk_id`` is returned with
        ``skipped=True`` (MASTER-PLAN §7: no work, and no LLM call, on a
        duplicate). Otherwise the chunk is inserted with ``skipped=False``.

        Note:
            Returns ``{"chunk_id": str, "skipped": bool}`` so callers can act on
            the dedup outcome; the WP brief's ``-> str`` annotation is
            superseded by its explicit "Flag ``skipped`` im Rückgabe-dict".
        """
        digest = content_hash or hashlib.sha256(text.encode("utf-8")).hexdigest()
        cast = self._vector_cast()
        with connection(tenant) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT chunk_id FROM chunks "
                "WHERE tenant_id = %s AND content_hash = %s LIMIT 1",
                (tenant, digest),
            )
            existing = cur.fetchone()
            if existing is not None:
                return {"chunk_id": str(_row_get(existing, "chunk_id", 0)), "skipped": True}

            cur.execute(
                "INSERT INTO chunks "
                "(tenant_id, source_uri, ordinal, text, content_hash, embedding) "
                f"VALUES (%s, %s, %s, %s, %s, %s::{cast}) RETURNING chunk_id",
                (tenant, source_uri, ordinal, text, digest, _vector_literal(embedding)),
            )
            row = cur.fetchone()
            conn.commit()
        return {"chunk_id": str(_row_get(row, "chunk_id", 0)), "skipped": False}
