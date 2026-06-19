"""
Coordinator Agent — Layer 6
Type: Python weighted aggregation (primary) / LLM escalation (conflict cases)
Model for escalation: Qwen3-32B via OpenRouter (config.MODEL_COORDINATOR)
"""

import json
import logging
import asyncio
import time
from typing import Optional, Literal
from pydantic import BaseModel, ValidationError, Field, field_validator

import config
from llm.news_analyst import NewsAnalystOutput
from llm.trade_decision import TradeDecisionOutput
from monitoring.telegram_alerts import alert_siliconflow_failover

logger = logging.getLogger(__name__)

class LLMFailFastError(Exception):
    def __init__(self, status: int, provider: str):
        self.status = status
        self.provider = provider
        super().__init__(f"Auth/quota error {status} on {provider}")

# ─────────────────────────────────────────────
# OUTPUT SCHEMA
# ─────────────────────────────────────────────

class CoordinatorOutput(BaseModel):
    """Validated structured output from the Coordinator."""

    direction: Literal["YES", "NO", "ABSTAIN"]
    confidence_score: float = Field(ge=0.0, le=1.0)
    reasoning: str = Field(max_length=300)

    @field_validator("confidence_score")
    @classmethod
    def clamp_confidence(cls, v: float) -> float:
        """Enforce the epistemic humility hard ceiling (RULE 3)."""
        return min(v, config.CONFIDENCE_CEILING)


# ─────────────────────────────────────────────
# SYSTEM PROMPT — DO NOT MODIFY
# ─────────────────────────────────────────────

_SYSTEM_PROMPT = """You are a prediction market trading coordinator. Resolve the disagreement between the News Analyst agent and the Trade Decision Agent.
Respond ONLY in valid JSON. No preamble. No explanation. No markdown. JSON only.
Required schema:
{
  "direction": "YES|NO|ABSTAIN",
  "confidence_score": 0.0,
  "reasoning": "max 50 words"
}
Rules:
- direction YES: execute YES trade
- direction NO: execute NO trade
- direction ABSTAIN: do not execute trade
- confidence_score: 0.0 to 0.88 maximum.
- Be conservative. Choose the safest, most logical option that protects portfolio capital."""


# ─────────────────────────────────────────────
# INTERNAL HELPERS
# ─────────────────────────────────────────────

async def _execute_llm_call(
    url: str,
    api_key: str,
    model: str,
    messages: list[dict[str, str]],
    is_fallback: bool = False,
) -> Optional[CoordinatorOutput]:
    """Execute raw HTTP completion call to the selected provider."""
    import aiohttp
    
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
    }

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
                    f"[COORDINATOR] Provider returned {response.status}: {text} "
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
                f"[COORDINATOR] LLM completion success | model={model} "
                f"usage={usage} fallback={is_fallback}"
            )

            if not choice_content:
                logger.error("[COORDINATOR] Provider returned empty message content")
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
                logger.error(f"[COORDINATOR] JSON decode error: {e} | Content: {choice_content}")
                return None

            try:
                return CoordinatorOutput(**parsed_json)
            except ValidationError as e:
                logger.error(f"[COORDINATOR] Pydantic validation error: {e} | Content: {choice_content}")
                return None


# ─────────────────────────────────────────────
# ESCALATION FLOW
# ─────────────────────────────────────────────

async def escalate_to_llm_coordinator(
    headline: str,
    market_question: str,
    news_output: NewsAnalystOutput,
    trade_output: TradeDecisionOutput,
) -> Optional[CoordinatorOutput]:
    """
    Escalate conflict to LLM Coordinator (Qwen3-32B) under an 18-second timeout.
    Attempts primary NVIDIA NIM first, falling back to OpenRouter.
    """
    import os

    user_prompt = (
        f"CONFLICT TO RESOLVE:\n"
        f"- News Headline: {headline}\n"
        f"- Market Question: {market_question}\n\n"
        f"NEWS ANALYST DECISION:\n"
        f"- Direction: {news_output.direction}\n"
        f"- Confidence: {news_output.confidence_score:.4f}\n"
        f"- Reasoning: {news_output.reasoning}\n\n"
        f"TRADE DECISION AGENT DECISION:\n"
        f"- Direction: {trade_output.direction}\n"
        f"- Confidence: {trade_output.confidence_score:.4f}\n"
        f"- Reasoning: {trade_output.reasoning}\n\n"
        f"Resolve this conflict and output the final direction, confidence_score, and reasoning."
    )

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    t_start = time.perf_counter()

    async def _do_escalation() -> Optional[CoordinatorOutput]:
        # 1. NVIDIA NIM (Primary) with strict 18s timeout wrapper
        nv_key = os.environ.get("NVIDIA_API_KEY")
        if nv_key and nv_key != "placeholder":
            url = f"{config.PROVIDER_NVIDIA}/chat/completions"
            model = config.MODEL_COORDINATOR
            
            try:
                logger.info("[COORDINATOR] Calling primary NVIDIA NIM for escalation...")
                result = await asyncio.wait_for(
                    _execute_llm_call(url, nv_key, model, messages, is_fallback=False),
                    timeout=config.LLM_TIMEOUT_SECONDS
                )
                if result is not None:
                    return result
                
                logger.warning("[COORDINATOR] NVIDIA NIM call returned None, initiating failover...")
            except asyncio.TimeoutError:
                latency_ms = int((time.perf_counter() - t_start) * 1000)
                logger.error(
                    f"[COORDINATOR] Primary NVIDIA NIM timed out after {latency_ms}ms (limit={config.LLM_TIMEOUT_SECONDS}s)."
                )
                # Send Telegram alert
                asyncio.create_task(
                    alert_siliconflow_failover(latency_ms, "OpenRouter")
                )
            except LLMFailFastError as e:
                logger.warning(
                    f"[COORDINATOR] Catching LLMFailFastError {e.status} on {e.provider} inside NVIDIA NIM primary attempt. Falling through to fallback immediately."
                )
            except Exception as e:
                logger.error(f"[COORDINATOR] NVIDIA NIM call failed: {e}, initiating failover...")
        else:
            logger.warning("[COORDINATOR] NVIDIA API key missing, bypassing primary...")

        # 2. OpenRouter (Fallback) with 15s timeout
        or_key = os.environ.get("OPENROUTER_API_KEY")
        if or_key and or_key != "placeholder":
            url = f"{config.PROVIDER_OPENROUTER}/chat/completions"
            model = config.MODEL_COORDINATOR
            
            try:
                logger.info("[COORDINATOR] Calling fallback OpenRouter for escalation...")
                result = await asyncio.wait_for(
                    _execute_llm_call(url, or_key, model, messages, is_fallback=True),
                    timeout=15.0
                )
                if result is not None:
                    return result
            except asyncio.TimeoutError:
                logger.error("[COORDINATOR] Fallback OpenRouter timed out as well.")
            except LLMFailFastError as e:
                logger.error(f"[COORDINATOR] Fallback OpenRouter call failed with LLMFailFastError: {e}")
                raise e
            except Exception as e:
                logger.error(f"[COORDINATOR] Fallback OpenRouter call failed: {e}")
        else:
            logger.error("[COORDINATOR] OpenRouter API key missing, fallback unavailable.")

        logger.error("[COORDINATOR] CRITICAL: Both NVIDIA NIM and OpenRouter failed.")
        return None

    try:
        # Wrap escalation with standard 18s timeout (config.LLM_TIMEOUT_SECONDS)
        return await asyncio.wait_for(
            _do_escalation(), timeout=config.LLM_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        latency_ms = int((time.perf_counter() - t_start) * 1000)
        logger.error(
            f"[COORDINATOR] Escalation timed out after {latency_ms}ms (limit={config.LLM_TIMEOUT_SECONDS}s)."
        )
        return None
    except Exception as e:
        logger.error(f"[COORDINATOR] Escalation failed with error: {e}")
        return None


# ─────────────────────────────────────────────
# PUBLIC COORDINATION INTERFACE
# ─────────────────────────────────────────────

def check_conflict(news_dir: str, news_conf: float, trade_dir: str) -> bool:
    """
    Check if a conflict between News Analyst and Trade Decision exists.
    Trigger conditional: disagree AND News Analyst confidence > 0.70.
    """
    return (news_dir != trade_dir) and (news_conf > 0.70)


def python_weighted_aggregation(news_conf: float, trade_conf: float) -> float:
    """
    Aggregate confidences when both agents agree.
    40% News Analyst weight + 60% Trade Decision weight, capped at 0.88.
    """
    weighted = (news_conf * 0.4) + (trade_conf * 0.6)
    return min(weighted, config.CONFIDENCE_CEILING)


async def coordinate_decision(
    headline: str,
    market_question: str,
    news_output: NewsAnalystOutput,
    trade_output: TradeDecisionOutput,
) -> Optional[CoordinatorOutput]:
    """
    Synthesize News Analyst and Trade Decision Agent outputs.
    
    Routes:
      1. News Analyst and Trade Decision Agent disagree AND News Analyst confidence > 0.70:
         -> LLM escalation via escalate_to_llm_coordinator().
      2. News Analyst confidence <= 0.70:
         -> Trade Decision Agent wins directly, no escalation.
      3. Both agents agree (same direction):
         -> Python aggregation (weighted average confidence, common direction).
    """
    news_dir = news_output.direction
    news_conf = news_output.confidence_score
    trade_dir = trade_output.direction
    trade_conf = trade_output.confidence_score

    # Case 1: Conflict escalation (Rule 8 conditional)
    if check_conflict(news_dir, news_conf, trade_dir):
        logger.info(
            f"[COORDINATOR] Conflict detected: News Analyst={news_dir} ({news_conf:.2f}), "
            f"Trade Decision={trade_dir} ({trade_conf:.2f}). Escalating to LLM..."
        )
        escalation_result = await escalate_to_llm_coordinator(
            headline=headline,
            market_question=market_question,
            news_output=news_output,
            trade_output=trade_output
        )
        if escalation_result is not None:
            return escalation_result
        
        # Fallback if escalation fails: Trade Decision wins to maintain safety
        logger.warning("[COORDINATOR] LLM Escalation failed or timed out. Falling back to Trade Decision.")
        return CoordinatorOutput(
            direction=trade_dir,
            confidence_score=trade_conf,
            reasoning=f"Escalation failed. Fallback to Trade Decision: {trade_output.reasoning[:150]}"
        )

    # Case 2: Trade Decision wins because News Analyst confidence is low
    if news_conf <= 0.70:
        logger.info(
            f"[COORDINATOR] News Analyst confidence {news_conf:.2f} <= 0.70. "
            "Trade Decision wins directly."
        )
        return CoordinatorOutput(
            direction=trade_dir,
            confidence_score=trade_conf,
            reasoning=f"News confidence low ({news_conf:.2f}). Trade Decision wins: {trade_output.reasoning[:200]}"
        )

    # Case 3: Both agree (same direction)
    logger.info(
        f"[COORDINATOR] Sub-agents agree on {trade_dir}. Applying Python weighted aggregation..."
    )
    aggregated_conf = python_weighted_aggregation(news_conf, trade_conf)
    return CoordinatorOutput(
        direction=trade_dir,
        confidence_score=aggregated_conf,
        reasoning=(
            f"Agents agree. Weighted confidence: {aggregated_conf:.2f}. "
            f"Trade Decision reason: {trade_output.reasoning[:100]} | News reason: {news_output.reasoning[:100]}"
        )
    )
