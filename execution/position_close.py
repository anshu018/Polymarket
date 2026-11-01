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
- exit_type is persisted (Step 5's `exit_type` column): 'resolved' for true
  resolutions; the exit engine names its own (exit_liquidity / exit_stop /
  exit_profit / exit_time_decay / exit_swap). exit_reason IS an existing
  column and is persisted as given.
- Copy-trade semantics are decided by exit_type inside this path (D-B06/D-B20):
  a RESOLUTION updates wallet W/L/trust (verbatim extracted behavior); an
  EARLY exit only records status 'exited_early' + exit_type on the
  copytrade_log row (performance_tracker.record_early_exit) — the wallet's
  W/L/trust is never touched, regardless of which closer calls this.
- outcome: resolutions keep the verbatim derivation (win iff exit_price ==
  1.0 — the side's token resolved). Early exits have no resolution, so
  outcome is the realized P&L sign (pnl > 0 → win) — an early exit booked at
  exit_price 0.7 on a 0.5 entry must never read as a "loss".
- resolved_at is accepted for the Step 5 interface and logged; Gamma
  payloads carry no reliable resolution timestamp today.

partial_close_position() lives in the SAME module on purpose (D-B19): the
module stays the ONE close path and the only closed_trades writer — a
partial exit (R4 time-decay) shrinks the open row and records the SOLD
portion in the ledger, keeping the equity identity (D-B12: constant +
Σ closed pnl − Σ open cost) continuous. It performs NO copy-trade
side-effect: the position continues, only full exits close the copy log.

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
    copy_trade: bool                 # a copy-trade side-effect ran (trust update on
                                     # resolutions; 'exited_early' recording otherwise)


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
            types from Step 5 (exit_liquidity / exit_stop / exit_profit /
            exit_time_decay / exit_swap). Persisted into closed_trades.exit_type.
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
    # outcome: resolutions keep the verbatim derivation (win iff the side's
    # token resolved at 1.0); early exits (exit_type != 'resolved') derive it
    # from the realized P&L sign — see the module docstring.
    if exit_type == "resolved":
        outcome = "win" if exit_price == 1.0 else "loss"
    else:
        outcome = "win" if pnl > 0.0 else "loss"

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
            "exit_type": exit_type,
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

    # ── Copy-trade side-effect (D-B06/D-B20 — decided HERE by exit_type, not
    # by the caller): a resolution updates wallet W/L/trust (verbatim guard
    # and keying from the extracted startup block); an early exit only records
    # status 'exited_early' + exit_type on the copytrade_log row and never
    # touches the wallet. Late binding on the module: tests patch
    # copytrade.performance_tracker.* and the patched client flows through.
    ran_copy_trade = False
    if str(position_row.get("strategy", "")).startswith("copy_edge_class_"):
        try:
            if exit_type == "resolved":
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
            else:
                await performance_tracker.record_early_exit(
                    market_id=market_id,
                    direction=position_row["direction"],
                    exit_price=exit_price,
                    entry_price=entry_price,
                    size_usdc=size_usdc,
                    exit_type=exit_type,
                    idempotency_uuid=position_row.get("idempotency_uuid"),
                )
                ran_copy_trade = True
                logger.info(
                    "[CLOSE] Early exit recorded on copytrade_log for %s "
                    "(exit_type=%s; wallet W/L/trust untouched — D-B06)",
                    market_id[:16], exit_type,
                )
        except Exception as ct_err:
            logger.error(
                "[CLOSE] copy-trade side-effect failed for %s: %s", market_id[:16], ct_err
            )

    return CloseResult(closed=True, already_closed=False, pnl_usdc=pnl, copy_trade=ran_copy_trade)


async def partial_close_position(
    client: Any,
    position_row: dict[str, Any],
    *,
    fraction: float,
    exit_price: float,
    exit_type: str,
    exit_reason: str,
    notes: Optional[str] = None,
) -> CloseResult:
    """Sell `fraction` of one open position at `exit_price` (List-B Step 5, D-B19).

    Lives in the SAME module as close_position on purpose: this module stays
    the ONE close path and the only closed_trades writer (D-B08 grep gate).
    Mechanics — claim-then-record analog of the full close:

      1. UPDATE the open_positions row's position_size_usdc DOWN by the sold
         cost first (the returned representation is the claim: an empty
         result means another closer won → nothing is written). Without the
         ledger row that follows, the equity identity (D-B12) would show the
         sold cost as a phantom drawdown — the order matters.
      2. Insert the closed_trades row for the SOLD portion only (its own
         size_usdc / pnl_usdc; the position's identity columns ride along).

    NO copy-trade side-effect (D-B20): the position continues after a partial
    reduction — copytrade_log closes only on full exits.

    Args:
        client: Supabase client (injected — caller owns connection policy).
        position_row: the open_positions row being reduced.
        fraction: share of the position to sell, strictly inside (0, 1) —
            the exit engine passes config.TIME_DECAY_POSITION_REDUCTION_PCT.
        exit_price: the position's own side price realized on the sold shares.
        exit_type: the rule tag (e.g. 'exit_time_decay'). Persisted.
        exit_reason: persisted into closed_trades.exit_reason.
        notes: caller context stored in closed_trades.notes.

    Returns:
        CloseResult for the SOLD portion: closed=True when the ledger row was
        inserted; already_closed=True when the open row vanished (another
        closer won) — nothing written in that case.
    """
    if not 0.0 < fraction < 1.0:
        raise ValueError(
            f"partial_close_position fraction must be strictly inside (0, 1), got {fraction}"
        )
    entry_price = float(position_row["entry_price"])
    size_usdc = float(position_row["position_size_usdc"])
    if entry_price <= 0.0 or size_usdc <= 0.0:
        raise ValueError(
            "partial_close_position requires positive entry_price and "
            f"position_size_usdc, got {entry_price}/{size_usdc}"
        )

    shares_total = size_usdc / entry_price
    shares_sell = shares_total * fraction
    cost_sell = size_usdc * fraction
    remaining = size_usdc - cost_sell
    pnl_sell = shares_sell * (exit_price - entry_price)
    pnl_pct_sell = (pnl_sell / cost_sell) * 100.0 if cost_sell > 0 else 0.0

    market_id = str(position_row.get("market_id") or "")

    # ── Claim: shrink the open row first (return=representation) ──────────────
    async def _claim() -> list[dict[str, Any]]:
        builder = (
            client.table("open_positions")
            .update({"position_size_usdc": remaining})
            .eq("id", position_row["id"])
        )
        res = await asyncio.to_thread(builder.execute)
        return list(getattr(res, "data", None) or [])

    try:
        async with asyncio.timeout(config.SUPABASE_TIMEOUT_SECONDS):
            claimed = await _claim()
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.error(
            "[CLOSE] Partial-claim update failed for %s (id=%s): %s: %s — nothing written",
            market_id[:16], position_row.get("id"), type(e).__name__, e,
        )
        raise

    if not claimed:
        logger.warning(
            "[CLOSE] Position %s (id=%s) already closed before the partial "
            "reduction — nothing written",
            market_id[:16], position_row.get("id"),
        )
        return CloseResult(closed=False, already_closed=True, pnl_usdc=None, copy_trade=False)

    # ── Record: closed_trades row for the SOLD portion ────────────────────────
    outcome = "win" if pnl_sell > 0.0 else "loss"
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
            "position_size_usdc": cost_sell,
            "pnl_usdc": pnl_sell,
            "pnl_percent": pnl_pct_sell,
            "strategy": position_row.get("strategy", "recalibration"),
            "agent_estimate": agent_est,
            "confidence_at_entry": position_row.get("confidence_at_entry"),
            "brier_contribution": brier,
            "category": position_row.get("category"),
            "outcome": outcome,
            "exit_reason": exit_reason,
            "exit_type": exit_type,
            "opened_at": position_row.get("opened_at"),
            "closed_at": datetime.now(timezone.utc).isoformat(),
            "was_memoryless": False,
            "notes": notes,
            "thesis_id": position_row.get("thesis_id"),
            "p_model_at_entry": position_row.get("p_model_at_entry"),
            "market_price_at_entry": position_row.get("market_price_at_entry"),
            "net_edge_at_entry": position_row.get("net_edge_at_entry"),
            "costs_paid_usdc": position_row.get("costs_paid_usdc"),
            "order_type": position_row.get("order_type"),
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
        logger.critical(
            "[CLOSE] Partial ledger insert failed for %s (id=%s): %s: %s — the "
            "position is REDUCED but the sold portion is UNRECORDED "
            "(pnl=%.4f, exit=%.4f, exit_type=%s); manual recovery may be required",
            market_id[:16], position_row.get("id"), type(e).__name__, e,
            pnl_sell, exit_price, exit_type,
        )
        raise

    logger.info(
        "[CLOSE] Partial exit %s (id=%s) exit_type=%s exit_reason=%s sold=%.2f/%.2f "
        "USDC exit=%.4f pnl=%.4f (%.2f%% of sold portion)",
        market_id[:16], position_row.get("id"), exit_type, exit_reason,
        cost_sell, size_usdc, exit_price, pnl_sell, pnl_pct_sell,
    )
    return CloseResult(closed=True, already_closed=False, pnl_usdc=pnl_sell, copy_trade=False)
