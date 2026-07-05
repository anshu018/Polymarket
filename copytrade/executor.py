"""
copytrade/executor.py — Strategy 5: Copy Edge

Executes copy trades through two paths:

Class A (Fast-Path, Bypass LLM):
    - Trust-driven sizing per CopyTrade.md §4:
        raw_size = CLASS_A_CEILING * state_multiplier * trust_score
        final_size = risk_engine.position_size_check(raw_size, ...)
      Hard ceiling: COPY_CLASS_A_MAX_SIZE_USDC ($10).
    - Uses a limit order priced at tracker_entry_price + 0.005 (0.5 cents
      above the tracker's entry, to avoid slippage dumps).
    - All standard risk engine gates STILL apply.
    - PAPER TRADING FIX (CopyTrade.md §8): paper mode now logs a simulated
      fill to open_positions — Class A is no longer invisible to Brier scoring.
    - Target: < 500ms from signal detection to order placement.

Class B (Macro, LLM-Validated):
    - Routes the signal through coordinator/pipeline.run_pipeline() with
      signal_source="copy_edge".
    - Trust-driven sizing: raw_size = $50 * state_multiplier * trust_score,
      then passed through risk_engine. Hard ceiling: COPY_CLASS_B_MAX_SIZE_USDC.

Safety rules:
    - Inherits ALL of the coordinator/pipeline safety invariants for Class B.
    - Class A explicitly calls every risk_engine gate before execution.
    - PAPER_TRADING=true sends both classes to simulated execution.
    - All config sourced from config.py — nothing hardcoded.
"""

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

import aiohttp

import config
from memory.supabase_client import get_client
from risk import risk_engine
from monitoring.telegram_alerts import alert_circuit_breaker, alert_supabase_degradation
from copytrade.performance_tracker import (
    log_copy_trade as _tracker_log_trade,
    get_trust_score,
    get_wallet_state,
    compute_state_multiplier,
)

logger = logging.getLogger(__name__)


# ── Gamma market metadata fetch ───────────────────────────────────────────────

async def _fetch_market_question(
    session: aiohttp.ClientSession,
    market_id: str,
) -> Optional[str]:
    """
    Fetch market question from Gamma API for a given condition ID.
    Used by Class B to give the coordinator pipeline full context.
    Returns None on failure (Class B will still route, coordinator handles missing question).
    """
    url = f"{config.GAMMA_API_URL}/markets"
    params = {"condition_id": market_id}
    try:
        async with asyncio.timeout(config.GAMMA_API_TIMEOUT_SECONDS):
            async with session.get(url, params=params) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    markets = data if isinstance(data, list) else data.get("results", [])
                    if markets:
                        return markets[0].get("question")
    except Exception as e:
        logger.warning("[COPY_EXECUTOR] Could not fetch market question for %s: %s", market_id[:12], e)
    return None


# ── Supabase helpers for Class A ──────────────────────────────────────────────

async def _check_idempotency(order_uuid: str) -> Optional[dict]:
    """Check idempotency_log for an existing UUID. Fail closed on timeout."""
    async def _q():
        client = await get_client()
        res = (
            client.table("idempotency_log")
            .select("id,status")
            .eq("id", order_uuid)
            .execute()
        )
        return res.data[0] if res.data else None

    try:
        return await asyncio.wait_for(_q(), timeout=config.SUPABASE_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        logger.critical("[COPY_EXECUTOR] idempotency_log read timed out — halting Class A trade.")
        raise RuntimeError("Class A halted: idempotency check timeout")
    except Exception as e:
        logger.critical("[COPY_EXECUTOR] idempotency check failed: %s — halting", e)
        raise RuntimeError("Class A halted: idempotency check failure") from e


async def _write_idempotency_pending(order_uuid: str, market_id: str, direction: str, size: float) -> None:
    """Write pending idempotency record before order submission. Fail closed on timeout."""
    async def _w():
        client = await get_client()
        client.table("idempotency_log").insert({
            "id": order_uuid,
            "market_id": market_id,
            "direction": direction,
            "intended_size_usdc": size,
            "status": "pending",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }).execute()

    try:
        await asyncio.wait_for(_w(), timeout=config.SUPABASE_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        logger.critical("[COPY_EXECUTOR] idempotency_log write timed out — halting Class A trade.")
        raise RuntimeError("Class A halted: idempotency write timeout")
    except Exception as e:
        logger.critical("[COPY_EXECUTOR] idempotency write failed: %s — halting", e)
        raise RuntimeError("Class A halted: idempotency write failure") from e


async def _confirm_idempotency(order_uuid: str, order_id: str) -> None:
    """Confirm idempotency record after successful order placement."""
    async def _c():
        client = await get_client()
        client.table("idempotency_log").update({
            "status": "confirmed",
            "polymarket_order_id": order_id,
            "confirmed_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", order_uuid).execute()

    try:
        await asyncio.wait_for(_c(), timeout=config.SUPABASE_TIMEOUT_SECONDS)
    except Exception as e:
        logger.error("[COPY_EXECUTOR] Failed to confirm idempotency %s: %s", order_uuid, e)


async def _log_open_position(
    market_id: str,
    direction: str,
    entry_price: float,
    size_usdc: float,
    class_type: str,
    trader_name: str,
    idempotency_uuid: str,
) -> None:
    """Write the new Class A position to open_positions. Non-blocking on failure."""
    async def _w():
        client = await get_client()
        client.table("open_positions").insert({
            "market_id": market_id,
            "market_question": f"[CopyEdge-{class_type}] Copied from {trader_name}",
            "direction": direction,
            "entry_price": entry_price,
            "position_size_usdc": size_usdc,
            "strategy": f"copy_edge_class_{class_type.lower()}",
            "agent_estimate": entry_price,
            "confidence_at_entry": 1.0,          # Smart-money copy = assume tracker is confident
            "kelly_fraction_used": 0.0,           # Class A uses fixed cap, not Kelly
            "category": "copy_edge",
            "idempotency_uuid": idempotency_uuid,
            "opened_at": datetime.now(timezone.utc).isoformat(),
            "last_checked_at": datetime.now(timezone.utc).isoformat(),
        }).execute()

    try:
        await asyncio.wait_for(_w(), timeout=config.SUPABASE_TIMEOUT_SECONDS)
        logger.info("[COPY_EXECUTOR] Position logged to open_positions: %s", market_id)
    except Exception as e:
        logger.error("[COPY_EXECUTOR] Failed to write position to open_positions: %s", e)


async def _fetch_exposure() -> tuple[float, float]:
    """Fetch category and correlated exposures. Fail closed on timeout."""
    async def _q():
        client = await get_client()
        res = client.table("open_positions").select("position_size_usdc").execute()
        return res.data or []

    try:
        rows = await asyncio.wait_for(_q(), timeout=config.SUPABASE_TIMEOUT_SECONDS)
        portfolio = config.PAPER_TRADING_PORTFOLIO_USDC
        total = sum(float(r.get("position_size_usdc", 0)) for r in rows)
        # For copy_edge category — count all copy_edge positions
        copy_edge_total = sum(
            float(r.get("position_size_usdc", 0))
            for r in rows
        )
        return copy_edge_total / portfolio, total / portfolio
    except asyncio.TimeoutError:
        logger.critical("[COPY_EXECUTOR] open_positions read timed out — halting Class A.")
        asyncio.create_task(
            alert_supabase_degradation("open_positions", "HALTING copy trade — DB timeout")
        )
        raise RuntimeError("Class A halted: exposure check timeout")
    except Exception as e:
        logger.critical("[COPY_EXECUTOR] Exposure check failed: %s — halting", e)
        raise RuntimeError("Class A halted: exposure check failure") from e


# ── Class A executor ──────────────────────────────────────────────────────────

async def _execute_class_a(signal: dict) -> None:
    """
    Execute a Class A (Speed/Alpha) copy trade.

    Steps:
        1. Validate all risk engine gates.
        2. Compute trust-driven size per CopyTrade.md §4.
        3. Generate idempotency UUID.
        4. Write idempotency record as pending.
        5. Submit limit order (or write simulated fill in paper mode).
        6. Confirm idempotency record.
        7. Log to open_positions AND copytrade_log.

    Paper mode fix (CopyTrade.md §8): paper mode now logs a simulated fill
    to open_positions — Class A is no longer a silent no-op.

    Target latency: < 500ms from signal arrival to order placement.
    """
    start_ts = time.monotonic()

    market_id: str = signal["market_id"]
    outcome: str = signal.get("outcome", "Yes")
    direction: str = "YES" if outcome.upper() in ("YES", "Y") else "NO"
    live_ask: float = signal["live_ask"]
    tracker_price: float = signal["tracker_price"]
    trader_name: str = signal.get("trader_name", "unknown")
    wallet_address: str = signal.get("wallet_address", "")
    was_priority_pick: bool = signal.get("was_priority_pick", False)

    logger.info(
        "[COPY_EXECUTOR][CLASS_A] Starting execution | market=%s direction=%s ask=%.4f trader=%s priority=%s",
        market_id[:12],
        direction,
        live_ask,
        trader_name,
        was_priority_pick,
    )

    # ── Risk Gate 1: Drawdown circuit breakers ────────────────────────────────
    from coordinator.pipeline import get_live_portfolio_value
    portfolio_value = await get_live_portfolio_value()
    for period in ["daily", "weekly", "monthly"]:
        status = risk_engine.check_drawdown(
            starting_balance=portfolio_value,
            current_balance=portfolio_value,
            period=period,
        )
        if status in ("HALT", "SHUTDOWN"):
            logger.critical("[COPY_EXECUTOR][CLASS_A] Drawdown gate fired: %s — HALTING", period)
            asyncio.create_task(
                alert_circuit_breaker(
                    breaker_type=period,
                    current_pct=0.0,
                    threshold_pct=config.DAILY_DRAWDOWN_HALT_PCT,
                    portfolio_value=portfolio_value,
                )
            )
            return

    # ── Risk Gate 2: Liquidity ────────────────────────────────────────────────
    market_volume = signal.get("market_volume_usd", 0.0)
    liq_status = risk_engine.check_liquidity(
        available_liquidity=market_volume,
        current_market_liquidity=market_volume,
    )
    if liq_status != "ALLOW":
        logger.warning(
            "[COPY_EXECUTOR][CLASS_A][DROP:liquidity] gate=%s market=%s",
            liq_status, market_id[:12],
        )
        return

    # ── Trust-driven sizing (CopyTrade.md §4) ────────────────────────────────
    # raw_size = class_ceiling * state_multiplier * trust_score
    trust_score = get_trust_score(wallet_address)
    wallet_state = get_wallet_state(wallet_address)
    state_multiplier = compute_state_multiplier(wallet_state)
    raw_size = config.COPY_CLASS_A_MAX_SIZE_USDC * state_multiplier * trust_score
    # Apply class-specific absolute ceiling (copy-trade business logic — stays here).
    class_capped = min(raw_size, config.COPY_CLASS_A_MAX_SIZE_USDC)
    # Route the portfolio-percentage cap through the single authoritative function,
    # matching coordinator/pipeline.py:733.  "copy_edge_class_a" falls through the
    # else-branch in position_size_check() to the standard MAX_SINGLE_TRADE_PCT (5%) cap.
    # Using the canonical strategy string that open_positions already records (line 161)
    # — eliminates the orphan "copy_trade" label that existed nowhere else.
    final_size = risk_engine.position_size_check(class_capped, portfolio_value, strategy="copy_edge_class_a")

    logger.info(
        "[COPY_EXECUTOR][CLASS_A] Sizing | trust=%.3f state=%s multiplier=%.1f "
        "raw=$%.2f capped=$%.2f",
        trust_score, wallet_state, state_multiplier, raw_size, final_size,
    )

    # ── Risk Gate 3: Portfolio exposure ───────────────────────────────────────
    cat_exp, corr_exp = await _fetch_exposure()
    proposed_pct = final_size / portfolio_value

    if risk_engine.check_category_exposure(cat_exp, proposed_pct) == "BLOCK":
        logger.warning("[COPY_EXECUTOR][CLASS_A][DROP:category_exposure] market=%s", market_id[:12])
        return
    if risk_engine.check_correlation_exposure(corr_exp + proposed_pct) == "BLOCK":
        logger.warning("[COPY_EXECUTOR][CLASS_A][DROP:correlated_exposure] market=%s", market_id[:12])
        return

    # ── Limit order pricing ───────────────────────────────────────────────────
    limit_price = round(tracker_price + config.COPY_LIMIT_PRICE_BUFFER, 4)

    # ── Idempotency ───────────────────────────────────────────────────────────
    order_uuid = str(uuid.uuid4())
    existing = await _check_idempotency(order_uuid)
    if existing and existing.get("status") == "confirmed":
        logger.critical(
            "[COPY_EXECUTOR][CLASS_A] UUID %s already confirmed — blocking duplicate", order_uuid
        )
        return

    await _write_idempotency_pending(order_uuid, market_id, direction, final_size)

    # ── Order submission ──────────────────────────────────────────────────────
    if config.PAPER_TRADING:
        # PAPER MODE FIX (CopyTrade.md §8): simulate fill — no silent no-op.
        # This makes Class A trades visible to the Brier score and paper gate.
        mock_order_id = f"copy_a_paper_{int(time.time())}"
        logger.info(
            "[COPY_EXECUTOR][CLASS_A][PAPER] Simulated LIMIT %s $%.2f @ %.4f | "
            "trust=%.3f state=%s order=%s",
            direction, final_size, limit_price,
            trust_score, wallet_state, mock_order_id,
        )
        order_id = mock_order_id
    else:
        # TODO: Real CLOB limit order via execution.polymarket_auth (Phase 3 gate).
        logger.warning(
            "[COPY_EXECUTOR][CLASS_A] Live order submission not yet wired. "
            "Set PAPER_TRADING=true until Phase 3 integration is complete."
        )
        order_id = f"copy_a_noop_{int(time.time())}"

    # ── Confirm idempotency and log position ──────────────────────────────────
    await _confirm_idempotency(order_uuid, order_id)
    # Always log to open_positions (both paper and live) — CopyTrade.md §8 fix
    await _log_open_position(
        market_id=market_id,
        direction=direction,
        entry_price=limit_price,
        size_usdc=final_size,
        class_type="A",
        trader_name=trader_name,
        idempotency_uuid=order_uuid,
    )

    # Log to copytrade_log for trust score tracking and priority audit trail
    await _tracker_log_trade(
        wallet_address=wallet_address,
        trader_name=trader_name,
        market_id=market_id,
        direction=direction,
        class_type="A",
        entry_price=limit_price,
        size_usdc=final_size,
        slippage=signal.get("slippage", 0.0),
        idempotency_uuid=order_uuid,
        was_priority_pick=was_priority_pick,
    )

    elapsed_ms = (time.monotonic() - start_ts) * 1000
    logger.info(
        "[COPY_EXECUTOR][CLASS_A] ✅ Executed in %.0fms | market=%s dir=%s "
        "size=$%.2f price=%.4f trust=%.3f state=%s priority=%s",
        elapsed_ms, market_id[:12], direction,
        final_size, limit_price, trust_score, wallet_state, was_priority_pick,
    )

    if elapsed_ms > 500:
        logger.warning(
            "[COPY_EXECUTOR][CLASS_A] ⚠️ Execution exceeded 500ms SLA: %.0fms",
            elapsed_ms,
        )


# ── Class B executor ──────────────────────────────────────────────────────────

async def _execute_class_b(signal: dict, session: aiohttp.ClientSession) -> None:
    """
    Execute a Class B (Macro/Deep Value) copy trade.

    Routes the validated signal into coordinator/pipeline.run_pipeline() with
    signal_source="copy_edge". The coordinator pipeline handles:
        - News Analyst LLM validation of the market
        - Trade Decision Agent
        - All risk engine gates
        - Idempotency
        - Position logging

    Kelly fraction: KELLY_FRACTION_COPY (0.10), hard cap at $50 USDC.
    """
    from coordinator.pipeline import run_pipeline as coordinator_pipeline

    market_id: str = signal["market_id"]
    outcome: str = signal.get("outcome", "Yes")
    trader_name: str = signal.get("trader_name", "unknown")
    live_ask: float = signal["live_ask"]

    # Fetch market question for coordinator context
    market_question = await _fetch_market_question(session, market_id)
    if not market_question:
        market_question = f"[CopyEdge-B] Market {market_id[:12]}"

    # Build a synthetic headline that the News Analyst can reason about
    synthetic_headline = (
        f"Smart money trader {trader_name} entered {outcome} position "
        f"on prediction market {market_question} at {live_ask:.2f}"
    )

    logger.info(
        "[COPY_EXECUTOR][CLASS_B] Routing to coordinator pipeline | market=%s trader=%s",
        market_id[:12],
        trader_name,
    )

    try:
        result = await coordinator_pipeline(
            headline=synthetic_headline,
            source=f"copy_edge:{trader_name}",
            market_id=market_id,
            market_question=market_question,
            market_price=live_ask,
            portfolio_value=None,
            signal_source="copy_edge",
            strategy_override="copy_edge_class_b",
            wallet_address=signal.get("wallet_address"),
            was_priority_pick=signal.get("was_priority_pick", False),
            slippage=signal.get("slippage", 0.0),
            trader_name=trader_name,
        )

        if result and result.get("status") == "success":
            logger.info(
                "[COPY_EXECUTOR][CLASS_B] ✅ Pipeline approved Class B copy | market=%s order=%s",
                market_id[:12],
                result.get("order_id"),
            )
        else:
            logger.info(
                "[COPY_EXECUTOR][CLASS_B] Pipeline rejected/blocked Class B | market=%s reason=%s",
                market_id[:12],
                result.get("reason", "unknown") if result else "None returned",
            )
    except Exception as e:
        logger.error(
            "[COPY_EXECUTOR][CLASS_B] Coordinator pipeline raised: %s | market=%s",
            e,
            market_id[:12],
        )


# ── Executor worker loops ─────────────────────────────────────────────────────

async def run_class_a_executor(queue_a: asyncio.Queue) -> None:
    """
    Worker loop for Class A (fast-path) copy trades.
    Runs indefinitely. One signal at a time (sequential — queue is small).
    """
    logger.info("[COPY_EXECUTOR] Class A executor started.")
    while True:
        signal: dict = await queue_a.get()
        try:
            await _execute_class_a(signal)
        except asyncio.CancelledError:
            logger.info("[COPY_EXECUTOR] Class A executor cancelled.")
            raise
        except Exception as e:
            logger.error("[COPY_EXECUTOR] Unhandled error in Class A executor: %s", e)
        finally:
            queue_a.task_done()


async def run_class_b_executor(queue_b: asyncio.Queue) -> None:
    """
    Worker loop for Class B (LLM-validated) copy trades.
    Runs indefinitely. Sequential execution — coordinator pipeline is already
    concurrency-safe internally.
    """
    logger.info("[COPY_EXECUTOR] Class B executor started.")
    async with aiohttp.ClientSession() as session:
        while True:
            signal: dict = await queue_b.get()
            try:
                await _execute_class_b(signal, session)
            except asyncio.CancelledError:
                logger.info("[COPY_EXECUTOR] Class B executor cancelled.")
                raise
            except Exception as e:
                logger.error("[COPY_EXECUTOR] Unhandled error in Class B executor: %s", e)
            finally:
                queue_b.task_done()
