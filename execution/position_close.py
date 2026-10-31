"""
The ONE shared close path — Part B Step 4 (B3), decision D-B08.

Every transition of an open position into the closed_trades ledger flows
through close_position(): startup reconciliation, the runtime resolution
sweep, and (from Step 5) the exit engine's six rules. One writer, ever —
multiple close writers mean double-close races and inconsistent P&L.

Design contract:

- The Supabase client is INJECTED (explicit dependency, no get_client
  import): the caller owns connection policy. This also keeps the
  existing reconciliation test suite green unedited — its mock is
  supplied through execution.reconciliation.get_client and flows in.
- PnL math is verbatim from the extracted startup-reconciliation path:
  qty = size / entry_price; pnl = qty × exit_price − size. exit_price is
  the POSITION'S OWN side price (direction-correct extraction is the
  caller's concern — the resolution predicate hands it over).
- Write order is claim-then-record: DELETE the open_positions row first
  and inspect the returned rows (postgrest-py deletes default to
  return=representation — verified in the installed client). An empty
  result means another closer won the race → nothing is inserted (a
  double-close would double-count realized P&L, which the equity anchor
  sums — capital-adjacent). The previous insert-first order silently
  duplicated the ledger row when a retry raced a partial failure; this
  order fails toward a loudly-logged missing row instead.
- Copy-trade trust updates ride INSIDE the path (same guard and
  idempotency_uuid keying as the extracted startup block): a resolution
  is a resolution regardless of which closer performed it. Failures are
  logged loudly and never fail the close itself (verbatim behavior).
- exit_type is accepted now and logged, but persisted only when Step 5's
  `exit_type` column migration lands (no invented columns this step).
  exit_reason IS an existing column and is persisted as given.
- resolved_at is accepted for the Step 5 interface and logged; Gamma
  payloads carry no reliable resolution timestamp today.

This module performs I/O through the injected client. It must NEVER be
imported by risk/risk_engine.py (purity rule).
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import config
from copytrade import performance_tracker

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CloseResult:
    """Outcome of one close attempt through the shared path."""

    closed: bool                     # a closed_trades row was inserted
    already_closed: bool             # the open_positions row was gone — nothing written
    pnl_usdc: Optional[float]        # realized P&L (None when already closed)
    copy_trade: bool                 # a copy-trade trust update ran


async def close_position(
    client: Any,
    position_row: dict[str, Any],
    *,
    exit_price: float,
    exit_type: str,
    exit_reason: str,
    resolved_at: Optional[datetime] = None,
    notes: Optional[str] = None,
) -> CloseResult:
    """Close one open position into the ledger (the only closed_trades writer).

    Args:
        client: Supabase client (injected — caller owns connection policy).
        position_row: the open_positions row being closed.
        exit_price: the position's own side price at exit (direction-correct;
            the caller extracts it from the resolution/exit source).
        exit_type: 'resolved' for resolutions; the exit engine names its own
            types from Step 5. Logged this step; persisted with Step 5's column.
        exit_reason: persisted into closed_trades.exit_reason ('resolved' for
            resolutions).
        resolved_at: the market's resolution time when known (logged; persisted
            with Step 5's schema change).
        notes: caller context stored in closed_trades.notes (startup recon
            passes its downtime note; the runtime sweep names its own).

    Returns:
        CloseResult. already_closed=True means the row was gone before this
        call's delete — the position was closed by someone else and NOTHING
        is written (no ledger row, no trust update, no double P&L)."""
    entry_price = float(position_row["entry_price"])
    size_usdc = float(position_row["position_size_usdc"])
    qty = size_usdc / entry_price

    exit_value = qty * exit_price
    pnl = exit_value - size_usdc
    pnl_pct = (pnl / size_usdc) * 100.0 if size_usdc > 0 else 0.0

    market_id = str(position_row.get("market_id") or "")

    # ── Claim: delete the open row first (return=representation) ──────────────
    async def _claim() -> list[dict[str, Any]]:
        builder = client.table("open_positions").delete().eq("id", position_row["id"])
        res = await asyncio.to_thread(builder.execute)
        return list(getattr(res, "data", None) or [])

    try:
        async with asyncio.timeout(config.SUPABASE_TIMEOUT_SECONDS):
            claimed = await _claim()
    except asyncio.CancelledError:
        raise
    except Exception as e:
        # The claim itself failed: the row is untouched, nothing recorded —
        # a later pass retries cleanly. Loud, fail-visible, no partial state.
        logger.error(
            "[CLOSE] Claim delete failed for %s (id=%s): %s: %s — nothing written",
            market_id[:16], position_row.get("id"), type(e).__name__, e,
        )
        raise

    if not claimed:
        # Another closer won: no ledger row, no trust update, no double P&L.
        logger.warning(
            "[CLOSE] Position %s (id=%s) already closed by another pass — nothing written",
            market_id[:16], position_row.get("id"),
        )
        return CloseResult(closed=False, already_closed=True, pnl_usdc=None, copy_trade=False)

    # ── Record: closed_trades insert (verbatim columns from the extracted path)
    outcome = "win" if exit_price == 1.0 else "loss"

    # Approximate Brier score contribution: (prob_estimate - outcome_value)^2
    # (legacy column semantics preserved — see Step 2's noticed issue: the
    # health producers use p_model_at_entry + the YES-line mapping instead).
    agent_est = float(position_row.get("agent_estimate") or entry_price)
    outcome_val = 1.0 if outcome == "win" else 0.0
    brier = (agent_est - outcome_val) ** 2

    async def _record() -> None:
        builder = client.table("closed_trades").insert({
            "market_id": position_row["market_id"],
            "market_question": position_row.get("market_question"),
            "direction": position_row["direction"],
            "entry_price": entry_price,
            "exit_price": exit_price,
            "position_size_usdc": size_usdc,
            "pnl_usdc": pnl,
            "pnl_percent": pnl_pct,
            "strategy": position_row.get("strategy", "recalibration"),
            "agent_estimate": agent_est,
            "confidence_at_entry": position_row.get("confidence_at_entry"),
            "brier_contribution": brier,
            "category": position_row.get("category"),
            "outcome": outcome,
            "exit_reason": exit_reason,
            "opened_at": position_row.get("opened_at"),
            "closed_at": datetime.now(timezone.utc).isoformat(),
            "was_memoryless": False,
            "notes": notes,
            # Attribution passthrough (List A.md Step 3 — A7): the entry's
            # self-description rides along so every closed trade keeps its
            # thesis and edge context. NULL for pre-Step-3 open rows.
            "thesis_id": position_row.get("thesis_id"),
            "p_model_at_entry": position_row.get("p_model_at_entry"),
            "market_price_at_entry": position_row.get("market_price_at_entry"),
            "net_edge_at_entry": position_row.get("net_edge_at_entry"),
            "costs_paid_usdc": position_row.get("costs_paid_usdc"),
            "order_type": position_row.get("order_type"),
            # Fill passthrough (List A.md Step 5 — A5): the simulated fill rides
            # along so the gate report can measure decision-vs-fill slippage on
            # closed trades. NULL for pre-Step-5 open rows.
            "fill_price": position_row.get("fill_price"),
            "fill_type": position_row.get("fill_type"),
        })
        await asyncio.to_thread(builder.execute)

    try:
        async with asyncio.timeout(config.SUPABASE_TIMEOUT_SECONDS):
            await _record()
    except asyncio.CancelledError:
        raise
    except Exception as e:
        # The claim succeeded but the ledger write failed: the position is
        # closed yet unrecorded — a MISSING row understates realized P&L and
        # is loudly reported here (the old insert-first order failed toward a
        # silent DUPLICATE row on the next pass instead, which the equity
        # anchor would double-count).
        logger.critical(
            "[CLOSE] Ledger insert failed for %s (id=%s): %s: %s — position is "
            "closed but UNRECORDED (pnl=%.4f, exit=%.4f, exit_type=%s, "
            "resolved_at=%s); manual recovery may be required",
            market_id[:16], position_row.get("id"), type(e).__name__, e,
            pnl, exit_price, exit_type,
            resolved_at.isoformat() if resolved_at else None,
        )
        raise

    logger.info(
        "[CLOSE] %s (id=%s) exit_type=%s exit_reason=%s exit=%.4f pnl=%.4f (%.2f%%) "
        "resolved_at=%s",
        market_id[:16], position_row.get("id"), exit_type, exit_reason,
        exit_price, pnl, pnl_pct,
        resolved_at.isoformat() if resolved_at else None,
    )

    # ── Copy-trade trust update (verbatim guard and keying from the extracted
    # startup block — a resolution is a resolution regardless of the closer).
    # Late binding on the module: tests patch copytrade.performance_tracker.*
    # and the patched client flows through.
    ran_copy_trade = False
    if str(position_row.get("strategy", "")).startswith("copy_edge_class_"):
        try:
            await performance_tracker.resolve_copy_trade(
                market_id=market_id,
                direction=position_row["direction"],
                exit_price=exit_price,
                entry_price=entry_price,
                size_usdc=size_usdc,
                idempotency_uuid=position_row.get("idempotency_uuid"),
            )
            ran_copy_trade = True
            logger.info(
                "[CLOSE] Trust score updated for copy-trade %s", market_id[:16]
            )
        except Exception as ct_err:
            logger.error(
                "[CLOSE] resolve_copy_trade failed for %s: %s", market_id[:16], ct_err
            )

    return CloseResult(closed=True, already_closed=False, pnl_usdc=pnl, copy_trade=ran_copy_trade)
