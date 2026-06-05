"""
strategies/calibration.py — Probability calibration engine (Layer 3).

Flags mispriced markets by comparing the agent's calibration-adjusted
probability estimate against the current market price.

Architecture
------------
CalibrationModel
    Loads (agent_estimate, outcome) pairs from Supabase closed_trades.
    Maintains per-category calibration curves for all 6 supported categories.
    Falls back to a global curve when a category has < MIN_CATEGORY_RECORDS.
    On Supabase timeout: uses market-price passthrough (no crash, no noise).

Module-level functions
    compute_brier_score()  — Probabilistic accuracy metric.  Pure Python.
    compute_edge()         — abs(agent_estimate - market_price).
    is_mispriced()         — True when edge > MIN_EDGE_CENTS (0.07).
    get_category_estimate()— Calibration-adjusted probability estimate.

Invariants (per GEMINI.md RULE 1 / PLAN.md Section 6)
------------------------------------------------------
    - Zero LLM calls anywhere in this file.
    - Zero external API calls (Supabase reads only).
    - All Supabase reads wrapped in asyncio.wait_for(timeout=SUPABASE_TIMEOUT).
    - All public functions have type hints and docstrings.
    - Zero print() statements — logging only.
    - Deterministic pure-Python fallbacks for every failure mode.
"""

import asyncio
import logging
import time
from typing import Any, Optional

import config
from memory.supabase_client import get_client

logger = logging.getLogger(__name__)

__all__ = [
    "CalibrationModel",
    "get_model",
    "compute_brier_score",
    "compute_edge",
    "is_mispriced",
    "get_category_estimate",
]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CATEGORIES: list[str] = [
    "politics",
    "crypto",
    "sports",
    "science",
    "legal",
    "economics",
]

# Minimum resolved trades before a category curve is trusted.
MIN_CATEGORY_RECORDS: int = 20

# Minimum records inside a single price-bin before using bin mean vs overall.
MIN_BIN_RECORDS: int = 5

# Bin-based calibration uses 10 equal-width bins over [0, 1].
NUM_BINS: int = 10
BIN_WIDTH: float = 1.0 / NUM_BINS

# Edge threshold: per PLAN.md Section 5 / GEMINI.md KEY THRESHOLDS
MIN_EDGE_CENTS: float = config.MIN_EDGE_CENTS  # 0.07


# ---------------------------------------------------------------------------
# CalibrationModel
# ---------------------------------------------------------------------------


class CalibrationModel:
    """
    Per-category probability calibration model backed by Supabase closed_trades.

    The model uses a bin-based calibration approach:
        - Historical (agent_estimate, outcome) pairs are bucketed into 10
          equal-width bins of width 0.10.
        - For a given market_price, the model finds the matching bin and
          returns the empirical mean outcome for that bin.
        - If a bin has fewer than MIN_BIN_RECORDS samples, the model falls
          back to the overall mean outcome for that curve.

    Priority order for estimate():
        1. Per-category curve when category has >= MIN_CATEGORY_RECORDS.
        2. Global curve when global records >= MIN_CATEGORY_RECORDS.
        3. None → caller falls back to market_price passthrough.

    Call refresh() at startup (and optionally on a schedule) to load data.
    All Supabase reads use asyncio.wait_for(timeout=SUPABASE_TIMEOUT_SECONDS).
    """

    def __init__(self) -> None:
        """Initialise with empty curves. Call refresh() to populate."""
        self._category_records: dict[str, list[dict]] = {
            cat: [] for cat in CATEGORIES
        }
        self._global_records: list[dict] = []
        self._loaded: bool = False
        self._last_refreshed_at: Optional[float] = None  # UNIX timestamp

    # ------------------------------------------------------------------
    # Data loading
    # ------------------------------------------------------------------

    async def refresh(self) -> None:
        """
        Load calibration data from Supabase closed_trades with a 2-second timeout.

        Fetches all rows that have non-null agent_estimate and outcome fields.
        Populates per-category and global calibration curves.

        On timeout or any Supabase error: logs a warning and sets self._loaded =
        False, leaving prior curves intact.  The model continues to operate via
        market-price passthrough until a successful refresh completes.
        """

        async def _fetch() -> list[dict]:
            """Inner coroutine — wrapped by wait_for for timeout enforcement."""
            client = await get_client()
            result = (
                client.table("closed_trades")
                .select("category, agent_estimate, outcome")
                .not_.is_("agent_estimate", "null")
                .not_.is_("outcome", "null")
                .execute()
            )
            return result.data or []

        try:
            records: list[dict] = await asyncio.wait_for(
                _fetch(), timeout=config.SUPABASE_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            logger.warning(
                "[CALIBRATION] Supabase timeout after %.0fs loading calibration data. "
                "Model will use market-price passthrough until next successful refresh.",
                config.SUPABASE_TIMEOUT_SECONDS,
            )
            self._loaded = False
            return
        except Exception as exc:
            logger.error(
                "[CALIBRATION] Failed to load calibration data from Supabase: %s",
                exc,
            )
            self._loaded = False
            return

        # Reset curves before repopulating so stale data never lingers.
        self._category_records = {cat: [] for cat in CATEGORIES}
        self._global_records = []

        skipped = 0
        for row in records:
            category: str = row.get("category", "")
            outcome_str: str = row.get("outcome", "")
            agent_est_raw = row.get("agent_estimate")

            if agent_est_raw is None or outcome_str not in ("win", "loss"):
                skipped += 1
                continue

            outcome: int = 1 if outcome_str == "win" else 0
            record: dict = {"estimate": float(agent_est_raw), "outcome": outcome}

            self._global_records.append(record)
            if category in self._category_records:
                self._category_records[category].append(record)

        self._loaded = True
        self._last_refreshed_at = time.monotonic()
        logger.info(
            "[CALIBRATION] Loaded %d records (%d skipped). "
            "Per-category counts: %s",
            len(self._global_records),
            skipped,
            {k: len(v) for k, v in self._category_records.items()},
        )

    # ------------------------------------------------------------------
    # Internal calibration
    # ------------------------------------------------------------------

    def _bin_calibrate(self, records: list[dict], market_price: float) -> float:
        """
        Apply bin-based calibration to estimate the true probability for market_price.

        Finds all historical records whose agent_estimate falls in the same
        0.10-wide bin as market_price.  Returns the empirical mean outcome for
        that bin.  Falls back to the overall mean of the supplied records when
        the matching bin has fewer than MIN_BIN_RECORDS samples.

        This is a lightweight version of Platt scaling that requires no
        external ML libraries and executes in O(n) time.

        Args:
            records:      List of {'estimate': float, 'outcome': int} dicts.
            market_price: Current market implied probability.

        Returns:
            Calibrated probability estimate in [0.0, 1.0].
            Returns market_price unchanged if records is empty.
        """
        if not records:
            return market_price

        bin_idx: int = min(int(market_price / BIN_WIDTH), NUM_BINS - 1)

        bin_records = [
            r
            for r in records
            if min(int(r["estimate"] / BIN_WIDTH), NUM_BINS - 1) == bin_idx
        ]

        if len(bin_records) >= MIN_BIN_RECORDS:
            return sum(r["outcome"] for r in bin_records) / len(bin_records)

        # Bin is sparse — use the overall mean of this curve as a conservative
        # fallback rather than trusting a tiny sample.
        return sum(r["outcome"] for r in records) / len(records)

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    def records_count(self, category: str) -> int:
        """Return the number of resolved trades loaded for a given category."""
        return len(self._category_records.get(category, []))

    def has_category_data(self, category: str) -> bool:
        """Return True if category has at least MIN_CATEGORY_RECORDS resolved trades."""
        return self.records_count(category) >= MIN_CATEGORY_RECORDS

    def has_global_data(self) -> bool:
        """Return True if the global record pool meets the minimum threshold."""
        return len(self._global_records) >= MIN_CATEGORY_RECORDS

    def is_stale(self, ttl_seconds: float = 3600.0) -> bool:
        """
        Return True if the model has never been loaded, or if the last successful
        refresh happened more than ttl_seconds ago.

        Default TTL: 1 hour.  The caller (e.g. main.py) should schedule periodic
        refresh() calls — this method lets it decide when to do so.

        Args:
            ttl_seconds: Age in seconds before the model is considered stale.

        Returns:
            True if stale or never loaded, False if fresh.
        """
        if self._last_refreshed_at is None:
            return True
        return (time.monotonic() - self._last_refreshed_at) > ttl_seconds

    def estimate(self, category: str, market_price: float) -> Optional[float]:
        """
        Return a calibration-adjusted probability estimate for market_price.

        Priority:
            1. Category-specific curve if category has >= MIN_CATEGORY_RECORDS.
            2. Global curve if global pool has >= MIN_CATEGORY_RECORDS.
            3. None — caller should fall back to market_price passthrough.

        Args:
            category:     Market category string.
            market_price: Current market implied probability.

        Returns:
            Float probability estimate, or None if data is insufficient.
        """
        if category in self._category_records and self.has_category_data(category):
            return self._bin_calibrate(
                self._category_records[category], market_price
            )

        if self.has_global_data():
            return self._bin_calibrate(self._global_records, market_price)

        return None


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_model: Optional[CalibrationModel] = None


def get_model() -> CalibrationModel:
    """Return the shared CalibrationModel singleton, creating it on first call."""
    global _model
    if _model is None:
        _model = CalibrationModel()
    return _model


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------


def compute_brier_score(
    predictions: list[float],
    outcomes: list[int],
) -> float:
    """
    Compute the Brier score for a set of probability predictions and binary outcomes.

    Formula: mean([(p - o)^2 for p, o in zip(predictions, outcomes)])

    The Brier score measures the mean squared error between predicted
    probabilities and actual binary outcomes.  Lower is better.
    Target for a well-calibrated model: < 0.23 (per PLAN.md Section 12).

    This function is pure Python — no external dependencies.

    Args:
        predictions: Probability estimates in [0.0, 1.0].
        outcomes:    Binary outcomes (1 = event occurred, 0 = did not occur).
                     Must be the same length as predictions.

    Returns:
        Brier score as a float.  Returns 0.0 for empty input.

    Raises:
        ValueError: If predictions and outcomes have different lengths.
                    Silently truncating mismatched pairs would produce a
                    wrong score with no indication to the caller — which is
                    far worse than a loud failure here.

    Example:
        compute_brier_score([0.9, 0.1, 0.8, 0.2], [1, 0, 1, 0])
        # = mean([0.01, 0.01, 0.04, 0.04]) = 0.025
    """
    if not predictions or not outcomes:
        return 0.0
    if len(predictions) != len(outcomes):
        raise ValueError(
            f"compute_brier_score: predictions length ({len(predictions)}) "
            f"!= outcomes length ({len(outcomes)}). "
            "Both lists must have the same number of elements."
        )
    n: int = len(predictions)
    total: float = sum(
        (p - float(o)) ** 2 for p, o in zip(predictions, outcomes)
    )
    return total / n


def compute_edge(agent_estimate: float, market_price: float) -> float:
    """
    Compute the absolute edge between the agent's estimate and the market price.

    Edge = |agent_estimate - market_price|

    This is the raw probability distance and is compared against MIN_EDGE_CENTS
    (0.07) to gate trade entry.

    Args:
        agent_estimate: Agent's estimated probability of YES resolution.
        market_price:   Current market implied probability.

    Returns:
        Non-negative float edge.  Example: compute_edge(0.62, 0.51) → 0.11
    """
    return abs(agent_estimate - market_price)


def is_mispriced(agent_estimate: float, market_price: float) -> bool:
    """
    Return True if the market is considered mispriced and tradeable.

    A market is mispriced when:
        |agent_estimate - market_price| > MIN_EDGE_CENTS (0.07)

    Strict greater-than is used: a market at exactly 0.07 edge is NOT flagged.
    This matches the PLAN.md Section 5 / GEMINI.md KEY THRESHOLDS definition.

    Args:
        agent_estimate: Agent's estimated probability.
        market_price:   Current market implied probability.

    Returns:
        True if edge strictly exceeds 0.07, False otherwise.
    """
    return compute_edge(agent_estimate, market_price) > MIN_EDGE_CENTS


def get_category_estimate(
    category: str,
    market_price: float,
    signals: dict[str, Any],
) -> float:
    """
    Return the agent's calibration-adjusted probability estimate for a market.

    Uses the CalibrationModel singleton.  Applies per-category calibration when
    the category has >= MIN_CATEGORY_RECORDS resolved trades.  Falls back to the
    global curve when a category is data-sparse.  When neither curve has enough
    data, returns market_price (no adjustment — we don't know better).

    For unknown/unsupported categories:
        Returns min(market_price, 0.50) — capping confidence at 0.50 to
        acknowledge maximum epistemic uncertainty for unrecognised domain.
        Logs the fallback so it is always auditable.

    All calibration model reads are synchronous (model is pre-populated via
    CalibrationModel.refresh() which is called asynchronously at startup).

    Args:
        category:     Market category string ('politics', 'crypto', etc.).
        market_price: Current market implied probability.
        signals:      Additional signal context (reserved for future enrichment).

    Returns:
        Float probability estimate in [0.0, 1.0].
        Unknown categories always return <= 0.50.
    """
    model = get_model()

    if category in CATEGORIES:
        calibrated: Optional[float] = model.estimate(category, market_price)

        if calibrated is not None:
            logger.debug(
                "[CALIBRATION] category='%s' market_price=%.4f → calibrated=%.4f",
                category,
                market_price,
                calibrated,
            )
            return calibrated

        # Insufficient data in both category and global curves.
        # Use the public records_count() method — never access private attrs
        # from outside the class.
        logger.info(
            "[CALIBRATION] Insufficient data for category='%s' "
            "(have %d, need %d). Returning market_price passthrough (%.4f).",
            category,
            model.records_count(category),
            MIN_CATEGORY_RECORDS,
            market_price,
        )
        return market_price

    # Unknown category — enforce conservative fallback.
    conservative: float = min(market_price, 0.50)
    logger.info(
        "[CALIBRATION] Unknown category='%s'. Conservative fallback applied: "
        "min(%.4f, 0.50) = %.4f. Confidence capped at 0.50.",
        category,
        market_price,
        conservative,
    )
    return conservative
