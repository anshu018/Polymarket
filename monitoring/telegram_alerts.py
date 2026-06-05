import asyncio
import logging
from datetime import datetime, timezone
import aiohttp
import config

logger = logging.getLogger(__name__)

async def _send_to_telegram(message: str) -> None:
    """Fire and forget wrapper to send messages to Telegram without blocking."""
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        logger.warning("Telegram credentials missing, alert skipped.")
        return
        
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": config.TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }
    
    try:
        async with asyncio.timeout(10.0):
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload) as response:
                    if response.status != 200:
                        logger.error(f"Telegram alert failed with status {response.status}")
    except Exception as e:
        logger.error(f"Telegram alert failed: {e}")

async def alert_system_halt(reason: str, drawdown_pct: float, portfolio_value: float) -> None:
    """Alert for complete trading system halt."""
    now = datetime.now(timezone.utc).isoformat()
    msg = (f"[ZERO-ALPHA] CRITICAL | SYSTEM_HALT\n"
           f"Time: {now}\n"
           f"Trigger: Drawdown {drawdown_pct*100:.2f}% (Reason: {reason})\n"
           f"State: Portfolio Value ${portfolio_value:.2f}\n"
           f"Action: ALL TRADES HALTED. Wait for human review.")
    asyncio.create_task(_send_to_telegram(msg))

async def alert_circuit_breaker(breaker_type: str, current_pct: float, threshold_pct: float, portfolio_value: float) -> None:
    """Alert for daily/weekly/monthly circuit breaker."""
    now = datetime.now(timezone.utc).isoformat()
    msg = (f"[ZERO-ALPHA] CRITICAL | CIRCUIT_BREAKER\n"
           f"Time: {now}\n"
           f"Trigger: {breaker_type} drawdown {current_pct*100:.2f}% (Threshold: {threshold_pct*100:.2f}%)\n"
           f"State: Portfolio Value ${portfolio_value:.2f}\n"
           f"Action: NO NEW TRADES. Human review required.")
    asyncio.create_task(_send_to_telegram(msg))

async def alert_reconciliation_failure(inconsistency_detail: str, market_id: str | None = None) -> None:
    """Alert for reconciliation errors on startup."""
    now = datetime.now(timezone.utc).isoformat()
    market_str = f" for market {market_id}" if market_id else ""
    msg = (f"[ZERO-ALPHA] CRITICAL | RECONCILIATION_FAILURE\n"
           f"Time: {now}\n"
           f"Trigger: State inconsistency detected{market_str}. Detail: {inconsistency_detail}\n"
           f"State: Startup failed\n"
           f"Action: HALTED. Human intervention required.")
    asyncio.create_task(_send_to_telegram(msg))

async def alert_supabase_degradation(table_name: str, fallback_behavior: str) -> None:
    """Alert when Supabase reads timeout and apply fallback behavior."""
    now = datetime.now(timezone.utc).isoformat()
    msg = (f"[ZERO-ALPHA] WARNING | DB_DEGRADATION\n"
           f"Time: {now}\n"
           f"Trigger: Supabase timeout on table {table_name}\n"
           f"State: Degraded operations\n"
           f"Action: Applying fallback - {fallback_behavior}")
    asyncio.create_task(_send_to_telegram(msg))

async def alert_siliconflow_failover(latency_ms: int, fallback_provider: str) -> None:
    """Alert when primary LLM timeouts and fails over."""
    now = datetime.now(timezone.utc).isoformat()
    msg = (f"[ZERO-ALPHA] WARNING | LLM_FAILOVER\n"
           f"Time: {now}\n"
           f"Trigger: Primary LLM timeout ({latency_ms}ms)\n"
           f"State: Rerouting request\n"
           f"Action: Failed over to {fallback_provider}")
    asyncio.create_task(_send_to_telegram(msg))

async def alert_idempotency_duplicate(idempotency_uuid: str, market_id: str, direction: str) -> None:
    """Alert on duplicate order submission blocks."""
    now = datetime.now(timezone.utc).isoformat()
    msg = (f"[ZERO-ALPHA] WARNING | DUPLICATE_ORDER_BLOCKED\n"
           f"Time: {now}\n"
           f"Trigger: Retry detected existing confirmed UUID {idempotency_uuid}\n"
           f"State: Idempotency log check passed\n"
           f"Action: Blocked duplicate {direction} order for {market_id}")
    asyncio.create_task(_send_to_telegram(msg))

async def alert_brier_score_exceeded(current_score: float, threshold: float, period_days: int) -> None:
    """Alert for calibration drift."""
    now = datetime.now(timezone.utc).isoformat()
    msg = (f"[ZERO-ALPHA] WARNING | KPI_DEGRADATION\n"
           f"Time: {now}\n"
           f"Trigger: {period_days}-day Brier score {current_score:.4f} > {threshold:.4f}\n"
           f"State: Calibration drifting\n"
           f"Action: Models flagged for retraining.")
    asyncio.create_task(_send_to_telegram(msg))

async def alert_pipeline_component_crash(component: str, error_type: str, error_detail: str) -> None:
    """Alert when a pipeline component raises an unhandled exception."""
    now = datetime.now(timezone.utc).isoformat()
    msg = (f"[ZERO-ALPHA] ERROR | COMPONENT_CRASH\n"
           f"Time: {now}\n"
           f"Component: {component}\n"
           f"Error: {error_type}\n"
           f"Detail: {error_detail}\n"
           f"Action: Component restarting. Check logs.")
    await _send_to_telegram(msg)

async def alert_startup(environment: str, paper_trading: bool) -> None:
    """Alert when agent is turned on and begins reconciliation."""
    now = datetime.now(timezone.utc).isoformat()
    pt_str = "ENABLED" if paper_trading else "DISABLED"
    msg = (f"[ZERO-ALPHA] INFO | AGENT_STARTUP\n"
           f"Time: {now}\n"
           f"Trigger: Process start\n"
           f"State: Env={environment}, PaperTrading={pt_str}\n"
           f"Action: Reconciliation beginning...")
    asyncio.create_task(_send_to_telegram(msg))
    # Give the task event loop time to kick off the fire-and-forget request before exit
    await asyncio.sleep(0.5)
