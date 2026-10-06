"""
strategies/estimator.py — A1-S: Probability estimator contract (List A.md Step 0).

Single entry point `get_estimate()` resolves a (strategy, category, market) signal to a
measured probability estimate. Every estimator is FAIL-CLOSED: when it has no data for a
cell it returns p_point=None, and the caller MUST drop the signal — the bot never trades
on a guess or a constant (List A.md ground rule 2/4).

YES-line convention: `p_point` is always the model's probability that the market resolves
YES, independent of trade side. Consumers derive the side-conditional win probability with
`side_probability()` (p for YES entries, 1−p for NO entries — Step 4 contract).

Method values:
    'recalibration_base_rate' — category × price-bin × ttr-bucket base rate (Step 6)
    'velocity_drift'          — signal_outcomes forward-price drift (Step 2 data)
    'resolution_analog'       — historical resolution analogs (Step 6)
    'copy_wallet_hitrate'     — Laplace-smoothed tracked-wallet hit rate (LIVE)
    'none'                    — no data available → caller drops the signal

No LLM calls. Supabase reads carry the standard 2-second timeout. On timeout or error the
result is fail-closed (no data), NOT a neutral guess: a failed estimate read must stop the
signal, whereas performance_tracker's fail-open 0.5 only degrades sizing.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

import config
from memory.supabase_client import get_client

logger = logging.getLogger(__name__)

# ── Method identifiers (stable strings — they appear in LLM prompts and logs) ──
METHOD_NONE = "none"
METHOD_RECALIBRATION_BASE_RATE = "recalibration_base_rate"
METHOD_VELOCITY_DRIFT = "velocity_drift"
METHOD_RESOLUTION_ANALOG = "resolution_analog"
METHOD_COPY_WALLET_HITRATE = "copy_wallet_hitrate"

# Registry strategy keys (strategy_override values / news-pipeline strategies)
STRATEGY_RECALIBRATION = "recalibration"
STRATEGY_VELOCITY = "velocity"
STRATEGY_RESOLUTION = "resolution"
STRATEGY_COPY_EDGE_CLASS_B = "copy_edge_class_b"

_SIDES: frozenset[str] = frozenset({"YES", "NO"})


# ── Estimate contract ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class EstimateResult:
    """
    Result of one estimator evaluation for a signal.

    Attributes:
        p_point:     Model probability the market resolves YES. None = "no data" →
                     the caller MUST drop the signal (never trade on a guess).
        sample_size: Number of observations behind the estimate. 0 exactly when
                     p_point is None.
        method:      One of the METHOD_* identifiers ('none' when p_point is None).
        computed_at: UTC timestamp of the evaluation.
    """
    p_point: Optional[float]
    sample_size: int
    method: str
    computed_at: datetime


def _no_data() -> EstimateResult:
    """Build the canonical fail-closed result (no data behind any cell)."""
    return EstimateResult(
        p_point=None,
        sample_size=0,
        method=METHOD_NONE,
        computed_at=datetime.now(timezone.utc),
    )


def _has_data(result: EstimateResult) -> bool:
    """Contract check: p_point is present exactly when there is sample evidence."""
    return result.p_point is not None and result.sample_size > 0


# ── Pure math ─────────────────────────────────────────────────────────────────

def laplace_hit_rate(wins: int, losses: int) -> float:
    """
    Laplace-smoothed hit rate: (wins + 1) / (wins + losses + 2).

    A uniform Beta(1,1) prior pulls a fresh wallet toward 0.5 and prevents
    0.0/1.0 certainty from tiny samples. Pure function — no I/O.

    Args:
        wins:   Observed winning trades (>= 0).
        losses: Observed losing trades (>= 0).

    Returns:
        Smoothed hit rate in (0.0, 1.0).

    Raises:
        ValueError: If wins or losses is negative.
    """
    if wins < 0 or losses < 0:
        raise ValueError(f"wins/losses must be non-negative, got wins={wins} losses={losses}")
    return (wins + 1) / (wins + losses + 2)


def side_probability(p_point: float, side: str) -> float:
    """
    Convert a YES-line model probability into the win probability for a trade side.

    p_side = p_point for YES entries, 1 − p_point for NO entries. This is the
    convention Step 4 (Kelly on real probability) consumes.

    Args:
        p_point: YES-line model probability in [0.0, 1.0].
        side:    "YES" or "NO".

    Returns:
        Win probability for the given side.

    Raises:
        ValueError: If side is not "YES"/"NO".
    """
    normalized = side.upper()
    if normalized == "YES":
        return p_point
    if normalized == "NO":
        return 1.0 - p_point
    raise ValueError(f"side must be 'YES' or 'NO', got '{side}'")


# ── Per-strategy estimators ───────────────────────────────────────────────────

async def _estimate_recalibration(
    category: str,
    market_id: str,
    market_price: float,
    side: str,
    wallet_address: Optional[str],
) -> EstimateResult:
    """
    Recalibration estimator — STUB until Step 6 (historical calibration curves).

    Deliberately fail-closed: without calibrated base rates the correct action is
    to drop the signal, not to guess (List A.md D-02/D-05).
    """
    return _no_data()


async def _estimate_velocity(
    category: str,
    market_id: str,
    market_price: float,
    side: str,
    wallet_address: Optional[str],
) -> EstimateResult:
    """
    Velocity estimator — STUB until Step 2 accumulates signal_outcomes drift data.

    Deliberately fail-closed (List A.md D-05): no velocity trades on assumption.
    """
    return _no_data()


async def _estimate_resolution(
    category: str,
    market_id: str,
    market_price: float,
    side: str,
    wallet_address: Optional[str],
) -> EstimateResult:
    """
    Resolution-analog estimator — STUB until Step 6 (historical analogs).

    Deliberately fail-closed (List A.md D-02).
    """
    return _no_data()


async def _estimate_copy_edge_class_b(
    category: str,
    market_id: str,
    market_price: float,
    side: str,
    wallet_address: Optional[str],
) -> EstimateResult:
    """
    Copy Edge Class B estimator — wallet hit rate from tracked_wallets (LIVE).

    The tracked wallet's resolved copy-trade history (wins_count/losses_count) is
    Laplace-smoothed into a hit rate: the estimated probability that a trade copied
    from this wallet resolves in its favor. The hit rate is mapped onto the YES line
    using the signal's side (YES → p_hit, NO → 1 − p_hit) so that p_point keeps the
    YES-line convention of this module.

    Fail-closed conditions (all return no-data → caller drops the signal):
        - wallet_address missing (signal cannot be attributed to a tracked wallet)
        - side not YES/NO
        - Supabase read timeout or error
        - wallet row not found
        - zero resolved history (wins + losses == 0) — no evidence, no estimate

    Returns:
        EstimateResult with method='copy_wallet_hitrate' and
        sample_size = wins + losses when data exists, else the no-data result.
    """
    if not wallet_address:
        logger.info("[ESTIMATOR] copy_edge_class_b: no wallet address on signal — no data.")
        return _no_data()
    normalized_side = side.upper() if side else ""
    if normalized_side not in _SIDES:
        logger.warning(
            "[ESTIMATOR] copy_edge_class_b: invalid side '%s' for market %s — no data.",
            side, market_id,
        )
        return _no_data()

    async def _q() -> dict:
        client = await get_client()
        res = (
            client.table("tracked_wallets")
            .select("wins_count,losses_count")
            .eq("wallet_address", wallet_address)
            .limit(1)
            .execute()
        )
        return res.data[0] if res.data else {}

    try:
        row = await asyncio.wait_for(_q(), timeout=config.SUPABASE_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        logger.error(
            "[ESTIMATOR] Supabase tracked_wallets read timed out for %s — fail-closed.",
            wallet_address[:10],
        )
        return _no_data()
    except Exception as e:
        logger.error(
            "[ESTIMATOR] Failed to read tracked_wallets for %s: %s — fail-closed.",
            wallet_address[:10], e,
        )
        return _no_data()

    if not row:
        logger.info(
            "[ESTIMATOR] copy_edge_class_b: no tracked_wallets row for %s — no data.",
            wallet_address[:10],
        )
        return _no_data()

    wins = int(row.get("wins_count", 0) or 0)
    losses = int(row.get("losses_count", 0) or 0)
    sample_size = wins + losses
    if sample_size <= 0:
        logger.info(
            "[ESTIMATOR] copy_edge_class_b: wallet %s has zero resolved history — no data.",
            wallet_address[:10],
        )
        return _no_data()

    p_hit = laplace_hit_rate(wins, losses)
    p_point = p_hit if normalized_side == "YES" else 1.0 - p_hit
    logger.info(
        "[ESTIMATOR] copy_edge_class_b wallet=%s wins=%d losses=%d p_hit=%.4f side=%s "
        "p_point=%.4f n=%d",
        wallet_address[:10], wins, losses, p_hit, normalized_side, p_point, sample_size,
    )
    return EstimateResult(
        p_point=p_point,
        sample_size=sample_size,
        method=METHOD_COPY_WALLET_HITRATE,
        computed_at=datetime.now(timezone.utc),
    )


# ── Registry + public entry point ─────────────────────────────────────────────

_EstimatorFn = Callable[[str, str, float, str, Optional[str]], Awaitable[EstimateResult]]

# One estimator per strategy. A strategy missing from this map fail-closes in
# get_estimate — adding a strategy here is the ONLY way it can trade.
_ESTIMATORS: dict[str, _EstimatorFn] = {
    STRATEGY_RECALIBRATION: _estimate_recalibration,
    STRATEGY_VELOCITY: _estimate_velocity,
    STRATEGY_RESOLUTION: _estimate_resolution,
    STRATEGY_COPY_EDGE_CLASS_B: _estimate_copy_edge_class_b,
}


async def get_estimate(
    strategy: str,
    category: str,
    market_id: str,
    market_price: float,
    side: str,
    wallet_address: Optional[str] = None,
) -> EstimateResult:
    """
    Resolve the probability estimate for one signal (fail-closed).

    Args:
        strategy:       Strategy key (e.g. 'recalibration', 'velocity',
                        'copy_edge_class_b'). Unknown keys yield no-data.
        category:       Event category from the News Analyst.
        market_id:      Polymarket market id.
        market_price:   Current YES-line market price in [0, 1].
        side:           Proposed trade direction ("YES"/"NO").
        wallet_address: Tracked-wallet address for copy strategies (optional;
                        required by copy_edge_class_b to attribute the estimate).

    Returns:
        EstimateResult — never raises. p_point is None when no estimator has data
        for this cell; the caller MUST drop the signal in that case.
    """
    estimator = _ESTIMATORS.get(strategy)
    if estimator is None:
        logger.warning(
            "[ESTIMATOR] No estimator registered for strategy '%s' (market %s) — no data.",
            strategy, market_id,
        )
        return _no_data()

    try:
        result = await estimator(category, market_id, market_price, side, wallet_address)
    except Exception as e:
        # Fail-closed: an estimator crash must never fabricate a probability.
        logger.error(
            "[ESTIMATOR] Estimator '%s' raised for market %s: %s — fail-closed.",
            strategy, market_id, e,
        )
        return _no_data()

    if not _has_data(result):
        # Enforce the None ⟺ sample_size==0 contract even on a buggy estimator.
        return _no_data()
    return result
