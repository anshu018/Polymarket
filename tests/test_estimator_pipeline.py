"""
tests/test_estimator_pipeline.py — Pipeline integration tests for List A.md Step 0.

Proves the fail-closed estimator swap end to end:
  1. A main-pipeline signal (recalibration estimator = dormant stub) is dropped at
     "estimate:no_data" BEFORE the Contract Parser and Trade Decision LLM calls —
     the LLM mocks record zero calls (zero token burn).
  2. A fast-path-eligible signal is dropped the same way (the main news pipeline is
     fully dormant on BOTH paths by design).
  3. A copy_edge_class_b signal with tracked-wallet history flows through with the REAL
     Laplace estimate: the constant (market_price + 0.10) is unreachable — the Trade
     Decision call receives p_point = (18+1)/(18+2+2) = 19/22 with its method and
     sample size, and the edge gate + open_positions row carry the same value.
  4. time_to_resolution_hours is parsed from the real end_date_iso (not the old
     simulated constant).
  5. A missing end_date_iso falls back to config.DEFAULT_TTR_HOURS with the visible
     "estimate:ttr_fallback" drop-counter tag, and the signal still proceeds.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Generator
from unittest.mock import AsyncMock, patch

import pytest
import config
from risk import cost_model
from coordinator.pipeline import run_pipeline, get_drop_counters, _drop_counters
from coordinator.market_state import reset_market_locks
from llm.news_analyst import NewsAnalystOutput
from llm.contract_parser import ContractParserOutput
from llm.trade_decision import TradeDecisionOutput
from risk.cost_model import BookSnapshot


# ── Mock Supabase (same pattern as tests/test_dedupe_gate.py) ─────────────────

class MockTableBuilder:
    def __init__(self, db_state: dict[str, list[dict[str, Any]]], table_name: str) -> None:
        self.db_state = db_state
        self.table_name = table_name
        self.filters: list[tuple[str, Any]] = []

    def select(self, cols: str) -> "MockTableBuilder":
        return self

    def eq(self, col: str, val: Any) -> "MockTableBuilder":
        self.filters.append((col, val))
        return self

    def order(self, col: str, desc: bool = False) -> "MockTableBuilder":
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
            if all(r.get(col) == val for col, val in self.filters):
                matched.append(r.copy())
        return Result(matched)

    def insert(self, data: Any) -> "MockTableBuilder":
        rows = data if isinstance(data, list) else [data]
        for item in rows:
            d = item.copy()
            self.db_state.setdefault(self.table_name, []).append(d)
        return self

    def update(self, data: dict[str, Any]) -> "MockTableBuilder":
        for row in self.db_state.get(self.table_name, []):
            if all(row.get(col) == val for col, val in self.filters):
                row.update(data)
        return self

    def delete(self) -> "MockTableBuilder":
        return self


class MockSupabaseClient:
    def __init__(self, db_state: dict[str, list[dict[str, Any]]]) -> None:
        self.db_state = db_state

    def table(self, name: str) -> MockTableBuilder:
        return MockTableBuilder(self.db_state, name)


@pytest.fixture(autouse=True)
def clean_test_environment():
    """Reset market locks and drop counters around every test."""
    reset_market_locks()
    for k in _drop_counters:
        _drop_counters[k] = 0
    yield
    reset_market_locks()


@pytest.fixture
def mock_db():
    state: dict[str, list[dict[str, Any]]] = {
        "open_positions": [],
        "closed_trades": [],
        "market_signals": [],
        "idempotency_log": [],
        "agent_memory": [],
        "resolution_keyword_cache": [],
        "tracked_wallets": [],
    }
    client = MockSupabaseClient(state)

    async def fake_get_client():
        return client

    # Sane live book for the Step 1 net-edge gate (harmless for the dormant-path
    # tests that drop before the gate; needed by the copy_class_b flow tests).
    async def fake_book(token_id: str):
        return BookSnapshot(best_bid=0.48, best_ask=0.52, depth_usd=5000.0)

    with patch("memory.supabase_client.get_client", fake_get_client), \
         patch("coordinator.pipeline.get_client", fake_get_client), \
         patch("llm.contract_parser.get_client", fake_get_client), \
         patch("strategies.estimator.get_client", fake_get_client), \
         patch("copytrade.performance_tracker.get_client", fake_get_client), \
         patch("coordinator.pipeline.get_market_book", fake_book):
        yield state


# ── Shared LLM fakes ──────────────────────────────────────────────────────────

def make_classify(confidence: float, direction: str = "YES", category: str = "politics"):
    async def fake_classify(*args, **kwargs):
        return NewsAnalystOutput(
            event_category=category,
            direction=direction,
            confidence_score=confidence,
            reasoning="Valid signal",
        )

    return fake_classify


async def fake_parse(*args, **kwargs):
    return ContractParserOutput(
        resolution_source="Reuters",
        resolution_condition="Condition",
        key_entities=["Trump"],
        resolution_keywords=["trump", "win", "president"],
        ambiguity_score=0.1,
        resolution_type="binary",
    )


# ── 1 + 2. Main pipeline dormant on both paths ────────────────────────────────

@pytest.mark.anyio
async def test_main_pipeline_signal_dropped_pre_llm_with_no_estimate(mock_db):
    """Full-path signal: dormant recalibration estimator → drop BEFORE any LLM call."""
    parse_mock = AsyncMock(return_value=None)
    decide_mock = AsyncMock(return_value=(TradeDecisionOutput(
        direction="YES", confidence_score=0.88, reasoning="r"), False))

    with patch("coordinator.pipeline.classify_signal", make_classify(0.80)), \
         patch("coordinator.pipeline.parse_contract", parse_mock), \
         patch("coordinator.pipeline.decide_trade", decide_mock):
        result = await run_pipeline(
            headline="Congress passes new spending bill",
            source="Reuters",
            market_id="mkt-dormant-full",
            market_question="Will the bill pass?",
            market_price=0.50,
            portfolio_value=10_000.0,
            starting_balances={"daily": 10000.0, "weekly": 10000.0, "monthly": 10000.0},
            current_balances={"daily": 10000.0, "weekly": 10000.0, "monthly": 10000.0},
        )

    assert result is not None
    assert result["status"] == "blocked"
    assert result["reason"] == "estimate_no_data"
    # Zero token burn: Contract Parser and Trade Decision never called.
    assert parse_mock.await_count == 0
    assert decide_mock.await_count == 0
    assert get_drop_counters()["estimate:no_data"] == 1
    assert len(mock_db["open_positions"]) == 0


@pytest.mark.anyio
async def test_fast_path_signal_dropped_at_estimator_gate(mock_db):
    """Fast-path signal: dormant velocity estimator → dropped, no order, no position."""
    mid = "mkt-dormant-fast"
    # Seed a fresh cache hit so the fast path would trigger (pre-validated category,
    # confidence > 0.87, cached keywords matching the headline).
    mock_db["resolution_keyword_cache"] = [{
        "market_id": mid,
        "resolution_keywords": ["spending", "bill", "congress"],
        "cached_at": datetime.now(timezone.utc).isoformat(),
    }]

    decide_mock = AsyncMock()

    with patch("coordinator.pipeline.classify_signal", make_classify(0.95)), \
         patch("coordinator.pipeline.parse_contract", AsyncMock()), \
         patch("coordinator.pipeline.decide_trade", decide_mock):
        result = await run_pipeline(
            headline="Congress passes new spending bill",
            source="Reuters",
            market_id=mid,
            market_question="Will the bill pass?",
            market_price=0.50,
            portfolio_value=10_000.0,
            starting_balances={"daily": 10000.0, "weekly": 10000.0, "monthly": 10000.0},
            current_balances={"daily": 10000.0, "weekly": 10000.0, "monthly": 10000.0},
        )

    assert result is not None
    assert result["status"] == "blocked"
    assert result["reason"] == "estimate_no_data"
    assert decide_mock.await_count == 0
    assert get_drop_counters()["estimate:no_data"] == 1
    assert len(mock_db["open_positions"]) == 0


# ── 3. copy_edge_class_b: real estimate flows end to end ──────────────────────

@pytest.mark.anyio
async def test_copy_class_b_uses_real_laplace_estimate_not_constant(mock_db):
    """Class B signal with 18W/2L wallet: estimate = 19/22 everywhere, no constant."""
    mock_db["tracked_wallets"] = [{
        "wallet_address": "wal-abc",
        "wins_count": 18,
        "losses_count": 2,
    }]

    decide_calls: list[dict] = []

    async def fake_decide(**kwargs):
        decide_calls.append(kwargs)
        return TradeDecisionOutput(
            direction="YES", confidence_score=0.80, reasoning="Edge confirmed"), False

    breakdown_calls: list[dict] = []
    real_breakdown = cost_model.compute_cost_breakdown

    def spy_breakdown(**kwargs):
        breakdown_calls.append(kwargs)
        return real_breakdown(**kwargs)

    with patch("coordinator.pipeline.classify_signal", make_classify(0.80)), \
         patch("coordinator.pipeline.parse_contract", fake_parse), \
         patch("coordinator.pipeline.decide_trade", fake_decide), \
         patch.object(cost_model, "compute_cost_breakdown", spy_breakdown), \
         patch("asyncio.sleep", AsyncMock()):
        result = await run_pipeline(
            headline="Smart money trader whale_1 entered YES position on Will the bill pass?",
            source="copy_edge:whale_1",
            market_id="mkt-copy-b",
            market_question="Will the bill pass?",
            market_price=0.50,
            portfolio_value=10_000.0,
            starting_balances={"daily": 10000.0, "weekly": 10000.0, "monthly": 10000.0},
            current_balances={"daily": 10000.0, "weekly": 10000.0, "monthly": 10000.0},
            strategy_override="copy_edge_class_b",
            wallet_address="wal-abc",
        )

    assert result is not None, "Class B signal with wallet history must not be dropped"
    assert result["status"] == "success"

    expected_p = 19 / 22  # Laplace: (18+1)/(18+2+2)
    assert len(decide_calls) == 1
    kwargs = decide_calls[0]
    # The Trade Decision call receives the measured estimate — the constant is unreachable.
    assert kwargs["agent_estimate"] == pytest.approx(expected_p)
    assert kwargs["agent_estimate"] != pytest.approx(0.50 + 0.10)
    assert kwargs["estimate_method"] == "copy_wallet_hitrate"
    assert kwargs["estimate_sample_size"] == 20

    # The net-edge gate consumes the same measured value (List A.md Step 1).
    assert len(breakdown_calls) == 1
    assert breakdown_calls[0]["p_model"] == pytest.approx(expected_p)

    # The open_positions row records the measured estimate as agent_estimate.
    assert len(mock_db["open_positions"]) == 1
    assert mock_db["open_positions"][0]["agent_estimate"] == pytest.approx(expected_p)
    assert mock_db["open_positions"][0]["strategy"] == "copy_edge_class_b"


# ── 4 + 5. time_to_resolution from real end_date_iso ──────────────────────────

def patch_market_discovery(end_date_iso: str):
    """Force the cache-match path so end_date_iso reaches the pipeline."""
    best_match = {
        "market_id": "mkt-ttr",
        "token_id": "tok-1",
        "question": "Will the bill pass?",
        "end_date_iso": end_date_iso,
    }

    async def fake_price(token_id: str) -> float:
        return 0.50

    async def fake_metadata(market_id: str) -> dict:
        return {
            "question": "Will the bill pass?",
            "resolution_criteria": "Official passage into law.",
            "end_date_iso": end_date_iso,
        }

    return patch("coordinator.pipeline.find_matching_markets", lambda entities: [best_match]), \
           patch("coordinator.pipeline.get_market_price", fake_price), \
           patch("coordinator.pipeline.get_market_metadata", fake_metadata)


@pytest.mark.anyio
async def test_time_to_resolution_parsed_from_real_end_date(mock_db):
    """end_date_iso 48h away → Trade Decision receives ≈48h, not the fallback."""
    mock_db["tracked_wallets"] = [{
        "wallet_address": "wal-abc", "wins_count": 18, "losses_count": 2,
    }]
    end_iso = (datetime.now(timezone.utc) + timedelta(hours=48)).isoformat()

    decide_calls: list[dict] = []

    async def fake_decide(**kwargs):
        decide_calls.append(kwargs)
        return TradeDecisionOutput(
            direction="YES", confidence_score=0.80, reasoning="Edge confirmed"), False

    p1, p2, p3 = patch_market_discovery(end_iso)
    with patch("coordinator.pipeline.classify_signal", make_classify(0.80)), \
         patch("coordinator.pipeline.parse_contract", fake_parse), \
         patch("coordinator.pipeline.decide_trade", fake_decide), \
         patch("asyncio.sleep", AsyncMock()), p1, p2, p3:
        result = await run_pipeline(
            headline="Smart money trader whale_1 entered YES position on Will the bill pass?",
            source="copy_edge:whale_1",
            market_price=0.50,
            portfolio_value=10_000.0,
            starting_balances={"daily": 10000.0, "weekly": 10000.0, "monthly": 10000.0},
            current_balances={"daily": 10000.0, "weekly": 10000.0, "monthly": 10000.0},
            strategy_override="copy_edge_class_b",
            wallet_address="wal-abc",
        )

    assert result is not None and result["status"] == "success"
    assert len(decide_calls) == 1
    ttr = decide_calls[0]["time_to_resolution_hours"]
    assert ttr == pytest.approx(48.0, abs=0.01)
    assert ttr != float(config.DEFAULT_TTR_HOURS)
    assert get_drop_counters()["estimate:ttr_fallback"] == 0


@pytest.mark.anyio
async def test_missing_end_date_falls_back_to_config_default_with_counter(mock_db):
    """Missing end_date_iso → DEFAULT_TTR_HOURS + visible estimate:ttr_fallback tag."""
    mock_db["tracked_wallets"] = [{
        "wallet_address": "wal-abc", "wins_count": 18, "losses_count": 2,
    }]

    decide_calls: list[dict] = []

    async def fake_decide(**kwargs):
        decide_calls.append(kwargs)
        return TradeDecisionOutput(
            direction="YES", confidence_score=0.80, reasoning="Edge confirmed"), False

    with patch("coordinator.pipeline.classify_signal", make_classify(0.80)), \
         patch("coordinator.pipeline.parse_contract", fake_parse), \
         patch("coordinator.pipeline.decide_trade", fake_decide), \
         patch("coordinator.pipeline.find_matching_markets", lambda entities: []), \
         patch("asyncio.sleep", AsyncMock()):
        result = await run_pipeline(
            headline="Smart money trader whale_1 entered YES position on Will the bill pass?",
            source="copy_edge:whale_1",
            market_id="mkt-no-enddate",
            market_question="Will the bill pass?",
            market_price=0.50,
            portfolio_value=10_000.0,
            starting_balances={"daily": 10000.0, "weekly": 10000.0, "monthly": 10000.0},
            current_balances={"daily": 10000.0, "weekly": 10000.0, "monthly": 10000.0},
            strategy_override="copy_edge_class_b",
            wallet_address="wal-abc",
        )

    # The fallback is a visibility tag, NOT a drop — the signal still completes.
    assert result is not None and result["status"] == "success"
    assert len(decide_calls) == 1
    assert decide_calls[0]["time_to_resolution_hours"] == pytest.approx(
        float(config.DEFAULT_TTR_HOURS))
    assert get_drop_counters()["estimate:ttr_fallback"] == 1
    assert get_drop_counters()["estimate:no_data"] == 0
