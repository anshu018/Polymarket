"""
Trade Decision Agent — Layer 6
Model: Qwen3-235B-A22B
Primary Provider: SiliconFlow (config.PROVIDER_PRIMARY)
Fallback Provider: OpenRouter (config.PROVIDER_FALLBACK)
Role: Final YES/NO/ABSTAIN decision on entering a trade.
"""

import json
import logging
import asyncio
import time
from datetime import datetime, timezone
from typing import Optional, Any
from pydantic import BaseModel, ValidationError, Field, field_validator
from typing_extensions import Literal

import config
from memory.supabase_client import get_client
from monitoring.telegram_alerts import alert_siliconflow_failover, alert_supabase_degradation

logger = logging.getLogger(__name__)

class LLMFailFastError(Exception):
    def __init__(self, status: int, provider: str):
        self.status = status
        self.provider = provider
        super().__init__(f"Auth/quota error {status} on {provider}")

# ─────────────────────────────────────────────
# OUTPUT SCHEMA
# ─────────────────────────────────────────────

class TradeDecisionOutput(BaseModel):
    """Validated structured output from the Trade Decision Agent."""

    direction: Literal["YES", "NO", "ABSTAIN"]
    confidence_score: float = Field(ge=0.0, le=1.0)
    reasoning: str = Field(default="")

    @field_validator("reasoning")
    @classmethod
    def sanitize_reasoning(cls, v: str) -> str:
        """Keep reasoning safe for DB/logs without crashing on thorough 3-step CoT."""
        if len(v) > 1000:
            return v[:997] + "..."
        return v

    @field_validator("confidence_score")
    @classmethod
    def clamp_confidence(cls, v: float) -> float:
        """Enforce the epistemic humility hard ceiling (RULE 3)."""
        return min(v, config.CONFIDENCE_CEILING)


# ─────────────────────────────────────────────
# SYSTEM PROMPT — DO NOT MODIFY
# ─────────────────────────────────────────────

_SYSTEM_PROMPT = """You are a professional prediction market trading agent. Evaluate if we should execute a trade.
Respond ONLY in valid JSON. No preamble. No explanation. No markdown. JSON only.
Required schema:
{
  "direction": "YES|NO|ABSTAIN",
  "confidence_score": 0.0,
  "reasoning": "Step 1: Event/timing check. Step 2: Edge vs price. Step 3: Conclusion."
}
Rules:
- direction YES: execute YES trade
- direction NO: execute NO trade
- direction ABSTAIN: do not execute trade
- confidence_score: 0.0 to 0.88 maximum. Clamped at 0.88 to enforce epistemic humility.
- reasoning: concise 3-step evaluation under 50 words: (1) verify event match and resolution date/timing, (2) compare market price against true probability to determine edge, (3) justify final direction.
- Be extremely conservative. Wrong trades lose capital. Missed trades cost nothing."""


# ─────────────────────────────────────────────
# INTERNAL HELPERS
# ─────────────────────────────────────────────

def _matches_condition(
    trigger: dict[str, Any],
    signal_category: str,
    signal_probability: Optional[float] = None,
    time_to_resolution: Optional[float] = None,
    signal_source: Optional[str] = None,
) -> bool:
    """Helper to check if a structured trigger_condition matches current trade context in Python."""
    if "category" in trigger and trigger["category"] != "all" and trigger["category"] != signal_category:
        return False

    if "probability_range" in trigger and signal_probability is not None:
        p_range = trigger["probability_range"]
        if isinstance(p_range, list) and len(p_range) == 2:
            p_min, p_max = p_range
            if not (p_min <= signal_probability <= p_max):
                return False

    if "time_to_resolution_hours" in trigger and time_to_resolution is not None:
        limits = trigger["time_to_resolution_hours"]
        if isinstance(limits, dict):
            if "max" in limits and time_to_resolution > limits["max"]:
                return False
            if "min" in limits and time_to_resolution < limits["min"]:
                return False

    if "signal_source" in trigger and signal_source is not None:
        sources = trigger["signal_source"]
        if isinstance(sources, list) and signal_source not in sources:
            return False

    return True


async def fetch_relevant_lessons(
    category: str,
    signal_probability: Optional[float] = None,
    time_to_resolution: Optional[float] = None,
    signal_source: Optional[str] = None,
) -> tuple[list[str], bool]:
    """
    Fetch relevant episodic lessons from Supabase agent_memory under a 2-second timeout (RULE 5).
    Filters lessons based on trigger_condition in Python.
    
    Returns:
        tuple[list[str], was_memoryless]
    """
    was_memoryless = False
    async def _query() -> list[str]:
        client = await get_client()
        # Query active lessons matching the category or 'all'
        res = (
            client.table("agent_memory")
            .select("lesson,trigger_condition,confidence_score")
            .eq("retired", False)
            .execute()
        )
        if not res.data:
            return []

        matched_lessons = []
        for row in res.data:
            trigger = row.get("trigger_condition") or {}
            lesson = row.get("lesson")
            if not lesson:
                continue
            
            # Match condition
            if _matches_condition(
                trigger=trigger,
                signal_category=category,
                signal_probability=signal_probability,
                time_to_resolution=time_to_resolution,
                signal_source=signal_source
            ):
                matched_lessons.append(lesson)

        # Sort or limit to max memory size per config
        matched_lessons = matched_lessons[:config.MEMORY_MAX_LESSONS_PER_QUERY]
        return matched_lessons

    try:
        lessons = await asyncio.wait_for(_query(), timeout=config.SUPABASE_TIMEOUT_SECONDS)
        return lessons, False
    except asyncio.TimeoutError:
        was_memoryless = True
        logger.warning(
            f"[TRADE_DECISION] Supabase agent_memory read timed out after {config.SUPABASE_TIMEOUT_SECONDS}s. "
            "Proceeding as memoryless (RULE 5 fallback)"
        )
        asyncio.create_task(
            alert_supabase_degradation("agent_memory", "proceeding with was_memoryless = True")
        )
        return [], True
    except Exception as e:
        was_memoryless = True
        logger.error(f"[TRADE_DECISION] Supabase agent_memory read failed: {e}")
        return [], True


async def _execute_llm_call(
    url: str,
    api_key: str,
    model: str,
    messages: list[dict[str, str]],
    is_fallback: bool = False,
) -> Optional[TradeDecisionOutput]:
    """Execute raw HTTP completion call to the selected provider."""
    import aiohttp
    
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": config.MAX_TOKENS_TRADE_DECISION,
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
    }
    if "deepseek" in model.lower():
        payload["max_tokens"] = getattr(config, "MAX_TOKENS_DEEPSEEK_TRADE", 300)
        payload["thinking"] = {"type": "disabled"}
    elif "qwen" in model.lower():
        payload["enable_thinking"] = False

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if is_fallback:
        headers["HTTP-Referer"] = "https://github.com/zeroalpha"
        headers["X-Title"] = "Zero Alpha Agent"

    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, headers=headers) as response:
            if response.status in config.FAIL_FAST_HTTP_CODES:
                provider_name = "OpenRouter" if "openrouter" in url.lower() else "NVIDIA NIM"
                logger.error(f"[LLM] Auth/quota error {response.status} on {provider_name} — immediate failover")
                raise LLMFailFastError(response.status, provider_name)
            if response.status != 200:
                text = await response.text()
                logger.error(
                    f"[TRADE_DECISION] Provider returned {response.status}: {text} "
                    f"| model={model} fallback={is_fallback}"
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
                f"[TRADE_DECISION] LLM completion success | model={model} "
                f"usage={usage} fallback={is_fallback}"
            )

            if not choice_content:
                logger.error("[TRADE_DECISION] Provider returned empty message content")
                return None

            # Clean markdown formatting if present
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
                logger.error(f"[TRADE_DECISION] JSON decode error: {e} | Content: {choice_content}")
                return None

            try:
                return TradeDecisionOutput(**parsed_json)
            except ValidationError as e:
                logger.error(f"[TRADE_DECISION] Pydantic validation error: {e} | Content: {choice_content}")
                return None


# ─────────────────────────────────────────────
# PUBLIC API
# ─────────────────────────────────────────────

async def decide_trade(
    headline: str,
    category: str,
    market_id: str,
    market_question: str,
    market_price: float,
    agent_estimate: float,
    portfolio_value: float,
    time_to_resolution_hours: Optional[float] = None,
    signal_source: Optional[str] = None,
    *,
    estimate_method: str,
    estimate_sample_size: int,
) -> tuple[Optional[TradeDecisionOutput], bool]:
    """
    Decide whether to execute a trade by calling Qwen3-235B-A22B on SiliconFlow
    with a strict 18-second timeout, failing over immediately to OpenRouter.

    Prepend agent_memory lessons as a marked "Warning Block" at the top of the prompt.

    Args:
        estimate_method:       Estimator method identifier behind agent_estimate
                               (strategies.estimator METHOD_* values). Shown to the LLM
                               as provenance so it can reason about estimate quality.
        estimate_sample_size:  Number of observations behind agent_estimate (n).
    """
    import os

    # 1. Fetch relevant lessons from database
    lessons, was_memoryless = await fetch_relevant_lessons(
        category=category,
        signal_probability=agent_estimate,
        time_to_resolution=time_to_resolution_hours,
        signal_source=signal_source,
    )

    # 2. Build lessons warning block
    lessons_prompt = ""
    if lessons:
        lessons_prompt = "*** WARNING: LESSONS FROM PAST MISTAKES ***\n"
        for i, lesson in enumerate(lessons, 1):
            lessons_prompt += f"{i}. {lesson}\n"
        lessons_prompt += "******************************************\n\n"

    # 3. Build user trade context
    user_prompt = (
        f"{lessons_prompt}"
        f"MARKET CONTEXT:\n"
        f"- Headline: {headline}\n"
        f"- Category: {category}\n"
        f"- Market ID: {market_id}\n"
        f"- Market Question: {market_question}\n"
        f"- Current Price (USDC): {market_price:.4f}\n"
        f"- Model Probability Estimate (source: {estimate_method}, n={estimate_sample_size}): {agent_estimate:.4f}\n"
        f"- Total Portfolio Value: ${portfolio_value:.2f}\n"
        f"Determine if a YES or NO trade is optimal using the 3-step reasoning format (Event/timing check, Edge vs price, Conclusion)."
    )

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    t_start = time.perf_counter()

    # 4. TokenRouter (Primary) with strict 18s timeout wrapper (RULE 6)
    tr_key = os.environ.get("TOKENROUTER_API_KEY")
    if tr_key and tr_key != "placeholder":
        url = f"{config.PROVIDER_TOKENROUTER}/v1/chat/completions"
        model = config.MODEL_TRADE_DECISION
        
        try:
            logger.info(f"[TRADE_DECISION] Calling primary TokenRouter ({model}) for {market_id}...")
            result = await asyncio.wait_for(
                _execute_llm_call(url, tr_key, model, messages, is_fallback=False),
                timeout=config.LLM_TIMEOUT_SECONDS
            )
            if result is not None:
                return result, was_memoryless
            
            logger.warning("[TRADE_DECISION] TokenRouter call returned None, initiating failover...")
        except asyncio.TimeoutError:
            latency_ms = int((time.perf_counter() - t_start) * 1000)
            logger.error(
                f"[TRADE_DECISION] Primary TokenRouter timed out after {latency_ms}ms (limit={config.LLM_TIMEOUT_SECONDS}s)."
            )
            # Send Telegram alert
            asyncio.create_task(
                alert_siliconflow_failover(latency_ms, "OpenRouter")
            )
        except LLMFailFastError as e:
            logger.warning(
                f"[TRADE_DECISION] Catching LLMFailFastError {e.status} on {e.provider} inside TokenRouter primary attempt. Falling through to fallback immediately."
            )
        except Exception as e:
            logger.error(f"[TRADE_DECISION] TokenRouter call failed: {e}, initiating failover...")
    else:
        logger.warning("[TRADE_DECISION] TokenRouter API key missing, bypassing primary...")

    # 5. OpenRouter (Fallback per RULE 6)
    or_key = os.environ.get("OPENROUTER_API_KEY")
    if or_key and or_key != "placeholder":
        url = f"{config.PROVIDER_OPENROUTER}/chat/completions"
        model = getattr(config, "MODEL_TRADE_DECISION_FALLBACK_OR", "qwen/qwen3-235b-a22b")
        
        try:
            logger.info(f"[TRADE_DECISION] Calling fallback OpenRouter for {market_id}...")
            result = await asyncio.wait_for(
                _execute_llm_call(url, or_key, model, messages, is_fallback=True),
                timeout=15.0
            )
            if result is not None:
                return result, was_memoryless
        except asyncio.TimeoutError:
            logger.error("[TRADE_DECISION] Fallback OpenRouter timed out as well.")
        except LLMFailFastError as e:
            logger.error(f"[TRADE_DECISION] Fallback OpenRouter call failed with LLMFailFastError: {e}")
            raise e
        except Exception as e:
            logger.error(f"[TRADE_DECISION] Fallback OpenRouter call failed: {e}")
    else:
        logger.error("[TRADE_DECISION] OpenRouter API key missing, fallback unavailable.")

    # Both failed
    logger.error("[TRADE_DECISION] CRITICAL: Both TokenRouter and OpenRouter failed.")
    return None, was_memoryless
