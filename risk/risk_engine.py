"""
risk_engine.py — Pure Python risk controls for the Polymarket trading agent.

ABSOLUTE RULES (enforced by risk/GEMINI.md):
  - Zero LLM calls.
  - Zero imports from /llm/.
  - Zero external API calls.
  - All thresholds sourced from config — never hardcoded here.
  - Every function is deterministic and executes in under 1ms.
"""

import math
import decimal
from decimal import Decimal
import datetime
from datetime import datetime, timezone
import logging
import config

logger = logging.getLogger(__name__)

# One-shot deprecation flag (List A.md Step 1): gross edge is retired as a metric.
_check_edge_deprecation_warned = False


def kelly_size(
    win_probability: float,
    odds: float,
    kelly_fraction: float,
    portfolio_value: float,
) -> float:
    """Compute fractional Kelly position size in USDC.

    Formula: f_full = (odds * p - (1 - p)) / odds
    Applied: f_fractional = f_full * kelly_fraction
    Position:  f_fractional * portfolio_value

    Args:
        win_probability: Agent's estimated probability of winning (0.0-1.0).
        odds: Decimal odds (1.0 for binary Polymarket markets = even-money).
        kelly_fraction: Fractional Kelly multiplier (e.g. 0.15 for velocity).
        portfolio_value: Total portfolio value in USDC.

    Returns:
        Recommended position size in USDC.
    """
    f_full = (odds * win_probability - (1.0 - win_probability)) / odds
    return f_full * kelly_fraction * portfolio_value


def position_size_check(
    proposed_size: float,
    portfolio_value: float,
    strategy: str,
) -> float:
    """Apply the hard position-size cap and return the permitted size.

    Resolution-edge strategy gets config.MAX_RESOLUTION_TRADE_PCT cap (8%).
    All other strategies get config.MAX_SINGLE_TRADE_PCT cap (5%).

    Args:
        proposed_size: Kelly-computed or proposed position size in USDC.
        portfolio_value: Total portfolio value in USDC.
        strategy: Strategy name; 'resolution' triggers the higher 8% cap.

    Returns:
        Permitted position size in USDC (may be reduced from proposed_size).
    """
    if strategy == "resolution":
        cap = config.MAX_RESOLUTION_TRADE_PCT
    else:
        cap = config.MAX_SINGLE_TRADE_PCT
    return min(proposed_size, portfolio_value * cap)


def check_drawdown(
    starting_balance: float,
    current_balance: float,
    period: str,
) -> str:
    """Check whether a drawdown circuit breaker should fire.

    Monthly shutdown is checked before weekly halt to ensure the stronger
    signal takes precedence when both thresholds are breached.

    Args:
        starting_balance: Balance at the start of the period in USDC.
        current_balance: Current balance in USDC.
        period: 'daily', 'weekly', or 'monthly'.

    Returns:
        'SHUTDOWN' | 'HALT' | 'CONTINUE'
    """
    drawdown = (starting_balance - current_balance) / starting_balance
    if period == "monthly" and drawdown > config.MONTHLY_DRAWDOWN_SHUTDOWN_PCT:
        logger.warning(
            "[RISK_ENGINE] Monthly drawdown circuit breaker: %.2f%% — SHUTDOWN",
            drawdown * 100,
        )
        return "SHUTDOWN"
    if period == "weekly" and drawdown > config.WEEKLY_DRAWDOWN_HALT_PCT:
        logger.warning(
            "[RISK_ENGINE] Weekly drawdown circuit breaker: %.2f%% — HALT",
            drawdown * 100,
        )
        return "HALT"
    if period == "daily" and drawdown > config.DAILY_DRAWDOWN_HALT_PCT:
        logger.warning(
            "[RISK_ENGINE] Daily drawdown circuit breaker: %.2f%% — HALT",
            drawdown * 100,
        )
        return "HALT"
    return "CONTINUE"


def check_liquidity(
    available_liquidity: float,
    current_market_liquidity: float,
) -> str:
    """Check whether liquidity conditions permit or require an exit.

    Two independent checks (checked in priority order):
      1. Auto-exit: current_market_liquidity below $3,000 floor → EXIT_NOW.
      2. Entry block: available_liquidity below $5,000 minimum → BLOCK.

    Args:
        available_liquidity: Liquidity available at the target price in USDC.
        current_market_liquidity: Total current market liquidity in USDC.

    Returns:
        'EXIT_NOW' | 'BLOCK' | 'ALLOW'
    """
    if current_market_liquidity < config.AUTO_EXIT_LIQUIDITY_FLOOR_USDC:
        logger.warning(
            "[RISK_ENGINE] Market liquidity $%.0f below auto-exit floor — EXIT_NOW",
            current_market_liquidity,
        )
        return "EXIT_NOW"
    if available_liquidity < config.MIN_MARKET_LIQUIDITY_USDC:
        logger.warning(
            "[RISK_ENGINE] Available liquidity $%.0f below minimum — BLOCK",
            available_liquidity,
        )
        return "BLOCK"
    return "ALLOW"


def apply_confidence_ceiling(
    confidence: float,
) -> float:
    """Clamp confidence to the hard ceiling enforcing epistemic humility.

    Any model output above config.CONFIDENCE_CEILING (0.88) is silently
    clamped. Values at or below the ceiling are returned unchanged.

    Args:
        confidence: Raw confidence score from an LLM agent (0.0-1.0).

    Returns:
        Confidence score clamped to at most config.CONFIDENCE_CEILING.
    """
    return min(confidence, config.CONFIDENCE_CEILING)


def check_min_confidence(
    confidence: float,
) -> str:
    """Gate on minimum confidence required to enter any trade.

    Args:
        confidence: Post-ceiling confidence score (0.0-1.0).

    Returns:
        'BLOCK' if confidence is below config.MIN_CONFIDENCE_THRESHOLD, else 'ALLOW'.
    """
    if confidence < config.MIN_CONFIDENCE_THRESHOLD:
        logger.info(
            "[RISK_ENGINE] Confidence %.3f below minimum %.2f — BLOCK",
            confidence,
            config.MIN_CONFIDENCE_THRESHOLD,
        )
        return "BLOCK"
    return "ALLOW"


def check_edge(
    agent_estimate: float,
    market_price: float,
) -> str:
    """DEPRECATED (List A.md Step 1) — do not call from live paths.

    Gross edge is retired as a metric: this gate is symmetric, direction-blind and
    cost-blind (no fees, spread or slippage). Use risk.cost_model.check_net_edge
    with a live BookSnapshot instead. Kept for one release for backwards
    compatibility with existing tests; logs a one-time deprecation warning.
    """
    global _check_edge_deprecation_warned
    if not _check_edge_deprecation_warned:
        logger.warning(
            "[RISK_ENGINE] check_edge is DEPRECATED (gross edge retired, List A.md "
            "Step 1) — migrate callers to risk.cost_model.check_net_edge."
        )
        _check_edge_deprecation_warned = True
    edge = abs(agent_estimate - market_price)
    if edge < config.MIN_EDGE_CENTS:
        logger.info(
            "[RISK_ENGINE] Edge %.4f below minimum %.2f — BLOCK",
            edge,
            config.MIN_EDGE_CENTS,
        )
        return "BLOCK"
    return "ALLOW"


def check_category_exposure(
    current_exposure_pct: float,
    proposed_trade_pct: float,
) -> str:
    """Gate on maximum category exposure cap.

    Prevents any single category from exceeding config.MAX_CATEGORY_EXPOSURE_PCT
    (30%) of the portfolio.

    Args:
        current_exposure_pct: Current portfolio % in this category (0.0-1.0).
        proposed_trade_pct: Proposed trade size as % of portfolio (0.0-1.0).

    Returns:
        'BLOCK' if combined exposure would exceed cap, else 'ALLOW'.
    """
    if current_exposure_pct + proposed_trade_pct > config.MAX_CATEGORY_EXPOSURE_PCT:
        logger.warning(
            "[RISK_ENGINE] Category exposure %.1f%% + %.1f%% > %.0f%% cap — BLOCK",
            current_exposure_pct * 100,
            proposed_trade_pct * 100,
            config.MAX_CATEGORY_EXPOSURE_PCT * 100,
        )
        return "BLOCK"
    return "ALLOW"


def check_correlation_exposure(
    correlated_exposure_pct: float,
) -> str:
    """Gate on maximum correlated exposure cap.

    Prevents total portfolio exposure that would be affected by a common
    shock event from exceeding config.MAX_CORRELATED_EXPOSURE_PCT (20%).

    Args:
        correlated_exposure_pct: Total correlated portfolio exposure (0.0-1.0).

    Returns:
        'BLOCK' if exposure exceeds cap, else 'ALLOW'.
    """
    if correlated_exposure_pct > config.MAX_CORRELATED_EXPOSURE_PCT:
        logger.warning(
            "[RISK_ENGINE] Correlated exposure %.1f%% > %.0f%% cap — BLOCK",
            correlated_exposure_pct * 100,
            config.MAX_CORRELATED_EXPOSURE_PCT * 100,
        )
        return "BLOCK"
    return "ALLOW"


def market_position_check(
    existing_market_usdc: float,
    existing_tranches: int,
    proposed_size: float,
    portfolio_value: float,
    confidence: float,
) -> float:
    """Bound cumulative exposure on a single (market_id, direction).

    Idempotency only prevents resubmitting the same order UUID; it cannot stop
    several distinct signals each opening a position on one market. This gate
    permits a first entry plus exactly one small, high-confidence repeat, and
    blocks everything after that.

    The opposite-direction check is the caller's responsibility — it needs the
    direction strings, which this pure function never sees. Tranche count and
    confidence are enforced here.

    Args:
        existing_market_usdc: Capital already committed to this market in this
            direction, summed from open_positions.
        existing_tranches: Number of open entries on this (market_id, direction).
        proposed_size: Incoming order size in USDC, already capped by
            position_size_check for this strategy.
        portfolio_value: Total portfolio value in USDC.
        confidence: Confidence for THIS signal, already clamped by
            apply_confidence_ceiling.

    Returns:
        Permitted incremental size in USDC. 0.0 means block the order.
    """
    # Note: Ceiling is evaluated per (market_id, direction). If the opposite_direction_open
    # block is ever relaxed, this scope must be revisited.
    ceiling = portfolio_value * config.MAX_MARKET_TRADE_PCT

    if existing_market_usdc >= ceiling:
        # Legitimate for a resolution-strategy first entry (8% cap == ceiling).
        logger.info(
            "[RISK_ENGINE] Cumulative market exposure %.2f at/over %.2f ceiling — "
            "no further room on this market",
            existing_market_usdc, ceiling,
        )
        return 0.0

    if existing_tranches >= config.MAX_MARKET_TRANCHES:
        logger.info(
            "[RISK_ENGINE] Tranche count %d >= %d — BLOCK",
            existing_tranches, config.MAX_MARKET_TRANCHES,
        )
        return 0.0

    if existing_tranches == 1:
        # NaN-safe: any non-comparable confidence fails closed.
        if not (confidence >= config.REPEAT_MIN_CONFIDENCE):
            logger.info(
                "[RISK_ENGINE] Repeat blocked: confidence %r below %.4f",
                confidence, config.REPEAT_MIN_CONFIDENCE,
            )
            return 0.0
        permitted = min(
            proposed_size,
            portfolio_value * config.REPEAT_ENTRY_PCT,
            ceiling - existing_market_usdc,
        )
    else:
        permitted = min(proposed_size, ceiling - existing_market_usdc)

    if existing_tranches >= 1 and permitted < config.MIN_ADD_TICKET_USDC:
        # Floor applies to REPEAT attempts only. Applying it to a first entry
        # would block every Class A copy-trade (ceiling is $10).
        logger.info(
            "[RISK_ENGINE] Permitted %.2f below %.2f floor — BLOCK",
            permitted, config.MIN_ADD_TICKET_USDC,
        )
        return 0.0

    return max(0.0, permitted)


def compute_health_score(
    win_rate_score: float,
    brier_score_score: float,
    slippage_score: float,
    feed_latency_score: float,
    drawdown_score: float,
    correlation_score: float,
) -> float:
    """Compute the composite health score from six equally-weighted components.

    Components (all on 0-100 scale, weighted equally):
      1. win_rate_score     — Recent win rate over last 20 trades
      2. brier_score_score  — Rolling 30-day Brier score
      3. slippage_score     — Average slippage vs expected
      4. feed_latency_score — Data feed latency
      5. drawdown_score     — Drawdown trend
      6. correlation_score  — Strategy correlation

    Args:
        win_rate_score: Win-rate component score (0-100).
        brier_score_score: Brier-score component score (0-100).
        slippage_score: Slippage component score (0-100).
        feed_latency_score: Feed-latency component score (0-100).
        drawdown_score: Drawdown-trend component score (0-100).
        correlation_score: Strategy-correlation component score (0-100).

    Returns:
        Composite health score (0-100).
    """
    return sum(
        [
            win_rate_score,
            brier_score_score,
            slippage_score,
            feed_latency_score,
            drawdown_score,
            correlation_score,
        ]
    ) / 6


def interpret_health_score(
    health_score: float,
) -> str:
    """Map a numeric health score to an operational mode.

    Thresholds from config:
      < config.HEALTH_SCORE_HALT_THRESHOLD (40)      → FULL_HALT
      < config.HEALTH_SCORE_DEFENSIVE_THRESHOLD (65) → DEFENSIVE_MODE
      >= 65                                           → NORMAL

    Args:
        health_score: Composite health score (0-100).

    Returns:
        'FULL_HALT' | 'DEFENSIVE_MODE' | 'NORMAL'
    """
    if health_score < config.HEALTH_SCORE_HALT_THRESHOLD:
        logger.warning(
            "[RISK_ENGINE] Health score %.1f below halt threshold — FULL_HALT",
            health_score,
        )
        return "FULL_HALT"
    if health_score < config.HEALTH_SCORE_DEFENSIVE_THRESHOLD:
        logger.warning(
            "[RISK_ENGINE] Health score %.1f below defensive threshold — DEFENSIVE_MODE",
            health_score,
        )
        return "DEFENSIVE_MODE"
    return "NORMAL"


def check_cash_reserve(
    proposed_size: float,
    available_cash: float,
    portfolio_value: float,
) -> str:
    """Check if the proposed trade size would violate the minimum cash reserve requirement.

    Args:
        proposed_size: Proposed trade size in USDC.
        available_cash: Current available cash (USDC balance) in wallet.
        portfolio_value: Total portfolio value (cash + positions value) in USDC.

    Returns:
        'BLOCK' if the trade would violate the 20% cash reserve rule, else 'ALLOW'.
    """
    reserve_limit = portfolio_value * config.MIN_CASH_RESERVE_PCT
    if (available_cash - proposed_size) < reserve_limit:
        logger.warning(
            "[RISK_ENGINE] Cash reserve violation: Cash=%.2f, Proposed=%.2f, Reserve Limit=%.2f — BLOCK",
            available_cash,
            proposed_size,
            reserve_limit,
        )
        return "BLOCK"
    return "ALLOW"


def check_deadline_risk(
    market_price: float,
    days_to_resolution: int,
) -> str:
    """Gate to block high-priced contracts close to strict resolution deadlines.

    Args:
        market_price: Price of the target contract (0.0-1.0).
        days_to_resolution: Number of days remaining until the resolution deadline.

    Returns:
        'BLOCK' if contract is high-priced and close to deadline, else 'ALLOW'.
    """
    if market_price > 0.90 and days_to_resolution < 30:
        logger.warning(
            "[RISK_ENGINE] Asymmetric deadline risk: price=%.4f, days=%d — BLOCK",
            market_price,
            days_to_resolution,
        )
        return "BLOCK"
    return "ALLOW"
