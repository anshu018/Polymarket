import pytest
import asyncio
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch, MagicMock
import httpx

import config
import data.market_discovery as md
from data.market_discovery import (
    refresh_market_cache,
    find_matching_markets,
    get_market_price,
    get_market_metadata,
    MarketPriceUnavailableError,
    MarketDiscoveryTimeoutError,
)

# Dummy class to mock HTTPX responses
class MockResponse:
    def __init__(self, status_code: int, json_data: any) -> None:
        self.status_code = status_code
        self._json_data = json_data

    def json(self) -> any:
        return self._json_data


@pytest.mark.anyio
async def test_refresh_market_cache_filters_low_volume() -> None:
    """Verify that markets with volume_usd < MIN_MARKET_VOLUME_USD are filtered out."""
    mock_data = [
        {
            "id": "m1",
            "question": "Will Trump impeachment happen?",
            "clobTokenIds": '["0x123"]',
            "endDate": "2026-12-31",
            "volume": "1000.0"
        },
        {
            "id": "m2",
            "question": "Will Biden resign?",
            "clobTokenIds": '["0x456"]',
            "endDate": "2026-12-31",
            "volume": "400.0"  # Below config.MIN_MARKET_VOLUME_USD (500.0)
        }
    ]

    async def mock_get(url: str, **kwargs):
        if "offset=0" in url:
            # First page: return data
            return MockResponse(200, mock_data)
        # Subsequent calls: empty (end of results)
        return MockResponse(200, [])

    with patch("httpx.AsyncClient.get", side_effect=mock_get):
        await refresh_market_cache()

        # Verify cache contains only m1 (m2 filtered by volume)
        assert len(md._MARKET_CACHE) == 1
        assert md._MARKET_CACHE[0]["market_id"] == "m1"
        assert md._MARKET_CACHE[0]["volume_usd"] == 1000.0
        assert md._CACHE_UPDATED_AT is not None


@pytest.mark.anyio
async def test_find_matching_markets_returns_sorted_by_score() -> None:
    """Verify that matches are sorted descending by overlap score."""
    md._MARKET_CACHE = [
        {
            "market_id": "m1",
            "question": "Will Donald Trump face impeachment?",
            "token_id": "0x123",
            "end_date_iso": "2026-12-31",
            "volume_usd": 1000.0
        },
        {
            "market_id": "m2",
            "question": "Will Donald Trump visit France or Germany?",
            "token_id": "0x456",
            "end_date_iso": "2026-12-31",
            "volume_usd": 1000.0
        }
    ]
    md._CACHE_UPDATED_AT = datetime.now(timezone.utc)

    # Entities matching both "Trump" and "France"
    res = find_matching_markets({"entities": ["Trump", "France"]})
    
    # m2 has both matches (score 1.0), m1 has one match (score 0.5)
    assert len(res) == 2
    assert res[0]["market_id"] == "m2"
    assert res[0]["score"] == 1.0
    assert res[1]["market_id"] == "m1"
    assert res[1]["score"] == 0.5


@pytest.mark.anyio
async def test_find_matching_markets_filters_below_threshold() -> None:
    """Verify that markets below config.MARKET_MATCH_THRESHOLD are excluded."""
    import config
    orig_threshold = config.MARKET_MATCH_THRESHOLD
    config.MARKET_MATCH_THRESHOLD = 0.50
    try:
        md._MARKET_CACHE = [
            {
                "market_id": "m1",
                "question": "Will Donald Trump face impeachment?",
                "token_id": "0x123",
                "end_date_iso": "2026-12-31",
                "volume_usd": 1000.0
            }
        ]
        md._CACHE_UPDATED_AT = datetime.now(timezone.utc)

        # Capped denominator is 3. 1 match ("Trump") out of 4 = 1/3 = 0.33 (below 0.50, should filter out)
        res = find_matching_markets({"entities": ["Trump", "France", "Germany", "Japan"]})
        assert len(res) == 0
    finally:
        config.MARKET_MATCH_THRESHOLD = orig_threshold



@pytest.mark.anyio
async def test_find_matching_markets_stale_cache_returns_empty() -> None:
    """Verify that stale cache (>10 mins old) causes empty return and warning."""
    md._MARKET_CACHE = [
        {
            "market_id": "m1",
            "question": "Will Donald Trump face impeachment?",
            "token_id": "0x123",
            "end_date_iso": "2026-12-31",
            "volume_usd": 1000.0
        }
    ]
    # Set cache timestamp to 11 minutes ago
    md._CACHE_UPDATED_AT = datetime.now(timezone.utc) - timedelta(minutes=11)

    res = find_matching_markets({"entities": ["Trump"]})
    assert len(res) == 0


@pytest.mark.anyio
async def test_get_market_price_wide_spread_raises() -> None:
    """Verify that spread exceeding config.MAX_SPREAD_THRESHOLD raises exception."""
    mock_orderbook = {
        "bids": [{"price": "0.40", "size": "100"}],
        "asks": [{"price": "0.58", "size": "100"}]  # Spread = 0.18 > 0.15 threshold
    }

    async def mock_get(url: str, **kwargs):
        return MockResponse(200, mock_orderbook)

    with patch("httpx.AsyncClient.get", side_effect=mock_get):
        with pytest.raises(MarketPriceUnavailableError, match="exceeds threshold"):
            await get_market_price("0x123")


@pytest.mark.anyio
async def test_find_matching_markets_empty_entities_returns_empty() -> None:
    """Verify empty/missing entity keys return empty list."""
    md._MARKET_CACHE = [
        {
            "market_id": "m1",
            "question": "Will Donald Trump face impeachment?",
            "token_id": "0x123",
            "end_date_iso": "2026-12-31",
            "volume_usd": 1000.0
        }
    ]
    md._CACHE_UPDATED_AT = datetime.now(timezone.utc)

    assert find_matching_markets({"entities": []}) == []
    assert find_matching_markets({}) == []


@pytest.mark.anyio
async def test_get_market_price_success() -> None:
    """Verify successful price fetching under narrow spread."""
    mock_orderbook = {
        "bids": [{"price": "0.50", "size": "100"}],
        "asks": [{"price": "0.54", "size": "100"}]  # Spread = 0.04 < 0.15
    }

    async def mock_get(url: str, **kwargs):
        return MockResponse(200, mock_orderbook)

    with patch("httpx.AsyncClient.get", side_effect=mock_get):
        price = await get_market_price("0x123")
        assert price == 0.52


@pytest.mark.anyio
async def test_get_market_metadata_success() -> None:
    """Verify successful metadata extraction from Gamma API."""
    mock_metadata = {
        "question": "Will Donald Trump face impeachment?",
        "description": "This market resolves to YES if Donald Trump is impeached.",
        "resolutionCriteria": "Resolves YES if house votes."
    }

    async def mock_get(url: str, **kwargs):
        return MockResponse(200, mock_metadata)

    with patch("httpx.AsyncClient.get", side_effect=mock_get):
        metadata = await get_market_metadata("m1")
        assert metadata["question"] == "Will Donald Trump face impeachment?"
        assert metadata["resolution_criteria"] == "Resolves YES if house votes."


@pytest.mark.anyio
async def test_refresh_market_cache_handles_http_error() -> None:
    """Verify that exception propagation works and formats ConnectError properly."""
    async def mock_get(url: str, **kwargs):
        raise httpx.ConnectError("Connection refused")

    with patch("httpx.AsyncClient.get", side_effect=mock_get), \
         patch("data.market_discovery._send_cache_failure_alert") as mock_alert:
        await refresh_market_cache()
        mock_alert.assert_called_once_with("ConnectError: Connection refused")


@pytest.mark.anyio
async def test_refresh_market_cache_handles_unexpected_error() -> None:
    """Verify that unexpected exception formatting works correctly."""
    async def mock_get(url: str, **kwargs):
        raise ValueError("Some weird JSON error")

    with patch("httpx.AsyncClient.get", side_effect=mock_get), \
         patch("data.market_discovery._send_cache_failure_alert") as mock_alert:
        await refresh_market_cache()
        mock_alert.assert_called_once_with("ValueError: Some weird JSON error")
