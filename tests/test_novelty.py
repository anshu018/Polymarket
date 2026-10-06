"""
tests/test_novelty.py — Unit tests for the Step 2 novelty module (List A.md
Step 2 — A6). Pure math only: hash stability, Jaccard math, verdict rules.
"""

import pytest
import config
from data.novelty import (
    NOVELTY_DUP,
    NOVELTY_NOVEL,
    NOVELTY_REPEAT,
    compute_headline_hash,
    jaccard,
    novelty_verdict,
    parse_entities,
)


# ── headline_hash ─────────────────────────────────────────────────────────────

def test_hash_is_stable_across_calls():
    """Same headline → identical hash (deterministic)."""
    h1 = compute_headline_hash("Fed cuts rates by 50 basis points")
    h2 = compute_headline_hash("Fed cuts rates by 50 basis points")
    assert h1 == h2
    assert len(h1) == 40  # sha1 hex digest


def test_hash_ignores_case_punctuation_and_word_order():
    """Normalization per spec: lowercase, strip punctuation, sort tokens."""
    base = compute_headline_hash("Congress Passes Spending Bill!")
    same_words_different_order = compute_headline_hash("bill spending passes congress")
    different_case_punct = compute_headline_hash("CONGRESS passes... spending, BILL?")
    assert base == same_words_different_order == different_case_punct


def test_hash_changes_on_word_change():
    """A word change must change the hash (different headline → different hash)."""
    h1 = compute_headline_hash("Fed cuts rates")
    h2 = compute_headline_hash("Fed hikes rates")
    assert h1 != h2


def test_hash_handles_none_and_empty():
    """None/empty headlines hash deterministically instead of raising."""
    assert compute_headline_hash("") == compute_headline_hash("")
    assert compute_headline_hash(None) == compute_headline_hash("")


# ── jaccard ───────────────────────────────────────────────────────────────────

def test_jaccard_exact_math():
    """|A∩B| / |A∪B| on hand-computed sets."""
    a = frozenset({"fed", "rates"})
    b = frozenset({"fed", "powell"})
    # ∩ = {fed} = 1, ∪ = {fed, rates, powell} = 3 → 1/3
    assert jaccard(a, b) == pytest.approx(1.0 / 3.0)


def test_jaccard_identical_sets_is_one():
    a = frozenset({"bitcoin", "etf"})
    assert jaccard(a, a) == 1.0


def test_jaccard_disjoint_sets_is_zero():
    assert jaccard(frozenset({"a"}), frozenset({"b"})) == 0.0


def test_jaccard_two_empty_sets_is_zero():
    """Empty vs empty must NOT read as a perfect match."""
    assert jaccard(frozenset(), frozenset()) == 0.0


def test_jaccard_boundary_at_threshold():
    """2/4 = 0.5 lands exactly ON the repeat threshold (≥ semantics)."""
    a = frozenset({"a", "b", "c"})
    b = frozenset({"a", "b", "d"})
    assert jaccard(a, b) == pytest.approx(0.5)


# ── parse_entities ────────────────────────────────────────────────────────────

def test_parse_entities_accepts_list_and_json_string():
    """PostgREST JSONB decodes to a list; defensive rows may carry a JSON string."""
    as_list = parse_entities(["Fed", "Powell "])
    as_json = parse_entities('["Fed", "Powell "]')
    assert as_list == as_json == frozenset({"fed", "powell"})


def test_parse_entities_malformed_degrades_to_empty():
    """A measurement gap must never crash the novelty check."""
    assert parse_entities(None) == frozenset()
    assert parse_entities("not json {") == frozenset()
    assert parse_entities(42) == frozenset()


# ── novelty_verdict ───────────────────────────────────────────────────────────

def test_verdict_first_signal_is_novel():
    """No prior in-window signals → novel, factor 1.0."""
    verdict, factor = novelty_verdict([], "hash-x", ["fed"])
    assert verdict == NOVELTY_NOVEL
    assert factor == 1.0


def test_verdict_identical_hash_is_repeat_then_dup():
    """2nd identical headline → repeat; 3rd → dup (the burst rule)."""
    rows = [{"headline_hash": "hash-x", "entities_json": ["fed"]}]
    verdict, factor = novelty_verdict(rows, "hash-x", ["fed"])
    assert verdict == NOVELTY_REPEAT
    assert factor == float(config.NOVELTY_REPEAT_FACTOR)

    rows.append({"headline_hash": "hash-x", "entities_json": ["fed"]})
    verdict, factor = novelty_verdict(rows, "hash-x", ["fed"])
    assert verdict == NOVELTY_DUP
    assert factor is None


def test_verdict_entity_similarity_counts_as_same_thread():
    """High Jaccard with ONE prior → repeat even with a different hash."""
    rows = [{"headline_hash": "hash-other", "entities_json": ["fed", "rates", "inflation", "powell"]}]
    # 3 of 4 overlap → 3/5 = 0.6 ≥ 0.5 threshold
    verdict, factor = novelty_verdict(rows, "hash-new", ["fed", "rates", "inflation"])
    assert verdict == NOVELTY_REPEAT
    assert factor == float(config.NOVELTY_REPEAT_FACTOR)


def test_verdict_low_jaccard_stays_novel():
    """Low-overlap entities on the same market are still fresh signals."""
    rows = [{"headline_hash": "hash-other", "entities_json": ["bitcoin", "etf", "sec"]}]
    # 1 of 4 overlap → 1/5 = 0.2 < 0.5
    verdict, _ = novelty_verdict(rows, "hash-new", ["bitcoin", "whale", "exchange"])
    assert verdict == NOVELTY_NOVEL


def test_verdict_two_similar_priors_drops():
    """A third similar signal (via mixed hash + Jaccard matches) is dropped."""
    rows = [
        {"headline_hash": "hash-x", "entities_json": ["fed"]},
        {"headline_hash": "hash-y", "entities_json": ["fed", "rates", "cuts"]},
    ]
    # hash match (row 1) + jaccard {fed,cuts} vs {fed,rates,cuts} = 2/3 ≥ 0.5 (row 2)
    verdict, _ = novelty_verdict(rows, "hash-x", ["fed", "cuts"])
    assert verdict == NOVELTY_DUP


def test_verdict_ignores_malformed_prior_entities():
    """Priors with unparseable entities only match via hash, never crash."""
    rows = [{"headline_hash": "hash-z", "entities_json": "broken {"}]
    verdict, factor = novelty_verdict(rows, "hash-other", ["fed"])
    assert verdict == NOVELTY_NOVEL
    assert factor == 1.0
    verdict, _ = novelty_verdict(rows, "hash-z", ["fed"])
    assert verdict == NOVELTY_REPEAT


def test_verdict_threshold_is_config_driven():
    """Raising the threshold above the actual overlap flips repeat → novel."""
    rows = [{"headline_hash": "hash-other", "entities_json": ["a", "b", "d"]}]
    # 2 of 3 entities overlap → 2/4 = 0.5
    original = config.JACCARD_REPEAT_THRESHOLD
    try:
        config.JACCARD_REPEAT_THRESHOLD = 0.5
        assert novelty_verdict(rows, "h", ["a", "b", "c"])[0] == NOVELTY_REPEAT
        config.JACCARD_REPEAT_THRESHOLD = 0.9
        assert novelty_verdict(rows, "h", ["a", "b", "c"])[0] == NOVELTY_NOVEL
    finally:
        config.JACCARD_REPEAT_THRESHOLD = original
