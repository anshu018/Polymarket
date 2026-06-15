"""
test_integration.py — Full pytest integration tests for Layer 6 Integration.

Matches TESTING.md criteria 6.1 to 6.10 exactly.
Uses complete mocks for database, network, and execution.
"""

import sys
import os
import json
import asyncio
import time
from datetime import datetime, timezone, timedelta
from typing import Generator, Any, Optional
from unittest.mock import patch

# Ensure project root is on the path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import config
from llm.news_analyst import NewsAnalystOutput
from llm.trade_decision import TradeDecisionOutput
from llm.coordinator import CoordinatorOutput
from coordinator.pipeline import run_pipeline

# ─────────────────────────────────────────────
# MOCK DATABASE STATE AND CLIENT
# ─────────────────────────────────────────────

class MockTableBuilder:
    """Mock Postgrest Table Builder for Supabase table operations."""

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

    def in_(self, col: str, vals: list[Any]) -> "MockTableBuilder":
        self.filters.append((col, vals))
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

        if self._order:
            col, desc = self._order
            matched.sort(key=lambda x: x.get(col, ""), reverse=desc)

        return Result(matched)

    def insert(self, data: dict[str, Any]) -> "MockTableBuilder":
        self.db_state.setdefault(self.table_name, []).append(data.copy())
        return self

    def upsert(self, data: dict[str, Any], on_conflict: str = None) -> "MockTableBuilder":
        table = self.db_state.setdefault(self.table_name, [])
        conflict_col = on_conflict or "market_id"
        conflict_val = data.get(conflict_col)
        
        updated = False
        for row in table:
            if row.get(conflict_col) == conflict_val:
                row.update(data)
                updated = True
                break
        if not updated:
            table.append(data.copy())
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


class MockSupabaseClient:
    def __init__(self, db_state: dict[str, list[dict[str, Any]]]) -> None:
        self.db_state = db_state
        self.timeout_on_calls = set()
        self.call_count = 0

    def table(self, name: str) -> MockTableBuilder:
        return MockTableBuilder(self.db_state, name)


@pytest.fixture
def db_state() -> dict[str, list[dict[str, Any]]]:
    """Provides a fresh, local mock database state for each test."""
    state = {
        "open_positions": [],
        "closed_trades": [],
        "market_signals": [],
        "daily_performance": [],
        "agent_memory": [],
        "resolution_keyword_cache": [],
        "idempotency_log": [],
        "layer_c_category_versions": [],
    }
    # Add a mock layer C category default
    state["layer_c_category_versions"].append({
        "category": "politics",
        "avg_resolution_ambiguity_score": 0.15,
        "recommended_confidence_threshold": 0.75,
        "historical_edge_percent": 0.08,
        "valid_from": datetime.now(timezone.utc).isoformat(),
        "superseded_by": None
    })
    return state


@pytest.fixture
def mock_supabase_client(db_state: dict[str, list[dict[str, Any]]]) -> Generator[MockSupabaseClient, None, None]:
    """Patches get_client() to return a mock client."""
    client = MockSupabaseClient(db_state)
    
    async def fake_get_client() -> MockSupabaseClient:
        client.call_count += 1
        if client.call_count in client.timeout_on_calls:
            await asyncio.sleep(2.5)
        return client

    with patch("coordinator.pipeline.get_client", fake_get_client), \
         patch("llm.contract_parser.get_client", fake_get_client), \
         patch("llm.trade_decision.get_client", fake_get_client), \
         patch("strategies.calibration.get_client", fake_get_client), \
         patch("memory.supabase_client.get_client", fake_get_client):
        yield client


# ─────────────────────────────────────────────
# MOCK RESPONSE FOR AIOHTTP CLIENT POST
# ─────────────────────────────────────────────

class MockResponse:
    def __init__(self, status: int, json_data: dict[str, Any], delay: float = 0.0) -> None:
        self.status = status
        self._json_data = json_data
        self._delay = delay

    async def __aenter__(self) -> "MockResponse":
        if self._delay > 0.0:
            await asyncio.sleep(self._delay)
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        pass

    async def json(self) -> dict[str, Any]:
        return self._json_data

    async def text(self) -> str:
        return json.dumps(self._json_data)


@pytest.fixture
def mock_llm_apis() -> Generator[dict[str, Any], None, None]:
    """
    Mock standard LLM endpoints (News Analyst, Contract Parser, Trade Decision, Coordinator).
    Provides properties to inject latency or errors.
    """
    api_state = {
        "news_analyst_confidence": 0.88,
        "news_analyst_direction": "YES",
        "trade_decision_confidence": 0.85,
        "trade_decision_direction": "YES",
        "coordinator_direction": "YES",
        "coordinator_confidence": 0.86,
        "siliconflow_delay": 0.0,
        "nvidia_delay": 0.0,
        "openrouter_delay": 0.0,
        "sf_calls": 0,
        "or_calls": 0,
        "prompts": [],
    }

    def mock_post(self_session: Any, url: str, **kwargs: Any) -> MockResponse:
        payload = kwargs.get("json", {})
        model = payload.get("model", "")
        
        # Determine agent type based on model and contents
        messages = payload.get("messages", [])
        sys_prompt = next((m.get("content", "") for m in messages if m.get("role") == "system"), "")
        
        # Log user prompt
        user_content = next((m.get("content", "") for m in messages if m.get("role") == "user"), "")
        api_state["prompts"].append((model, user_content))
        
        delay = 0.0
        choice_content = {}

        # Increment call counters and set delays based on provider
        if "integrate.api.nvidia.com" in url or "nvidia" in url:
            api_state["sf_calls"] += 1
            delay = api_state["nvidia_delay"] or api_state["siliconflow_delay"]
        elif "openrouter.ai" in url or "api.deepseek.com" in url:
            api_state["or_calls"] += 1
            delay = api_state["openrouter_delay"]
        else:
            raise ValueError(f"Unknown API provider URL: {url}")
        
        # Handle News Analyst startup validation probe ("Reply OK")
        if user_content == "Reply OK":
            if (model == "google/gemma-4-31b-it:free" and "openrouter.ai" in url) or \
               (model == "meta/llama-3.3-70b-instruct" and ("integrate.api.nvidia.com" in url or "nvidia" in url)):
                response_json = {
                    "choices": [{"message": {"role": "assistant", "content": "OK"}}],
                    "usage": {"total_tokens": 10}
                }
                return MockResponse(200, response_json, delay=delay)
            else:
                return MockResponse(404, {"error": "Model not found"}, delay=delay)

        # Handle News Analyst
        if "prediction market signal classifier" in sys_prompt:
            if (model == "google/gemma-4-31b-it:free" and "openrouter.ai" in url) or \
               (model == "meta/llama-3.3-70b-instruct" and ("integrate.api.nvidia.com" in url or "nvidia" in url)):
                choice_content = {
                    "event_category": "politics",
                    "affected_market_ids": [],
                    "confidence_score": api_state["news_analyst_confidence"],
                    "direction": api_state["news_analyst_direction"],
                    "reasoning": "Mocked News Analyst reasoning",
                }
            else:
                raise ValueError(f"Mocked News Analyst endpoint not mapped for {url} model {model}")

            
        # Handle Contract Parser
        elif "resolution criteria parser" in sys_prompt:
            if (model == "moonshotai/kimi-k2.6:free" and "openrouter.ai" in url) or \
               (model == "deepseek-ai/deepseek-v4-flash" and ("api.deepseek.com" in url or "integrate.api.nvidia.com" in url or "nvidia" in url)):
                choice_content = {
                    "resolution_source": "Mock Resolution Source",
                    "resolution_condition": "Mock Resolution Condition",
                    "key_entities": ["Trump", "Politics"],
                    "resolution_keywords": ["impeach", "Trump", "January"],
                    "ambiguity_score": 0.15,
                    "resolution_type": "binary",
                }
            else:
                raise ValueError(f"Mocked Contract Parser endpoint not mapped for {url} model {model}")

        # Handle Trade Decision
        elif "prediction market trading agent" in sys_prompt or model == "qwen/qwen3-235b-a22b":
            if (model in ("qwen/qwen3-235b-a22b", "qwen/qwen3-32b")) and (
                ("integrate.api.nvidia.com" in url or "nvidia" in url) or ("openrouter.ai" in url)
            ):
                provider_name = "NVIDIA NIM" if ("integrate.api.nvidia.com" in url or "nvidia" in url) else "OpenRouter"
                choice_content = {
                    "direction": api_state["trade_decision_direction"],
                    "confidence_score": api_state["trade_decision_confidence"],
                    "reasoning": f"Mocked Trade Decision {provider_name} reasoning",
                }
            else:
                raise ValueError(f"Mocked Trade Decision endpoint not mapped for {url} model {model}")

        # Handle LLM Coordinator
        elif "prediction market trading coordinator" in sys_prompt or model == "qwen/qwen3-32b":
            if (model in ("qwen/qwen3-235b-a22b", "qwen/qwen3-32b")) and (
                ("integrate.api.nvidia.com" in url or "nvidia" in url) or ("openrouter.ai" in url)
            ):
                provider_name = "NVIDIA NIM" if ("integrate.api.nvidia.com" in url or "nvidia" in url) else "OpenRouter"
                choice_content = {
                    "direction": api_state["coordinator_direction"],
                    "confidence_score": api_state["coordinator_confidence"],
                    "reasoning": "Mocked LLM Coordinator conflict resolved",
                }
            else:
                raise ValueError(f"Mocked LLM Coordinator endpoint not mapped for {url} model {model}")
            
        else:
            raise ValueError(f"Mocked endpoint not mapped for {url} model {model}")

        response_json = {
            "choices": [{"message": {"role": "assistant", "content": json.dumps(choice_content)}}],
            "usage": {"total_tokens": 100}
        }
        return MockResponse(200, response_json, delay=delay)

    with patch("aiohttp.ClientSession.post", mock_post):
        yield api_state


# ─────────────────────────────────────────────
# UNIT TESTS MAPPED TO TESTING.MD
# ─────────────────────────────────────────────

@pytest.mark.anyio
async def test_6_1_full_pipeline_end_to_end(
    mock_supabase_client: MockSupabaseClient,
    mock_llm_apis: dict[str, Any],
) -> None:
    """Criterion 6.1: Processes high-confidence signals through all stages down to mock order without exceptions."""
    res = await run_pipeline(
        headline="Donald Trump faces impeachment house vote",
        source="AP News",
        market_id="P1",
        market_question="Will Trump be impeached?",
        resolution_criteria="Resolves YES if house votes to impeach before 2027.",
        market_price=0.55,
        portfolio_value=10000.0,
        starting_balances={"daily": 10000.0, "weekly": 10000.0, "monthly": 10000.0},
        current_balances={"daily": 10000.0, "weekly": 10000.0, "monthly": 10000.0},
    )
    
    assert res is not None
    assert res["status"] == "success"
    assert res["market_id"] == "P1"
    assert res["direction"] == "YES"
    assert res["size_usdc"] > 0.0
    assert "order_id" in res
    assert "uuid" in res
    
    # Confirm that open_positions was written to
    positions = mock_supabase_client.db_state["open_positions"]
    assert len(positions) == 1
    assert positions[0]["market_id"] == "P1"
    assert positions[0]["direction"] == "YES"


@pytest.mark.anyio
async def test_6_2_fast_path_under_5_seconds(
    mock_supabase_client: MockSupabaseClient,
    mock_llm_apis: dict[str, Any],
) -> None:
    """Criterion 6.2: Fresh cache hit, pre-validated category, and high confidence routes fast path under 5s."""
    # Seed cache to create fresh cache hit
    mock_supabase_client.db_state["resolution_keyword_cache"].append({
        "market_id": "P1",
        "market_question": "Will Trump be impeached?",
        "resolution_keywords": ["impeachment", "house", "Trump"],
        "resolution_conditions": {},
        "resolution_type": "binary",
        "ambiguity_score": 0.15,
        "cached_at": datetime.now(timezone.utc).isoformat()
    })

    mock_llm_apis["news_analyst_confidence"] = 0.91  # Above 0.87 fast path trigger

    t0 = time.perf_counter()
    res = await run_pipeline(
        headline="Donald Trump faces impeachment house vote",
        source="AP News",
        market_id="P1",
        market_question="Will Trump be impeached?",
        resolution_criteria="Resolves YES if house votes to impeach before 2027.",
        market_price=0.55,
        portfolio_value=10000.0,
        starting_balances={"daily": 10000.0, "weekly": 10000.0, "monthly": 10000.0},
        current_balances={"daily": 10000.0, "weekly": 10000.0, "monthly": 10000.0},
    )
    duration = time.perf_counter() - t0

    assert res is not None
    assert res["status"] == "success"
    assert duration < 5.0
    
    # Confirm Trade Decision was completely skipped (sf_calls = 0)
    assert mock_llm_apis["sf_calls"] == 0


@pytest.mark.anyio
async def test_6_3_full_pipeline_under_22_seconds(
    mock_supabase_client: MockSupabaseClient,
    mock_llm_apis: dict[str, Any],
) -> None:
    """Criterion 6.3: Confirm full pipeline execution completes well within the 22-second limit."""
    mock_llm_apis["news_analyst_confidence"] = 0.80  # Triggers full pipeline slow path

    t0 = time.perf_counter()
    res = await run_pipeline(
        headline="Donald Trump faces impeachment house vote",
        source="AP News",
        market_id="P1",
        market_question="Will Trump be impeached?",
        resolution_criteria="Resolves YES if house votes to impeach before 2027.",
        market_price=0.55,
        portfolio_value=10000.0,
        starting_balances={"daily": 10000.0, "weekly": 10000.0, "monthly": 10000.0},
        current_balances={"daily": 10000.0, "weekly": 10000.0, "monthly": 10000.0},
    )
    duration = time.perf_counter() - t0

    assert res is not None
    assert res["status"] == "success"
    assert duration < 22.0
    # Confirm Trade Decision was evaluated
    assert mock_llm_apis["sf_calls"] == 1


@pytest.mark.anyio
async def test_6_4_memory_prepended(
    mock_supabase_client: MockSupabaseClient,
    mock_llm_apis: dict[str, Any],
) -> None:
    """Criterion 6.4: agent_memory lessons are correctly formatted and placed at the top of the Trade Decision prompt."""
    # Seed lessons in agent_memory
    mock_supabase_client.db_state["agent_memory"].extend([
        {
            "category": "politics",
            "lesson": "Never trade early on impeachment news.",
            "trigger_condition": {"category": "politics"},
            "severity": "warning",
            "retired": False
        },
        {
            "category": "politics",
            "lesson": "Verify actual House vote scheduling.",
            "trigger_condition": {"category": "politics"},
            "severity": "warning",
            "retired": False
        }
    ])

    mock_llm_apis["news_analyst_confidence"] = 0.80  # Forces full pipeline

    await run_pipeline(
        headline="Donald Trump faces impeachment house vote",
        source="AP News",
        market_id="P1",
        market_question="Will Trump be impeached?",
        resolution_criteria="Resolves YES if house votes to impeach.",
        market_price=0.55,
        portfolio_value=10000.0,
        starting_balances={"daily": 10000.0, "weekly": 10000.0, "monthly": 10000.0},
        current_balances={"daily": 10000.0, "weekly": 10000.0, "monthly": 10000.0},
    )

    # Find the Trade Decision agent prompt in mock logs
    td_prompt = next((p for model, p in mock_llm_apis["prompts"] if model == "qwen/qwen3-235b-a22b"), "")
    assert td_prompt != ""
    
    # Assert lessons are prepended as warning block at the top
    assert "*** WARNING: LESSONS FROM PAST MISTAKES ***" in td_prompt
    assert "1. Never trade early on impeachment news." in td_prompt
    assert "2. Verify actual House vote scheduling." in td_prompt
    assert td_prompt.index("*** WARNING: LESSONS FROM PAST MISTAKES ***") < td_prompt.index("MARKET CONTEXT:")


@pytest.mark.anyio
async def test_6_5_conflict_detection(
    mock_supabase_client: MockSupabaseClient,
    mock_llm_apis: dict[str, Any],
) -> None:
    """Criterion 6.5: Disagreement with high News Analyst confidence (>0.70) triggers LLM Coordinator; low confidence does not."""
    # Case 1: High News Analyst confidence (>0.70) + Disagreement
    mock_llm_apis["news_analyst_confidence"] = 0.80
    mock_llm_apis["news_analyst_direction"] = "YES"
    mock_llm_apis["trade_decision_direction"] = "NO"
    
    # We should see Coordinator call triggered
    mock_llm_apis["or_calls"] = 0
    mock_llm_apis["sf_calls"] = 0
    res1 = await run_pipeline(
        headline="Donald Trump impeachment",
        source="AP News",
        market_id="P1",
        market_question="Will Trump be impeached?",
        resolution_criteria="Resolves YES if House votes.",
        market_price=0.55,
        portfolio_value=10000.0,
        starting_balances={"daily": 10000.0, "weekly": 10000.0, "monthly": 10000.0},
        current_balances={"daily": 10000.0, "weekly": 10000.0, "monthly": 10000.0},
    )
    assert res1 is not None
    # Calls: 1 (News Analyst) + 1 (Contract Parser) + 1 (Trade Decision) + 1 (Coordinator)
    assert (mock_llm_apis["or_calls"] + mock_llm_apis["sf_calls"]) >= 4

    # Case 2: Low News Analyst confidence (<=0.70) + Disagreement
    mock_llm_apis["news_analyst_confidence"] = 0.65
    mock_llm_apis["news_analyst_direction"] = "YES"
    mock_llm_apis["trade_decision_direction"] = "NO"
    mock_llm_apis["or_calls"] = 0
    
    with patch("config.MIN_CONFIDENCE_THRESHOLD", 0.50):
        res2 = await run_pipeline(
            headline="Donald Trump impeachment",
            source="AP News",
            market_id="P2",
            market_question="Will Trump be impeached?",
            resolution_criteria="Resolves YES if House votes.",
            market_price=0.55,
            portfolio_value=10000.0,
            starting_balances={"daily": 10000.0, "weekly": 10000.0, "monthly": 10000.0},
            current_balances={"daily": 10000.0, "weekly": 10000.0, "monthly": 10000.0},
        )
    # Under low confidence, Trade Decision wins without escalation.
    assert res2 is not None
    assert res2["direction"] == "NO"  # Trade Decision direction won
    # Verify no LLM coordinator call occurred (total OpenRouter calls strictly < 3, just Analyst + Parser)
    assert mock_llm_apis["or_calls"] == 2


@pytest.mark.anyio
async def test_6_6_risk_check_on_all_paths(
    mock_supabase_client: MockSupabaseClient,
    mock_llm_apis: dict[str, Any],
) -> None:
    """Criterion 6.6: Confirm risk_engine checks execute on both fast path and full pipeline."""
    # Fast path
    mock_supabase_client.db_state["resolution_keyword_cache"].append({
        "market_id": "P1",
        "market_question": "Question?",
        "resolution_keywords": ["impeachment"],
        "resolution_conditions": {},
        "resolution_type": "binary",
        "ambiguity_score": 0.10,
        "cached_at": datetime.now(timezone.utc).isoformat()
    })
    mock_llm_apis["news_analyst_confidence"] = 0.91

    with patch("risk.risk_engine.kelly_size", return_value=500.0) as spy_risk:
        await run_pipeline(
            headline="Donald Trump impeachment",
            source="AP News",
            market_id="P1",
            market_question="Question?",
            resolution_criteria="Resolves YES if House votes.",
            market_price=0.55,
            portfolio_value=10000.0,
            starting_balances={"daily": 10000.0, "weekly": 10000.0, "monthly": 10000.0},
            current_balances={"daily": 10000.0, "weekly": 10000.0, "monthly": 10000.0},
        )
        assert spy_risk.called

    # Full path
    mock_llm_apis["news_analyst_confidence"] = 0.80
    with patch("risk.risk_engine.kelly_size", return_value=500.0) as spy_risk_slow:
        await run_pipeline(
            headline="Donald Trump impeachment",
            source="AP News",
            market_id="P1",
            market_question="Question?",
            resolution_criteria="Resolves YES if House votes.",
            market_price=0.55,
            portfolio_value=10000.0,
            starting_balances={"daily": 10000.0, "weekly": 10000.0, "monthly": 10000.0},
            current_balances={"daily": 10000.0, "weekly": 10000.0, "monthly": 10000.0},
        )
        assert spy_risk_slow.called


@pytest.mark.anyio
async def test_6_7_circuit_breaker_halt(
    mock_supabase_client: MockSupabaseClient,
    mock_llm_apis: dict[str, Any],
) -> None:
    """Criterion 6.7: Daily drawdown of 9% (>8%) blocks trading and records circuit breaker trip."""
    res = await run_pipeline(
        headline="Donald Trump impeachment",
        source="AP News",
        market_id="P1",
        market_question="Question?",
        resolution_criteria="Resolves YES.",
        market_price=0.55,
        portfolio_value=10000.0,
        starting_balances={"daily": 10000.0, "weekly": 10000.0, "monthly": 10000.0},
        current_balances={"daily": 9099.0, "weekly": 10000.0, "monthly": 10000.0},  # 9.01% daily drawdown
    )
    
    assert res is not None
    assert res["status"] == "blocked"
    assert res["reason"] == "circuit_breaker"


@pytest.mark.anyio
async def test_6_8_pre_order_idempotency(
    mock_supabase_client: MockSupabaseClient,
    mock_llm_apis: dict[str, Any],
) -> None:
    """Criterion 6.8: Pre-order idempotency generates a UUID and logs it in Supabase as pending before order goes out."""
    # Spy on Supabase insert of idempotency_log
    res = await run_pipeline(
        headline="Donald Trump faces impeachment house vote",
        source="AP News",
        market_id="P1",
        market_question="Question?",
        resolution_criteria="Resolves YES.",
        market_price=0.55,
        portfolio_value=10000.0,
        starting_balances={"daily": 10000.0, "weekly": 10000.0, "monthly": 10000.0},
        current_balances={"daily": 10000.0, "weekly": 10000.0, "monthly": 10000.0},
    )
    
    assert res is not None
    assert res["status"] == "success"
    order_uuid = res["uuid"]
    
    logs = mock_supabase_client.db_state["idempotency_log"]
    assert len(logs) == 1
    assert logs[0]["id"] == order_uuid
    assert logs[0]["status"] == "confirmed"  # Confirmed after mock execution finishes


@pytest.mark.anyio
async def test_6_9_cache_timeout_fallback_to_full_pipeline(
    mock_supabase_client: MockSupabaseClient,
    mock_llm_apis: dict[str, Any],
) -> None:
    """Criterion 6.9: Cache lookup timeout triggers graceful fallback to the full pipeline."""
    # Seed cache keyword to simulate hit
    mock_supabase_client.db_state["resolution_keyword_cache"].append({
        "market_id": "P1",
        "market_question": "Question?",
        "resolution_keywords": ["impeachment", "trump", "house"],
        "resolution_conditions": {},
        "resolution_type": "binary",
        "ambiguity_score": 0.10,
        "cached_at": datetime.now(timezone.utc).isoformat()
    })
    
    # Normally fast path eligible
    mock_llm_apis["news_analyst_confidence"] = 0.95
    mock_llm_apis["sf_calls"] = 0

    # Call #2: get_cached_keywords will time out!
    mock_supabase_client.timeout_on_calls = {2}

    res = await run_pipeline(
        headline="Donald Trump impeachment",
        source="AP News",
        market_id="P1",
        market_question="Question?",
        resolution_criteria="Resolves YES.",
        market_price=0.55,
        portfolio_value=10000.0,
        starting_balances={"daily": 10000.0, "weekly": 10000.0, "monthly": 10000.0},
        current_balances={"daily": 10000.0, "weekly": 10000.0, "monthly": 10000.0},
    )

    assert res is not None
    assert res["status"] == "success"
    # Full pipeline evaluated Trade Decision Agent because fast path check timed out
    assert mock_llm_apis["sf_calls"] == 1


@pytest.mark.anyio
async def test_6_9_memory_timeout_proceeds_memoryless(
    mock_supabase_client: MockSupabaseClient,
    mock_llm_apis: dict[str, Any],
) -> None:
    """Criterion 6.9: agent_memory timeout proceeds with the trade, flagging was_memoryless = true."""
    # Full pipeline
    mock_llm_apis["news_analyst_confidence"] = 0.80
    
    # Call 1: _log_to_supabase in news_analyst (succeeds)
    # Call 2: _read in contract_parser (no hit)
    # Call 3: _write in contract_parser (succeeds)
    # Call 4: fetch_relevant_lessons (times out)
    mock_supabase_client.timeout_on_calls = {4}

    res = await run_pipeline(
        headline="Donald Trump impeachment",
        source="AP News",
        market_id="P1",
        market_question="Question?",
        resolution_criteria="Resolves YES.",
        market_price=0.55,
        portfolio_value=10000.0,
        starting_balances={"daily": 10000.0, "weekly": 10000.0, "monthly": 10000.0},
        current_balances={"daily": 10000.0, "weekly": 10000.0, "monthly": 10000.0},
    )

    assert res is not None
    assert res["status"] == "success"
    # Succeeded but flagged memoryless
    assert res["was_memoryless"] is True


@pytest.mark.anyio
async def test_6_9_idempotency_timeout_fails_closed(
    mock_supabase_client: MockSupabaseClient,
    mock_llm_apis: dict[str, Any],
) -> None:
    """Criterion 6.9: Supabase timeout on idempotency check halts trading and fails closed."""
    # Full pipeline
    mock_llm_apis["news_analyst_confidence"] = 0.80
    
    # Call 1: get_cached_keywords (no hit)
    # Call 2: _check_cache (no hit)
    # Call 3: fetch_relevant_lessons (succeeds)
    # Call 4: _write_cache (succeeds)
    # Call 5: fetch_open_positions_exposure (succeeds)
    # Call 6: check_pre_order_idempotency (times out!)
    mock_supabase_client.timeout_on_calls = {6}

    with pytest.raises(RuntimeError, match="Trading halted due to idempotency (check|write) timeout"):
        await run_pipeline(
            headline="Donald Trump impeachment",
            source="AP News",
            market_id="P1",
            market_question="Question?",
            resolution_criteria="Resolves YES.",
            market_price=0.55,
            portfolio_value=10000.0,
            starting_balances={"daily": 10000.0, "weekly": 10000.0, "monthly": 10000.0},
            current_balances={"daily": 10000.0, "weekly": 10000.0, "monthly": 10000.0},
        )


@pytest.mark.anyio
async def test_6_10_siliconflow_failover(
    mock_supabase_client: MockSupabaseClient,
    mock_llm_apis: dict[str, Any],
) -> None:
    """Criterion 6.10: NVIDIA NIM delay of 19s (>18s) triggers immediate cancel and failover to OpenRouter."""
    mock_llm_apis["news_analyst_confidence"] = 0.80  # Forces full pipeline slow path
    mock_llm_apis["nvidia_delay"] = 19.0             # Exceeds NVIDIA NIM timeout limits (18.0)
    mock_llm_apis["siliconflow_delay"] = 19.0        # Keep for backward compatibility
    mock_llm_apis["sf_calls"] = 0
    mock_llm_apis["or_calls"] = 0

    res = await run_pipeline(
        headline="Donald Trump impeachment",
        source="AP News",
        market_id="P1",
        market_question="Question?",
        resolution_criteria="Resolves YES.",
        market_price=0.55,
        portfolio_value=10000.0,
        starting_balances={"daily": 10000.0, "weekly": 10000.0, "monthly": 10000.0},
        current_balances={"daily": 10000.0, "weekly": 10000.0, "monthly": 10000.0},
    )

    assert res is not None
    assert res["status"] == "success"
    # Confirm that primary NVIDIA NIM was attempted
    assert mock_llm_apis["sf_calls"] == 1
    # Confirm that OpenRouter fallback was triggered and succeeded
    assert mock_llm_apis["or_calls"] > 1
    assert "OpenRouter reasoning" in res["reasoning"]
