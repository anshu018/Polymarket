"""
data/novelty.py — A6: deterministic novelty detection (List A.md Step 2).

Pure functions, zero I/O, zero LLM: the pipeline hook (coordinator/pipeline.py)
supplies the trailing-window history read; everything here is deterministic
math so a headline burst on one event can no longer masquerade as fresh
signals.

Model (List A.md Step 2 spec):
    - headline_hash: normalized headline (lowercase, punctuation stripped,
      tokens sorted) → sha1. Two headlines with the same word content hash
      equal regardless of order or punctuation.
    - Entity-set Jaccard vs signals seen in the trailing NOVELTY_WINDOW_HOURS
      per market: overlap ≥ JACCARD_REPEAT_THRESHOLD means "same event thread".
    - novelty_verdict counts how many prior in-window signals for the market
      are "similar" (identical hash OR Jaccard ≥ threshold):
          0 similar → ("novel", 1.0)
          1 similar → ("repeat", config.NOVELTY_REPEAT_FACTOR)
          2+ similar → ("dup", None)   # caller drops, counter novelty:dup
    The novelty_factor is consumed by the velocity estimator (Step 6f); Step 2
    only uses the verdict for drop logic and counters.
"""

import hashlib
import json
import logging
import re
from typing import Any, Iterable

import config

logger = logging.getLogger(__name__)

# Verdict identifiers (stable strings — they appear in drop counters and logs)
NOVELTY_NOVEL = "novel"
NOVELTY_REPEAT = "repeat"
NOVELTY_DUP = "dup"

_TOKEN_PATTERN = re.compile(r"[a-z0-9]+")


def compute_headline_hash(headline: str) -> str:
    """
    Stable content hash of a headline (List A.md Step 2 spec).

    Normalization: lowercase, strip punctuation (keep [a-z0-9] tokens), sort
    the tokens, join, sha1-hex. Word order and punctuation therefore do not
    affect the hash; any word change does.

    Args:
        headline: Raw headline text.

    Returns:
        40-char sha1 hex digest of the normalized headline.
    """
    tokens = _TOKEN_PATTERN.findall((headline or "").lower())
    normalized = " ".join(sorted(tokens))
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()


def jaccard(set_a: frozenset[str], set_b: frozenset[str]) -> float:
    """
    Jaccard similarity |A ∩ B| / |A ∪ B| of two entity sets.

    Two empty sets are defined as dissimilar (0.0): "no entities" carries no
    evidence of being the same event.

    Args:
        set_a: First entity set (lowercase).
        set_b: Second entity set (lowercase).

    Returns:
        Similarity in [0.0, 1.0].
    """
    if not set_a and not set_b:
        return 0.0
    union = set_a | set_b
    if not union:
        return 0.0
    return len(set_a & set_b) / len(union)


def parse_entities(raw: Any) -> frozenset[str]:
    """
    Normalize a stored entity value into a lowercase entity set.

    Accepts a list of strings (PostgREST JSONB decode) or a JSON array string
    (defensive: rows written by other tooling). Malformed/missing values
    degrade to an empty set — a measurement gap must not crash novelty checks.

    Args:
        raw: entities_json value as read from signal_outcomes.

    Returns:
        Lowercase entity set.
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return frozenset()
    if not isinstance(raw, (list, tuple, set, frozenset)):
        return frozenset()
    return frozenset(str(item).strip().lower() for item in raw if str(item).strip())


def novelty_verdict(
    prior_rows: Iterable[dict[str, Any]],
    headline_hash: str,
    entities: list[str],
) -> tuple[str, float | None]:
    """
    Decide the novelty verdict for one signal against its market's history.

    A prior in-window signal is "similar" when it carries the identical
    headline_hash OR its entity set overlaps this signal's set by
    config.JACCARD_REPEAT_THRESHOLD or more. Count of similar priors drives
    the verdict: 0 → novel (factor 1.0), 1 → repeat (factor
    config.NOVELTY_REPEAT_FACTOR), 2+ → dup (factor None — caller drops).

    Args:
        prior_rows:   In-window signal_outcomes rows for this market; each
                      needs at least headline_hash and entities_json.
        headline_hash: This signal's normalized headline hash.
        entities:     This signal's extracted entities.

    Returns:
        (verdict, novelty_factor) tuple.
    """
    entity_set = parse_entities(entities)
    threshold = config.JACCARD_REPEAT_THRESHOLD
    similar_count = 0
    for row in prior_rows:
        row_hash = row.get("headline_hash") or ""
        if row_hash == headline_hash:
            similar_count += 1
            continue
        if jaccard(entity_set, parse_entities(row.get("entities_json"))) >= threshold:
            similar_count += 1

    if similar_count == 0:
        return NOVELTY_NOVEL, 1.0
    if similar_count == 1:
        return NOVELTY_REPEAT, float(config.NOVELTY_REPEAT_FACTOR)
    return NOVELTY_DUP, None
