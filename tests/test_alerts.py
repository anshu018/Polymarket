"""
test_alerts.py — pytest suite for Telegram alerts formatting and dispatching.

Ensures that all 7 (and more) critical Telegram alert states generate messages
matching the exact format: [ZERO-ALPHA] {severity} | {trigger} | {timestamp}
"""

import sys
import os
import asyncio
from unittest.mock import patch, AsyncMock

import pytest

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from monitoring.telegram_alerts import (
    alert_system_halt,
    alert_circuit_breaker,
    alert_reconciliation_failure,
    alert_supabase_degradation,
    alert_siliconflow_failover,
    alert_idempotency_duplicate,
    alert_pipeline_component_crash,
    alert_startup,
)


@pytest.fixture
def mock_telegram_sender() -> AsyncMock:
    """Interceptors the private _send_to_telegram function to assert formatting."""
    with patch("monitoring.telegram_alerts._send_to_telegram", new_callable=AsyncMock) as mock_send:
        yield mock_send


@pytest.mark.anyio
async def test_alert_system_halt(mock_telegram_sender: AsyncMock) -> None:
    """Verify system halt alert format."""
    await alert_system_halt(reason="health score too low", drawdown_pct=0.12, portfolio_value=5000.0)
    
    # Wait for fire-and-forget task
    await asyncio.sleep(0.1)
    
    assert mock_telegram_sender.called
    msg = mock_telegram_sender.call_args[0][0]
    
    # Expected: [ZERO-ALPHA] CRITICAL | SYSTEM_HALT
    assert msg.startswith("[ZERO-ALPHA] CRITICAL | SYSTEM_HALT")
    assert "Time:" in msg
    assert "Trigger: Drawdown 12.00% (Reason: health score too low)" in msg
    assert "Portfolio Value $5000.00" in msg


@pytest.mark.anyio
async def test_alert_circuit_breaker(mock_telegram_sender: AsyncMock) -> None:
    """Verify daily/weekly/monthly drawdown circuit breaker alert format."""
    await alert_circuit_breaker(breaker_type="daily", current_pct=0.092, threshold_pct=0.080, portfolio_value=9000.0)
    
    await asyncio.sleep(0.1)
    
    assert mock_telegram_sender.called
    msg = mock_telegram_sender.call_args[0][0]
    
    # Expected: [ZERO-ALPHA] CRITICAL | CIRCUIT_BREAKER
    assert msg.startswith("[ZERO-ALPHA] CRITICAL | CIRCUIT_BREAKER")
    assert "Time:" in msg
    assert "Trigger: daily drawdown 9.20% (Threshold: 8.00%)" in msg
    assert "Portfolio Value $9000.00" in msg


@pytest.mark.anyio
async def test_alert_reconciliation_failure(mock_telegram_sender: AsyncMock) -> None:
    """Verify reconciliation failure alert format."""
    await alert_reconciliation_failure(inconsistency_detail="USDC balance mismatch: got $100, expected $50", market_id="m123")
    
    await asyncio.sleep(0.1)
    
    assert mock_telegram_sender.called
    msg = mock_telegram_sender.call_args[0][0]
    
    # Expected: [ZERO-ALPHA] CRITICAL | RECONCILIATION_FAILURE
    assert msg.startswith("[ZERO-ALPHA] CRITICAL | RECONCILIATION_FAILURE")
    assert "Time:" in msg
    assert "Trigger: State inconsistency detected for market m123. Detail: USDC balance mismatch" in msg


@pytest.mark.anyio
async def test_alert_supabase_degradation(mock_telegram_sender: AsyncMock) -> None:
    """Verify database degradation alert format."""
    await alert_supabase_degradation(table_name="open_positions", fallback_behavior="halt trading")
    
    await asyncio.sleep(0.1)
    
    assert mock_telegram_sender.called
    msg = mock_telegram_sender.call_args[0][0]
    
    # Expected: [ZERO-ALPHA] WARNING | DB_DEGRADATION
    assert msg.startswith("[ZERO-ALPHA] WARNING | DB_DEGRADATION")
    assert "Time:" in msg
    assert "Trigger: Supabase timeout on table open_positions" in msg
    assert "Action: Applying fallback - halt trading" in msg


@pytest.mark.anyio
async def test_alert_siliconflow_failover(mock_telegram_sender: AsyncMock) -> None:
    """Verify SiliconFlow timeout failover alert format."""
    await alert_siliconflow_failover(latency_ms=19500, fallback_provider="OpenRouter")
    
    await asyncio.sleep(0.1)
    
    assert mock_telegram_sender.called
    msg = mock_telegram_sender.call_args[0][0]
    
    # Expected: [ZERO-ALPHA] WARNING | LLM_FAILOVER
    assert msg.startswith("[ZERO-ALPHA] WARNING | LLM_FAILOVER")
    assert "Time:" in msg
    assert "Trigger: Primary LLM timeout (19500ms)" in msg
    assert "Action: Failed over to OpenRouter" in msg


@pytest.mark.anyio
async def test_alert_idempotency_duplicate(mock_telegram_sender: AsyncMock) -> None:
    """Verify duplicate order blocked alert format."""
    await alert_idempotency_duplicate(idempotency_uuid="uuid-123", market_id="m456", direction="YES")
    
    await asyncio.sleep(0.1)
    
    assert mock_telegram_sender.called
    msg = mock_telegram_sender.call_args[0][0]
    
    # Expected: [ZERO-ALPHA] WARNING | DUPLICATE_ORDER_BLOCKED
    assert msg.startswith("[ZERO-ALPHA] WARNING | DUPLICATE_ORDER_BLOCKED")
    assert "Time:" in msg
    assert "Trigger: Retry detected existing confirmed UUID uuid-123" in msg
    assert "Action: Blocked duplicate YES order for m456" in msg


@pytest.mark.anyio
async def test_alert_pipeline_component_crash(mock_telegram_sender: AsyncMock) -> None:
    """Verify component crash alert format."""
    await alert_pipeline_component_crash(component="news_analyst", error_type="OpenRouterAPIError", error_detail="Out of credit")
    
    assert mock_telegram_sender.called
    msg = mock_telegram_sender.call_args[0][0]
    
    # Expected: [ZERO-ALPHA] ERROR | COMPONENT_CRASH
    assert msg.startswith("[ZERO-ALPHA] ERROR | COMPONENT_CRASH")
    assert "Time:" in msg
    assert "Component: news_analyst" in msg
    assert "Error: OpenRouterAPIError" in msg
    assert "Detail: Out of credit" in msg


@pytest.mark.anyio
async def test_alert_startup(mock_telegram_sender: AsyncMock) -> None:
    """Verify startup alert format."""
    await alert_startup(environment="development", paper_trading=True)
    
    assert mock_telegram_sender.called
    msg = mock_telegram_sender.call_args[0][0]
    
    # Expected: [ZERO-ALPHA] INFO | AGENT_STARTUP
    assert msg.startswith("[ZERO-ALPHA] INFO | AGENT_STARTUP")
    assert "Time:" in msg
    assert "State: Env=development, PaperTrading=ENABLED" in msg
