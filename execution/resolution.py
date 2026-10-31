"""
Market resolution predicate + Gamma reader — Part B Step 4 (B3).

The runtime reconciliation sweep must apply the SAME resolved-market
predicate the rest of the system uses — List A D-11 convention, with the
void/0.5 rejection the gate report enforces. The pure predicate here is
ported VERBATIM from scripts/gate_report.py:market_yes_outcome (the
established D-11 implementation, which itself mirrors startup
reconciliation's paper path) and extended with the direction-correct exit
price extraction from reconciliation's paper path:

  resolved := umaResolutionStatus == 'resolved' OR closed OR resolved
  exit price := outcomePrices[outcomes.index(direction)] (JSON strings handled)
  void := resolved at any price other than exactly 0.0 / 1.0 (D-B15: never
          auto-closed — a human decides; the sweep skips + warns)

Three states so callers can distinguish "checked, not resolved" (keep the
position) from "could not check" (retry next tick — never treated as data):

  RESOLVED     — cleanly resolved at 0.0/1.0; exit_price is the side price
  VOID         — resolved at a fractional price (0.5); exit_price carries it
  NOT_RESOLVED — the market is still live
  UNAVAILABLE  — the Gamma fetch failed or the payload was unusable

This module performs I/O (Gamma HTTP). It must NEVER be imported by
risk/risk_engine.py (purity rule).
"""

import asyncio
import json
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Optional

import config

logger = logging.getLogger(__name__)


class ResolutionStatus(str, Enum):
    RESOLVED = "resolved"          # cleanly resolved at 0.0 / 1.0
    VOID = "void"                  # resolved at a fractional price — never auto-close
    NOT_RESOLVED = "not_resolved"  # market still live
    UNAVAILABLE = "unavailable"    # fetch failed / payload unusable — retry later


@dataclass(frozen=True)
class MarketResolution:
    """One resolution check's outcome plus the direction-correct side price."""

    status: ResolutionStatus
    exit_price: Optional[float]    # position's own side price (None unless RESOLVED/VOID)
    yes_price: Optional[float]     # the YES-line price, for logs


def market_yes_price(market_json: dict[str, Any]) -> Optional[float]:
    """YES-line resolution price from a Gamma market payload, or None when
    unresolved/unusable.

    Resolved = umaResolutionStatus 'resolved' OR closed OR resolved (the exact
    predicate the startup reconciliation uses — ported verbatim from
    scripts/gate_report.py:market_yes_outcome, which returns the int form).
    Outcome read from the outcomePrices/outcomes pair (both may arrive as
    JSON strings). No void filtering here — callers decide (D-B15)."""
    is_resolved = (
        market_json.get("umaResolutionStatus") == "resolved"
        or market_json.get("closed") is True
        or market_json.get("resolved") is True
    )
    if not is_resolved:
        return None
    outcome_prices = market_json.get("outcomePrices")
    outcomes = market_json.get("outcomes")
    if isinstance(outcome_prices, str):
        try:
            outcome_prices = json.loads(outcome_prices)
        except json.JSONDecodeError:
            return None
    if isinstance(outcomes, str):
        try:
            outcomes = json.loads(outcomes)
        except json.JSONDecodeError:
            return None
    if not outcomes or not outcome_prices:
        return None
    try:
        idx = [str(o).strip().title() for o in outcomes].index("Yes")
        return float(outcome_prices[idx])
    except (ValueError, IndexError, TypeError):
        return None


def classify_resolution(
    market_json: dict[str, Any], direction: str
) -> MarketResolution:
    """Pure: classify one Gamma payload for one position direction."""
    yes_price = market_yes_price(market_json)
    if yes_price is None:
        return MarketResolution(ResolutionStatus.NOT_RESOLVED, None, None)
    target_dir = str(direction).strip().upper()
    if target_dir not in ("YES", "NO"):
        return MarketResolution(ResolutionStatus.VOID, None, yes_price)
    exit_price = yes_price if target_dir == "YES" else 1.0 - yes_price
    if yes_price not in (0.0, 1.0):
        # Resolved at a fractional price (0.5 void) — surfaced, never closed.
        return MarketResolution(ResolutionStatus.VOID, exit_price, yes_price)
    return MarketResolution(ResolutionStatus.RESOLVED, exit_price, yes_price)


async def _default_transport(url: str, timeout: float) -> Any:
    """One-shot Gamma GET (mirrors reconciliation's paper-path fetcher)."""
    import httpx

    async with httpx.AsyncClient() as client_http:
        return await client_http.get(url, timeout=timeout)


async def fetch_market_resolution(
    market_id: str,
    direction: str,
    transport: Optional[Callable[..., Any]] = None,
) -> MarketResolution:
    """Fetch one market from Gamma and classify it for `direction`.

    UNAVAILABLE on any fetch/timeout/payload failure (retry next tick — never
    treated as 'not resolved'). Tests inject a fake transport; production uses
    the httpx default."""
    try:
        async with asyncio.timeout(config.POLYMARKET_API_TIMEOUT_SECONDS):
            fetch = transport or _default_transport
            url = f"https://gamma-api.polymarket.com/markets/{market_id}"
            res = await fetch(url, config.POLYMARKET_API_TIMEOUT_SECONDS)
            if res.status_code != 200:
                raise RuntimeError(f"Gamma API status {res.status_code}")
            market = res.json()
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.warning(
            "[RESOLUTION] Gamma fetch failed for %s (retry next tick): %s: %s",
            market_id[:16], type(e).__name__, e,
        )
        return MarketResolution(ResolutionStatus.UNAVAILABLE, None, None)
    return classify_resolution(market, direction)
