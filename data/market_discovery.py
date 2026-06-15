import asyncio
import logging
from datetime import datetime, timezone
import json
from typing import Optional
import httpx
import config

logger = logging.getLogger(__name__)

class MarketPriceUnavailableError(Exception):
    """Raised when CLOB midpoint price or order book is unavailable/invalid."""
    pass

class MarketDiscoveryTimeoutError(Exception):
    """Raised when price or metadata queries exceed timeout limit."""
    pass

# Global module-level cache
_MARKET_CACHE: list[dict] = []
_CACHE_UPDATED_AT: Optional[datetime] = None

def _parse_markets_page(data: list) -> list[dict]:
    """Parse a single page of Gamma API market responses into cache entries."""
    parsed = []
    for market in data:
        market_id = market.get("id")
        if not market_id:
            continue

        # Parse clobTokenIds (YES token is index 0)
        try:
            clob_ids_str = market.get("clobTokenIds", "[]")
            clob_ids = json.loads(clob_ids_str)
            if not clob_ids or not isinstance(clob_ids, list):
                logger.warning(f"[MARKET_DISCOVERY] clobTokenIds is empty or invalid for market {market_id}")
                continue
            token_id = clob_ids[0]
        except Exception as e:
            logger.warning(f"[MARKET_DISCOVERY] Failed to parse clobTokenIds for market {market_id}: {e}")
            continue

        question = market.get("question", "")
        end_date = market.get("endDate") or market.get("resolveBy")

        # Parse volume
        try:
            volume_usd = float(market.get("volume") or 0)
        except ValueError:
            volume_usd = 0.0

        if volume_usd < config.MIN_MARKET_VOLUME_USD:
            continue

        parsed.append({
            "market_id": market_id,
            "question": question,
            "token_id": token_id,
            "end_date_iso": end_date,
            "volume_usd": volume_usd
        })
    return parsed


async def refresh_market_cache() -> None:
    """
    Fetches all active, non-resolved markets from Polymarket Gamma API using
    offset-based pagination (API hard cap: 100 results per request).
    Filters out markets below MIN_MARKET_VOLUME_USD and updates the global cache.
    Wrapped in a 60-second total timeout. On failure, logs CRITICAL and sends
    a Telegram alert — never fails silently.
    """
    global _MARKET_CACHE, _CACHE_UPDATED_AT

    PAGE_LIMIT = 100      # API hard cap per request
    MAX_PAGES = 50        # Safety cap: 5,000 markets maximum
    PER_REQUEST_TIMEOUT = 8.0
    TOTAL_TIMEOUT = 60.0

    async def _fetch_all_pages() -> list[dict]:
        """Paginate through all market pages using offset."""
        all_markets: list[dict] = []
        base_url = f"{config.GAMMA_API_BASE}/markets?active=true&closed=false&limit={PAGE_LIMIT}"

        async with httpx.AsyncClient() as client:
            for page in range(MAX_PAGES):
                offset = page * PAGE_LIMIT
                url = f"{base_url}&offset={offset}"
                response = await client.get(url, timeout=PER_REQUEST_TIMEOUT)

                if response.status_code != 200:
                    logger.critical(
                        f"[MARKET_DISCOVERY] Gamma API returned HTTP {response.status_code} "
                        f"on page {page} (offset={offset})"
                    )
                    break

                data = response.json()
                if not isinstance(data, list):
                    logger.critical(
                        "[MARKET_DISCOVERY] Gamma API returned invalid format (expected list)"
                    )
                    break

                if not data:
                    # Empty page — end of results
                    logger.debug(f"[MARKET_DISCOVERY] Empty page at offset={offset}, stopping.")
                    break

                parsed = _parse_markets_page(data)
                all_markets.extend(parsed)
                logger.debug(
                    f"[MARKET_DISCOVERY] Page {page}: fetched {len(data)}, "
                    f"kept {len(parsed)} (volume filter), running total={len(all_markets)}"
                )

                if len(data) < PAGE_LIMIT:
                    # Last page (partial) — no more results
                    break

        return all_markets

    try:
        all_markets = await asyncio.wait_for(_fetch_all_pages(), timeout=TOTAL_TIMEOUT)
        _MARKET_CACHE = all_markets
        _CACHE_UPDATED_AT = datetime.now(timezone.utc)
        logger.info(f"[MARKET_DISCOVERY] Cache refreshed: {len(_MARKET_CACHE)} active markets.")

    except (httpx.HTTPError, httpx.TimeoutException) as e:
        logger.critical(f"[MARKET_DISCOVERY] Connection/timeout exception during cache refresh: {e}")
        _send_cache_failure_alert(str(e))
    except asyncio.TimeoutError:
        logger.critical(f"[MARKET_DISCOVERY] Cache refresh timed out (>{TOTAL_TIMEOUT}s)")
        _send_cache_failure_alert(f"Total refresh timeout exceeded {TOTAL_TIMEOUT}s")
    except Exception as e:
        logger.critical(f"[MARKET_DISCOVERY] Unexpected exception refreshing market cache: {e}")
        _send_cache_failure_alert(str(e))


def _send_cache_failure_alert(error_detail: str) -> None:
    """Fire-and-forget Telegram CRITICAL alert on market cache refresh failure."""
    try:
        import asyncio as _asyncio
        from monitoring.telegram_alerts import alert_pipeline_component_crash
        loop = _asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(
                alert_pipeline_component_crash(
                    component="market_discovery.refresh_market_cache",
                    error_type="CacheRefreshFailure",
                    error_detail=error_detail[:200]
                )
            )
    except Exception as alert_err:
        logger.error(f"[MARKET_DISCOVERY] Failed to send cache failure alert: {alert_err}")

def find_matching_markets(signal_entities: dict) -> list[dict]:
    """
    Score each cached market against signal entities using overlap.
    Returns sorted list of markets with score >= MARKET_MATCH_THRESHOLD.
    Runs against in-memory cache, zero HTTP calls.
    """
    entities = signal_entities.get("entities", [])
    if not entities:
        return []
        
    if not _MARKET_CACHE or _CACHE_UPDATED_AT is None:
        return []
        
    # Cache freshness check (stale if > 10 minutes)
    age = (datetime.now(timezone.utc) - _CACHE_UPDATED_AT).total_seconds()
    if age > 600:
        logger.critical(
            f"[MARKET_DISCOVERY] Cache is stale ({age:.1f}s old). "
            f"Market matching disabled until refresh succeeds."
        )
        return []
        
    matched_markets = []
    total_entities = len(entities)
    
    for market in _MARKET_CACHE:
        question_lower = market["question"].lower()
        matched_count = 0
        for entity in entities:
            if entity.lower() in question_lower:
                matched_count += 1
                
        score = matched_count / total_entities
        
        if score >= config.MARKET_MATCH_THRESHOLD:
            # Create a copy with score included
            entry = market.copy()
            entry["score"] = score
            matched_markets.append(entry)
            
    # Sort descending by score
    matched_markets.sort(key=lambda x: x["score"], reverse=True)
    return matched_markets

async def get_market_price(token_id: str) -> float:
    """
    Queries Polymarket CLOB midpoint for the token.
    Raises MarketPriceUnavailableError if spread > MAX_SPREAD_THRESHOLD or order book is empty.
    Raises MarketDiscoveryTimeoutError if the request exceeds a 4-second timeout.
    """
    async def _fetch() -> float:
        url = f"https://clob.polymarket.com/book?token_id={token_id}"
        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(url, timeout=4.0)
            except Exception as e:
                raise MarketPriceUnavailableError(f"HTTP request failed: {e}") from e
                
            if response.status_code != 200:
                raise MarketPriceUnavailableError(f"CLOB book returned HTTP {response.status_code}")
                
            data = response.json()
            bids = data.get("bids", [])
            asks = data.get("asks", [])
            
            if not bids:
                raise MarketPriceUnavailableError("Order book has no bids")
            if not asks:
                raise MarketPriceUnavailableError("Order book has no asks")
                
            try:
                best_bid = max(float(b["price"]) for b in bids)
                best_ask = min(float(a["price"]) for a in asks)
            except (ValueError, KeyError) as e:
                raise MarketPriceUnavailableError(f"Failed to parse orderbook prices: {e}") from e
                
            spread = best_ask - best_bid
            if spread > config.MAX_SPREAD_THRESHOLD:
                raise MarketPriceUnavailableError(f"Spread {spread:.3f} exceeds threshold")
                
            return (best_bid + best_ask) / 2.0

    try:
        return await asyncio.wait_for(_fetch(), timeout=4.0)
    except asyncio.TimeoutError as e:
        raise MarketDiscoveryTimeoutError("get_market_price timed out") from e
    except MarketPriceUnavailableError:
        raise
    except Exception as e:
        raise MarketPriceUnavailableError(f"Unexpected pricing error: {e}") from e

async def get_market_metadata(market_id: str) -> dict:
    """
    Fetches question, description, and resolution criteria from Gamma API.
    Raises MarketDiscoveryTimeoutError if the request exceeds a 4-second timeout.
    """
    async def _fetch() -> dict:
        url = f"{config.GAMMA_API_BASE}/markets/{market_id}"
        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(url, timeout=4.0)
            except Exception as e:
                raise MarketPriceUnavailableError(f"HTTP request failed: {e}") from e
                
            if response.status_code != 200:
                raise MarketPriceUnavailableError(f"Gamma API returned HTTP {response.status_code}")
                
            market = response.json()
            if isinstance(market, list):
                if not market:
                    raise MarketPriceUnavailableError(f"Gamma API returned empty list for market {market_id}")
                market = market[0]
                
            return {
                "question": market.get("question", ""),
                "description": market.get("description", ""),
                "resolution_criteria": market.get("resolutionCriteria") or market.get("description", "")
            }

    try:
        return await asyncio.wait_for(_fetch(), timeout=4.0)
    except asyncio.TimeoutError as e:
        raise MarketDiscoveryTimeoutError("get_market_metadata timed out") from e
    except Exception as e:
        raise MarketPriceUnavailableError(f"Unexpected metadata error: {e}") from e
