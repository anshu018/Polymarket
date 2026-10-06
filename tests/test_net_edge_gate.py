"""
tests/test_net_edge_gate.py — Pipeline integration tests for the Step 1 net-edge gate
(List A.md Step 1 — A2).

Proves end to end:
  1. The gate blocks when the live book's costs eat the edge (net_edge ≤
     MIN_NET_EDGE_CENTS) with the "risk_gate:low_net_edge" counter.
  2. A taker at an extreme low price is blocked by the tradeable price band even
     when the net edge passes — the 2¢ taker-fee trap, at pipeline level.
  3. A maker-order strategy (copy_edge_class_b) bypasses the price band and completes.
  4. An unavailable book fails closed ("risk_gate:book_unavailable").
  5. The mandatory decision-log line ([OBSERVABILITY][NET_EDGE]) carries every
     required field on every entry evaluation.
"""

from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import config
from coordinator.pipeline import run_pipeline, get_drop_counters, _drop_counters
from coordinator.market_state import reset_market_locks
from llm.news_analyst import NewsAnalystOutput
from llm.contract_parser import ContractParserOutput
from llm.trade_decision import TradeDecisionOutput
from risk.cost_model import BookSnapshot


# ── Mock Supabase + book + estimate plumbing ──────────────────────────────────

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
        return Result([r.copy() for r in rows
                       if all(r.get(col) == val for col, val in self.filters)])

    def insert(self, data: Any) -> "MockTableBuilder":
        rows = data if isinstance(data, list) else [data]
        for item in rows:
            self.db_state.setdefault(self.table_name, []).append(item.copy())
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
    reset_market_locks()
    for k in _drop_counters:
        _drop_counters[k] = 0
    yield
    reset_market_locks()


@pytest.fixture
def book_holder() -> dict[str, Any]:
    """The live book used by the gate — tests mutate this to shape the market."""
    return {"book": BookSnapshot(best_bid=0.48, best_ask=0.52, depth_usd=5000.0)}


@pytest.fixture
def estimate_holder() -> dict[str, Any]:
    """The fake estimate value — tests mutate this to shape the model's view."""
    return {"p_point": 0.65}


@pytest.fixture
def mock_db(book_holder: dict[str, Any], estimate_holder: dict[str, Any]):
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

    async def fake_book(token_id: str):
        return book_holder["book"]

    async def fake_get_estimate(*args, **kwargs):
        from strategies.estimator import EstimateResult
        return EstimateResult(
            p_point=estimate_holder["p_point"],
            sample_size=100,
            method="recalibration_base_rate",
            computed_at=datetime.now(timezone.utc),
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
            direction="YES", confidence_score=0.80, reasoning="Edge confirmed"), False

    async def fake_classify(*args, **kwargs):
        return NewsAnalystOutput(
            event_category="politics",
            direction="YES",
            confidence_score=0.80,
            reasoning="Valid signal",
        )

    with patch("memory.supabase_client.get_client", fake_get_client), \
         patch("coordinator.pipeline.get_client", fake_get_client), \
         patch("llm.contract_parser.get_client", fake_get_client), \
         patch("strategies.estimator.get_client", fake_get_client), \
         patch("copytrade.performance_tracker.get_client", fake_get_client), \
         patch("coordinator.pipeline.classify_signal", fake_classify), \
         patch("coordinator.pipeline.get_estimate", fake_get_estimate), \
         patch("coordinator.pipeline.get_market_book", fake_book), \
         patch("coordinator.pipeline.parse_contract", fake_parse), \
         patch("coordinator.pipeline.decide_trade", fake_decide), \
         patch("asyncio.sleep", AsyncMock()):
        yield state


async def run_default_pipeline(**overrides):
    kwargs: dict[str, Any] = dict(
        headline="Congress passes new spending bill",
        source="Reuters",
        market_id="mkt-gate",
        market_question="Will the bill pass?",
        market_price=0.50,
        portfolio_value=10_000.0,
        starting_balances={"daily": 10000.0, "weekly": 10000.0, "monthly": 10000.0},
        current_balances={"daily": 10000.0, "weekly": 10000.0, "monthly": 10000.0},
    )
    kwargs.update(overrides)
    return await run_pipeline(**kwargs)


# ── 1. Net-edge minimum ───────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_gate_blocks_when_costs_eat_the_edge(mock_db, book_holder, estimate_holder):
    """A wide book (spread + slippage > gross edge) must block at low_net_edge.
    Uses a taker strategy — makers pay no spread and would correctly pass."""
    book_holder["book"] = BookSnapshot(best_bid=0.20, best_ask=0.80, depth_usd=5000.0)

    result = await run_default_pipeline(strategy_override="velocity")  # velocity → taker

    assert result is not None
    assert result["status"] == "blocked"
    assert result["reason"] == "low_net_edge"
    assert get_drop_counters()["risk_gate:low_net_edge"] == 1
    assert len(mock_db["open_positions"]) == 0


# ── 2. Price band (the 2¢ taker-fee trap, pipeline level) ─────────────────────

@pytest.mark.anyio
async def test_low_price_taker_blocked_by_price_band(mock_db, book_holder, estimate_holder):
    """Taker at a 2¢ mid: net edge passes but the tradeable band blocks the entry."""
    estimate_holder["p_point"] = 0.09  # "7¢ gross edge" fiction at a 2¢ market
    book_holder["book"] = BookSnapshot(best_bid=0.01, best_ask=0.03, depth_usd=5000.0)

    result = await run_default_pipeline(strategy_override="velocity")  # velocity → taker

    assert result is not None
    assert result["status"] == "blocked"
    assert result["reason"] == "price_band"
    assert get_drop_counters()["risk_gate:price_band"] == 1
    assert get_drop_counters()["risk_gate:low_net_edge"] == 0
    assert len(mock_db["open_positions"]) == 0


# ── 3. Maker bypass of the band ───────────────────────────────────────────────

@pytest.mark.anyio
async def test_maker_bypasses_price_band_and_completes(
    mock_db, book_holder, caplog
):
    """copy_edge_class_b (maker) at a 2¢ mid: band bypassed, real estimate flows."""
    import logging

    mock_db["tracked_wallets"].append({
        "wallet_address": "wal-abc", "wins_count": 18, "losses_count": 2,
    })
    book_holder["book"] = BookSnapshot(best_bid=0.01, best_ask=0.03, depth_usd=5000.0)

    with caplog.at_level(logging.INFO, logger="coordinator.pipeline"):
        result = await run_default_pipeline(
            strategy_override="copy_edge_class_b",
            wallet_address="wal-abc",
        )

    assert result is not None
    assert result["status"] == "success"
    assert len(mock_db["open_positions"]) == 1
    assert mock_db["open_positions"][0]["strategy"] == "copy_edge_class_b"
    # The mandatory log line records the maker decision.
    assert "order_type=maker" in caplog.text
    assert get_drop_counters()["risk_gate:price_band"] == 0


# ── 4. Unavailable book fails closed ──────────────────────────────────────────

@pytest.mark.anyio
async def test_unavailable_book_fails_closed(mock_db, book_holder, estimate_holder):
    book_holder["book"] = None  # book fetch failed

    result = await run_default_pipeline()

    assert result is not None
    assert result["status"] == "blocked"
    assert result["reason"] == "book_unavailable"
    assert get_drop_counters()["risk_gate:book_unavailable"] == 1
    assert len(mock_db["open_positions"]) == 0


# ── 5. Mandatory decision-log line ────────────────────────────────────────────

@pytest.mark.anyio
async def test_decision_log_line_has_all_mandatory_fields(
    mock_db, book_holder, estimate_holder, caplog
):
    """Every entry evaluation logs gross_edge, fee_units, spread_units,
    slippage_units, net_edge, order_type, price_band_ok (List A.md Step 1)."""
    import logging

    with caplog.at_level(logging.INFO, logger="coordinator.pipeline"):
        result = await run_default_pipeline()

    assert result is not None and result["status"] == "success"
    net_edge_lines = [line for line in caplog.text.splitlines()
                      if "[OBSERVABILITY][NET_EDGE]" in line]
    assert len(net_edge_lines) >= 1, "mandatory decision-log line missing"
    line = net_edge_lines[0]
    for field in ("gross_edge=", "fee_units=", "spread_units=", "slippage_units=",
                  "haircut_units=", "net_edge=", "order_type=", "price_band_ok="):
        assert field in line, f"decision-log line missing {field}"
