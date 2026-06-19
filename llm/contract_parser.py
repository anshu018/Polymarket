"""
Contract Parser Agent — Layer 5
Model: DeepSeek V3 via OpenRouter (config.MODEL_CONTRACT_PARSER)
Role: Parse Polymarket resolution criteria once per market discovery.
NOT in hot path. Runs once per market, result cached for 24 hours.
"""

import os
import json
import logging
import asyncio
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

import aiohttp
from pydantic import BaseModel, ValidationError, Field, field_validator

import config
from memory.supabase_client import get_client

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# OUTPUT SCHEMA
# ─────────────────────────────────────────────

class ContractParserOutput(BaseModel):
    """Validated structured output from DeepSeek V3 contract parsing."""

    resolution_source: str = Field(min_length=1)
    resolution_condition: str = Field(min_length=1)
    key_entities: list[str] = Field(min_length=1)
    resolution_keywords: list[str] = Field(min_length=3)
    ambiguity_score: float
    resolution_type: str = Field(min_length=1)

    @field_validator("ambiguity_score")
    @classmethod
    def clamp_ambiguity(cls, v: float) -> float:
        """Clamp ambiguity score to [0.0, 1.0]."""
        return max(0.0, min(1.0, v))

    @field_validator("key_entities")
    @classmethod
    def at_least_one_entity(cls, v: list[str]) -> list[str]:
        """Enforce minimum 1 key entity."""
        if len(v) < 1:
            raise ValueError("key_entities must have at least 1 item")
        return v

    @field_validator("resolution_keywords")
    @classmethod
    def at_least_three_keywords(cls, v: list[str]) -> list[str]:
        """Enforce minimum 3 resolution keywords."""
        if len(v) < 3:
            raise ValueError("resolution_keywords must have at least 3 items")
        return v


# ─────────────────────────────────────────────
# SYSTEM PROMPT — DO NOT MODIFY
# ─────────────────────────────────────────────

_SYSTEM_PROMPT = """You are a prediction market resolution criteria parser. Extract structured information from Polymarket resolution criteria text.
Respond ONLY in valid JSON. No preamble. No explanation. No markdown. JSON only.
Required schema:
{
  "resolution_source": "the authoritative source that resolves this market",
  "resolution_condition": "exact condition that triggers YES resolution",
  "key_entities": ["list", "of", "key", "named", "entities"],
  "resolution_keywords": ["specific", "keywords", "for", "matching"],
  "ambiguity_score": 0.0,
  "resolution_type": "binary|continuous|categorical"
}
Rules:
- resolution_keywords must be specific terms from the criteria, not generic words
- ambiguity_score: 0.0=perfectly clear, 1.0=completely ambiguous
- key_entities: named people, organizations, dates, prices, thresholds
- minimum 3 resolution_keywords required
- minimum 1 key_entity required"""


# ─────────────────────────────────────────────
# INTERNAL HELPERS
# ─────────────────────────────────────────────

async def _check_cache(market_id: str) -> Optional[ContractParserOutput]:
    """
    Check resolution_keyword_cache for a fresh entry (< 24 hours old).
    Returns ContractParserOutput if cache hit, None if missing or stale.
    2-second Supabase timeout with None fallback per RULE 5.
    """
    async def _read() -> Optional[ContractParserOutput]:
        client = await get_client()
        rows = (
            client.table("resolution_keyword_cache")
            .select(
                "market_id,market_question,resolution_keywords,"
                "resolution_conditions,resolution_type,ambiguity_score,cached_at"
            )
            .eq("market_id", market_id)
            .execute()
        )
        if not rows.data:
            return None

        row = rows.data[0]
        cached_at_str = row.get("cached_at")
        if not cached_at_str:
            return None

        # Parse timestamp — Supabase returns ISO 8601 strings
        if cached_at_str.endswith("Z"):
            cached_at_str = cached_at_str[:-1] + "+00:00"
        cached_at = datetime.fromisoformat(cached_at_str)
        if cached_at.tzinfo is None:
            cached_at = cached_at.replace(tzinfo=timezone.utc)

        age_hours = (datetime.now(timezone.utc) - cached_at).total_seconds() / 3600
        if age_hours >= config.RESOLUTION_CACHE_TTL_HOURS:
            logger.info(
                f"[CONTRACT_PARSER] Cache stale ({age_hours:.1f}h old): {market_id}"
            )
            return None

        # Reconstruct ContractParserOutput from cached data
        cond = row.get("resolution_conditions") or {}
        try:
            output = ContractParserOutput(
                resolution_source=cond.get("resolution_source", ""),
                resolution_condition=cond.get("resolution_condition", ""),
                key_entities=cond.get("key_entities", [""]),
                resolution_keywords=row.get("resolution_keywords") or [],
                ambiguity_score=float(row.get("ambiguity_score") or 0.0),
                resolution_type=row.get("resolution_type") or "",
            )
            logger.info(f"[CONTRACT_PARSER] Cache hit: {market_id}")
            return output
        except (ValidationError, Exception) as e:
            logger.warning(
                f"[CONTRACT_PARSER] Cache entry invalid for {market_id}, will re-parse: {e}"
            )
            return None

    try:
        return await asyncio.wait_for(_read(), timeout=config.SUPABASE_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        logger.warning(
            f"[CONTRACT_PARSER] Supabase cache read timed out for {market_id}, "
            "falling back to full parse (RULE 5)"
        )
        return None
    except Exception as e:
        logger.error(f"[CONTRACT_PARSER] Cache read error for {market_id}: {e}")
        return None


async def _write_cache(
    market_id: str,
    market_question: str,
    output: ContractParserOutput,
) -> None:
    """
    Upsert parsed result into resolution_keyword_cache.
    Failure is logged but never raised — caller always continues.
    """
    async def _write() -> None:
        client = await get_client()
        client.table("resolution_keyword_cache").upsert(
            {
                "market_id": market_id,
                "market_question": market_question,
                "resolution_keywords": output.resolution_keywords,
                "resolution_conditions": {
                    "resolution_source": output.resolution_source,
                    "resolution_condition": output.resolution_condition,
                    "resolution_type": output.resolution_type,
                    "key_entities": output.key_entities,
                },
                "resolution_type": output.resolution_type,
                "ambiguity_score": output.ambiguity_score,
                "cached_at": datetime.now(timezone.utc).isoformat(),
                "last_used_at": datetime.now(timezone.utc).isoformat(),
            },
            on_conflict="market_id",
        ).execute()

    try:
        await asyncio.wait_for(_write(), timeout=config.SUPABASE_TIMEOUT_SECONDS)
        logger.info(f"[CONTRACT_PARSER] Cache written: {market_id}")
    except asyncio.TimeoutError:
        logger.error(
            f"[CONTRACT_PARSER] Cache write timed out for {market_id} — continuing without cache"
        )
    except Exception as e:
        logger.error(f"[CONTRACT_PARSER] Cache write failed for {market_id}: {e}")


class LLMFailFastError(Exception):
    def __init__(self, status: int, provider: str):
        self.status = status
        self.provider = provider
        super().__init__(f"Auth/quota error {status} on {provider}")


async def _execute_parser_call(
    url: str,
    api_key: str,
    model: str,
    user_prompt: str,
    provider_name: str,
    is_openrouter: bool = False,
) -> Optional[ContractParserOutput]:
    """Execute raw API call to parse contract."""
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.1,
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if is_openrouter:
        headers["HTTP-Referer"] = "https://github.com/zeroalpha"
        headers["X-Title"] = "Zero Alpha Agent"

    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, headers=headers) as response:
            if response.status in config.FAIL_FAST_HTTP_CODES:
                logger.error(
                    f"[LLM] Auth/quota error {response.status} on {provider_name} — immediate failover"
                )
                raise LLMFailFastError(response.status, provider_name)
            if response.status != 200:
                text = await response.text()
                logger.error(
                    f"[CONTRACT_PARSER] {provider_name} returned {response.status}: {text} | model={model}"
                )
                return None

            data = await response.json()
            usage = data.get("usage", {})
            choice_content = (
                data.get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
            )

            logger.info(
                f"[CONTRACT_PARSER] {provider_name} call succeeded | model={model} tokens={usage}"
            )

            if not choice_content:
                logger.error(f"[CONTRACT_PARSER] {provider_name} returned empty message content")
                return None

            cleaned = choice_content.strip()
            for prefix in ("```json", "```"):
                if cleaned.startswith(prefix):
                    cleaned = cleaned[len(prefix):]
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3]
            cleaned = cleaned.strip()

            try:
                parsed_json = json.loads(cleaned)
                if isinstance(parsed_json, list) and parsed_json:
                    parsed_json = parsed_json[0]
            except json.JSONDecodeError as e:
                logger.error(
                    f"[CONTRACT_PARSER] JSON decode failed: {e} | raw: {choice_content[:300]}"
                )
                return None

            try:
                return ContractParserOutput(**parsed_json)
            except (ValidationError, TypeError) as e:
                logger.error(
                    f"[CONTRACT_PARSER] Pydantic validation failed: {e} | raw: {choice_content[:300]}"
                )
                return None


async def _call_deepseek(
    market_question: str,
    resolution_criteria: str,
) -> Optional[ContractParserOutput]:
    """
    Parse Polymarket resolution criteria using primary/fallback models (18-second total limit).
    Attempts:
      1. Primary: moonshotai/kimi-k2.6:free on OpenRouter (9s timeout)
      2. Fallback 1: deepseek-ai/deepseek-v4-flash on DeepSeek API (5s timeout)
      3. Fallback 2: deepseek-ai/deepseek-v4-flash on NVIDIA NIM (4s timeout)
    """
    user_prompt = (
        f"Market question: {market_question}\n"
        f"Resolution criteria: {resolution_criteria}\n"
        f"Parse this into structured JSON."
    )

    # 1. Primary: OpenRouter (moonshotai/kimi-k2.6:free)
    or_key = os.environ.get("OPENROUTER_API_KEY")
    if or_key and or_key != "placeholder":
        url = f"{config.PROVIDER_OPENROUTER}/chat/completions"
        model = getattr(config, "MODEL_CONTRACT_PARSER", "moonshotai/kimi-k2.6:free")
        try:
            logger.info(f"[CONTRACT_PARSER] Calling primary OpenRouter ({model})...")
            res = await asyncio.wait_for(
                _execute_parser_call(url, or_key, model, user_prompt, "OpenRouter", is_openrouter=True),
                timeout=9.0
            )
            if res is not None:
                return res
        except LLMFailFastError as e:
            logger.warning(f"[CONTRACT_PARSER] Primary OpenRouter auth/quota error: {e}. Proceeding immediately to fallback.")
        except asyncio.TimeoutError:
            logger.warning("[CONTRACT_PARSER] Primary OpenRouter call timed out (limit=9s).")
        except Exception as e:
            logger.error(f"[CONTRACT_PARSER] Primary OpenRouter call failed: {e}")

    # 2. Fallback 1: DeepSeek API (deepseek-ai/deepseek-v4-flash)
    ds_key = os.environ.get("DEEPSEEK_API_KEY")
    if ds_key and ds_key != "placeholder":
        url = f"{config.PROVIDER_DEEPSEEK}/chat/completions"
        model = getattr(config, "MODEL_CONTRACT_PARSER_FALLBACK", "deepseek-ai/deepseek-v4-flash")
        try:
            logger.info(f"[CONTRACT_PARSER] Calling Fallback 1 DeepSeek API ({model})...")
            res = await asyncio.wait_for(
                _execute_parser_call(url, ds_key, model, user_prompt, "DeepSeek API", is_openrouter=False),
                timeout=5.0
            )
            if res is not None:
                return res
        except LLMFailFastError as e:
            logger.warning(f"[CONTRACT_PARSER] Fallback 1 DeepSeek API auth/quota error: {e}. Proceeding immediately to fallback.")
        except asyncio.TimeoutError:
            logger.warning("[CONTRACT_PARSER] Fallback 1 DeepSeek API call timed out (limit=5s).")
        except Exception as e:
            logger.error(f"[CONTRACT_PARSER] Fallback 1 DeepSeek API call failed: {e}")
    else:
        logger.warning("[CONTRACT_PARSER] DeepSeek API key missing, Fallback 1 skipped.")

    # 3. Fallback 2: NVIDIA NIM (deepseek-ai/deepseek-v4-flash)
    nv_key = os.environ.get("NVIDIA_API_KEY")
    if nv_key and nv_key != "placeholder":
        url = f"{config.PROVIDER_NVIDIA}/chat/completions"
        model = getattr(config, "MODEL_CONTRACT_PARSER_FALLBACK", "deepseek-ai/deepseek-v4-flash")
        try:
            logger.info(f"[CONTRACT_PARSER] Calling Fallback 2 NVIDIA NIM ({model})...")
            res = await asyncio.wait_for(
                _execute_parser_call(url, nv_key, model, user_prompt, "NVIDIA NIM", is_openrouter=False),
                timeout=4.0
            )
            if res is not None:
                return res
        except LLMFailFastError as e:
            logger.warning(f"[CONTRACT_PARSER] Fallback 2 NVIDIA NIM auth/quota error: {e}.")
        except asyncio.TimeoutError:
            logger.warning("[CONTRACT_PARSER] Fallback 2 NVIDIA NIM call timed out (limit=4s).")
        except Exception as e:
            logger.error(f"[CONTRACT_PARSER] Fallback 2 NVIDIA NIM call failed: {e}")
    else:
        logger.warning("[CONTRACT_PARSER] NVIDIA API key missing, Fallback 2 skipped.")

    logger.error("[CONTRACT_PARSER] All parser attempts failed or timed out.")
    return None


# ─────────────────────────────────────────────
# PUBLIC API
# ─────────────────────────────────────────────

async def parse_contract(
    market_id: str,
    market_question: str,
    resolution_criteria: str,
) -> Optional[ContractParserOutput]:
    """
    Parse Polymarket resolution criteria into structured JSON via DeepSeek V3.

    Behavior:
      1. Check resolution_keyword_cache (2s Supabase timeout).
         Fresh hit (< 24h): return cached result, zero API calls.
      2. Stale or missing: call DeepSeek V3 via OpenRouter (18s timeout).
      3. On success: upsert into resolution_keyword_cache.
      4. On timeout or parse failure: log, return None.
      5. Never raises to caller.

    NOT in hot path. Runs once per market discovery.
    """
    # 1. Cache check
    cached = await _check_cache(market_id)
    if cached is not None:
        return cached

    logger.info(
        f"[CONTRACT_PARSER] Cache miss — calling DeepSeek V3 for: {market_id}"
    )

    # 2. API call
    output = await _call_deepseek(market_question, resolution_criteria)
    if output is None:
        return None

    # 3. Write cache (never raises)
    await _write_cache(market_id, market_question, output)

    return output


async def get_cached_keywords(market_id: str) -> Optional[list[str]]:
    """
    Return resolution_keywords for a market_id if the cache entry is fresh.

    Used by fast path to perform keyword matching without any API calls.
    Returns None if the entry is missing, stale, or Supabase times out.
    Never makes LLM API calls.
    2-second Supabase timeout with None fallback (RULE 5).
    """
    async def _read_keywords() -> Optional[list[str]]:
        client = await get_client()
        rows = (
            client.table("resolution_keyword_cache")
            .select("resolution_keywords,cached_at")
            .eq("market_id", market_id)
            .execute()
        )
        if not rows.data:
            return None

        row = rows.data[0]
        cached_at_str = row.get("cached_at")
        if not cached_at_str:
            return None

        if cached_at_str.endswith("Z"):
            cached_at_str = cached_at_str[:-1] + "+00:00"
        cached_at = datetime.fromisoformat(cached_at_str)
        if cached_at.tzinfo is None:
            cached_at = cached_at.replace(tzinfo=timezone.utc)

        age_hours = (datetime.now(timezone.utc) - cached_at).total_seconds() / 3600
        if age_hours >= config.RESOLUTION_CACHE_TTL_HOURS:
            logger.debug(
                f"[CONTRACT_PARSER] get_cached_keywords: stale entry for {market_id}"
            )
            return None

        keywords = row.get("resolution_keywords")
        if keywords and len(keywords) >= 3:
            return keywords
        return None

    try:
        return await asyncio.wait_for(
            _read_keywords(), timeout=config.SUPABASE_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        logger.warning(
            f"[CONTRACT_PARSER] get_cached_keywords timed out for {market_id} — "
            "returning None (RULE 5 fallback)"
        )
        return None
    except Exception as e:
        logger.error(f"[CONTRACT_PARSER] get_cached_keywords error for {market_id}: {e}")
        return None
