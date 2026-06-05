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
    """Gate on minimum edge (in decimal cents) required to enter a trade.

    Edge = |agent_estimate - market_price|.
    Must exceed config.MIN_EDGE_CENTS (0.07 = 7 cents).

    Args:
        agent_estimate: Agent's probability estimate for the market (0.0-1.0).
        market_price: Current Polymarket price for the outcome (0.0-1.0).

    Returns:
        'BLOCK' if edge is below the minimum threshold, else 'ALLOW'.
    """
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
