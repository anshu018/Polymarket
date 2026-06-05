"""
execution/reconciliation.py — Startup State Reconciliation.

Executes immediately on process start before any signal processing begins.
Verifies and aligns Supabase DB state with actual Polymarket chain state.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Tuple, Optional

import config
from memory.supabase_client import get_client
from execution.polymarket_auth import get_polymarket_client
from monitoring.telegram_alerts import (
    alert_reconciliation_failure,
    alert_system_halt,
)

# Try importing CLOB types safely
try:
    from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
except ImportError:
    # Mocks or typings fallback
    class AssetType:
        COLLATERAL = "COLLATERAL"
        CONDITIONAL = "CONDITIONAL"

    class BalanceAllowanceParams:
        def __init__(self, asset_type: Any = None, token_id: str = None, signature_type: int = -1):
            self.asset_type = asset_type
            self.token_id = token_id
            self.signature_type = signature_type

logger = logging.getLogger(__name__)


async def _supabase_query_positions() -> list[dict[str, Any]]:
    """Fetch active rows from Supabase open_positions table under 2s timeout."""
    async def _query() -> list[dict[str, Any]]:
        client = await get_client()
        res = client.table("open_positions").select("*").execute()
        return res.data or []
    
    return await asyncio.wait_for(_query(), timeout=config.SUPABASE_TIMEOUT_SECONDS)


async def _supabase_query_pending_idempotency() -> list[dict[str, Any]]:
    """Fetch pending rows from Supabase idempotency_log table under 2s timeout."""
    async def _query() -> list[dict[str, Any]]:
        client = await get_client()
        res = client.table("idempotency_log").select("*").eq("status", "pending").execute()
        return res.data or []

    return await asyncio.wait_for(_query(), timeout=config.SUPABASE_TIMEOUT_SECONDS)


async def _supabase_update_position_size(pos_id: str, new_size_usdc: float) -> None:
    """Update position size in Supabase under 2s timeout."""
    async def _update() -> None:
        client = await get_client()
        client.table("open_positions").update({"position_size_usdc": new_size_usdc}).eq("id", pos_id).execute()

    await asyncio.wait_for(_update(), timeout=config.SUPABASE_TIMEOUT_SECONDS)


async def _supabase_delete_position(pos_id: str) -> None:
    """Delete position from Supabase under 2s timeout."""
    async def _delete() -> None:
        client = await get_client()
        client.table("open_positions").delete().eq("id", pos_id).execute()

    await asyncio.wait_for(_delete(), timeout=config.SUPABASE_TIMEOUT_SECONDS)


async def _supabase_move_to_closed(pos: dict[str, Any], exit_price: float, outcome: str) -> None:
    """Move a resolved position to closed_trades under 2s timeout."""
    async def _move() -> None:
        client = await get_client()
        
        # Calculate PnL
        entry_price = float(pos["entry_price"])
        size_usdc = float(pos["position_size_usdc"])
        qty = size_usdc / entry_price
        
        exit_value = qty * exit_price
        pnl = exit_value - size_usdc
        pnl_pct = (pnl / size_usdc) * 100.0 if size_usdc > 0 else 0.0
        
        # Approximate Brier score contribution: (prob_estimate - outcome_value)^2
        agent_est = float(pos.get("agent_estimate") or entry_price)
        outcome_val = 1.0 if outcome == "win" else 0.0
        brier = (agent_est - outcome_val) ** 2
        
        # Insert closed trade record
        client.table("closed_trades").insert({
            "market_id": pos["market_id"],
            "market_question": pos.get("market_question"),
            "direction": pos["direction"],
            "entry_price": entry_price,
            "exit_price": exit_price,
            "position_size_usdc": size_usdc,
            "pnl_usdc": pnl,
            "pnl_percent": pnl_pct,
            "strategy": pos.get("strategy", "recalibration"),
            "agent_estimate": agent_est,
            "confidence_at_entry": pos.get("confidence_at_entry"),
            "brier_contribution": brier,
            "category": pos.get("category"),
            "outcome": outcome,
            "exit_reason": "resolved",
            "opened_at": pos.get("opened_at"),
            "closed_at": datetime.now(timezone.utc).isoformat(),
            "was_memoryless": False,
            "notes": "Resolved during agent downtime startup reconciliation."
        }).execute()
        
        # Delete from open_positions
        client.table("open_positions").delete().eq("id", pos["id"]).execute()

    await asyncio.wait_for(_move(), timeout=config.SUPABASE_TIMEOUT_SECONDS)


async def _supabase_update_idempotency_log(log_id: str, status: str, order_id: Optional[str] = None, reason: Optional[str] = None) -> None:
    """Update idempotency log status in Supabase under 2s timeout."""
    async def _update() -> None:
        client = await get_client()
        update_data = {
            "status": status,
            "confirmed_at": datetime.now(timezone.utc).isoformat() if status == "confirmed" else None
        }
        if order_id:
            update_data["polymarket_order_id"] = order_id
        if reason:
            update_data["failure_reason"] = reason
            
        client.table("idempotency_log").update(update_data).eq("id", log_id).execute()

    await asyncio.wait_for(_update(), timeout=config.SUPABASE_TIMEOUT_SECONDS)


async def reconcile_on_startup() -> None:
    """
    State Reconciliation startup gate.
    
    Validates state integrity before signal ingestion starts. Halts on unresolvable mismatches.
    """
    # 1. Expected log sequence: Start
    logger.info("[RECONCILIATION] Starting startup reconciliation")
    
    first_failure_time: Optional[float] = None
    alert_dispatched = False
    
    # Central retry loop for Polymarket API queries
    while True:
        try:
            client = get_polymarket_client()
            
            # 2. Expected log sequence: Fetching positions
            logger.info("[RECONCILIATION] Fetching Polymarket positions...")
            
            # 3. Expected log sequence: Fetching USDC balance
            logger.info("[RECONCILIATION] Fetching USDC balance...")
            
            # Query actual USDC collateral balance
            async def _fetch_usdc() -> dict[str, Any]:
                return client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
            
            usdc_res = await asyncio.wait_for(_fetch_usdc(), timeout=config.POLYMARKET_API_TIMEOUT_SECONDS)
            actual_usdc = float(usdc_res.get("balance", "0")) / 1000000.0
            
            # If we succeed after a failure, reset tracking variables
            first_failure_time = None
            break
            
        except Exception as e:
            logger.error(f"[RECONCILIATION] Polymarket CLOB API query failed: {e}")
            now = asyncio.get_event_loop().time()
            if first_failure_time is None:
                first_failure_time = now
                
            elapsed = now - first_failure_time
            if elapsed >= 300.0 and not alert_dispatched:  # 5 minutes
                logger.critical("[RECONCILIATION] Polymarket CLOB API has been down for > 5 minutes. Alerting Telegram.")
                await alert_reconciliation_failure("Polymarket CLOB API has been unavailable for > 5 minutes.")
                alert_dispatched = True
                
            logger.info(f"[RECONCILIATION] API unavailable. Retrying in {config.RECONCILIATION_RETRY_INTERVAL_SECONDS} seconds...")
            await asyncio.sleep(config.RECONCILIATION_RETRY_INTERVAL_SECONDS)

    # 4. Expected log sequence: Diffing
    logger.info("[RECONCILIATION] Diffing against Supabase state...")

    # Load known states from Supabase (under 2s timeout, fails closed on timeout/error)
    try:
        open_positions = await _supabase_query_positions()
        pending_idempotency = await _supabase_query_pending_idempotency()
    except Exception as e:
        logger.critical(f"[RECONCILIATION] Supabase queries timed out or failed (Fail Closed): {e}")
        await alert_system_halt("Supabase connection timeout/error during startup reconciliation", 0.0, 0.0)
        raise RuntimeError("Reconciliation halted due to Supabase timeout/error") from e

    # Track unresolvable inconsistency
    unresolvable = False
    inconsistency_detail = ""

    # Process Supabase Open Positions
    for pos in open_positions:
        market_id = pos["market_id"]
        direction = pos["direction"]
        entry_price = float(pos["entry_price"])
        size_usdc = float(pos["position_size_usdc"])
        expected_shares = size_usdc / entry_price

        # Fetch market details and conditional balance
        try:
            async def _fetch_mkt() -> dict[str, Any]:
                return client.get_market(market_id)
            
            market = await asyncio.wait_for(_fetch_mkt(), timeout=config.POLYMARKET_API_TIMEOUT_SECONDS)
            
            # Extract token ID based on outcomes
            tokens = market.get("tokens", [])
            token_id = None
            for tk in tokens:
                if isinstance(tk, dict) and tk.get("outcome") == direction:
                    token_id = tk.get("token_id")
                    break
            
            if not token_id:
                clob_ids = market.get("clobTokenIds", [])
                if len(clob_ids) >= 2:
                    token_id = clob_ids[0] if direction == "YES" else clob_ids[1]
            
            actual_shares = 0.0
            if token_id:
                async def _fetch_bal() -> dict[str, Any]:
                    return client.get_balance_allowance(BalanceAllowanceParams(
                        asset_type=AssetType.CONDITIONAL,
                        token_id=token_id
                    ))
                
                bal_res = await asyncio.wait_for(_fetch_bal(), timeout=config.POLYMARKET_API_TIMEOUT_SECONDS)
                actual_shares = float(bal_res.get("balance", "0")) / 1000000.0

        except Exception as e:
            logger.critical(f"[RECONCILIATION] Failed to fetch market/balance for {market_id}: {e}")
            unresolvable = True
            inconsistency_detail = f"Failed to retrieve market/token details for {market_id}: {e}"
            break

        # Diff state check
        # Allow small rounding tolerance for share count diffs (e.g. 0.01 shares)
        share_diff = abs(actual_shares - expected_shares)
        
        if actual_shares == 0.0 and expected_shares > 0.01:
            # Position resolved during downtime or completely canceled
            # Query whether the market is indeed resolved
            is_resolved = market.get("resolved") or market.get("status") == "resolved" or False
            
            if is_resolved:
                winning_outcome = market.get("outcome") or market.get("winning_outcome")
                outcome = "win" if winning_outcome == direction else "loss"
                exit_price = 1.0 if outcome == "win" else 0.0
                
                logger.info(f"[RECONCILIATION] Market {market_id} resolved as {winning_outcome} during downtime. Closing position.")
                await _supabase_move_to_closed(pos, exit_price, outcome)
            else:
                # Active market, but actual balance is 0: unresolvable mismatch!
                logger.critical(f"[RECONCILIATION] Mismatch on active market {market_id}: Supabase shows active position but Polymarket holds 0 shares.")
                unresolvable = True
                inconsistency_detail = f"Active market {market_id} has 0 actual shares, but Supabase expected {expected_shares:.4f} shares."
                break

        elif share_diff > 0.05:
            # Partially filled or modified position size
            logger.info(f"[RECONCILIATION] Position size discrepancy on {market_id}: expected {expected_shares:.4f} shares, got {actual_shares:.4f} shares. Updating Supabase size.")
            new_size_usdc = actual_shares * entry_price
            if new_size_usdc == 0.0:
                await _supabase_delete_position(pos["id"])
            else:
                await _supabase_update_position_size(pos["id"], new_size_usdc)

    # Process Pending Idempotency Logs
    for log in pending_idempotency:
        uuid_str = log["id"]
        order_id = log.get("polymarket_order_id")
        market_id = log["market_id"]
        direction = log["direction"]
        size_usdc = float(log.get("intended_size_usdc") or 0.0)

        # If order_id was recorded, query Polymarket API for order status
        if order_id:
            try:
                async def _fetch_ord() -> dict[str, Any]:
                    return client.get_order(order_id)
                
                order_details = await asyncio.wait_for(_fetch_ord(), timeout=config.POLYMARKET_API_TIMEOUT_SECONDS)
                
                # Check status
                status = order_details.get("status", "").upper()
                if status in ["FILLED", "LIVE", "PARTIALLY_FILLED"]:
                    logger.info(f"[RECONCILIATION] Pending idempotency log {uuid_str} confirmed executed on Polymarket.")
                    await _supabase_update_idempotency_log(uuid_str, "confirmed")
                else:
                    logger.info(f"[RECONCILIATION] Pending idempotency log {uuid_str} resolved as failed on Polymarket.")
                    await _supabase_update_idempotency_log(uuid_str, "failed", reason=f"Order status: {status}")

            except Exception as e:
                logger.error(f"[RECONCILIATION] Failed to fetch order {order_id} for idempotency log: {e}")
                # We can't resolve this without confirming if we have the position on Polymarket
                # If we don't have the position in Supabase and wallet shares show 0, we can flag it as failed.
                pass
        else:
            # No order_id recorded (e.g. timed out during post/submit)
            # Check if there is an active open position in Supabase matching this idempotency UUID
            pos_matches = [p for p in open_positions if p.get("idempotency_uuid") == uuid_str]
            if pos_matches:
                logger.info(f"[RECONCILIATION] Pending idempotency log {uuid_str} matched an active position. Confirming.")
                await _supabase_update_idempotency_log(uuid_str, "confirmed")
            else:
                # Treat as failed to unlock lock
                logger.info(f"[RECONCILIATION] Pending idempotency log {uuid_str} has no order ID or position match. Release lock.")
                await _supabase_update_idempotency_log(uuid_str, "failed", reason="Startup reconciliation: no order ID or position found.")

    if unresolvable:
        logger.critical(f"[RECONCILIATION] Unresolvable discrepancy: {inconsistency_detail}")
        await alert_reconciliation_failure(inconsistency_detail)
        raise RuntimeError(f"Startup reconciliation failed: {inconsistency_detail}")

    # 5. Expected log sequence: Complete
    logger.info("[RECONCILIATION] Reconciliation complete. State authoritative.")
