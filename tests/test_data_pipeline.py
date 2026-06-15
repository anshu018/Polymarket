import asyncio
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from datetime import datetime, timezone

from data.pipeline import _process_loop
from tests.test_integration import MockSupabaseClient, db_state

@pytest.fixture
def mock_db(db_state):
    client = MockSupabaseClient(db_state)
    async def fake_get_client():
        return client
    return client, fake_get_client

@pytest.mark.anyio
async def test_process_loop_spacy_blocked(mock_db):
    """If spaCy filter blocks, coordinator is not called and nothing is logged."""
    client, fake_get_client = mock_db
    
    queue = asyncio.Queue()
    await queue.put({
        "headline": "Some minor unimportant news",
        "source_name": "Test Source"
    })
    
    # Mock spacy filter to return False
    with patch("data.pipeline.filter_signal", AsyncMock(return_value=False)) as mock_filter, \
         patch("data.pipeline.run_trading_pipeline", AsyncMock()) as mock_coordinator, \
         patch("memory.supabase_client.get_client", fake_get_client):
         
        # Run process loop and cancel it since it runs forever
        task = asyncio.create_task(_process_loop(queue))
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
            
        assert mock_filter.called
        assert not mock_coordinator.called
        # Check that no signal was logged
        assert len(client.db_state["market_signals"]) == 0

@pytest.mark.anyio
async def test_process_loop_traded(mock_db):
    """If signal passes spaCy and is successfully traded, log outcome as traded."""
    client, fake_get_client = mock_db
    
    queue = asyncio.Queue()
    await queue.put({
        "headline": "Trump wins political vote",
        "source_name": "AP News"
    })
    
    # Mock spacy filter to return True, coordinator to return success
    with patch("data.pipeline.filter_signal", AsyncMock(return_value=True)) as mock_filter, \
         patch("data.pipeline.run_trading_pipeline", AsyncMock(return_value={"status": "success"})) as mock_coordinator, \
         patch("memory.supabase_client.get_client", fake_get_client):
         
        # Run process loop
        task = asyncio.create_task(_process_loop(queue))
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
            
        assert mock_filter.called
        assert mock_coordinator.called
        
        # Verify Supabase write
        signals = client.db_state["market_signals"]
        assert len(signals) == 1
        assert signals[0]["raw_headline"] == "Trump wins political vote"
        assert signals[0]["action_taken"] == "traded"
        assert signals[0]["discard_reason"] is None

@pytest.mark.anyio
async def test_process_loop_risk_blocked(mock_db):
    """If signal passes spaCy but is blocked by risk check, log outcome as discarded with reason."""
    client, fake_get_client = mock_db
    
    queue = asyncio.Queue()
    await queue.put({
        "headline": "Bitcoin crashes 20 percent",
        "source_name": "CoinDesk"
    })
    
    # Mock spacy filter to return True, coordinator to return blocked
    with patch("data.pipeline.filter_signal", AsyncMock(return_value=True)) as mock_filter, \
         patch("data.pipeline.run_trading_pipeline", AsyncMock(return_value={"status": "blocked", "reason": "daily_drawdown"})) as mock_coordinator, \
         patch("memory.supabase_client.get_client", fake_get_client):
         
        # Run process loop
        task = asyncio.create_task(_process_loop(queue))
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
            
        assert mock_filter.called
        assert mock_coordinator.called
        
        # Verify Supabase write
        signals = client.db_state["market_signals"]
        assert len(signals) == 1
        assert signals[0]["raw_headline"] == "Bitcoin crashes 20 percent"
        assert signals[0]["action_taken"] == "discarded"
        assert signals[0]["discard_reason"] == "risk-blocked: daily_drawdown"
