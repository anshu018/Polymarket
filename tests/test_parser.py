"""
test_parser.py — Full pytest unit tests for llm/contract_parser.py (Layer 5).

Rules:
  - No real Supabase calls (mocked).
  - No real API/LLM calls (mocked).
  - Matches TESTING.md criteria 5.1 to 5.8 exactly.
  - Complete type hints and docstrings.
"""

import sys
import os
import json
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Generator, Any
from unittest.mock import patch, AsyncMock

# Ensure project root is on the path so 'config' and 'llm' resolve correctly.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import config
from llm.contract_parser import (
    parse_contract,
    get_cached_keywords,
    ContractParserOutput,
)

# ─────────────────────────────────────────────────────────────────────────────
# MOCK DATA FOR 10 REAL CONTRACTS (POLITICS, CRYPTO, SPORTS, LEGAL, ECONOMICS)
# ─────────────────────────────────────────────────────────────────────────────

MOCK_MARKETS: dict[str, dict[str, Any]] = {
    "P1": {
        "question": "Will Donald Trump be impeached before January 2027?",
        "criteria": "Resolves YES if House votes to impeach before Jan 2027.",
        "resolution_source": "US House of Representatives",
        "resolution_condition": "The House of Representatives votes to impeach Donald Trump prior to January 1, 2027.",
        "key_entities": ["Donald Trump", "January 2027", "House of Representatives"],
        "resolution_keywords": ["impeached", "House vote", "Donald Trump", "January 2027"],
        "ambiguity_score": 0.15,
        "resolution_type": "binary",
    },
    "P2": {
        "question": "Will the UK hold a general election in 2026?",
        "criteria": "Resolves YES if a UK general election is held in 2026.",
        "resolution_source": "UK Parliament / Official Records",
        "resolution_condition": "A UK general election for Westminster parliament is held during the calendar year 2026.",
        "key_entities": ["UK", "general election", "2026"],
        "resolution_keywords": ["general election", "Westminster", "parliament", "United Kingdom"],
        "ambiguity_score": 0.10,
        "resolution_type": "binary",
    },
    "P3": {
        "question": "Will France government collapse in 2026?",
        "criteria": "Resolves YES if no confidence vote passes before Dec 31 2026.",
        "resolution_source": "French National Assembly official journal",
        "resolution_condition": "A vote of no confidence passes in the French National Assembly against the government before December 31, 2026.",
        "key_entities": ["France government", "National Assembly", "December 31, 2026"],
        "resolution_keywords": ["no confidence", "France government", "National Assembly", "French government"],
        "ambiguity_score": 0.20,
        "resolution_type": "binary",
    },
    "C1": {
        "question": "Will BTC close above $100k on Dec 31 2026 according to Coinbase?",
        "criteria": "Resolves YES if the price of BTC is above $100,000 on Coinbase at 11:59:59 PM UTC on Dec 31, 2026.",
        "resolution_source": "Coinbase API / BTC-USD price feed",
        "resolution_condition": "BTC price is greater than $100,000 at 11:59:59 PM UTC on Dec 31, 2026.",
        "key_entities": ["BTC", "$100,000", "Dec 31, 2026", "Coinbase"],
        "resolution_keywords": ["BTC-USD", "Coinbase price", "December 31 2026", "one hundred thousand"],
        "ambiguity_score": 0.05,
        "resolution_type": "binary",
    },
    "C2": {
        "question": "Will Ethereum gas fee average below 10 gwei in June 2026?",
        "criteria": "Resolves YES if the monthly average gas fee on Ethereum mainnet is below 10 gwei in June 2026.",
        "resolution_source": "Etherscan gas tracker or equivalent public blockchain indexer",
        "resolution_condition": "Ethereum mainnet average gas price is less than 10 gwei during the month of June 2026.",
        "key_entities": ["Ethereum", "10 gwei", "June 2026"],
        "resolution_keywords": ["average gas fee", "Ethereum mainnet", "June 2026", "gwei price"],
        "ambiguity_score": 0.18,
        "resolution_type": "binary",
    },
    "S1": {
        "question": "Will France win the 2026 FIFA World Cup?",
        "criteria": "Resolves YES if France men's national team wins the 2026 FIFA World Cup tournament.",
        "resolution_source": "FIFA Official Website",
        "resolution_condition": "France wins the final match of the 2026 FIFA World Cup.",
        "key_entities": ["France", "2026 FIFA World Cup"],
        "resolution_keywords": ["World Cup champion", "FIFA 2026", "France team", "tournament winner"],
        "ambiguity_score": 0.02,
        "resolution_type": "binary",
    },
    "S2": {
        "question": "Will LeBron James announce retirement in 2026?",
        "criteria": "Resolves YES if LeBron James officially announces his retirement from professional basketball in 2026.",
        "resolution_source": "LeBron James official statements or NBA official press release",
        "resolution_condition": "LeBron James announces retirement from the NBA during the year 2026.",
        "key_entities": ["LeBron James", "retirement", "2026"],
        "resolution_keywords": ["LeBron retirement", "basketball retirement", "NBA release", "James retirement"],
        "ambiguity_score": 0.25,
        "resolution_type": "binary",
    },
    "L1": {
        "question": "Will the Supreme Court rule on the social media censorship case by July 2026?",
        "criteria": "Resolves YES if the US Supreme Court issues a final opinion in the social media censorship case by June 30, 2026.",
        "resolution_source": "Supreme Court of the United States official website / opinions",
        "resolution_condition": "SCOTUS issues a final opinion in the social media censorship case on or before June 30, 2026.",
        "key_entities": ["Supreme Court", "social media censorship", "June 30, 2026"],
        "resolution_keywords": ["SCOTUS opinion", "social media censorship", "Circuit Court ruling", "Supreme Court decision"],
        "ambiguity_score": 0.12,
        "resolution_type": "binary",
    },
    "L2": {
        "question": "Will DOJ file an antitrust lawsuit against company X in 2026?",
        "criteria": "Resolves YES if the US Department of Justice files an antitrust lawsuit against company X in federal court in 2026.",
        "resolution_source": "Department of Justice official press releases or court docket",
        "resolution_condition": "DOJ files a civil antitrust lawsuit against company X in a federal district court during the calendar year 2026.",
        "key_entities": ["Department of Justice", "antitrust lawsuit", "company X", "2026"],
        "resolution_keywords": ["DOJ antitrust", "Department of Justice lawsuit", "anti-competitive practices", "federal court suit"],
        "ambiguity_score": 0.15,
        "resolution_type": "binary",
    },
    "E1": {
        "question": "Will the economy be in recession by Q3 2026?",
        "criteria": "Resolves YES if the NBER officially declares a recession in the US economy starting in or before Q3 2026.",
        "resolution_source": "National Bureau of Economic Research (NBER)",
        "resolution_condition": "NBER Business Cycle Dating Committee declares a recession that begins on or before September 30, 2026.",
        "key_entities": ["NBER", "recession", "Q3 2026", "US economy"],
        "resolution_keywords": ["NBER recession", "Business Cycle Dating Committee", "economic recession", "GDP contraction"],
        "ambiguity_score": 0.75,
        "resolution_type": "binary",
    },
}


# ─────────────────────────────────────────────────────────────────────────────
# MOCK SUPABASE CLIENT BUILDER
# ─────────────────────────────────────────────────────────────────────────────

class MockTableBuilder:
    """Mock implementation of Supabase postgrest Table Builder for sync queries."""

    def __init__(self, db_state: dict[str, dict[str, Any]], table_name: str) -> None:
        """Initialize with pointer to shared db state and target table."""
        self.db_state = db_state
        self.table_name = table_name
        self.filters: list[tuple[str, str, Any]] = []
        self.selected_cols: str | None = None

    def select(self, cols: str) -> "MockTableBuilder":
        """Store selected columns."""
        self.selected_cols = cols
        return self

    def eq(self, col: str, val: Any) -> "MockTableBuilder":
        """Store equality filter."""
        self.filters.append(("eq", col, val))
        return self

    def in_(self, col: str, vals: list[Any]) -> "MockTableBuilder":
        """Store in-list filter."""
        self.filters.append(("in", col, vals))
        return self

    def upsert(self, data: dict[str, Any], on_conflict: str | None = None) -> "MockTableBuilder":
        """Insert or update mock record in shared database dict."""
        table = self.db_state.setdefault(self.table_name, {})
        key = data.get("market_id")
        if key:
            if key in table:
                table[key].update(data)
            else:
                table[key] = data.copy()
        return self

    def update(self, data: dict[str, Any]) -> "MockTableBuilder":
        """Update records matching stored filters in shared database dict."""
        table = self.db_state.get(self.table_name, {})
        for key, row in list(table.items()):
            match = True
            for f_type, col, val in self.filters:
                if f_type == "eq":
                    if row.get(col) != val:
                        match = False
                elif f_type == "in":
                    if row.get(col) not in val:
                        match = False
            if match:
                row.update(data)
        return self

    def execute(self) -> Any:
        """Execute mock query and return Result object."""
        class Result:
            def __init__(self, data: list[dict[str, Any]]) -> None:
                self.data = data

        table = self.db_state.get(self.table_name, {})
        results: list[dict[str, Any]] = []
        for key, row in table.items():
            match = True
            for f_type, col, val in self.filters:
                if f_type == "eq":
                    if row.get(col) != val:
                        match = False
                elif f_type == "in":
                    if row.get(col) not in val:
                        match = False
            if match:
                results.append(row.copy())
        return Result(results)


# ─────────────────────────────────────────────────────────────────────────────
# MOCK RESPONSE FOR AIOHTTP
# ─────────────────────────────────────────────────────────────────────────────

class MockResponse:
    """Mock class representing an aiohttp HTTP response."""

    def __init__(self, status: int, json_data: dict[str, Any], text_data: str = "", delay: float = 0.0) -> None:
        """Initialize mock response parameters."""
        self.status = status
        self._json_data = json_data
        self._text_data = text_data
        self._delay = delay

    async def __aenter__(self) -> "MockResponse":
        """Enter async context manager, simulating delay or timeout."""
        if self._delay > 0:
            raise asyncio.TimeoutError("Simulated LLM API Timeout")
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Exit async context manager."""
        pass

    async def json(self) -> dict[str, Any]:
        """Return pre-defined mock JSON data."""
        return self._json_data

    async def text(self) -> str:
        """Return pre-defined mock text data."""
        return self._text_data


# ─────────────────────────────────────────────────────────────────────────────
# FIXTURES
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_db_state() -> dict[str, dict[str, Any]]:
    """Fixture providing clean, local in-memory DB dictionary state."""
    return {}


@pytest.fixture
def mock_supabase(mock_db_state: dict[str, dict[str, Any]]) -> Generator[dict[str, dict[str, Any]], None, None]:
    """Fixture to intercept Supabase client creation and redirect queries to mock."""
    class MockClient:
        def table(self, name: str) -> MockTableBuilder:
            return MockTableBuilder(mock_db_state, name)

    async def fake_get_client() -> MockClient:
        return MockClient()

    with patch("llm.contract_parser.get_client", fake_get_client):
        yield mock_db_state


@pytest.fixture
def mock_openrouter() -> Generator[dict[str, Any], None, None]:
    """Fixture to intercept OpenRouter API client calls and return realistic completions."""
    api_calls: dict[str, Any] = {"count": 0, "delay": 0}

    def mock_post(self_session: Any, url: str, **kwargs: Any) -> MockResponse:
        api_calls["count"] += 1

        json_payload = kwargs.get("json", {})
        messages = json_payload.get("messages", [])
        user_content = ""
        for m in messages:
            if m.get("role") == "user":
                user_content = m.get("content", "")
                break

        market_id = None
        for mid in MOCK_MARKETS:
            if mid in user_content or MOCK_MARKETS[mid]["question"] in user_content:
                market_id = mid
                break

        if not market_id:
            for mid in MOCK_MARKETS:
                if mid in user_content:
                    market_id = mid
                    break

        if not market_id:
            market_id = "P1"

        market_data = MOCK_MARKETS[market_id]

        choice_content = {
            "resolution_source": market_data["resolution_source"],
            "resolution_condition": market_data["resolution_condition"],
            "key_entities": market_data["key_entities"],
            "resolution_keywords": market_data["resolution_keywords"],
            "ambiguity_score": market_data["ambiguity_score"],
            "resolution_type": market_data["resolution_type"],
        }

        response_json = {
            "id": "gen-123",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": json.dumps(choice_content),
                    }
                }
            ],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 50,
                "total_tokens": 150,
            },
        }

        return MockResponse(200, response_json, delay=api_calls["delay"])

    with patch("aiohttp.ClientSession.post", mock_post):
        yield api_calls


# ─────────────────────────────────────────────────────────────────────────────
# UNIT TESTS
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_parser_valid_structured_json_all_10_markets_criterion_5_1_and_5_4(
    mock_supabase: dict[str, dict[str, Any]],
    mock_openrouter: dict[str, Any],
) -> None:
    """Criterion 5.1 & 5.4: Test standard parsing on all 10 real test contracts."""
    for market_id, market_data in MOCK_MARKETS.items():
        result = await parse_contract(
            market_id=market_id,
            market_question=market_data["question"],
            resolution_criteria=market_data["criteria"],
        )

        # Assert output matches valid non-empty structured JSON
        assert result is not None, f"Failed parsing {market_id}"
        assert isinstance(result, ContractParserOutput)
        assert len(result.resolution_source) >= 1
        assert len(result.resolution_condition) >= 1
        assert len(result.key_entities) >= 1
        assert len(result.resolution_keywords) >= 3
        assert 0.0 <= result.ambiguity_score <= 1.0
        assert len(result.resolution_type) >= 1

        # Check that result was successfully cached in mock DB
        cached_row = mock_supabase.get("resolution_keyword_cache", {}).get(market_id)
        assert cached_row is not None
        assert cached_row["market_id"] == market_id
        assert cached_row["resolution_keywords"] == market_data["resolution_keywords"]
        assert cached_row["ambiguity_score"] == market_data["ambiguity_score"]
        assert cached_row["resolution_type"] == market_data["resolution_type"]
        assert "cached_at" in cached_row
        assert "resolution_conditions" in cached_row

    # Validate that we have exactly 10 cache entries
    assert len(mock_supabase.get("resolution_keyword_cache", {})) == 10


@pytest.mark.anyio
async def test_keywords_are_meaningful_and_specific_criterion_5_2(
    mock_supabase: dict[str, dict[str, Any]],
    mock_openrouter: dict[str, Any],
) -> None:
    """Criterion 5.2: Ensure extracted keywords are meaningful and not generic stopwords."""
    stopwords = {"the", "will", "market", "yes", "no", "a", "an", "in", "of", "to", "is", "be"}

    for market_id, market_data in MOCK_MARKETS.items():
        result = await parse_contract(
            market_id=market_id,
            market_question=market_data["question"],
            resolution_criteria=market_data["criteria"],
        )
        assert result is not None
        for keyword in result.resolution_keywords:
            clean_kw = keyword.lower().strip()
            assert clean_kw not in stopwords, (
                f"Market {market_id} has generic keyword: '{keyword}'"
            )


@pytest.mark.anyio
async def test_ambiguity_score_calibrated_reasonably_criterion_5_3(
    mock_supabase: dict[str, dict[str, Any]],
    mock_openrouter: dict[str, Any],
) -> None:
    """Criterion 5.3: Verify that clear markets score lower than ambiguous ones."""
    # C1 (BTC price feed close) is clear
    btc_clear = await parse_contract(
        market_id="C1",
        market_question=MOCK_MARKETS["C1"]["question"],
        resolution_criteria=MOCK_MARKETS["C1"]["criteria"],
    )

    # E1 (US economy recession definition) is ambiguous
    economy_ambig = await parse_contract(
        market_id="E1",
        market_question=MOCK_MARKETS["E1"]["question"],
        resolution_criteria=MOCK_MARKETS["E1"]["criteria"],
    )

    assert btc_clear is not None
    assert economy_ambig is not None

    # Assert calibrated thresholds
    assert btc_clear.ambiguity_score < 0.3
    assert economy_ambig.ambiguity_score > 0.6
    assert btc_clear.ambiguity_score < economy_ambig.ambiguity_score


@pytest.mark.anyio
async def test_cache_hit_bypasses_api_calls_criterion_5_5(
    mock_supabase: dict[str, dict[str, Any]],
    mock_openrouter: dict[str, Any],
) -> None:
    """Criterion 5.5: Assert subsequent requests read from cache and trigger zero API calls."""
    # First parse (cache miss)
    res1 = await parse_contract(
        market_id="P1",
        market_question=MOCK_MARKETS["P1"]["question"],
        resolution_criteria=MOCK_MARKETS["P1"]["criteria"],
    )
    assert res1 is not None
    assert mock_openrouter["count"] == 1

    # Second parse (cache hit)
    res2 = await parse_contract(
        market_id="P1",
        market_question=MOCK_MARKETS["P1"]["question"],
        resolution_criteria=MOCK_MARKETS["P1"]["criteria"],
    )
    assert res2 is not None
    assert mock_openrouter["count"] == 1  # count unchanged, proving zero API calls


@pytest.mark.anyio
async def test_stale_cache_entries_trigger_refresh_criterion_5_6(
    mock_supabase: dict[str, dict[str, Any]],
    mock_openrouter: dict[str, Any],
) -> None:
    """Criterion 5.6: Test that cache entries older than 24 hours trigger an API refresh."""
    # Parse to populate cache
    res1 = await parse_contract(
        market_id="P2",
        market_question=MOCK_MARKETS["P2"]["question"],
        resolution_criteria=MOCK_MARKETS["P2"]["criteria"],
    )
    assert res1 is not None
    assert mock_openrouter["count"] == 1

    # Force cache entry to be stale (25 hours ago)
    stale_time = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    mock_supabase["resolution_keyword_cache"]["P2"]["cached_at"] = stale_time

    # Re-run parse (triggers refresh)
    res2 = await parse_contract(
        market_id="P2",
        market_question=MOCK_MARKETS["P2"]["question"],
        resolution_criteria=MOCK_MARKETS["P2"]["criteria"],
    )
    assert res2 is not None
    assert mock_openrouter["count"] == 2  # API call count incremented


@pytest.mark.anyio
async def test_parser_timeout_fails_gracefully_criterion_5_7(
    mock_supabase: dict[str, dict[str, Any]],
    mock_openrouter: dict[str, Any],
) -> None:
    """Criterion 5.7: Verify that delay past LLM timeout is handled gracefully."""
    mock_openrouter["delay"] = 19  # More than config.LLM_TIMEOUT_SECONDS (18)

    result = await parse_contract(
        market_id="S1",
        market_question=MOCK_MARKETS["S1"]["question"],
        resolution_criteria=MOCK_MARKETS["S1"]["criteria"],
    )

    # Verify graceful recovery: returns None and doesn't crash
    assert result is None


@pytest.mark.anyio
async def test_get_cached_keywords_routing_criterion_5_8(
    mock_supabase: dict[str, dict[str, Any]],
    mock_openrouter: dict[str, Any],
) -> None:
    """Criterion 5.8: Assert get_cached_keywords behaves correctly for fast path checks."""
    # Cache is empty initially
    kws = await get_cached_keywords("C1")
    assert kws is None

    # Populate cache
    res = await parse_contract(
        market_id="C1",
        market_question=MOCK_MARKETS["C1"]["question"],
        resolution_criteria=MOCK_MARKETS["C1"]["criteria"],
    )
    assert res is not None

    # Fresh cache returns keywords
    kws = await get_cached_keywords("C1")
    assert kws is not None
    assert len(kws) >= 3
    assert kws == MOCK_MARKETS["C1"]["resolution_keywords"]

    # Make cache stale
    stale_time = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    mock_supabase["resolution_keyword_cache"]["C1"]["cached_at"] = stale_time

    # Stale cache returns None (forces slow path)
    kws_stale = await get_cached_keywords("C1")
    assert kws_stale is None
