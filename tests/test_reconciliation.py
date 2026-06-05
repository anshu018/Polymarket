"""
test_reconciliation.py — pytest suite for Layer 7 Startup Reconciliation.

Verifies Criteria 7.2, 7.3, and 7.4.
Uses robust mocks for the database, network, and Polymarket ClobClient.
"""

import sys
import os
import asyncio
from datetime import datetime, timezone
from typing import Any, Generator
from unittest.mock import patch, MagicMock, AsyncMock

import pytest
import config
from execution.reconciliation import reconcile_on_startup

# Ensure project root is on the path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ─────────────────────────────────────────────
# MOCK DATABASE STATE AND CLIENT
# ─────────────────────────────────────────────

class MockTableBuilder:
    """Mock Table Builder for Supabase table operations."""
    def __init__(self, db_state: dict[str, list[dict[str, Any]]], table_name: str) -> None:
        self.db_state = db_state
        self.table_name = table_name
        self.filters = []
        self._is_null_filters = []
        self._order = None

    def select(self, cols: str) -> "MockTableBuilder":
        return self

    def eq(self, col: str, val: Any) -> "MockTableBuilder":
        self.filters.append((col, val))
        return self

    def is_(self, col: str, val: Any) -> "MockTableBuilder":
        self._is_null_filters.append((col, val))
        return self

    def order(self, col: str, desc: bool = False) -> "MockTableBuilder":
        self._order = (col, desc)
        return self

    def limit(self, val: int) -> "MockTableBuilder":
        return self

    def execute(self) -> Any:
        class Result:
            def __init__(self, data: list[dict[str, Any]]) -> None:
                self.data = data

        rows = self.db_state.get(self.table_name, [])
        matched = []
        for row in rows:
            ok = True
            for col, val in self.filters:
                row_val = row.get(col)
                if isinstance(val, list):
                    if row_val not in val:
                        ok = False
                else:
                    if row_val != val:
                        ok = False
            for col, val in self._is_null_filters:
                row_val = row.get(col)
                if val == "null" and row_val is not None:
                    ok = False
            if ok:
                matched.append(row.copy())
        return Result(matched)

    def insert(self, data: dict[str, Any]) -> "MockTableBuilder":
        self.db_state.setdefault(self.table_name, []).append(data.copy())
        return self

    def update(self, data: dict[str, Any]) -> "MockTableBuilder":
        table = self.db_state.get(self.table_name, [])
        for row in table:
            match = True
            for col, val in self.filters:
                if row.get(col) != val:
                    match = False
            if match:
                row.update(data)
        return self

    def delete(self) -> "MockTableBuilder":
        table = self.db_state.get(self.table_name, [])
        remaining = []
        for row in table:
            match = True
            for col, val in self.filters:
                if row.get(col) != val:
                    match = False
            if not match:
                remaining.append(row)
        self.db_state[self.table_name] = remaining
        return self


class MockSupabaseClient:
    def __init__(self, db_state: dict[str, list[dict[str, Any]]]) -> None:
        self.db_state = db_state

    def table(self, name: str) -> MockTableBuilder:
        return MockTableBuilder(self.db_state, name)


@pytest.fixture
def db_state() -> dict[str, list[dict[str, Any]]]:
    """Fresh mock database state."""
    return {
        "open_positions": [],
        "closed_trades": [],
        "idempotency_log": []
    }


@pytest.fixture
def mock_supabase_client(db_state: dict[str, list[dict[str, Any]]]) -> Generator[MockSupabaseClient, None, None]:
    client = MockSupabaseClient(db_state)
    async def fake_get_client() -> MockSupabaseClient:
        return client

    with patch("execution.reconciliation.get_client", fake_get_client):
        yield client


# ─────────────────────────────────────────────
# UNIT TESTS MAPPED TO TESTING.MD LAYER 7
# ─────────────────────────────────────────────

@pytest.mark.anyio
async def test_7_2_reconciliation_before_signal_processing(
    mock_supabase_client: MockSupabaseClient,
    db_state: dict[str, list[dict[str, Any]]]
) -> None:
    """Criterion 7.2: Startup reconciliation completes successfully before signal processing begins."""
    # Seed Supabase with clean initial state
    db_state["open_positions"].append({
        "id": "pos-1",
        "market_id": "M1",
        "market_question": "Will rates cut in May?",
        "direction": "YES",
        "entry_price": 0.50,
        "position_size_usdc": 10.0,
        "strategy": "recalibration"
    })
    
    # Mock CLOB client
    mock_clob = MagicMock()
    mock_clob.get_balance_allowance.side_effect = [
        {"balance": "10000000"},  # Collateral USDC balance ($10 USDC)
        {"balance": "20000000"},  # Conditional YES shares balance (20 shares = $10 / 0.5)
    ]
    mock_clob.get_market.return_value = {
        "resolved": False,
        "tokens": [{"outcome": "YES", "token_id": "token-yes-1"}]
    }
    
    # Track order of log operations
    log_sequence = []
    
    def fake_log_info(msg: str, *args: Any, **kwargs: Any) -> None:
        if "[RECONCILIATION]" in msg:
            log_sequence.append(msg)
            
    with patch("execution.reconciliation.get_polymarket_client", return_value=mock_clob), \
         patch("execution.reconciliation.logger.info", fake_log_info):
        
        await reconcile_on_startup()

    # Assert expected exact log sequence order
    assert len(log_sequence) >= 5
    assert "[RECONCILIATION] Starting startup reconciliation" in log_sequence[0]
    assert "[RECONCILIATION] Fetching Polymarket positions..." in log_sequence[1]
    assert "[RECONCILIATION] Fetching USDC balance..." in log_sequence[2]
    assert "[RECONCILIATION] Diffing against Supabase state..." in log_sequence[3]
    assert "[RECONCILIATION] Reconciliation complete. State authoritative." in log_sequence[-1]


@pytest.mark.anyio
async def test_7_3_reconciliation_retry_and_alert_on_api_unavailability(
    mock_supabase_client: MockSupabaseClient,
    db_state: dict[str, list[dict[str, Any]]]
) -> None:
    """Criterion 7.3: Reconciliation retry loop executes every 60s and dispatches alert on > 5m downtime."""
    mock_clob = MagicMock()
    
    # Simulate API failure three times, then succeed
    mock_clob.get_balance_allowance.side_effect = [
        Exception("Polymarket CLOB API down"),  # Attempt 1
        Exception("Polymarket CLOB API down"),  # Attempt 2
        Exception("Polymarket CLOB API down"),  # Attempt 3 (past 5m)
        {"balance": "10000000"},  # Attempt 4: Collateral USDC balance ($10)
    ]
    
    sleep_calls = []
    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        
    loop_times = [0.0, 60.0, 360.0, 420.0]  # Simulate passage of time past 5 minutes (300 seconds)
    time_idx = 0
    
    def fake_loop_time() -> float:
        nonlocal time_idx
        t = loop_times[time_idx]
        time_idx = min(time_idx + 1, len(loop_times) - 1)
        return t

    with patch("execution.reconciliation.get_polymarket_client", return_value=mock_clob), \
         patch("asyncio.sleep", fake_sleep), \
         patch("asyncio.get_event_loop") as mock_loop, \
         patch("execution.reconciliation.alert_reconciliation_failure") as mock_alert:
         
        # Setup event loop time mock
        mock_loop_instance = MagicMock()
        mock_loop_instance.time = fake_loop_time
        mock_loop.return_value = mock_loop_instance
        
        await reconcile_on_startup()

    # Verifies retry every 60 seconds (3 sleeps total before success)
    assert len(sleep_calls) == 3
    assert all(s == 60.0 for s in sleep_calls)
    
    # Verify Telegram alert fires for >5m downtime
    assert mock_alert.called
    assert "unavailable for > 5 minutes" in mock_alert.call_args[0][0]


@pytest.mark.anyio
async def test_7_4_reconciliation_halt_on_inconsistency(
    mock_supabase_client: MockSupabaseClient,
    db_state: dict[str, list[dict[str, Any]]]
) -> None:
    """Criterion 7.4: Reconciliation halts and dispatches alert on unresolvable state inconsistency."""
    # Seed Supabase with active position
    db_state["open_positions"].append({
        "id": "pos-1",
        "market_id": "M1",
        "market_question": "Question?",
        "direction": "YES",
        "entry_price": 0.50,
        "position_size_usdc": 10.0,
        "strategy": "recalibration"
    })
    
    # Mock Polymarket CLOB client to return 0 shares for this active market
    mock_clob = MagicMock()
    mock_clob.get_balance_allowance.side_effect = [
        {"balance": "10000000"},  # USDC collateral balance
        {"balance": "0"},         # Conditional YES shares balance (0 shares, expected 20)
    ]
    # Simulate active (not resolved) market
    mock_clob.get_market.return_value = {
        "resolved": False,
        "tokens": [{"outcome": "YES", "token_id": "token-yes-1"}]
    }

    with patch("execution.reconciliation.get_polymarket_client", return_value=mock_clob), \
         patch("execution.reconciliation.alert_reconciliation_failure") as mock_alert:
         
        with pytest.raises(RuntimeError, match="Startup reconciliation failed"):
            await reconcile_on_startup()

    # Verifies system halt alert dispatched to Telegram
    assert mock_alert.called
    assert "M1" in mock_alert.call_args[0][0]
    assert "0 actual shares" in mock_alert.call_args[0][0]
