"""
coordinator/exposure.py — Part B Step 6 (B6): the ONE portfolio-exposure
fetcher, shared by both entry paths (D-B21).

It replaces two fetchers that agreed on nothing: the pipeline's
fetch_open_positions_exposure summed every open position and called the
total "correlated" exposure (E-B6 — the 20% cap was really a second
total-exposure cap), and the Class A executor's _fetch_exposure divided by
the frozen PAPER_TRADING_PORTFOLIO_USDC constant so its math never saw
P&L. Both are gone; every consumer reads the same numbers from here.

What the gate is fed now (D-B23): `cluster_pct` is the MARKED value of the
cluster the PROPOSED trade joins (its own event_key), as a fraction of
marked portfolio equity — the proposed trade is added by the caller
(+ proposed_pct), never pre-counted. Marks come from the Step 1 equity
snapshot (equity.marked_by_key — the snapshot already marks every position
each tick; retaining them costs the entry path zero I/O). Rows the
snapshot does not know (opened after the last tick) count at COST basis —
a just-opened position's mark is its cost, the conservative answer. NULL
event_key rows are singleton clusters keyed by market_id
(equity.cluster_key is the single keying rule).

`category_pct` and `open_value_usdc` keep their pre-Step-6 semantics
(cost-basis sums) so the category cap and cash-reserve gate behave exactly
as before.

This module performs I/O. It must NEVER be imported by risk/risk_engine.py
(purity rule).
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Optional

import config
from coordinator import equity
from memory.supabase_client import get_client
from monitoring.telegram_alerts import alert_supabase_degradation

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExposureSnapshot:
    """One exposure read for the entry gates.

    category_pct    — Σ open cost in `category` / marked equity (0.0-1.0+)
    cluster_pct     — Σ MARKED value of the proposed trade's event cluster /
                      marked equity (the proposed trade itself NOT included)
    open_value_usdc — Σ open cost across all positions (cash-reserve input)
    """

    category_pct: float
    cluster_pct: float
    open_value_usdc: float


async def fetch_portfolio_exposure(
    category: str, proposed_event_key: Optional[str]
) -> ExposureSnapshot:
    """Read the exposure inputs for one entry evaluation under a 2-second
    timeout. If the database read times out or fails: HALT trading (the
    caller's gates fail closed).

    One Supabase read per call — the entry-path call-order ordinals treat
    this as the exposure read (Call 7 in the documented sequence), and the
    portfolio denominator arrives warm from the Step 1 equity cache, so the
    read count is identical to the pre-Step-6 fetchers."""
    async def _read() -> list[dict[str, Any]]:
        # to_thread: supabase-py execute() is blocking HTTP — asyncio.timeout
        # can only cancel an awaitable, never a direct call (the strict
        # pattern lease/recon use; the loose direct-call pattern is a
        # recorded repo-wide noticed issue).
        client = await get_client()
        builder = client.table("open_positions").select(
            "position_size_usdc,category,event_key"
        )
        res = await asyncio.to_thread(builder.execute)
        return res.data or []

    try:
        async with asyncio.timeout(config.SUPABASE_TIMEOUT_SECONDS):
            rows = await _read()

        total_portfolio = await equity.get_current_portfolio_value()
        cat_total = 0.0
        open_total = 0.0
        cluster_value = _cluster_value(rows, proposed_event_key)
        for row in rows:
            cost = float(row.get("position_size_usdc") or 0.0)
            open_total += cost
            if row.get("category") == category:
                cat_total += cost

        return ExposureSnapshot(
            category_pct=cat_total / total_portfolio,
            cluster_pct=cluster_value / total_portfolio,
            open_value_usdc=open_total,
        )
    except asyncio.TimeoutError as e:
        logger.critical("Supabase open_positions read timed out! Halting new trades (RULE 5).")
        asyncio.create_task(
            alert_supabase_degradation("open_positions", "HALTING new trades - database timeout")
        )
        raise RuntimeError("Trading halted due to open_positions read timeout") from e
    except Exception as e:
        logger.critical("open_positions exposure check failed: %s. Halting.", e)
        raise RuntimeError("Trading halted due to exposure check failure") from e


def _cluster_value(
    rows: list[dict[str, Any]], proposed_event_key: Optional[str]
) -> float:
    """The marked value of the cluster the PROPOSED trade joins (D-B22).

    The Step 1 snapshot's per-key sum is authoritative when it knows the
    key — one lookup, never a per-row re-sum (the snapshot already summed
    the cluster). Without a snapshot, or when it has no mark for the key
    (every row fresher than the last tick), the cluster costs what its rows
    committed — a just-opened position's mark is its cost, so this is the
    conservative answer either way."""
    if proposed_event_key is None:
        return 0.0
    snapshot = equity.get_cached_snapshot()  # cache-only: never blocks, may be None
    if snapshot is not None:
        marked = snapshot.marked_by_key.get(proposed_event_key)
        if marked is not None:
            return marked
    return sum(
        float(row.get("position_size_usdc") or 0.0)
        for row in rows
        if equity.cluster_key(row) == proposed_event_key
    )
