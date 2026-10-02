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
import data.market_discovery

@pytest.fixture(autouse=True)
def clear_market_discovery_cache():
    data.market_discovery._MARKET_CACHE = []
    data.market_discovery._CACHE_UPDATED_AT = None

@pytest.fixture(autouse=True)
def mock_asyncio_sleep():
    """Mock asyncio.sleep to run instantly for rate limit delays (1s/2s)."""
    orig_sleep = asyncio.sleep
    async def fake_sleep(delay, result=None):
        if delay in (2.0, 1.0):
            return await orig_sleep(0.001, result)
        return await orig_sleep(delay, result)
    with patch("asyncio.sleep", fake_sleep):
        yield


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
        self._is_delete = False

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
        remaining = []
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
            else:
                remaining.append(row)

        if self._is_delete:
            self.db_state[self.table_name] = remaining

        if self._order:
            col, desc = self._order
            matched.sort(key=lambda x: x.get(col, ""), reverse=desc)

        return Result(matched)

    def insert(self, data: dict[str, Any]) -> "MockTableBuilder":
        inserted_data = data.copy()
        if "id" not in inserted_data:
            import uuid
            inserted_data["id"] = f"mock-id-{uuid.uuid4()}"
        self.db_state.setdefault(self.table_name, []).append(inserted_data)
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

    def delete(self) -> "MockTableBuilder":
        self._is_delete = True
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
         patch("copytrade.performance_tracker.get_client", fake_get_client), \
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
        "news_analyst_category": "politics",
        "trade_decision_confidence": 0.85,
        "trade_decision_direction": "YES",
        "coordinator_direction": "YES",
        "coordinator_confidence": 0.86,
        "siliconflow_delay": 0.0,
        "nvidia_delay": 0.0,
        "openrouter_delay": 0.0,
        "sf_calls": 0,
        "or_calls": 0,
        "jev_calls": 0,
        "prompts": [],
        "news_analyst_status": 200,
        "jev_status": 200,
        "contract_parser_status": 200,
        "trade_decision_status": 200,
        "coordinator_status": 200,
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

        # Handle Jev Decision API calls (TokenRouter & OpenRouter) — separate from generative LLM
        if "api/alpha/decisions" in url:
            api_state["jev_calls"] = api_state.get("jev_calls", 0) + 1
            jev_status = api_state.get("jev_status", 200)
            if jev_status != 200:
                return MockResponse(jev_status, {"error": "Mocked Jev error"})
            jev_response = {
                "model": "typesafe/jev-1.13",
                "answers": {
                    "event_category": {"choice": api_state.get("news_analyst_category", "politics")},
                    "direction": {
                        "choice": api_state["news_analyst_direction"],
                        "confidence": api_state["news_analyst_confidence"]
                    }
                },
                "usage": {"input_tokens": 100}
            }
            return MockResponse(200, jev_response)

        # Increment call counters and set delays based on provider
        if "api.tokenrouter.com" in url:
            if "resolution criteria parser" in sys_prompt:
                api_state["or_calls"] += 1
                delay = api_state.get("openrouter_delay", 0.0)
            else:
                api_state["sf_calls"] += 1
                if "prediction market trading agent" in sys_prompt or model == config.MODEL_TRADE_DECISION:
                    delay = api_state["siliconflow_delay"] or api_state.get("nvidia_delay", 0.0)
                else:
                    delay = 0.0
        elif "integrate.api.nvidia.com" in url or "nvidia" in url:
            api_state["sf_calls"] += 1
            if "prediction market trading agent" in sys_prompt or model == config.MODEL_TRADE_DECISION:
                delay = api_state["nvidia_delay"] or api_state["siliconflow_delay"]
            else:
                delay = 0.0
        elif "openrouter.ai" in url or "api.deepseek.com" in url:
            api_state["or_calls"] += 1
            delay = api_state["openrouter_delay"]
        elif "api.siliconflow.cn" in url or "siliconflow" in url:
            api_state["sf_calls"] += 1
            if "prediction market trading agent" in sys_prompt or model == config.MODEL_TRADE_DECISION:
                delay = api_state["siliconflow_delay"]
            else:
                delay = 0.0
        else:
            raise ValueError(f"Unknown API provider URL: {url}")
        
        # Check status code overrides for testing fail-fast HTTP codes (only fail primary models)
        status_code = 200
        if "prediction market signal classifier" in sys_prompt or user_content == "Reply OK":
            if model == getattr(config, "MODEL_NEWS_ANALYST", "typesafe/jev-1.13"):
                status_code = api_state.get("news_analyst_status", 200)
        elif "resolution criteria parser" in sys_prompt:
            if model == getattr(config, "MODEL_CONTRACT_PARSER", "qwen/qwen3.8-flash"):
                status_code = api_state.get("contract_parser_status", 200)
        elif "prediction market trading agent" in sys_prompt or model == config.MODEL_TRADE_DECISION:
            if model == getattr(config, "MODEL_TRADE_DECISION", "qwen/qwen3.5-flash"):
                status_code = api_state.get("trade_decision_status", 200)
        elif "prediction market trading coordinator" in sys_prompt or model == config.MODEL_COORDINATOR:
            if model == getattr(config, "MODEL_COORDINATOR", "qwen/qwen3.5-flash"):
                status_code = api_state.get("coordinator_status", 200)

        if status_code != 200:
            return MockResponse(status_code, {"error": "Mocked fail fast error"}, delay=delay)
        
        # Handle News Analyst startup validation probe ("Reply OK")
        if user_content == "Reply OK":
            response_json = {
                "choices": [{"message": {"role": "assistant", "content": "OK"}}],
                "usage": {"total_tokens": 10}
            }
            return MockResponse(200, response_json, delay=delay)

        # Handle News Analyst
        if "prediction market signal classifier" in sys_prompt:
            choice_content = {
                "event_category": "politics",
                "affected_market_ids": [],
                "confidence_score": api_state["news_analyst_confidence"],
                "direction": api_state["news_analyst_direction"],
                "reasoning": "Mocked News Analyst reasoning",
            }
            
        # Handle Contract Parser
        elif "resolution criteria parser" in sys_prompt:
            choice_content = {
                "resolution_source": "Mock Resolution Source",
                "resolution_condition": "Mock Resolution Condition",
                "key_entities": ["Trump", "Politics"],
                "resolution_keywords": ["impeach", "Trump", "January"],
                "ambiguity_score": 0.15,
                "resolution_type": "binary",
            }

        # Handle Trade Decision
        elif "prediction market trading agent" in sys_prompt or model == config.MODEL_TRADE_DECISION:
            provider_name = "OpenRouter" if "openrouter" in url.lower() else ("TokenRouter" if "api.tokenrouter.com" in url else "NVIDIA NIM")
            choice_content = {
                "direction": api_state["trade_decision_direction"],
                "confidence_score": api_state["trade_decision_confidence"],
                "reasoning": f"Mocked Trade Decision {provider_name} reasoning",
            }

        # Handle LLM Coordinator
        elif "prediction market trading coordinator" in sys_prompt or model == config.MODEL_COORDINATOR:
            provider_name = "NVIDIA NIM" if ("integrate.api.nvidia.com" in url or "nvidia" in url) else "OpenRouter"
            choice_content = {
                "direction": api_state["coordinator_direction"],
                "confidence_score": api_state["coordinator_confidence"],
                "reasoning": "Mocked LLM Coordinator conflict resolved",
            }
            
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
    
    # Confirm Trade Decision was completely skipped (sf_calls = 0: Jev handled news analyst, TD skipped)
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
    # Confirm Trade Decision was evaluated (sf_calls = 1: trade decision only, Jev handled news analyst)
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
    td_prompt = next((p for model, p in mock_llm_apis["prompts"] if model == config.MODEL_TRADE_DECISION), "")
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
    # Calls: Jev(news, not counted) + 1 (Contract Parser) + 1 (Trade Decision) + 1 (Coordinator)
    assert (mock_llm_apis["or_calls"] + mock_llm_apis["sf_calls"]) >= 3

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
    # Verify no LLM coordinator call occurred (total OpenRouter calls strictly < 2, just Parser)
    assert mock_llm_apis["or_calls"] == 1


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

    # Call #1: get_cached_keywords will time out! (Jev skips _log_to_supabase, so cache read is Call 1)
    mock_supabase_client.timeout_on_calls = {1}

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
    # Full pipeline evaluated Trade Decision Agent because fast path check timed out (sf_calls = 1: TD only, Jev handled news)
    assert mock_llm_apis["sf_calls"] == 1


@pytest.mark.anyio
async def test_6_9_memory_timeout_proceeds_memoryless(
    mock_supabase_client: MockSupabaseClient,
    mock_llm_apis: dict[str, Any],
) -> None:
    """Criterion 6.9: agent_memory timeout proceeds with the trade, flagging was_memoryless = true."""
    # Full pipeline
    mock_llm_apis["news_analyst_confidence"] = 0.80
    
    # Jev handles news (no _log_to_supabase), so Supabase call sequence shifts by -1:
    # Call 1: _check_cache in contract_parser (no hit)
    # Call 2: _write_cache in contract_parser (succeeds)
    # Call 3: fetch_relevant_lessons (times out!)
    mock_supabase_client.timeout_on_calls = {3}

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
    
    # Jev handles news (no _log_to_supabase), so Supabase call sequence shifts by -1:
    # Call 1: _check_cache (no hit)
    # Call 2: _write_cache (succeeds)
    # Call 3: fetch_relevant_lessons (succeeds)
    # Call 4: fetch_open_positions_exposure (succeeds)
    # Call 5: check_pre_order_idempotency (times out!)
    mock_supabase_client.timeout_on_calls = {5}

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
    mock_llm_apis["nvidia_delay"] = 0.2              # Exceeds mock patched timeout limits (0.1)
    mock_llm_apis["siliconflow_delay"] = 0.2         # Keep for backward compatibility
    mock_llm_apis["sf_calls"] = 0
    mock_llm_apis["or_calls"] = 0

    with patch("config.LLM_TIMEOUT_SECONDS", 0.1):
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
    # Confirm that primary NVIDIA NIM was attempted for trade decision (sf_calls = 1: Jev handled news)
    assert mock_llm_apis["sf_calls"] == 1
    # Confirm that OpenRouter fallback was triggered and succeeded (or_calls = 2: parser + trade decision fallback)
    assert mock_llm_apis["or_calls"] == 2
    assert "OpenRouter reasoning" in res["reasoning"]


@pytest.mark.anyio
async def test_news_analyst_fail_fast(
    mock_supabase_client: MockSupabaseClient,
    mock_llm_apis: dict[str, Any],
) -> None:
    """Verify that a 401 response on primary SiliconFlow triggers immediate News Analyst fallback to NVIDIA NIM."""
    mock_llm_apis["jev_status"] = 401          # Force Jev to fail so generative fallback is reached
    mock_llm_apis["news_analyst_status"] = 401
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
    # SiliconFlow primary was called, failed fast, and NVIDIA NIM was called
    assert mock_llm_apis["sf_calls"] >= 2


@pytest.mark.anyio
async def test_trade_decision_fail_fast(
    mock_supabase_client: MockSupabaseClient,
    mock_llm_apis: dict[str, Any],
) -> None:
    """Verify that a 403 response on primary NVIDIA NIM triggers immediate Trade Decision fallback to OpenRouter."""
    mock_llm_apis["news_analyst_confidence"] = 0.80  # Force full pipeline
    mock_llm_apis["trade_decision_status"] = 403
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
    # Primary NVIDIA NIM was called for trade decision (Jev handled news)
    assert mock_llm_apis["sf_calls"] >= 1  # trade decision primary
    # Fallback OpenRouter was called
    assert mock_llm_apis["or_calls"] >= 2  # contract parser + trade decision fallback


@pytest.mark.anyio
async def test_copy_trade_class_b_end_to_end_flow(
    mock_supabase_client: MockSupabaseClient,
    mock_llm_apis: dict[str, Any],
) -> None:
    """Integration: Class B copy-trade signal routes through the coordinator pipeline,
    tags strategy='copy_edge_class_b', writes to copytrade_log, and resolves via reconciliation.
    """
    from unittest.mock import patch, MagicMock
    from execution.reconciliation import reconcile_on_startup

    wallet_addr = "0xClassBCopy0000000000000000000000000000"
    
    # 1. Seed the tracked_wallets starting state
    mock_supabase_client.db_state.setdefault("tracked_wallets", []).append({
        "wallet_address": wallet_addr,
        "trader_name": "MacroGenius",
        "class_type": "B",
        "state": "NEW",
        "is_active": True,
        "resolved_trades_count": 0,
        "wins_count": 0,
        "losses_count": 0,
        "trust_score": 0.5000,
        "avg_roi_per_trade": 0.0,
        "probation_entered_at": None,
        "probation_resolved_at_entry": 0,
        "is_priority": False,
        "added_at": datetime.now(timezone.utc).isoformat(),
        "last_updated_at": datetime.now(timezone.utc).isoformat(),
    })
    
    # 2. Run the pipeline with the Class B copy-trade signal
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
        signal_source="copy_edge",
        strategy_override="copy_edge_class_b",
        wallet_address=wallet_addr,
        was_priority_pick=False,
        slippage=0.002,
        trader_name="MacroGenius",
    )
    
    assert res is not None
    assert res["status"] == "success"
    
    # Assert strategy is tagged correctly in open_positions
    positions = mock_supabase_client.db_state.get("open_positions", [])
    assert len(positions) == 1
    assert positions[0]["strategy"] == "copy_edge_class_b", (
        f"Expected strategy='copy_edge_class_b', got '{positions[0]['strategy']}'"
    )
    
    # Assert copytrade_log row was written
    logs = mock_supabase_client.db_state.get("copytrade_log", [])
    assert len(logs) == 1
    assert logs[0]["wallet_address"] == wallet_addr
    assert logs[0]["class_type"] == "B"
    assert logs[0]["status"] == "open"
    
    # Save the generated idempotency_uuid to simulate reconciliation resolution
    idem_uuid = positions[0]["idempotency_uuid"]
    
    # 3. Simulate reconciliation resolution (Paper Trading = True)
    mock_gamma_data = {
        "id": "P1",
        "umaResolutionStatus": "resolved",
        "outcomes": ["Yes", "No"],
        "outcomePrices": ["1", "0"],
        "closed": True,
    }
    
    mock_clob = MagicMock()
    mock_clob.get_balance_allowance.return_value = {"balance": "100000000"}
    
    class MockResponse:
        def __init__(self, status_code: int, data: dict[str, Any]) -> None:
            self.status_code = status_code
            self._data = data
        def json(self) -> dict[str, Any]:
            return self._data
            
    async def mock_get(_self: Any, url: str, **kwargs: Any) -> MockResponse:
        return MockResponse(200, mock_gamma_data)
        
    async def fake_get_client_pt() -> MockSupabaseClient:
        return mock_supabase_client
        
    with patch("execution.reconciliation.get_polymarket_client", return_value=mock_clob),          patch("httpx.AsyncClient.get", mock_get),          patch("config.PAPER_TRADING", True),          patch("execution.reconciliation.get_client", fake_get_client_pt),          patch("copytrade.performance_tracker.get_client", fake_get_client_pt):
         
        await reconcile_on_startup()
        
    # Assert open_positions was closed
    assert len(mock_supabase_client.db_state["open_positions"]) == 0
    
    # Assert copytrade_log row was updated to won
    assert logs[0]["status"] == "won"
    
    # Assert tracked_wallets was updated with win + trust_score recomputed
    wallets = mock_supabase_client.db_state.get("tracked_wallets", [])
    assert len(wallets) == 1
    w = wallets[0]
    assert w["wins_count"] == 1
    assert w["resolved_trades_count"] == 1
    # Bayesian: (0+1+5)/(0+0+1+10) = 6/11
    expected_trust = 6.0 / 11.0
    assert abs(w["trust_score"] - expected_trust) < 0.001
