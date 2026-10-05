"""
tests/test_dedupe_gate.py — Comprehensive integration test suite for the Per-Market Tranche Gate.

Verifies:
  1. Exact order counts: 4 signals at 0.88 -> exactly 2 orders.
  2. Exact order counts: 4 signals with 2nd, 3rd, 4th at 0.86 -> exactly 1 order.
  3. Second signal at 0.87 -> allowed, capped at 3% of portfolio ($300).
  4. Concurrent workers on same market -> exactly 2 orders total.
  5. load_market_state returns None -> 0 orders, no idempotency write, reason 'exposure_unavailable'.
  6. Opposite-direction signal -> blocked, no hedge order placed.
  7. Pre-check prevents Trade Decision LLM call when max tranches reached (0 LLM calls).
  8. All four order paths (Fast, Full, Class A, Class B) enforce the gate, and share the lock registry.
  9. $10 Class A first entry is permitted without being blocked by repeat floor.
  10. Drop counters increment accurately across rejection reasons.
  11. Real thread blocking timeout test (time.sleep) returns None.
"""

import asyncio
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Generator
from unittest.mock import patch, AsyncMock

import pytest
import config
from coordinator.pipeline import run_pipeline, get_drop_counters, _drop_counters
from coordinator.market_state import (
    get_market_lock,
    reset_market_locks,
    load_market_state,
    _MARKET_LOCKS,
)
from copytrade.executor import _execute_class_a, _execute_class_b
from llm.news_analyst import NewsAnalystOutput
from llm.contract_parser import ContractParserOutput
from llm.trade_decision import TradeDecisionOutput
from llm.coordinator import CoordinatorOutput


@pytest.fixture(autouse=True)
def clean_test_environment():
    """Reset market locks and drop counters before and after every test."""
    reset_market_locks()
    for k in _drop_counters:
        _drop_counters[k] = 0
    yield
    reset_market_locks()


class MockTableBuilder:
    def __init__(self, db_state: dict[str, list[dict[str, Any]]], table_name: str) -> None:
        self.db_state = db_state
        self.table_name = table_name
        self.filters = []
        self._order = None
        self._is_delete = False

    def select(self, cols: str) -> "MockTableBuilder":
        return self

    def eq(self, col: str, val: Any) -> "MockTableBuilder":
        self.filters.append((col, val))
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
        for r in rows:
            match = True
            for col, val in self.filters:
                if r.get(col) != val:
                    match = False
                    break
            if match:
                matched.append(r.copy())
        return Result(matched)

    def insert(self, data: Any) -> "MockTableBuilder":
        rows = data if isinstance(data, list) else [data]
        for item in rows:
            d = item.copy()
            if "id" not in d:
                d["id"] = f"mock-{uuid.uuid4()}"
            self.db_state.setdefault(self.table_name, []).append(d)
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
        self._is_delete = True
        return self


class MockSupabaseClient:
    def __init__(self, db_state: dict[str, list[dict[str, Any]]]) -> None:
        self.db_state = db_state

    def table(self, name: str) -> MockTableBuilder:
        return MockTableBuilder(self.db_state, name)


@pytest.fixture
def mock_db():
    state = {
        "open_positions": [],
        "closed_trades": [],
        "market_signals": [],
        "idempotency_log": [],
        "agent_memory": [],
        "resolution_keyword_cache": [],
    }
    client = MockSupabaseClient(state)

    async def fake_get_client():
        return client

    with patch("memory.supabase_client.get_client", fake_get_client), \
         patch("coordinator.pipeline.get_client", fake_get_client), \
         patch("copytrade.executor.get_client", fake_get_client), \
         patch("llm.contract_parser.get_client", fake_get_client):
        yield state


@pytest.mark.anyio
async def test_shared_lock_registry_across_modules():
    """Requirement 8: pipeline and executor resolve to the same lock registry."""
    from coordinator.pipeline import get_market_lock as pipeline_get_lock
    from copytrade.executor import get_market_lock as executor_get_lock

    lock1 = pipeline_get_lock("market-123")
    lock2 = executor_get_lock("market-123")
    assert lock1 is lock2
    assert "market-123" in _MARKET_LOCKS
    assert "market-123:YES" not in _MARKET_LOCKS


@pytest.mark.anyio
async def test_four_signals_at_high_confidence_produces_exactly_two_orders(mock_db):
    """Test 1: 4 distinct signals at 0.88 confidence -> exactly 2 orders."""
    market_id = "test-mkt-4-signals"

    async def fake_classify(*args, **kwargs):
        return NewsAnalystOutput(
            event_category="politics",
            direction="YES",
            confidence_score=0.88,
            reasoning="Valid high confidence signal",
        )

    async def fake_parse(*args, **kwargs):
        return ContractParserOutput(
            resolution_source="Reuters",
            resolution_condition="Condition",
            key_entities=["Trump"],
            resolution_keywords=["trump", "win", "president"],
            ambiguity_score=0.1,
            resolution_type="binary",
        )

    async def fake_decide(*args, **kwargs):
        return TradeDecisionOutput(
            direction="YES",
            confidence_score=0.88,
            reasoning="Strong edge"
        ), False

    with patch("coordinator.pipeline.classify_signal", fake_classify), \
         patch("coordinator.pipeline.parse_contract", fake_parse), \
         patch("coordinator.pipeline.decide_trade", fake_decide), \
         patch("asyncio.sleep", AsyncMock()):

        results = []
        for i in range(4):
            res = await run_pipeline(
                headline=f"Headline variation {i}",
                source="Reuters",
                market_id=market_id,
                market_question="Will Trump win?",
                market_price=0.50,
                portfolio_value=10_000.0,
                starting_balances={"daily": 10000.0, "weekly": 10000.0, "monthly": 10000.0},
                current_balances={"daily": 10000.0, "weekly": 10000.0, "monthly": 10000.0},
            )
            results.append(res)

    success_orders = [r for r in results if r and r.get("status") == "success"]
    assert len(success_orders) == 2, f"Expected exactly 2 orders, got {len(success_orders)}"
    assert results[2]["reason"] == "max_tranches_reached"
    assert results[3]["reason"] == "max_tranches_reached"
    assert len(mock_db["open_positions"]) == 2
    assert get_drop_counters()["risk_gate:max_market_tranches"] >= 2


@pytest.mark.anyio
async def test_four_signals_repeats_below_threshold_produces_exactly_one_order(mock_db):
    """Test 2: 4 signals with repeats at 0.86 confidence -> exactly 1 order."""
    market_id = "test-mkt-low-conf-repeat"
    current_conf = [0.88]

    async def fake_classify(*args, **kwargs):
        return NewsAnalystOutput(
            event_category="politics",
            direction="YES",
            confidence_score=current_conf[0],
            reasoning="Valid signal",
        )

    async def fake_parse(*args, **kwargs):
        return ContractParserOutput(
            resolution_source="Reuters",
            resolution_condition="Condition",
            key_entities=["Trump"],
            resolution_keywords=["trump", "win", "president"],
            ambiguity_score=0.1,
            resolution_type="binary",
        )

    async def fake_decide(*args, **kwargs):
        return TradeDecisionOutput(
            direction="YES",
            confidence_score=current_conf[0],
            reasoning="Edge"
        ), False

    with patch("coordinator.pipeline.classify_signal", fake_classify), \
         patch("coordinator.pipeline.parse_contract", fake_parse), \
         patch("coordinator.pipeline.decide_trade", fake_decide), \
         patch("asyncio.sleep", AsyncMock()):

        # 1st entry at 0.88 -> success
        res1 = await run_pipeline(
            headline="Headline 1", source="Reuters", market_id=market_id,
            market_question="Question?", market_price=0.50, portfolio_value=10_000.0,
            starting_balances={"daily": 10000.0, "weekly": 10000.0, "monthly": 10000.0},
            current_balances={"daily": 10000.0, "weekly": 10000.0, "monthly": 10000.0},
        )
        assert res1["status"] == "success"

        # 2nd, 3rd, 4th entries drop to 0.86
        current_conf[0] = 0.86
        blocked_results = []
        for i in range(2, 5):
            res = await run_pipeline(
                headline=f"Headline {i}", source="Reuters", market_id=market_id,
                market_question="Question?", market_price=0.50, portfolio_value=10_000.0,
                starting_balances={"daily": 10000.0, "weekly": 10000.0, "monthly": 10000.0},
                current_balances={"daily": 10000.0, "weekly": 10000.0, "monthly": 10000.0},
            )
            blocked_results.append(res)

    for r in blocked_results:
        assert r["status"] == "blocked"
        assert r["reason"] == "low_confidence_for_add"

    assert len(mock_db["open_positions"]) == 1, "Expected exactly 1 order in open_positions"
    assert get_drop_counters()["risk_gate:low_confidence_add"] == 3


@pytest.mark.anyio
async def test_repeat_at_087_capped_at_3pct_portfolio(mock_db):
    """Test 3: Repeat at 0.87 confidence allowed and capped at 3% of portfolio ($300)."""
    market_id = "test-mkt-087-repeat"
    current_conf = [0.88]

    async def fake_classify(*args, **kwargs):
        return NewsAnalystOutput(
            event_category="politics",
            direction="YES",
            confidence_score=current_conf[0],
            reasoning="Valid",
        )

    async def fake_parse(*args, **kwargs):
        return ContractParserOutput(
            resolution_source="AP",
            resolution_condition="Fed rate cut",
            key_entities=["Fed"],
            resolution_keywords=["fed", "rate", "cut"],
            ambiguity_score=0.1,
            resolution_type="binary",
        )

    async def fake_decide(*args, **kwargs):
        return TradeDecisionOutput(
            direction="YES",
            confidence_score=current_conf[0],
            reasoning="Edge"
        ), False

    with patch("coordinator.pipeline.classify_signal", fake_classify), \
         patch("coordinator.pipeline.parse_contract", fake_parse), \
         patch("coordinator.pipeline.decide_trade", fake_decide), \
         patch("asyncio.sleep", AsyncMock()):

        # 1st entry: 5% of $10,000 = $500
        res1 = await run_pipeline(
            headline="Fed rate cut expected", source="AP", market_id=market_id,
            market_question="Will Fed cut rates?", market_price=0.50, portfolio_value=10_000.0,
            starting_balances={"daily": 10000.0, "weekly": 10000.0, "monthly": 10000.0},
            current_balances={"daily": 10000.0, "weekly": 10000.0, "monthly": 10000.0},
        )
        assert res1["status"] == "success"

        # 2nd entry at exactly 0.87
        current_conf[0] = 0.87
        res2 = await run_pipeline(
            headline="Fed confirms rate cut", source="AP", market_id=market_id,
            market_question="Will Fed cut rates?", market_price=0.50, portfolio_value=10_000.0,
            starting_balances={"daily": 10000.0, "weekly": 10000.0, "monthly": 10000.0},
            current_balances={"daily": 10000.0, "weekly": 10000.0, "monthly": 10000.0},
        )
        assert res2["status"] == "success"

    positions = mock_db["open_positions"]
    assert len(positions) == 2
    assert positions[0]["position_size_usdc"] == 500.0
    assert positions[1]["position_size_usdc"] == 300.0  # Exactly 3% of $10,000


@pytest.mark.anyio
async def test_concurrent_workers_on_same_market(mock_db):
    """Test 4: Concurrent pipeline executions on one market produce at most 2 orders total."""
    market_id = "test-concurrent-market"

    async def fake_classify(*args, **kwargs):
        return NewsAnalystOutput(
            event_category="politics",
            direction="YES",
            confidence_score=0.88,
            reasoning="Valid",
        )

    async def fake_parse(*args, **kwargs):
        return ContractParserOutput(
            resolution_source="AP",
            resolution_condition="Rate hike",
            key_entities=["Rate"],
            resolution_keywords=["rate", "hike", "fed"],
            ambiguity_score=0.1,
            resolution_type="binary",
        )

    async def fake_decide(*args, **kwargs):
        return TradeDecisionOutput(
            direction="YES",
            confidence_score=0.88,
            reasoning="Edge"
        ), False

    with patch("coordinator.pipeline.classify_signal", fake_classify), \
         patch("coordinator.pipeline.parse_contract", fake_parse), \
         patch("coordinator.pipeline.decide_trade", fake_decide), \
         patch("asyncio.sleep", AsyncMock()):

        tasks = [
            run_pipeline(
                headline=f"Concurrent headline {i}", source="AP", market_id=market_id,
                market_question="Question?", market_price=0.50, portfolio_value=10_000.0,
                starting_balances={"daily": 10000.0, "weekly": 10000.0, "monthly": 10000.0},
                current_balances={"daily": 10000.0, "weekly": 10000.0, "monthly": 10000.0},
            )
            for i in range(4)
        ]
        results = await asyncio.gather(*tasks)

    success_orders = [r for r in results if r and r.get("status") == "success"]
    assert len(success_orders) == 2
    assert len(mock_db["open_positions"]) == 2


@pytest.mark.anyio
async def test_concurrent_workers_at_086_confidence_produce_exactly_one_order(mock_db):
    """Concurrent pipeline executions at 0.86 confidence on an empty market produce exactly 1 order."""
    market_id = "test-concurrent-market-086"

    async def fake_classify(*args, **kwargs):
        return NewsAnalystOutput(
            event_category="politics",
            direction="YES",
            confidence_score=0.86,
            reasoning="Valid",
        )

    async def fake_parse(*args, **kwargs):
        return ContractParserOutput(
            resolution_source="AP",
            resolution_condition="Rate hike",
            key_entities=["Rate"],
            resolution_keywords=["rate", "hike", "fed"],
            ambiguity_score=0.1,
            resolution_type="binary",
        )

    async def fake_decide(*args, **kwargs):
        return TradeDecisionOutput(
            direction="YES",
            confidence_score=0.86,
            reasoning="Edge",
        ), False

    with patch("coordinator.pipeline.classify_signal", fake_classify), \
         patch("coordinator.pipeline.parse_contract", fake_parse), \
         patch("coordinator.pipeline.decide_trade", fake_decide), \
         patch("asyncio.sleep", AsyncMock()):

        tasks = [
            run_pipeline(
                headline=f"Concurrent headline {i}", source="AP", market_id=market_id,
                market_question="Question?", market_price=0.50, portfolio_value=10_000.0,
                starting_balances={"daily": 10000.0, "weekly": 10000.0, "monthly": 10000.0},
                current_balances={"daily": 10000.0, "weekly": 10000.0, "monthly": 10000.0},
            )
            for i in range(4)
        ]
        results = await asyncio.gather(*tasks)

    success_orders = [r for r in results if r and r.get("status") == "success"]
    blocked_orders = [r for r in results if r and r.get("status") == "blocked"]

    assert len(success_orders) == 1, f"Expected exactly 1 order, got {len(success_orders)}"
    assert len(blocked_orders) == 3
    for b in blocked_orders:
        assert b["reason"] == "low_confidence_for_add"
    assert len(mock_db["open_positions"]) == 1


@pytest.mark.anyio
async def test_load_market_state_none_fails_closed(mock_db):
    """Test 5: load_market_state returning None causes fail-closed, no idempotency write."""
    market_id = "test-fail-closed"

    async def fake_classify(*args, **kwargs):
        return NewsAnalystOutput(
            event_category="politics",
            direction="YES",
            confidence_score=0.88,
            reasoning="Valid",
        )

    async def fake_parse(*args, **kwargs):
        return ContractParserOutput(
            resolution_source="Reuters",
            resolution_condition="Condition",
            key_entities=["Trump"],
            resolution_keywords=["trump", "win", "president"],
            ambiguity_score=0.1,
            resolution_type="binary",
        )

    async def fake_decide(*args, **kwargs):
        return TradeDecisionOutput(
            direction="YES",
            confidence_score=0.88,
            reasoning="Edge"
        ), False

    with patch("coordinator.pipeline.classify_signal", fake_classify), \
         patch("coordinator.pipeline.parse_contract", fake_parse), \
         patch("coordinator.pipeline.decide_trade", fake_decide), \
         patch("coordinator.pipeline.load_market_state", AsyncMock(return_value=None)), \
         patch("asyncio.sleep", AsyncMock()):

        res = await run_pipeline(
            headline="News signal", source="AP", market_id=market_id,
            market_question="Question?", market_price=0.50, portfolio_value=10_000.0,
            starting_balances={"daily": 10000.0, "weekly": 10000.0, "monthly": 10000.0},
            current_balances={"daily": 10000.0, "weekly": 10000.0, "monthly": 10000.0},
        )

    assert res["status"] == "blocked"
    assert res["reason"] == "exposure_unavailable"
    assert len(mock_db["idempotency_log"]) == 0
    assert len(mock_db["open_positions"]) == 0
    assert get_drop_counters()["risk_gate:exposure_unavailable"] >= 1


@pytest.mark.anyio
async def test_opposite_direction_signal_blocked(mock_db):
    """Test 6: Opposite-direction signal on a market with an open position is blocked."""
    market_id = "test-opposite-market"
    # Seed an open YES position
    mock_db["open_positions"].append({
        "id": "existing-yes",
        "market_id": market_id,
        "direction": "YES",
        "position_size_usdc": 500.0,
        "entry_price": 0.50,
        "category": "politics",
    })

    async def fake_classify(*args, **kwargs):
        return NewsAnalystOutput(
            event_category="politics",
            direction="NO",
            confidence_score=0.88,
            reasoning="Valid opposite",
        )

    with patch("coordinator.pipeline.classify_signal", fake_classify), \
         patch("asyncio.sleep", AsyncMock()):

        res = await run_pipeline(
            headline="Opposite news", source="AP", market_id=market_id,
            market_question="Question?", market_price=0.50, portfolio_value=10_000.0,
            starting_balances={"daily": 10000.0, "weekly": 10000.0, "monthly": 10000.0},
            current_balances={"daily": 10000.0, "weekly": 10000.0, "monthly": 10000.0},
        )

    assert res["status"] == "blocked"
    assert res["reason"] == "opposite_direction_open"
    assert len(mock_db["open_positions"]) == 1
    assert mock_db["open_positions"][0]["direction"] == "YES"
    assert get_drop_counters()["risk_gate:opposite_direction"] >= 1


@pytest.mark.anyio
async def test_precheck_skips_trade_decision_llm_when_max_tranches(mock_db):
    """Test 7: Pre-check skips Trade Decision LLM when max tranches reached."""
    market_id = "test-precheck-llm-skip"
    mock_db["open_positions"].extend([
        {"id": "p1", "market_id": market_id, "direction": "YES", "position_size_usdc": 500.0, "entry_price": 0.5},
        {"id": "p2", "market_id": market_id, "direction": "YES", "position_size_usdc": 300.0, "entry_price": 0.5},
    ])

    async def fake_classify(*args, **kwargs):
        return NewsAnalystOutput(
            event_category="politics",
            direction="YES",
            confidence_score=0.80,
            reasoning="Valid news",
        )

    decide_mock = AsyncMock()
    with patch("coordinator.pipeline.classify_signal", fake_classify), \
         patch("coordinator.pipeline.decide_trade", decide_mock), \
         patch("asyncio.sleep", AsyncMock()):

        res = await run_pipeline(
            headline="News when full", source="AP", market_id=market_id,
            market_question="Question?", market_price=0.50, portfolio_value=10_000.0,
            starting_balances={"daily": 10000.0, "weekly": 10000.0, "monthly": 10000.0},
            current_balances={"daily": 10000.0, "weekly": 10000.0, "monthly": 10000.0},
        )

    assert res["status"] == "blocked"
    assert res["reason"] == "max_tranches_reached"
    assert decide_mock.call_count == 0, "Trade Decision LLM should NOT be called on pre-check block"


@pytest.mark.anyio
async def test_class_a_first_entry_10_dollars_permitted(mock_db):
    """Test 9: A $10 Class A first entry is permitted without being blocked by repeat floor."""
    market_id = "test-class-a-small"
    signal = {
        "market_id": market_id,
        "outcome": "Yes",
        "live_ask": 0.50,
        "tracker_price": 0.495,
        "trader_name": "ProTrader",
        "wallet_address": "0x123",
        "was_priority_pick": False,
        "trust_score": 0.85,
        "market_volume_usd": 50000.0,
    }

    with patch("coordinator.pipeline.get_live_portfolio_value", AsyncMock(return_value=10_000.0)), \
         patch("copytrade.executor._fetch_exposure", AsyncMock(return_value=(0.0, 0.0))), \
         patch("copytrade.executor.get_trust_score", return_value=0.85), \
         patch("copytrade.executor.get_wallet_state", return_value="ACTIVE"), \
         patch("copytrade.executor.compute_state_multiplier", return_value=1.0), \
         patch("copytrade.executor._tracker_log_trade", AsyncMock()):

        await _execute_class_a(signal)

    assert len(mock_db["open_positions"]) == 1
    pos = mock_db["open_positions"][0]
    assert pos["position_size_usdc"] <= 10.0
    assert pos["position_size_usdc"] > 0.0


@pytest.mark.anyio
async def test_all_four_paths_hit_the_gate(mock_db):
    """Test 8: Fast path, Full path, Class A, and Class B all enforce the gate."""
    mkt = "test-mkt-four-paths"
    mock_db["open_positions"].extend([
        {"id": "t1", "market_id": mkt, "direction": "YES", "position_size_usdc": 500.0, "entry_price": 0.5},
        {"id": "t2", "market_id": mkt, "direction": "YES", "position_size_usdc": 300.0, "entry_price": 0.5},
    ])

    # Path 1: Fast Path (Seed keyword cache so fast path triggers)
    mock_db["resolution_keyword_cache"].append({
        "market_id": mkt,
        "keywords": ["fast", "path"],
        "cached_at": datetime.now(timezone.utc).isoformat(),
    })

    async def fake_fast_classify(*args, **kwargs):
        return NewsAnalystOutput(
            event_category="politics",
            direction="YES",
            confidence_score=0.88,
            reasoning="Fast path signal",
        )

    with patch("coordinator.pipeline.classify_signal", fake_fast_classify), \
         patch("asyncio.sleep", AsyncMock()):
        res_fast = await run_pipeline(
            headline="Fast path breaking news", source="AP", market_id=mkt,
            market_question="Q?", market_price=0.50, portfolio_value=10_000.0,
            starting_balances={"daily": 10000.0, "weekly": 10000.0, "monthly": 10000.0},
            current_balances={"daily": 10000.0, "weekly": 10000.0, "monthly": 10000.0},
        )
        assert res_fast["status"] == "blocked"
        assert res_fast["reason"] == "max_tranches_reached"

    # Path 2: Full Path
    async def fake_full_classify(*args, **kwargs):
        return NewsAnalystOutput(
            event_category="politics",
            direction="YES",
            confidence_score=0.80,
            reasoning="Full path signal",
        )

    with patch("coordinator.pipeline.classify_signal", fake_full_classify), \
         patch("asyncio.sleep", AsyncMock()):
        res_full = await run_pipeline(
            headline="Full path other headline", source="AP", market_id=mkt,
            market_question="Q?", market_price=0.50, portfolio_value=10_000.0,
            starting_balances={"daily": 10000.0, "weekly": 10000.0, "monthly": 10000.0},
            current_balances={"daily": 10000.0, "weekly": 10000.0, "monthly": 10000.0},
        )
        assert res_full["status"] == "blocked"
        assert res_full["reason"] == "max_tranches_reached"

    # Path 3: Copy Edge Class A
    sig_a = {
        "market_id": mkt,
        "outcome": "Yes",
        "live_ask": 0.50,
        "tracker_price": 0.50,
        "wallet_address": "0x1",
        "market_volume_usd": 50000.0,
    }
    with patch("coordinator.pipeline.get_live_portfolio_value", AsyncMock(return_value=10_000.0)), \
         patch("copytrade.executor._fetch_exposure", AsyncMock(return_value=(0.0, 0.0))), \
         patch("copytrade.executor.get_trust_score", return_value=0.85), \
         patch("copytrade.executor.get_wallet_state", return_value="ACTIVE"), \
         patch("copytrade.executor.compute_state_multiplier", return_value=1.0):
        await _execute_class_a(sig_a)

    # Path 4: Copy Edge Class B
    sig_b = {"market_id": mkt, "outcome": "Yes", "live_ask": 0.50, "trader_name": "TraderB"}
    session_mock = AsyncMock()
    await _execute_class_b(sig_b, session_mock)

    # Assert no new positions were opened across all 4 attempts
    assert len(mock_db["open_positions"]) == 2


@pytest.mark.anyio
async def test_drop_counters_increment_accurately(mock_db):
    """Test 10: Drop counters accurately increment for all tranche gate block scenarios."""
    mkt = "test-drop-counters-mkt"

    # 1. Opposite direction block
    mock_db["open_positions"].append({
        "id": "drop-pos-yes",
        "market_id": mkt,
        "direction": "YES",
        "position_size_usdc": 500.0,
        "entry_price": 0.50,
        "category": "politics",
    })

    async def fake_classify_no(*args, **kwargs):
        return NewsAnalystOutput(
            event_category="politics",
            direction="NO",
            confidence_score=0.88,
            reasoning="Opposite direction",
        )

    with patch("coordinator.pipeline.classify_signal", fake_classify_no), \
         patch("asyncio.sleep", AsyncMock()):
        await run_pipeline(
            headline="News in opposite direction", source="AP", market_id=mkt,
            market_price=0.50, portfolio_value=10000.0,
        )

    assert get_drop_counters()["risk_gate:opposite_direction"] == 1

    # 2. Low confidence repeat block (< 0.87)
    async def fake_classify_low_repeat(*args, **kwargs):
        return NewsAnalystOutput(
            event_category="politics",
            direction="YES",
            confidence_score=0.80,
            reasoning="Repeat under 0.87",
        )

    async def fake_parse(*args, **kwargs):
        return ContractParserOutput(
            resolution_source="AP",
            resolution_condition="Cond",
            key_entities=["Trump"],
            resolution_keywords=["trump", "win", "president"],
            ambiguity_score=0.1,
            resolution_type="binary",
        )

    async def fake_decide(*args, **kwargs):
        return TradeDecisionOutput(
            direction="YES",
            confidence_score=0.80,
            reasoning="Trade decision",
        ), False

    with patch("coordinator.pipeline.classify_signal", fake_classify_low_repeat), \
         patch("coordinator.pipeline.parse_contract", fake_parse), \
         patch("coordinator.pipeline.decide_trade", fake_decide), \
         patch("asyncio.sleep", AsyncMock()):
        await run_pipeline(
            headline="Repeat news under threshold", source="AP", market_id=mkt,
            market_price=0.50, portfolio_value=10000.0,
        )

    assert get_drop_counters()["risk_gate:low_confidence_add"] == 1

    # 3. Max tranches reached block
    mock_db["open_positions"].append({
        "id": "drop-pos-yes-2",
        "market_id": mkt,
        "direction": "YES",
        "position_size_usdc": 300.0,
        "entry_price": 0.50,
        "category": "politics",
    })

    async def fake_classify_high(*args, **kwargs):
        return NewsAnalystOutput(
            event_category="politics",
            direction="YES",
            confidence_score=0.88,
            reasoning="Tranche 3 attempt",
        )

    with patch("coordinator.pipeline.classify_signal", fake_classify_high), \
         patch("coordinator.pipeline.parse_contract", fake_parse), \
         patch("coordinator.pipeline.decide_trade", fake_decide), \
         patch("asyncio.sleep", AsyncMock()):
        await run_pipeline(
            headline="Third tranche attempt", source="AP", market_id=mkt,
            market_price=0.50, portfolio_value=10000.0,
        )

    assert get_drop_counters()["risk_gate:max_market_tranches"] >= 1


@pytest.mark.anyio
async def test_real_blocking_callable_times_out():
    """Test 11: Real blocking callable (time.sleep) triggers timeout near 2s and fails closed."""
    market_id = "test-real-blocking-timeout"

    class SlowTable:
        def select(self, *args, **kwargs):
            return self
        def eq(self, *args, **kwargs):
            return self
        def execute(self):
            # Real blocking thread sleep
            time.sleep(2.5)
            return type("Result", (), {"data": []})()

    class SlowClient:
        def table(self, name):
            return SlowTable()

    async def fake_get_client():
        return SlowClient()

    with patch("memory.supabase_client.get_client", fake_get_client):
        t0 = time.monotonic()
        state = await load_market_state(market_id, "YES")
        elapsed = time.monotonic() - t0

    assert state is None, "Should fail closed and return None on timeout"
    assert 1.9 <= elapsed <= 2.8, f"Expected timeout near 2.0s, took {elapsed:.2f}s"
