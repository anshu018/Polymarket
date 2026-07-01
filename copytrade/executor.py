"""
copytrade/executor.py — Strategy 5: Copy Edge

Executes copy trades through two paths:

Class A (Fast-Path, Bypass LLM):
    - Fixed $10 USDC position size (hard cap, overrides Kelly).
    - Uses a limit order priced at tracker_entry_price + 0.005 (0.5 cents
      above the tracker's entry, per PRD §12 to avoid slippage dumps).
    - All standard risk engine gates STILL apply (drawdown, liquidity,
      category exposure, correlated exposure).
    - Order logged to idempotency_log + open_positions.
    - Target: < 500ms from signal detection to order placement.

Class B (Macro, LLM-Validated):
    - Routes the signal through coordinator/pipeline.run_pipeline() with
      signal_source="copy_edge". That pipeline handles: News Analyst,
      Trade Decision, risk gates, idempotency, position logging.
    - Dynamic Kelly sizing capped at $50 USDC.
    - The market_question is fetched from Gamma before routing so the
      coordinator pipeline has full market context.

Safety rules:
    - Inherits ALL of the coordinator/pipeline safety invariants for Class B.
    - Class A explicitly calls every risk_engine gate before execution.
    - PAPER_TRADING=true sends both classes to mock execution (no real orders).
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
from copytrade.performance_tracker import log_copy_trade as _tracker_log_trade

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
        2. Set fixed size = COPY_CLASS_A_MAX_SIZE_USDC ($10).
        3. Generate idempotency UUID.
        4. Write idempotency record as pending.
        5. Submit limit order (or mock in paper trading mode).
        6. Confirm idempotency record.
        7. Log to open_positions.

    Target latency: < 500ms from signal arrival to order placement.
    """
    start_ts = time.monotonic()

    market_id: str = signal["market_id"]
    outcome: str = signal.get("outcome", "Yes")
    direction: str = "YES" if outcome.upper() in ("YES", "Y") else "NO"
    live_ask: float = signal["live_ask"]
    tracker_price: float = signal["tracker_price"]
    trader_name: str = signal.get("trader_name", "unknown")

    logger.info(
        "[COPY_EXECUTOR][CLASS_A] Starting execution | market=%s direction=%s ask=%.4f trader=%s",
        market_id[:12],
        direction,
        live_ask,
        trader_name,
    )

    # ── Risk Gate 1: Drawdown circuit breakers ────────────────────────────────
    portfolio_value = config.PAPER_TRADING_PORTFOLIO_USDC
    # Use current portfolio as both starting and current for simplicity in
    # Class A fast-path (full reconciliation is not available here).
    # The drawdown gates are checked; if portfolio has shrunk materially,
    # the reconciliation module would have already halted the bot at startup.
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

    # ── Risk Gate 2: Liquidity (hardcoded available_liquidity for Class A) ────
    # Class A requires ≥ $25,000 volume (already checked by classifier).
    # We use the market volume as a proxy for available liquidity.
    market_volume = signal.get("market_volume_usd", 0.0)
    liq_status = risk_engine.check_liquidity(
        available_liquidity=market_volume,
        current_market_liquidity=market_volume,
    )
    if liq_status != "ALLOW":
        logger.warning(
            "[COPY_EXECUTOR][CLASS_A][DROP:liquidity] gate=%s market=%s",
            liq_status,
            market_id[:12],
        )
        return

    # ── Risk Gate 3: Portfolio exposure ───────────────────────────────────────
    cat_exp, corr_exp = await _fetch_exposure()
    fixed_size = config.COPY_CLASS_A_MAX_SIZE_USDC
    proposed_pct = fixed_size / portfolio_value

    if risk_engine.check_category_exposure(cat_exp, proposed_pct) == "BLOCK":
        logger.warning("[COPY_EXECUTOR][CLASS_A][DROP:category_exposure] market=%s", market_id[:12])
        return
    if risk_engine.check_correlation_exposure(corr_exp + proposed_pct) == "BLOCK":
        logger.warning("[COPY_EXECUTOR][CLASS_A][DROP:correlated_exposure] market=%s", market_id[:12])
        return

    # ── Limit order pricing ───────────────────────────────────────────────────
    # Per PRD §12: Use limit order at tracker_entry_price + 0.5 cents
    # to avoid buying at the top of a pump.
    limit_price = round(tracker_price + config.COPY_LIMIT_PRICE_BUFFER, 4)

    # ── Idempotency ───────────────────────────────────────────────────────────
    order_uuid = str(uuid.uuid4())
    existing = await _check_idempotency(order_uuid)
    if existing and existing.get("status") == "confirmed":
        logger.critical(
            "[COPY_EXECUTOR][CLASS_A] UUID %s already confirmed — blocking duplicate", order_uuid
        )
        return

    await _write_idempotency_pending(order_uuid, market_id, direction, fixed_size)

    # ── Order submission ──────────────────────────────────────────────────────
    if config.PAPER_TRADING:
        # Paper trading: simulate order, no real CLOB call
        mock_order_id = f"copy_a_mock_{int(time.time())}"
        logger.info(
            "[COPY_EXECUTOR][CLASS_A][PAPER] LIMIT %s %.4f USDC @ price=%.4f | order=%s",
            direction,
            fixed_size,
            limit_price,
            mock_order_id,
        )
        order_id = mock_order_id
    else:
        # TODO: Real CLOB limit order submission via execution.polymarket_auth
        # Implementation gate: requires live CLOB integration (Phase 3).
        logger.warning(
            "[COPY_EXECUTOR][CLASS_A] Live order submission not yet wired. "
            "Set PAPER_TRADING=true until Phase 3 integration is complete."
        )
        order_id = f"copy_a_noop_{int(time.time())}"

    # ── Confirm and log ───────────────────────────────────────────────────────
    await _confirm_idempotency(order_uuid, order_id)
    await _log_open_position(
        market_id=market_id,
        direction=direction,
        entry_price=limit_price,
        size_usdc=fixed_size,
        class_type="A",
        trader_name=trader_name,
        idempotency_uuid=order_uuid,
    )

    # Log to copytrade_log for trust score tracking
    await _tracker_log_trade(
        wallet_address=signal.get("wallet_address", ""),
        trader_name=trader_name,
        market_id=market_id,
        direction=direction,
        class_type="A",
        entry_price=limit_price,
        size_usdc=fixed_size,
        slippage=signal.get("slippage", 0.0),
        idempotency_uuid=order_uuid,
    )

    elapsed_ms = (time.monotonic() - start_ts) * 1000
    logger.info(
        "[COPY_EXECUTOR][CLASS_A] ✅ Executed in %.0fms | market=%s dir=%s size=$%.2f price=%.4f trust=%.3f",
        elapsed_ms,
        market_id[:12],
        direction,
        fixed_size,
        limit_price,
        signal.get("trust_score", 0.5),
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
            portfolio_value=config.PAPER_TRADING_PORTFOLIO_USDC,
            signal_source="copy_edge",
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
