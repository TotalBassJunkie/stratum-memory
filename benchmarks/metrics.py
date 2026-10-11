"""Pure, deterministic benchmark metrics (WP-16, MASTER-PLAN §14).

Every function here is a *reine Funktion*: no I/O, no randomness, no global
state. Inputs are plain Python values; outputs are ``float`` in ``[0.0, 1.0]``
(except :func:`ndcg_at_k`, which is also in ``[0.0, 1.0]``). Empty inputs return
``0.0`` by default so a metric never silently becomes ``NaN``.

Inputs may carry surrounding prose (a retrieved fact often looks like
``"backend ip_address 10.0.0.5"`` while the gold answer is ``"10.0.0.5"``).
Scoring is therefore token-based and case-insensitive:

* :func:`f1` — symmetric token-overlap F1 (SQuAD-style),
* :func:`exact_match` / :func:`accuracy` — normalised string equality,
* :func:`temporal_correctness` — gold *contained* in the prediction (the
  versioned value the engine should surface),
* :func:`contradiction_rate` — fraction of answers that surface a *stale*
  (closed) value,
* :func:`ndcg_at_k` / :func:`recall_at_k` — ranked retrieval quality.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence

__all__ = [
    "_contains",
    "accuracy",
    "contradiction_rate",
    "exact_match",
    "f1",
    "ndcg_at_k",
    "normalize",
    "recall_at_k",
    "temporal_correctness",
    "tokens",
]

#: Split on anything that is not a word character or a decimal separator, so
#: ``10.0.0.5`` stays one token while punctuation is stripped.
_TOKEN_RE = re.compile(r"[a-z0-9]+(?:[._-][a-z0-9]+)*")


def normalize(text: str) -> str:
    """Lowercase, strip and collapse internal whitespace for comparison."""
    return " ".join(str(text).lower().split())


def tokens(text: str) -> list[str]:
    """Tokenise ``text`` into lowercase alphanumeric/url-like tokens."""
    return _TOKEN_RE.findall(normalize(text))


def f1(pred: str, gold: str) -> float:
    """Return the token-overlap F1 of ``pred`` against ``gold`` in ``[0, 1]``.

    Both strings are tokenised (case-insensitive); the score is the harmonic
    mean of token precision and recall. Two empty strings score ``1.0`` (a
    trivial match); exactly one empty side scores ``0.0``.
    """
    pred_tokens = tokens(pred)
    gold_tokens = tokens(gold)
    if not pred_tokens and not gold_tokens:
        return 1.0
    if not pred_tokens or not gold_tokens:
        return 0.0
    overlap = sum((Counter(pred_tokens) & Counter(gold_tokens)).values())
    if overlap == 0:
        return 0.0
    precision = overlap / len(pred_tokens)
    recall = overlap / len(gold_tokens)
    return 2.0 * precision * recall / (precision + recall)


def exact_match(pred: str, gold: str) -> float:
    """Return ``1.0`` when ``pred`` and ``gold`` are equal after normalisation."""
    return 1.0 if normalize(pred) == normalize(gold) else 0.0


def accuracy(preds: Sequence[str], golds: Sequence[str]) -> float:
    """Return the mean :func:`exact_match` over paired predictions/golds.

    Returns ``0.0`` for an empty input. Mismatched lengths raise
    :class:`ValueError` (a silent zip-truncation would hide a broken dataset).
    """
    if len(preds) != len(golds):
        raise ValueError(f"length mismatch: {len(preds)} predictions, {len(golds)} golds")
    if not preds:
        return 0.0
    total = sum(exact_match(p, g) for p, g in zip(preds, golds, strict=True))
    return total / len(preds)


def temporal_correctness(preds: Sequence[str], golds: Sequence[str]) -> float:
    """Return the fraction of temporal probes answered with the right version.

    A probe is *correct* when the normalised gold value appears in the
    prediction (as a contiguous token subsequence). This is stricter than plain
    token overlap and captures the temporal-suite requirement: the engine must
    surface the version valid at the queried ``as_of_valid`` / ``as_of_tx``, not
    merely mention related words. Empty input scores ``0.0``.
    """
    if len(preds) != len(golds):
        raise ValueError(f"length mismatch: {len(preds)} predictions, {len(golds)} golds")
    if not preds:
        return 0.0
    hits = sum(1.0 for p, g in zip(preds, golds, strict=True) if _contains(p, g))
    return hits / len(preds)


def _contains(haystack: str, needle: str) -> bool:
    """Return ``True`` when ``needle``'s tokens are a contiguous slice of ``haystack``."""
    hay = tokens(haystack)
    pin = tokens(needle)
    if not pin:
        return False
    if len(pin) > len(hay):
        return False
    for start in range(len(hay) - len(pin) + 1):
        if hay[start : start + len(pin)] == pin:
            return True
    return False


def contradiction_rate(flags: Iterable[object]) -> float:
    """Return the fraction of ``flags`` that are truthy.

    Used for the temporal suite where a flag marks an answer that surfaced a
    *stale* (closed) value; after curation this must be ``0.0``. Empty input
    scores ``0.0``.
    """
    values = list(flags)
    if not values:
        return 0.0
    return sum(1.0 for flag in values if flag) / len(values)


def ndcg_at_k(ranked: Sequence[str], gains: Mapping[str, float], k: int) -> float:
    """Return normalised discounted cumulative gain ``@k`` in ``[0, 1]``.

    Args:
        ranked: Item ids in the order the system returned them.
        gains: Relevance per item id (missing ids count as ``0``).
        k: Cut-off rank (``>= 1``); items beyond ``k`` are ignored.

    Returns ``0.0`` when no item has a positive gain (ideal DCG is zero).
    """
    if k < 1:
        raise ValueError(f"k must be >= 1, got {k!r}")
    dcg = _dcg([float(gains.get(item, 0.0)) for item in ranked[:k]])
    ideal = sorted((float(v) for v in gains.values()), reverse=True)[:k]
    idcg = _dcg(ideal)
    if idcg == 0.0:
        return 0.0
    return min(1.0, dcg / idcg)


def _dcg(rel: Sequence[float]) -> float:
    """Discounted cumulative gain for a graded relevance list (1-based ranks)."""
    return sum((2.0**r - 1.0) / math.log2(index + 2.0) for index, r in enumerate(rel))


def recall_at_k(retrieved: Sequence[str], relevant: Iterable[str], k: int) -> float:
    """Return the fraction of ``relevant`` ids present in the top ``k`` hits.

    Returns ``0.0`` when ``relevant`` is empty; extra retrieved items are
    ignored. Order within the cut-off does not matter.
    """
    if k < 1:
        raise ValueError(f"k must be >= 1, got {k!r}")
    relevant_set = set(relevant)
    if not relevant_set:
        return 0.0
    top = set(retrieved[:k])
    return len(top & relevant_set) / len(relevant_set)
