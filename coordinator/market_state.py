"""
coordinator/market_state.py — Shared per-market state loading and lock registry.

Provides:
  - MarketState: Dataclass tracking open tranches, committed USDC, and opposite direction.
  - get_market_lock: Process-wide asyncio.Lock keyed strictly on market_id (never direction).
  - load_market_state: Supabase reader with asyncio.to_thread and 2-second timeout, failing closed.
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional, Dict

import config
from memory import supabase_client

logger = logging.getLogger(__name__)

# Process-wide lock registry keyed strictly on market_id
_MARKET_LOCKS: Dict[str, asyncio.Lock] = {}


@dataclass(frozen=True)
class MarketState:
    """Snapshot of open positions for a given market."""
    existing_usdc: float
    tranches: int
    opposite_direction_open: bool
    last_entry_price: Optional[float] = None
    last_opened_at: Optional[str] = None


def get_market_lock(market_id: str) -> asyncio.Lock:
    """Return the shared asyncio.Lock for a market_id. Keyed strictly on market_id alone."""
    if market_id not in _MARKET_LOCKS:
        _MARKET_LOCKS[market_id] = asyncio.Lock()
    return _MARKET_LOCKS[market_id]


def reset_market_locks() -> None:
    """Reset the market lock registry. Used for test isolation between event loops."""
    _MARKET_LOCKS.clear()


async def load_market_state(market_id: str, direction: str) -> Optional[MarketState]:
    """Load current exposure state for a market_id from open_positions table.

    Runs synchronous query in asyncio.to_thread under a strict 2-second timeout.
    Fails closed (returns None) on timeout or any query error.

    Args:
        market_id: Polymarket condition or market identifier.
        direction: Target trade direction ('YES' or 'NO').

    Returns:
        MarketState snapshot or None if reading fails / times out.
    """
    target_dir = direction.upper()
    opp_dir = "NO" if target_dir == "YES" else "YES"

    try:
        client = await supabase_client.get_client()

        def _query():
            return (
                client.table("open_positions")
                .select("direction,position_size_usdc,entry_price,opened_at")
                .eq("market_id", market_id)
                .execute()
            )

        res = await asyncio.wait_for(
            asyncio.to_thread(_query),
            timeout=config.SUPABASE_TIMEOUT_SECONDS,
        )
        rows = res.data or []

        existing_usdc = 0.0
        tranches = 0
        opposite_open = False
        last_entry_price: Optional[float] = None
        last_opened_at: Optional[str] = None

        for r in rows:
            r_dir = str(r.get("direction", "")).upper()
            if r_dir == target_dir:
                tranches += 1
                size = float(r.get("position_size_usdc") or 0.0)
                existing_usdc += size
                last_entry_price = float(r.get("entry_price")) if r.get("entry_price") is not None else None
                last_opened_at = r.get("opened_at")
            elif r_dir == opp_dir:
                opposite_open = True

        return MarketState(
            existing_usdc=existing_usdc,
            tranches=tranches,
            opposite_direction_open=opposite_open,
            last_entry_price=last_entry_price,
            last_opened_at=last_opened_at,
        )

    except asyncio.TimeoutError as e:
        logger.warning(
            "[MARKET_STATE] read failed — blocking: Supabase timeout after %ss on market %s",
            config.SUPABASE_TIMEOUT_SECONDS,
            market_id,
        )
        return None
    except Exception as e:
        logger.warning(
            "[MARKET_STATE] read failed — blocking: %s: %s on market %s",
            type(e).__name__,
            e,
            market_id,
        )
        return None
