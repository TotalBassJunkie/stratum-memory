"""Tests for the vector/KV layer and add-only dedup (WP-05).

The pure functions (``cosine_similarity``, ``chunk_text``) are tested directly.
The DB-backed :class:`MemoryStore` methods are tested by monkeypatching
``find_similar_facts`` on the instance and by asserting that invalid input is
rejected before any connection is opened — no Postgres is required.
"""

from __future__ import annotations

import random

import pytest

from stratum.config import Settings
from stratum.vector import MemoryStore, chunk_text, cosine_similarity

# --------------------------------------------------------------- similarity


def test_cosine_identical_is_one():
    assert cosine_similarity([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) == pytest.approx(1.0)


def test_cosine_orthogonal_is_zero():
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_cosine_opposite_is_minus_one():
    assert cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)


def test_cosine_zero_vector_is_zero():
    assert cosine_similarity([0.0, 0.0], [1.0, 2.0]) == 0.0
    assert cosine_similarity([1.0, 2.0], [0.0, 0.0]) == 0.0
    assert cosine_similarity([0.0, 0.0], [0.0, 0.0]) == 0.0


def test_cosine_length_mismatch_raises():
    with pytest.raises(ValueError):
        cosine_similarity([1.0, 2.0], [1.0])


def test_cosine_scale_invariant():
    assert cosine_similarity([1.0, 2.0, 3.0], [10.0, 20.0, 30.0]) == pytest.approx(1.0)


# ------------------------------------------------------------------- decide


def _store(**settings_overrides) -> MemoryStore:
    return MemoryStore(Settings(**settings_overrides))


def _stub_similar_facts(store: MemoryStore, matches: list[dict]) -> None:
    """Replace the DB-backed nearest-neighbour lookup with a fixed result."""
    store.find_similar_facts = lambda *a, **k: matches  # type: ignore[method-assign]


def test_decide_noop_when_similarity_high_and_same_object():
    store = _store()
    _stub_similar_facts(
        store, [{"fact_id": "f1", "object_text": "Berlin", "similarity": 0.99}]
    )
    assert store.decide(
        "default", candidate={"object_text": "  berlin "}, embedding=[1.0, 0.0]
    ) == "NOOP"


def test_decide_update_when_similarity_high_but_object_differs():
    store = _store()
    _stub_similar_facts(
        store, [{"fact_id": "f1", "object_text": "Berlin", "similarity": 0.99}]
    )
    assert store.decide(
        "default", candidate={"object_text": "Munich"}, embedding=[1.0, 0.0]
    ) == "UPDATE"


def test_decide_add_below_threshold():
    store = _store()
    _stub_similar_facts(
        store, [{"fact_id": "f1", "object_text": "Berlin", "similarity": 0.50}]
    )
    assert store.decide(
        "default", candidate={"object_text": "Berlin"}, embedding=[1.0, 0.0]
    ) == "ADD"


def test_decide_add_when_no_matches():
    store = _store()
    _stub_similar_facts(store, [])
    assert store.decide(
        "default", candidate={"object_text": "Berlin"}, embedding=[1.0, 0.0]
    ) == "ADD"


def test_decide_force_delete():
    store = _store()
    # Even with a very similar neighbour, explicit force_delete wins.
    _stub_similar_facts(
        store, [{"fact_id": "f1", "object_text": "Berlin", "similarity": 1.0}]
    )
    assert store.decide(
        "default",
        candidate={"object_text": "Berlin"},
        embedding=[1.0, 0.0],
        force_delete=True,
    ) == "DELETE"


def test_decide_never_add_at_or_above_threshold_property():
    """Property: for any similarity >= 0.92, ADD must never be returned."""
    store = _store()
    rng = random.Random(20261011)
    threshold = Settings().dedup_threshold  # 0.92
    for _ in range(500):
        similarity = rng.uniform(threshold, 1.0)
        same = rng.random() < 0.5
        existing = "Washington"
        candidate = existing if same else "Canberra" + str(rng.random())
        _stub_similar_facts(
            store,
            [{"fact_id": "f1", "object_text": existing, "similarity": similarity}],
        )
        decision = store.decide(
            "default", candidate={"object_text": candidate}, embedding=[1.0, 0.0]
        )
        assert decision in {"NOOP", "UPDATE", "DELETE"}, decision
        assert decision != "ADD"


def test_decide_respects_custom_threshold():
    store = _store()
    _stub_similar_facts(
        store, [{"fact_id": "f1", "object_text": "X", "similarity": 0.80}]
    )
    # With a lower threshold, 0.80 is above the gate -> UPDATE (object differs).
    assert store.decide(
        "default", candidate={"object_text": "Y"}, embedding=[1.0, 0.0], threshold=0.75
    ) == "UPDATE"
    # With the default 0.92, the same similarity is below the gate -> ADD.
    assert store.decide(
        "default", candidate={"object_text": "Y"}, embedding=[1.0, 0.0]
    ) == "ADD"


def test_decide_passes_threshold_to_search():
    store = _store()
    seen: dict = {}

    def fake(tenant, embedding, *, threshold, limit=5):
        seen["tenant"] = tenant
        seen["threshold"] = threshold
        seen["limit"] = limit
        return []

    store.find_similar_facts = fake  # type: ignore[method-assign]
    assert store.decide("acme", candidate={"object_text": "x"}, embedding=[1.0]) == "ADD"
    assert seen["tenant"] == "acme"
    assert seen["threshold"] == pytest.approx(0.92)
    assert seen["limit"] == 1


# ------------------------------------------- B1: meaningful-change detection


@pytest.mark.parametrize(
    ("existing", "candidate"),
    [
        # Changed IP address (digit-only edit, wording identical).
        ("IP ist 10.0.0.5", "IP ist 10.0.0.6"),
        # Changed number (digit-only edit, wording identical).
        ("rotation_days = 30", "rotation_days = 90"),
        # Flipped flag (no digits, but the decision word changed).
        ("cache enabled", "cache disabled"),
        # Added negation (decision-word set differs).
        ("service is active", "service is not active"),
    ],
)
def test_decide_update_on_meaningful_short_change(existing, candidate):
    """A value/flag edit must never be swallowed as NOOP (B1)."""
    store = _store()
    _stub_similar_facts(
        store, [{"fact_id": "f1", "object_text": existing, "similarity": 1.0}]
    )
    assert store.decide(
        "default", candidate={"object_text": candidate}, embedding=[1.0, 0.0]
    ) == "UPDATE"


def test_decide_noop_on_exact_normalised_restatement():
    """A genuine (whitespace/case) restatement is still NOOP (B1)."""
    store = _store()
    _stub_similar_facts(
        store, [{"fact_id": "f1", "object_text": "Berlin", "similarity": 1.0}]
    )
    assert store.decide(
        "default", candidate={"object_text": "  berlin  "}, embedding=[1.0, 0.0]
    ) == "NOOP"


# ------------------------------------------------- B4: threshold validation


@pytest.mark.parametrize("bad_threshold", [1.5, -0.1, 2.0, -1.0])
def test_decide_rejects_out_of_range_threshold(bad_threshold):
    """threshold outside [0, 1] must raise before any decision is made (B4)."""
    store = _store()
    _stub_similar_facts(
        store, [{"fact_id": "f1", "object_text": "X", "similarity": 0.99}]
    )
    with pytest.raises(ValueError):
        store.decide(
            "default",
            candidate={"object_text": "Y"},
            embedding=[1.0, 0.0],
            threshold=bad_threshold,
        )


@pytest.mark.parametrize("ok_threshold", [0.0, 1.0])
def test_decide_accepts_boundary_thresholds(ok_threshold):
    """0.0 and 1.0 are valid boundaries and must not raise (B4)."""
    store = _store()
    _stub_similar_facts(
        store, [{"fact_id": "f1", "object_text": "X", "similarity": 1.0}]
    )
    assert store.decide(
        "default",
        candidate={"object_text": "X"},
        embedding=[1.0, 0.0],
        threshold=ok_threshold,
    ) == "NOOP"


# ---------------------------------------------- B5: non-finite components


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ([float("nan"), 1.0], [1.0, 1.0]),
        ([1.0, 1.0], [float("nan"), 1.0]),
        ([float("inf"), 1.0], [1.0, 1.0]),
        ([1.0, float("-inf")], [1.0, 1.0]),
    ],
)
def test_cosine_non_finite_component_raises(a, b):
    """NaN/inf components must raise instead of yielding a bogus score (B5)."""
    with pytest.raises(ValueError):
        cosine_similarity(a, b)



# ---------------------------------------------------------------- log_event


@pytest.mark.parametrize("bad_op", ["add", "FROB", "", "Insert", "delete"])
def test_log_event_rejects_invalid_op(bad_op):
    store = _store()
    # The op guard runs before any connection is opened, so no DB is needed.
    with pytest.raises(ValueError):
        store.log_event("default", op=bad_op, target_uri="stratum://facts/default/x")


# --------------------------------------------------------------- chunk_text


def test_chunk_text_empty_returns_empty():
    assert chunk_text("") == []


def test_chunk_text_short_input_single_chunk():
    text = "short text"
    assert chunk_text(text, target_tokens=800, overlap=100) == [text]


def test_chunk_text_covers_and_overlaps_in_order():
    text = "".join(str(i % 10) for i in range(20000))  # 20k chars ~ 5000 tokens
    chunks = chunk_text(text, target_tokens=800, overlap=100)

    assert len(chunks) > 1
    # Coverage: concatenating the non-overlapping prefixes reconstructs the text.
    # Check start alignment: first chunk starts at 0, last chunk ends at len(text).
    assert text.startswith(chunks[0])
    assert text.endswith(chunks[-1])

    target_chars = 800 * 4
    overlap_chars = 100 * 4
    step = target_chars - overlap_chars
    for i, chunk in enumerate(chunks):
        start = i * step
        end = min(start + target_chars, len(text))
        assert chunk == text[start:end], f"chunk {i} mismatch"
        if i > 0:
            # Consecutive chunks overlap by exactly ``overlap_chars``.
            prev = chunks[i - 1]
            shared = prev[-(overlap_chars):]
            assert chunk.startswith(shared)


def test_chunk_text_respects_max_size():
    text = "a" * 10000
    chunks = chunk_text(text, target_tokens=100, overlap=20)
    for chunk in chunks:
        assert len(chunk) <= 100 * 4


def test_chunk_text_overlap_never_stalls():
    # Overlap >= target must not produce an infinite loop or a single chunk.
    text = "b" * 5000
    chunks = chunk_text(text, target_tokens=50, overlap=100)
    assert len(chunks) > 1
    assert all(chunk for chunk in chunks)  # no empty chunks
