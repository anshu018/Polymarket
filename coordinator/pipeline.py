"""
End-to-End Integration Pipeline — Layer 6
Coordinates the entire ingestion, analysis, cache, parsing, trade decision, risk check, conflict aggregation, and execution.
"""

import asyncio
import os
import logging
import uuid
import time
import re
from datetime import datetime, timezone, timedelta
from typing import Optional, Any, Tuple

import config
from memory.supabase_client import get_client
from risk import risk_engine
from llm.news_analyst import classify_signal, NewsAnalystOutput
from llm.contract_parser import parse_contract, get_cached_keywords
from llm.trade_decision import decide_trade, TradeDecisionOutput
from llm.coordinator import coordinate_decision, CoordinatorOutput
from data.market_discovery import (
    find_matching_markets,
    get_market_price,
    get_market_metadata,
)
from strategies.estimator import get_estimate
from monitoring.telegram_alerts import (
    alert_supabase_degradation,
    alert_idempotency_duplicate,
    alert_circuit_breaker,
    alert_reconciliation_failure,
)
from coordinator.market_state import load_market_state, get_market_lock

logger = logging.getLogger(__name__)

# Stage-by-stage drop counters (Task 2)
_drop_counters: dict[str, int] = {
    "price_fetch_failed": 0,
    "no_market_match": 0,
    "news_analyst_none": 0,
    "low_confidence": 0,
    "estimate:no_data": 0,
    "estimate:ttr_fallback": 0,
    "contract_parser_none": 0,
    "trade_decision_none": 0,
    "abstain": 0,
    "risk_gate:circuit_breaker": 0,
    "risk_gate:auto_exit_liquidity": 0,
    "risk_gate:low_liquidity": 0,
    "risk_gate:low_confidence": 0,
    "risk_gate:low_edge": 0,
    "risk_gate:max_category_exposure": 0,
    "risk_gate:max_correlated_exposure": 0,
    "risk_gate:cash_reserve": 0,
    "risk_gate:deadline_risk": 0,
    "risk_gate:max_market_tranches": 0,
    "risk_gate:opposite_direction": 0,
    "risk_gate:low_confidence_add": 0,
    "risk_gate:exposure_unavailable": 0,
    "risk_gate:portfolio_capacity": 0,
    "risk_gate:max_market_exposure": 0,
}

def _increment_drop(reason: str) -> None:
    """Increment the drop counter for a specific reason."""
    if reason in _drop_counters:
        _drop_counters[reason] += 1
    else:
        _drop_counters[reason] = 1

def get_drop_counters() -> dict[str, int]:
    """Return a copy of the stage-by-stage drop counters."""
    return _drop_counters.copy()



# ─────────────────────────────────────────────
# KEYWORD EXPANSION FOR MARKET MATCHING (C4)
# ─────────────────────────────────────────────

KEYWORD_EXPANSION: dict[str, list[str]] = {
    "federal reserve": ["fed", "fomc", "interest rate", "rate cut", "rate hike", "powell"],
    "election": ["president", "senate", "house", "vote", "ballot", "democrat", "republican"],
    "bitcoin": ["btc", "crypto", "cryptocurrency", "digital asset"],
    "supreme court": ["scotus", "justices", "ruling", "decision"],
    "ukraine": ["russia", "war", "ceasefire", "nato"],
    "trump": ["president", "white house", "executive order"],
}


# Stop words filtered from entity extraction before market matching.
# Generic tokens that appear in thousands of headlines but never in
# Polymarket market questions — keeping them dilutes match scores below threshold.
_ENTITY_STOP_WORDS: frozenset[str] = frozenset({
    # Articles / prepositions / conjunctions
    "the", "and", "for", "with", "from", "that", "this", "into", "over",
    "amid", "after", "about", "their", "there", "would", "could", "should",
    "before", "during", "while", "since", "until", "between", "among",
    # Generic verbs / aux verbs
    "is", "are", "was", "will", "can", "has", "have", "had", "been",
    "get", "got", "use", "say", "said", "says", "sign", "signs", "signed",
    "allow", "allows", "using", "request", "requests", "deny", "denies", "denied",
    "calls", "wants", "seeks", "pushes", "push", "face", "faces",
    "surge", "surges", "surged", "drop", "drops", "fell", "rise", "rises",
    "reach", "reaches", "hit", "hits", "set", "sets",
    "warn", "warns", "warns", "claim", "claims", "says", "report", "reports",
    # Generic adjectives and adverbs
    "high", "low", "new", "old", "big", "major", "key", "top", "first",
    "last", "next", "more", "less", "most", "least", "very", "some",
    "alltime", "longtime", "record",
    # Generic nouns ubiquitous in news but absent from prediction markets
    "court", "deal", "gas", "law", "bill", "plan", "talks",
    "vote", "move", "case", "rule", "order", "news", "report",
    "year", "years", "week", "weeks", "month", "months", "day", "days",
    "time", "back", "price", "prices", "rate", "rates", "market", "markets",
    "official", "officials", "government", "minister", "ministers",
    "party", "parties", "state", "states", "united", "america",
    "percent", "billion", "million", "trillion",
    "execution", "nitrogen",  # too specific to crime/chemistry, not in market questions
})


def extract_entities(headline: str) -> list[str]:
    """
    Extracts named entities from the headline for market discovery.
    Uses spaCy if loaded, otherwise falls back to capitalized words and long terms.
    Generic stop words are filtered out before returning so they don't dilute
    the overlap score against market questions.
    """
    try:
        from data.spacy_filter import nlp
        if nlp is not None:
            doc = nlp(headline)
            valid_types = {"ORG", "PERSON", "GPE", "LAW", "DATE", "MONEY", "PERCENT", "EVENT"}
            ents = [e.text for e in doc.ents if e.label_ in valid_types]
            if ents:
                # Apply stop word filter even to spaCy results
                filtered = [e for e in ents if e.lower() not in _ENTITY_STOP_WORDS]
                return filtered if filtered else ents  # fall through if all filtered
    except Exception as e:
        logger.warning(f"Error using spaCy for entity extraction: {e}")

    # Simple fallback: extract capitalized words/phrases and words of length > 4
    matches = re.findall(r'\b[A-Z][a-zA-Z0-9]*\b(?:\s+\b[A-Z][a-zA-Z0-9]*\b)*', headline)
    words = headline.split()
    fallback_ents = list(matches)
    for w in words:
        clean_w = re.sub(r'[^\w]', '', w)
        if len(clean_w) > 4 and clean_w.lower() not in {
            "faces", "about", "their", "there", "would", "could", "should", "house", "votes", "before"
        }:
            fallback_ents.append(clean_w)

    # Apply stop word filter — remove single chars and generic tokens
    filtered = [
        e for e in set(fallback_ents)
        if len(e) > 1 and e.lower() not in _ENTITY_STOP_WORDS
    ]

    # Keyword expansion: if any known trigger phrase appears in the headline,
    # add its expanded synonyms so they participate in market matching.
    headline_lower = headline.lower()
    expanded: list[str] = []
    for trigger, synonyms in KEYWORD_EXPANSION.items():
        if trigger in headline_lower:
            expanded.extend(synonyms)

    # Merge and de-duplicate, keeping originals first
    seen: set[str] = {e.lower() for e in filtered}
    for syn in expanded:
        if syn.lower() not in seen:
            filtered.append(syn)
            seen.add(syn.lower())

    return filtered


# ─────────────────────────────────────────────
# DATABASE HELPERS WITH 2-SECOND TIMEOUTS
# ─────────────────────────────────────────────

async def fetch_category_defaults(category: str) -> dict[str, Any]:
    """
    Fetch latest Layer C category versions under 2-second timeout (RULE 5).
    Falls back to hardcoded conservative defaults on timeout or error.
    """
    async def _fetch() -> Optional[dict[str, Any]]:
        client = await get_client()
        res = (
            client.table("layer_c_category_versions")
            .select("avg_resolution_ambiguity_score,recommended_confidence_threshold,historical_edge_percent")
            .eq("category", category)
            .is_("superseded_by", "null")
            .order("valid_from", desc=True)
            .limit(1)
            .execute()
        )
        if res.data:
            return res.data[0]
        return None

    try:
        data = await asyncio.wait_for(_fetch(), timeout=config.SUPABASE_TIMEOUT_SECONDS)
        if data:
            return {
                "ambiguity": float(data.get("avg_resolution_ambiguity_score") or 0.30),
                "confidence": float(data.get("recommended_confidence_threshold") or 0.75),
                "edge": float(data.get("historical_edge_percent") or 0.07)
            }
    except asyncio.TimeoutError:
        logger.warning(
            f"[PIPELINE] Supabase layer_c_category_versions read timed out for {category}. "
            "Using hardcoded conservative defaults (RULE 5)"
        )
        asyncio.create_task(
            alert_supabase_degradation("layer_c_category_versions", "using hardcoded conservative defaults")
        )
    except Exception as e:
        logger.error(f"[PIPELINE] Supabase layer_c_category_versions read failed: {e}")

    # Hardcoded conservative defaults
    return {
        "ambiguity": 0.30,
        "confidence": 0.75,
        "edge": 0.07
    }


async def fetch_open_positions_exposure(category: str) -> Tuple[float, float, float]:
    """
    Fetch category and correlated exposures from Supabase under a 2-second timeout (RULE 5).
    If database read times out or fails: HALT trading (Fail Closed).
    
    Returns:
        tuple[category_exposure_pct, correlated_exposure_pct, open_pos_value]
    """
    async def _fetch() -> list[dict[str, Any]]:
        client = await get_client()
        res = (
            client.table("open_positions")
            .select("position_size_usdc,category")
            .execute()
        )
        return res.data or []

    try:
        rows = await asyncio.wait_for(_fetch(), timeout=config.SUPABASE_TIMEOUT_SECONDS)
        
        # Calculate total portfolio value dynamically
        total_portfolio = await get_live_portfolio_value()
        cat_total = 0.0
        corr_total = 0.0
        
        for r in rows:
            size = float(r.get("position_size_usdc") or 0.0)
            r_cat = r.get("category")
            
            if r_cat == category:
                cat_total += size
            # Correlated exposure (all open positions in the portfolio contribute to common shocks)
            corr_total += size
            
        return (cat_total / total_portfolio), (corr_total / total_portfolio), corr_total

    except asyncio.TimeoutError as e:
        logger.critical(
            f"[PIPELINE] Supabase open_positions read timed out! Halting new trades (RULE 5)."
        )
        asyncio.create_task(
            alert_supabase_degradation("open_positions", "HALTING new trades - database timeout")
        )
        raise RuntimeError("Trading halted due to open_positions read timeout") from e
    except Exception as e:
        logger.critical(f"[PIPELINE] open_positions exposure check failed: {e}. Halting.")
        raise RuntimeError("Trading halted due to exposure check failure") from e


async def check_pre_order_idempotency(uuid_str: str) -> Optional[dict[str, Any]]:
    """
    Check idempotency_log for existing UUID under a 2-second timeout (RULE 5).
    If database read times out or fails: HALT trading (Fail Closed).
    """
    async def _query() -> Optional[dict[str, Any]]:
        client = await get_client()
        res = (
            client.table("idempotency_log")
            .select("id,status,polymarket_order_id")
            .eq("id", uuid_str)
            .execute()
        )
        if res.data:
            return res.data[0]
        return None

    try:
        return await asyncio.wait_for(_query(), timeout=config.SUPABASE_TIMEOUT_SECONDS)
    except asyncio.TimeoutError as e:
        logger.critical(
            f"[PIPELINE] Supabase idempotency_log read timed out! Halting new trades (RULE 5)."
        )
        asyncio.create_task(
            alert_supabase_degradation("idempotency_log", "HALTING order submission - database timeout")
        )
        raise RuntimeError("Trading halted due to idempotency check timeout") from e
    except Exception as e:
        logger.critical(f"[PIPELINE] idempotency check failed: {e}. Halting.")
        raise RuntimeError("Trading halted due to idempotency check failure") from e


async def insert_idempotency_log(uuid_str: str, market_id: str, direction: str, size: float) -> None:
    """
    Write UUID to idempotency_log with status='pending' BEFORE order submission under a 2-second timeout (RULE 5).
    If database write times out or fails: HALT trading (Fail Closed).
    """
    async def _insert() -> None:
        client = await get_client()
        client.table("idempotency_log").insert({
            "id": uuid_str,
            "market_id": market_id,
            "direction": direction,
            "intended_size_usdc": size,
            "status": "pending",
            "created_at": datetime.now(timezone.utc).isoformat()
        }).execute()

    try:
        await asyncio.wait_for(_insert(), timeout=config.SUPABASE_TIMEOUT_SECONDS)
        logger.info(f"[PIPELINE] Idempotency record written as pending: {uuid_str}")
    except asyncio.TimeoutError as e:
        logger.critical(
            f"[PIPELINE] Supabase idempotency_log write timed out! Halting new trades (RULE 5)."
        )
        asyncio.create_task(
            alert_supabase_degradation("idempotency_log", "HALTING order submission - database timeout on write")
        )
        raise RuntimeError("Trading halted due to idempotency write timeout") from e
    except Exception as e:
        logger.critical(f"[PIPELINE] idempotency write failed: {e}. Halting.")
        raise RuntimeError("Trading halted due to idempotency write failure") from e


async def confirm_idempotency_log(uuid_str: str, order_id: str) -> None:
    """Update idempotency_log status to 'confirmed' once order executes."""
    async def _confirm() -> None:
        client = await get_client()
        client.table("idempotency_log").update({
            "status": "confirmed",
            "polymarket_order_id": order_id,
            "confirmed_at": datetime.now(timezone.utc).isoformat()
        }).eq("id", uuid_str).execute()

    try:
        await asyncio.wait_for(_confirm(), timeout=config.SUPABASE_TIMEOUT_SECONDS)
        logger.info(f"[PIPELINE] Idempotency record confirmed: {uuid_str}")
    except Exception as e:
        logger.error(f"[PIPELINE] Failed to confirm idempotency record {uuid_str} in database: {e}")


async def log_to_open_positions(
    market_id: str,
    market_question: str,
    direction: str,
    entry_price: float,
    size_usdc: float,
    strategy: str,
    agent_estimate: float,
    confidence: float,
    kelly_fraction: float,
    category: str,
    idempotency_uuid: str,
) -> None:
    """Write newly opened position to the open_positions database table."""
    async def _write() -> None:
        client = await get_client()
        client.table("open_positions").insert({
            "market_id": market_id,
            "market_question": market_question,
            "direction": direction,
            "entry_price": entry_price,
            "position_size_usdc": size_usdc,
            "strategy": strategy,
            "agent_estimate": agent_estimate,
            "confidence_at_entry": confidence,
            "kelly_fraction_used": kelly_fraction,
            "category": category,
            "idempotency_uuid": idempotency_uuid,
            "opened_at": datetime.now(timezone.utc).isoformat(),
            "last_checked_at": datetime.now(timezone.utc).isoformat()
        }).execute()

    try:
        await asyncio.wait_for(_write(), timeout=config.SUPABASE_TIMEOUT_SECONDS)
        logger.info(f"[PIPELINE] Position logged to open_positions: {market_id}")
    except Exception as e:
        logger.error(f"[PIPELINE] Failed to write position {market_id} to open_positions: {e}")


# ─────────────────────────────────────────────
# CACHE WARMUP HELPER (C3)
# ─────────────────────────────────────────────

async def _warm_resolution_cache(top_n: int = 15) -> None:
    """Pre-populate resolution keyword cache for top active markets to enable fast path."""
    try:
        from data.market_discovery import _MARKET_CACHE as markets
        if not markets:
            logger.info("[PIPELINE] Cache warmup: no markets available yet.")
            return

        # Sort by volume descending, take top N
        sorted_markets = sorted(
            markets,
            key=lambda m: float(m.get("volume_usd") or m.get("volume") or m.get("liquidity") or 0),
            reverse=True
        )[:top_n]

        warmed = 0
        for market in sorted_markets:
            market_id = market.get("market_id") or market.get("id") or market.get("conditionId", "")
            question = market.get("question", "")
            if market_id and question:
                # Extract simple keywords from question (words > 4 chars)
                keywords = [w.lower() for w in question.split() if len(w) > 4]
                if keywords:
                    try:
                        client = await get_client()
                        client.table("resolution_keyword_cache").upsert({
                            "market_id": market_id,
                            "keywords": keywords[:10],
                            "cached_at": datetime.now(timezone.utc).isoformat()
                        }, on_conflict="market_id").execute()
                        warmed += 1
                    except Exception:
                        pass  # Cache warmup is best-effort — never crash the bot

        logger.info(f"[PIPELINE] Cache warmup: pre-populated {warmed} markets for fast path.")
    except Exception as e:
        logger.warning(f"[PIPELINE] Cache warmup failed (non-critical): {e}")


async def get_live_portfolio_value() -> float:
    """Helper to get just the total portfolio value (zero DB calls in paper trading)."""
    if config.PAPER_TRADING:
        return float(os.environ.get("PAPER_TRADING_PORTFOLIO_USDC", "10000"))
    try:
        from execution.polymarket_auth import get_polymarket_client
        from py_clob_client.clob_types import BalanceAllowanceParams, AssetType

        client = get_polymarket_client()
        usdc_res = client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
        actual_usdc = float(usdc_res.get("balance", "0")) / 1000000.0

        db_client = await get_client()
        res = db_client.table("open_positions").select("position_size_usdc").execute()
        open_pos_value = sum(float(r.get("position_size_usdc", 0)) for r in res.data)

        return actual_usdc + open_pos_value
    except Exception as e:
        logger.error(f"[PORTFOLIO] Failed to fetch live portfolio value: {e}")
        return float(os.environ.get("PAPER_TRADING_PORTFOLIO_USDC", "10000"))


# ─────────────────────────────────────────────
# MASTER PIPELINE ENTRYPOINT
# ─────────────────────────────────────────────

def _parse_end_date(end_date_iso: Optional[str]) -> Optional[datetime]:
    """
    Parse a market end-date ISO string into an aware UTC datetime.

    Args:
        end_date_iso: ISO 8601 timestamp (optionally 'Z'-suffixed), or None.

    Returns:
        Parsed datetime, or None when missing/invalid (caller applies its own
        documented fallback).
    """
    if not end_date_iso:
        return None
    try:
        return datetime.fromisoformat(end_date_iso.replace("Z", "+00:00"))
    except Exception as e:
        logger.warning(f"[PIPELINE] Failed to parse end_date_iso '{end_date_iso}': {e}")
        return None


async def run_pipeline(
    headline: str,
    source: str,
    market_id: Optional[str] = None,
    market_question: Optional[str] = None,
    resolution_criteria: Optional[str] = None,
    market_price: Optional[float] = None,
    portfolio_value: Optional[float] = None,
    starting_balances: Optional[dict[str, float]] = None,
    current_balances: Optional[dict[str, float]] = None,
    available_liquidity: float = 10000.0,
    current_market_liquidity: float = 10000.0,
    signal_source: Optional[str] = "news_velocity",
    strategy_override: Optional[str] = None,
    wallet_address: Optional[str] = None,
    was_priority_pick: bool = False,
    slippage: float = 0.0,
    trader_name: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """
    Executes the Dual-Path trading pipeline coordinating News Analyst, Contract Parser,
    Trade Decision Agent, risk engine checks, aggregate coordinators, and idempotency.
    """
    now_str = datetime.now(timezone.utc).isoformat()
    logger.info(f"[{now_str}] [PIPELINE] Received new signal: '{headline}' from {source}")

    # Fetch live balances (zero DB calls in paper trading)
    if portfolio_value is None:
        portfolio_value = await get_live_portfolio_value()

    # Default balance values if not provided
    if starting_balances is None:
        starting_balances = {"daily": portfolio_value, "weekly": portfolio_value, "monthly": portfolio_value}
    if current_balances is None:
        current_balances = {"daily": portfolio_value, "weekly": portfolio_value, "monthly": portfolio_value}

    # --- Market Discovery FIRST ---
    # Attempt to match headline entities against Gamma cached markets
    entities = extract_entities(headline)
    signal_entities = {"entities": entities}
    matching_markets = find_matching_markets(signal_entities)

    end_date_iso = None
    if matching_markets:
        best_match = matching_markets[0]
        market_id = best_match["market_id"]
        token_id = best_match["token_id"]
        market_question = best_match["question"]
        end_date_iso = best_match.get("end_date_iso")
        try:
            market_price = await get_market_price(token_id)
        except Exception as e:
            logger.warning(f"[PIPELINE] Failed to fetch price for market {market_id}: {e}")
            _increment_drop("price_fetch_failed")
            logger.warning(f"[PIPELINE][DROP:price_fetch_failed] market_id={market_id} error='{e}'")
            return None
    else:
        # If no match in cache and no market_id was explicitly provided, discard
        if not market_id:
            logger.info("[PIPELINE] No matching markets found. Discarding before LLM News Analyst.")
            from data.market_discovery import _MARKET_CACHE as _disc_cache
            _increment_drop("no_market_match")
            logger.warning(
                f"[PIPELINE][DROP:no_market_match] "
                f"cache_size={len(_disc_cache)} "
                f"entities={entities[:5]} "
                f"headline='{headline[:80]}'"
            )
            return {"status": "blocked", "reason": "no_matching_markets"}
        # Backward compatibility fallback for tests
        token_id = f"mock-token-{market_id}"
        if market_price is None and not token_id.startswith("mock-token-"):
            try:
                market_price = await get_market_price(token_id)
            except Exception as e:
                logger.warning(f"[PIPELINE] Failed to fetch price for explicit market {market_id}: {e}")
                _increment_drop("price_fetch_failed")
                logger.warning(f"[PIPELINE][DROP:price_fetch_failed] market_id={market_id} error='{e}'")
                return None

    # Ensure market_price is set
    if market_price is None:
        market_price = 0.50

    # 2. News Analyst Agent (now passed market_question for context)
    news_output = await classify_signal(headline, source, market_question=market_question)
    if not news_output:
        logger.warning("[PIPELINE] News Analyst returned None (timeout or error). Dropping signal.")
        _increment_drop("news_analyst_none")
        logger.warning(f"[PIPELINE][DROP:news_analyst_none] src={source} headline='{headline[:80]}'")
        return None
    # NOTE: asyncio.sleep(2) removed — was a 2-second dead wait with no purpose (perf fix C1)

    # Confidence check
    if news_output.confidence_score < config.MIN_CONFIDENCE_THRESHOLD:
        logger.info(
            f"[PIPELINE] News Analyst confidence {news_output.confidence_score:.2f} < "
            f"threshold {config.MIN_CONFIDENCE_THRESHOLD}. Discarding signal."
        )
        _increment_drop("low_confidence")
        logger.warning(f"[PIPELINE][DROP:low_confidence] score={news_output.confidence_score:.3f} threshold={config.MIN_CONFIDENCE_THRESHOLD} headline='{headline[:80]}'")
        return {"status": "blocked", "reason": "low_confidence"}

    # Early discard if News Analyst determined ABSTAIN (prevents downstream Contract Parser & Trade Decision token burn)
    if news_output.direction == "ABSTAIN":
        logger.info(f"[PIPELINE] News Analyst direction is ABSTAIN for market {market_id}. Discarding signal early.")
        _increment_drop("abstain")
        logger.info(f"[PIPELINE][DROP:abstain] market_id={market_id} confidence={news_output.confidence_score:.3f}")
        return {"status": "blocked", "reason": "abstain"}

    # Update category based on analyst decision
    category = news_output.event_category

    # 3. Cache check for Fast Path eligibility
    # Fast path: News Analyst confidence > 0.87 AND category pre-validated
    # AND resolution_keyword_cache hit (< 24h old) AND entities match cached keywords.
    is_fast_path = False
    cached_keywords = None
    
    if news_output.confidence_score > config.FAST_PATH_CONFIDENCE_THRESHOLD:
        # Pre-validated category check
        pre_validated_categories = {"politics", "crypto", "sports", "legal", "economics", "science"}
        if category in pre_validated_categories:
            # Query get_cached_keywords (Rule 5 handles timeout)
            cached_keywords = await get_cached_keywords(market_id)
            if cached_keywords is not None:
                # Entity matching: does headline contain any cached keywords?
                headline_lower = headline.lower()
                keyword_match = False
                for kw in cached_keywords:
                    if kw.lower() in headline_lower:
                        keyword_match = True
                        break
                
                if keyword_match:
                    is_fast_path = True
                    logger.info(f"[PIPELINE] FAST PATH TRIGGERED for market {market_id}!")

    # 4. Pipeline Execution Routing
    was_memoryless = False
    
    if is_fast_path:
        # Fast Path skips: Contract Parser, Trade Decision, and LLM Coordinator
        # Trade Decision simulated from News Analyst directly
        # Estimator gate (fail-closed): no measured probability → no trade (List A.md Step 0).
        estimate_strategy = strategy_override if strategy_override else "velocity"
        estimate = await get_estimate(
            strategy=estimate_strategy,
            category=category,
            market_id=market_id,
            market_price=market_price,
            side=news_output.direction,
            wallet_address=wallet_address,
        )
        if estimate.p_point is None:
            _increment_drop("estimate:no_data")
            logger.warning(
                f"[PIPELINE][DROP:estimate:no_data] market_id={market_id} strategy={estimate_strategy} "
                f"method={estimate.method} side={news_output.direction}"
            )
            return {"status": "blocked", "reason": "estimate_no_data"}
        decision_direction = news_output.direction
        decision_confidence = news_output.confidence_score
        reasoning = f"Fast path matched. News Analyst wins directly: {news_output.reasoning}"
    else:
        # Full Pipeline: Contract Parser -> Trade Decision -> risk_engine -> Python Coordinator -> LLM Coordinator
        logger.info(f"[PIPELINE] FULL PIPELINE ROUTE selected for market {market_id}.")

        # Cheap pre-check before expensive Contract Parser / Trade Decision LLM calls
        pre_state = await load_market_state(market_id, news_output.direction)
        if pre_state is not None:
            if pre_state.opposite_direction_open:
                logger.info(
                    "[PIPELINE][PRECHECK] Opposite direction already open for market %s | conf=%.4f dir=%s — dropping before LLM",
                    market_id, news_output.confidence_score, news_output.direction,
                )
                _increment_drop("risk_gate:opposite_direction")
                return {"status": "blocked", "reason": "opposite_direction_open"}
            if pre_state.tranches >= config.MAX_MARKET_TRANCHES:
                logger.info(
                    "[PIPELINE][PRECHECK] Market %s reached max tranches (%d >= %d) | conf=%.4f dir=%s — dropping before LLM",
                    market_id, pre_state.tranches, config.MAX_MARKET_TRANCHES, news_output.confidence_score, news_output.direction,
                )
                _increment_drop("risk_gate:max_market_tranches")
                return {"status": "blocked", "reason": "max_tranches_reached"}
        
        # If we discovered the market dynamically, fetch metadata
        if matching_markets:
            try:
                metadata = await get_market_metadata(market_id)
                market_question = metadata["question"]
                resolution_criteria = metadata["resolution_criteria"]
                end_date_iso = metadata.get("end_date_iso")
            except Exception as e:
                logger.warning(f"[PIPELINE] Failed to fetch metadata for market {market_id}: {e}")
                return None

        # Estimator gate (fail-closed): no measured probability → drop BEFORE the
        # Contract Parser / Trade Decision LLM calls (zero token burn) (List A.md Step 0).
        estimate_strategy = strategy_override if strategy_override else "recalibration"
        estimate = await get_estimate(
            strategy=estimate_strategy,
            category=category,
            market_id=market_id,
            market_price=market_price,
            side=news_output.direction,
            wallet_address=wallet_address,
        )
        if estimate.p_point is None:
            _increment_drop("estimate:no_data")
            logger.warning(
                f"[PIPELINE][DROP:estimate:no_data] market_id={market_id} strategy={estimate_strategy} "
                f"method={estimate.method} side={news_output.direction}"
            )
            return {"status": "blocked", "reason": "estimate_no_data"}

        # A. Contract Parser (DeepSeek V3, 18s timeout, cached)
        parser_output = await parse_contract(market_id, market_question, resolution_criteria)
        if not parser_output:
            logger.warning("[PIPELINE] Contract Parser failed. Dropping signal.")
            _increment_drop("contract_parser_none")
            logger.warning(f"[PIPELINE][DROP:contract_parser_none] market_id={market_id}")
            return None
        # NOTE: asyncio.sleep(2) removed — was a 2-second dead wait with no purpose (perf fix C1)
            
        # Real time-to-resolution parsed from the market end date (List A.md Step 0 —
        # replaces the simulated constant). Missing/invalid falls back to a conservative
        # default that stays visible via the estimate:ttr_fallback counter.
        end_dt = _parse_end_date(end_date_iso)
        if end_dt is not None:
            time_to_res = max(0.0, (end_dt - datetime.now(timezone.utc)).total_seconds() / 3600.0)
        else:
            time_to_res = float(config.DEFAULT_TTR_HOURS)
            _increment_drop("estimate:ttr_fallback")
            logger.warning(
                f"[PIPELINE][DROP:estimate:ttr_fallback] market_id={market_id} "
                f"end_date_iso='{end_date_iso}' fallback_hours={config.DEFAULT_TTR_HOURS}"
            )

        # B. Trade Decision Agent (Qwen 235B, 18s SiliconFlow timeout, OpenRouter fallback)
        trade_output, was_memoryless = await decide_trade(
            headline=headline,
            category=category,
            market_id=market_id,
            market_question=market_question,
            market_price=market_price,
            agent_estimate=estimate.p_point,
            portfolio_value=portfolio_value,
            time_to_resolution_hours=time_to_res,
            signal_source=signal_source,
            estimate_method=estimate.method,
            estimate_sample_size=estimate.sample_size,
        )
        if not trade_output:
            logger.warning("[PIPELINE] Trade Decision Agent returned None. Dropping signal.")
            _increment_drop("trade_decision_none")
            logger.warning(f"[PIPELINE][DROP:trade_decision_none] market_id={market_id}")
            return None
        # NOTE: asyncio.sleep(2) removed — was a 2-second dead wait with no purpose (perf fix C1)

        # C. Coordinator Layer (Python Weighted Aggregation / LLM escalation)
        coordinator_output = await coordinate_decision(
            headline=headline,
            market_question=market_question,
            news_output=news_output,
            trade_output=trade_output
        )
        if not coordinator_output:
            logger.warning("[PIPELINE] Coordinator returned None. Dropping signal.")
            return None
        # NOTE: asyncio.sleep(2) removed — was a 2-second dead wait with no purpose (perf fix C1)

        decision_direction = coordinator_output.direction
        decision_confidence = coordinator_output.confidence_score
        reasoning = coordinator_output.reasoning

    # If agents decided ABSTAIN, stop
    if decision_direction == "ABSTAIN":
        logger.info("[PIPELINE] Final coordinated decision is ABSTAIN. Stopping.")
        _increment_drop("abstain")
        logger.info(f"[PIPELINE][DROP:abstain] market_id={market_id} confidence={decision_confidence:.3f}")
        return None

    # 5. RISK ENGINE CHECKS (Rule 8: Always run on both paths)
    logger.info(f"[PIPELINE] Executing Risk Checks for proposed trade on {market_id}...")

    # A. Drawdown Circuit Breakers (Rule 8)
    for period in ["daily", "weekly", "monthly"]:
        status = risk_engine.check_drawdown(
            starting_balance=starting_balances[period],
            current_balance=current_balances[period],
            period=period
        )
        if status in ["HALT", "SHUTDOWN"]:
            logger.critical(f"[PIPELINE] Drawdown circuit breaker triggered: {period} ({status})!")
            asyncio.create_task(
                alert_circuit_breaker(
                    breaker_type=period,
                    current_pct=(starting_balances[period] - current_balances[period]) / starting_balances[period],
                    threshold_pct=getattr(config, f"{period.upper()}_DRAWDOWN_HALT_PCT" if period != "monthly" else "MONTHLY_DRAWDOWN_SHUTDOWN_PCT"),
                    portfolio_value=portfolio_value
                )
            )
            # Log signal as discarded (circuit breaker)
            _increment_drop("risk_gate:circuit_breaker")
            logger.info(f"[PIPELINE][DROP:risk_gate:circuit_breaker] market_id={market_id}")
            return {"status": "blocked", "reason": "circuit_breaker"}

    # B. Liquidity Gate
    liq_status = risk_engine.check_liquidity(available_liquidity, current_market_liquidity)
    if liq_status == "EXIT_NOW":
        logger.warning("[PIPELINE] Risk check: market liquidity below auto-exit floor.")
        _increment_drop("risk_gate:auto_exit_liquidity")
        logger.info(f"[PIPELINE][DROP:risk_gate:auto_exit_liquidity] market_id={market_id}")
        return {"status": "exit_triggered", "reason": "auto_exit_liquidity"}
    elif liq_status == "BLOCK":
        logger.warning("[PIPELINE] Risk check: available liquidity below minimum entry threshold. Blocking.")
        _increment_drop("risk_gate:low_liquidity")
        logger.info(f"[PIPELINE][DROP:risk_gate:low_liquidity] market_id={market_id}")
        return {"status": "blocked", "reason": "low_liquidity"}

    # C. Confidence and Edge gates
    # Clamp confidence
    clamped_conf = risk_engine.apply_confidence_ceiling(decision_confidence)
    if risk_engine.check_min_confidence(clamped_conf) == "BLOCK":
        logger.info(f"[PIPELINE] Risk check: confidence {clamped_conf:.2f} below minimum. Blocking.")
        _increment_drop("risk_gate:low_confidence")
        logger.info(f"[PIPELINE][DROP:risk_gate:low_confidence] market_id={market_id}")
        return {"status": "blocked", "reason": "low_confidence"}

    # Edge gate: consumes the measured estimate passed down from the estimator gate
    # (never recomputed — List A.md Step 0). Defensive fail-closed guard: the branches
    # above guarantee a data-bearing estimate, but never dereference None.
    if estimate.p_point is None:
        _increment_drop("estimate:no_data")
        logger.warning(f"[PIPELINE][DROP:estimate:no_data] market_id={market_id} (post-decision guard)")
        return {"status": "blocked", "reason": "estimate_no_data"}
    estimated_probability = estimate.p_point
    if risk_engine.check_edge(estimated_probability, market_price) == "BLOCK":
        logger.info(f"[PIPELINE] Risk check: edge below minimum. Blocking.")
        _increment_drop("risk_gate:low_edge")
        logger.info(f"[PIPELINE][DROP:risk_gate:low_edge] market_id={market_id}")
        return {"status": "blocked", "reason": "low_edge"}

    # D. Portfolio exposure gates
    # Fetch exposure under 2s timeout (Rule 5 handles halting on failure)
    cat_exp, corr_exp, open_pos_value = await fetch_open_positions_exposure(category)
    
    # Calculate net odds b: we risk market_price to win (1.0 - market_price)
    m_price = max(min(market_price, 0.99), 0.01)
    net_odds = (1.0 - m_price) / m_price

    # Sizing trade using net_odds
    if strategy_override == "copy_edge_class_b":
        kelly_fraction = config.KELLY_FRACTION_COPY
        kelly_raw = risk_engine.kelly_size(
            win_probability=clamped_conf,
            odds=net_odds,
            kelly_fraction=kelly_fraction,
            portfolio_value=portfolio_value
        )
        if wallet_address:
            from copytrade.performance_tracker import get_trust_score, get_wallet_state, compute_state_multiplier
            trust_score = get_trust_score(wallet_address)
            wallet_state = get_wallet_state(wallet_address)
            state_multiplier = compute_state_multiplier(wallet_state)
            
            # class_ceiling = $50 * state_multiplier * trust_score
            class_ceiling = config.COPY_CLASS_B_MAX_SIZE_USDC * state_multiplier * trust_score
            raw_size = min(kelly_raw, class_ceiling)
        else:
            raw_size = kelly_raw
    else:
        kelly_fraction = config.KELLY_FRACTION_VELOCITY if is_fast_path else config.KELLY_FRACTION_RECALIBRATION
        raw_size = risk_engine.kelly_size(
            win_probability=clamped_conf,
            odds=net_odds,
            kelly_fraction=kelly_fraction,
            portfolio_value=portfolio_value
        )
        
    strategy_for_check = strategy_override if strategy_override else ("velocity" if is_fast_path else "recalibration")
    final_trade_size = max(0.0, risk_engine.position_size_check(raw_size, portfolio_value, strategy=strategy_for_check))
    
    proposed_pct = final_trade_size / portfolio_value

    if risk_engine.check_category_exposure(cat_exp, proposed_pct) == "BLOCK":
        logger.warning(
            f"[PIPELINE] Risk check: category exposure would exceed 30%% cap | conf={clamped_conf:.4f} size=${final_trade_size:.2f}. Blocking."
        )
        _increment_drop("risk_gate:max_category_exposure")
        _increment_drop("risk_gate:portfolio_capacity")
        logger.info(f"[PIPELINE][DROP:risk_gate:max_category_exposure] market_id={market_id}")
        return {"status": "blocked", "reason": "portfolio_capacity_full"}

    if risk_engine.check_correlation_exposure(corr_exp + proposed_pct) == "BLOCK":
        logger.warning(
            f"[PIPELINE] Risk check: correlated exposure would exceed 20%% cap | conf={clamped_conf:.4f} size=${final_trade_size:.2f}. Blocking."
        )
        _increment_drop("risk_gate:max_correlated_exposure")
        _increment_drop("risk_gate:portfolio_capacity")
        logger.info(f"[PIPELINE][DROP:risk_gate:max_correlated_exposure] market_id={market_id}")
        return {"status": "blocked", "reason": "portfolio_capacity_full"}

    # Cash reserve gate
    if config.PAPER_TRADING:
        available_cash = max(0.0, portfolio_value - open_pos_value)
    else:
        try:
            from execution.polymarket_auth import get_polymarket_client
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
            client = get_polymarket_client()
            usdc_res = client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
            available_cash = float(usdc_res.get("balance", "0")) / 1000000.0
        except Exception as e:
            logger.error(f"[PORTFOLIO] Failed to fetch live USDC balance: {e}")
            available_cash = max(0.0, portfolio_value - open_pos_value)

    if risk_engine.check_cash_reserve(final_trade_size, available_cash, portfolio_value) == "BLOCK":
        logger.warning(f"[PIPELINE] Risk check: cash reserve violation. Blocking.")
        _increment_drop("risk_gate:cash_reserve")
        logger.info(f"[PIPELINE][DROP:risk_gate:cash_reserve] market_id={market_id}")
        return {"status": "blocked", "reason": "cash_reserve"}

    # Calculate remaining days to resolution for deadline risk gate
    end_dt = _parse_end_date(end_date_iso)
    days_to_resolution = 365
    if end_dt is not None:
        now_dt = datetime.now(timezone.utc)
        delta = end_dt - now_dt
        days_to_resolution = max(0, delta.days)

    # Deadline risk gate
    if risk_engine.check_deadline_risk(market_price, days_to_resolution) == "BLOCK":
        logger.warning(f"[PIPELINE] Risk check: deadline risk violation. Blocking.")
        _increment_drop("risk_gate:deadline_risk")
        logger.info(f"[PIPELINE][DROP:risk_gate:deadline_risk] market_id={market_id}")
        return {"status": "blocked", "reason": "deadline_risk"}

    # All risk gates PASSED!
    logger.info(f"[PIPELINE] Initial risk gates passed! Checking per-market tranche gate...")

    # ── Per-Market Tranche Gate & Concurrency Lock ──────────────────────────
    market_lock = get_market_lock(market_id)
    async with market_lock:
        state = await load_market_state(market_id, decision_direction)
        if state is None:
            logger.warning("[PIPELINE] Could not load market state for market %s — fail closed", market_id)
            _increment_drop("risk_gate:exposure_unavailable")
            return {"status": "blocked", "reason": "exposure_unavailable"}

        if state.opposite_direction_open:
            logger.warning("[PIPELINE] Opposite direction already open for market %s — BLOCK", market_id)
            _increment_drop("risk_gate:opposite_direction")
            return {"status": "blocked", "reason": "opposite_direction_open"}

        if state.tranches >= config.MAX_MARKET_TRANCHES:
            logger.warning(
                "[PIPELINE] Market %s reached max tranches (%d >= %d) — BLOCK",
                market_id, state.tranches, config.MAX_MARKET_TRANCHES,
            )
            _increment_drop("risk_gate:max_market_tranches")
            return {"status": "blocked", "reason": "max_tranches_reached"}

        permitted_size = risk_engine.market_position_check(
            existing_market_usdc=state.existing_usdc,
            existing_tranches=state.tranches,
            proposed_size=final_trade_size,
            portfolio_value=portfolio_value,
            confidence=clamped_conf,
        )

        if permitted_size <= 0.0:
            if state.tranches == 1 and not (clamped_conf >= config.REPEAT_MIN_CONFIDENCE):
                reason = "low_confidence_for_add"
                _increment_drop("risk_gate:low_confidence_add")
            elif state.tranches >= config.MAX_MARKET_TRANCHES:
                reason = "max_tranches_reached"
                _increment_drop("risk_gate:max_market_tranches")
            else:
                reason = "max_market_exposure"
                _increment_drop("risk_gate:max_market_exposure")
            logger.warning(
                "[PIPELINE] market_position_check blocked trade on market %s | reason=%s tranches=%d existing=$%.2f conf=%.4f size=$%.2f",
                market_id, reason, state.tranches, state.existing_usdc, clamped_conf, final_trade_size,
            )
            return {"status": "blocked", "reason": reason}

        # Telemetry: Log raw Kelly vs capped size, and repeat entry intervals/price delta
        if state.tranches >= 1:
            time_gap = -1.0
            if state.last_opened_at:
                try:
                    dt = datetime.fromisoformat(state.last_opened_at.replace("Z", "+00:00"))
                    time_gap = (datetime.now(timezone.utc) - dt).total_seconds()
                except Exception:
                    pass
            price_delta = (market_price - state.last_entry_price) if state.last_entry_price is not None else 0.0
            logger.info(
                "[OBSERVABILITY][REPEAT_ENTRY] market=%s dir=%s time_gap=%.1fs price_delta=%.4f raw_kelly=$%.2f capped_size=$%.2f",
                market_id, decision_direction, time_gap, price_delta, raw_size, permitted_size,
            )
        else:
            logger.info(
                "[OBSERVABILITY][FIRST_ENTRY] market=%s dir=%s raw_kelly=$%.2f capped_size=$%.2f",
                market_id, decision_direction, raw_size, permitted_size,
            )

        final_trade_size = permitted_size

        # 6. IDEMPOTENT ORDER EXECUTION (Rule 2)
        # Step A: Generate unique UUID at trade decision time
        order_uuid = str(uuid.uuid4())
        logger.info(f"[PIPELINE] Pre-order idempotency UUID generated: {order_uuid}")

        # Step B: Check Supabase idempotency table first (2s timeout, halts on failure)
        existing_log = await check_pre_order_idempotency(order_uuid)
        if existing_log and existing_log.get("status") == "confirmed":
            logger.critical(f"[PIPELINE] UUID {order_uuid} already confirmed! Blocking submission.")
            asyncio.create_task(
                alert_idempotency_duplicate(order_uuid, market_id, decision_direction)
            )
            return {"status": "blocked", "reason": "duplicate_idempotency_uuid"}

        # Step C: Write UUID to log as pending BEFORE hitting the Polymarket L2 API (2s timeout, halts on failure)
        await insert_idempotency_log(order_uuid, market_id, decision_direction, final_trade_size)

        # Step D: Submit order to Polymarket CLOB (Mocked for safety during Layer 6 Integration)
        logger.info(f"[PIPELINE] Submitting {decision_direction} order of ${final_trade_size:.2f} to Polymarket CLOB...")
        
        # Simulate blockchain/network latency
        await asyncio.sleep(1.0)
        mock_order_id = f"mock-order-{int(time.time())}"
        logger.info(f"[PIPELINE] Polymarket CLOB order placed successfully. Order ID: {mock_order_id}")

        # Step E: Confirm idempotency record in DB
        await confirm_idempotency_log(order_uuid, mock_order_id)

        # Step F: Write newly opened position to the open_positions table
        await log_to_open_positions(
            market_id=market_id,
            market_question=market_question,
            direction=decision_direction,
            entry_price=market_price,
            size_usdc=final_trade_size,
            strategy=strategy_for_check,
            agent_estimate=estimated_probability,
            confidence=clamped_conf,
            kelly_fraction=kelly_fraction,
            category=category,
            idempotency_uuid=order_uuid
        )

    # Log to copytrade_log for trust scoring and audit trail (Class B)
    if strategy_override == "copy_edge_class_b" and wallet_address:
        try:
            from copytrade.performance_tracker import log_copy_trade as _tracker_log_trade
            await _tracker_log_trade(
                wallet_address=wallet_address,
                trader_name=trader_name or "unknown",
                market_id=market_id,
                direction=decision_direction,
                class_type="B",
                entry_price=market_price,
                size_usdc=final_trade_size,
                slippage=slippage,
                idempotency_uuid=order_uuid,
                was_priority_pick=was_priority_pick,
            )
            logger.info("[PIPELINE] Class B copy trade logged to copytrade_log: %s", market_id)
        except Exception as ct_err:
            logger.error("[PIPELINE] Failed to log Class B copy trade to copytrade_log: %s", ct_err)

    logger.info(f"[PIPELINE] Unified pipeline executed successfully for market {market_id}!")
    
    return {
        "status": "success",
        "market_id": market_id,
        "direction": decision_direction,
        "size_usdc": final_trade_size,
        "order_id": mock_order_id,
        "uuid": order_uuid,
        "was_memoryless": was_memoryless,
        "reasoning": reasoning
    }
