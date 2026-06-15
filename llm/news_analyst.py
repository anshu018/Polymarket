import os
import json
import logging
import asyncio
import aiohttp
from typing import Literal, Optional
from pydantic import BaseModel, ValidationError, Field, field_validator
import config
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

class NewsAnalystOutput(BaseModel):
    event_category: Literal[
        "politics", "crypto", "sports",
        "legal", "economics", "science",
        "other"
    ]
    affected_market_ids: list[str] = Field(default_factory=list)
    confidence_score: float = Field(ge=0.0)
    direction: Literal["YES", "NO", "ABSTAIN"]
    reasoning: str = Field(max_length=300)

    @field_validator('confidence_score')
    @classmethod
    def clamp_confidence(cls, v: float) -> float:
        return min(v, 0.88)

async def _execute_news_call(url: str, api_key: str, model: str, system_content: str, prompt: str, is_fallback: bool) -> Optional[str]:
    import aiohttp
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_content},
            {"role": "user", "content": prompt}
        ],
        "response_format": {"type": "json_object"}
    }
    
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if not is_fallback:
        headers["HTTP-Referer"] = "https://github.com/zeroalpha"
        headers["X-Title"] = "Zero Alpha Agent"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers) as response:
                if response.status != 200:
                    text = await response.text()
                    provider_name = "NVIDIA" if is_fallback else "OpenRouter"
                    logger.error(f"[NEWS_ANALYST] {provider_name} returned {response.status}: {text}")
                    return None
                data = await response.json()
                usage = data.get("usage", {})
                logger.info(f"[NEWS_ANALYST] Token usage: {usage} | Fallback: {is_fallback}")
                return data.get("choices", [{}])[0].get("message", {}).get("content", "")
    except Exception as e:
        provider_name = "NVIDIA" if is_fallback else "OpenRouter"
        logger.error(f"[NEWS_ANALYST] {provider_name} request failed: {e}")
        return None

async def classify_signal(headline: str, source: str) -> Optional[NewsAnalystOutput]:
    """
    Classify a news headline into a trading action.
    Returns None on timeout or failure.
    """
    try:
        system_content = """You are a prediction market signal classifier.
Classify news headlines for relevance to open
prediction markets.
Respond ONLY in valid JSON. No preamble.
No explanation. No markdown. JSON only.
Exact schema required:
{
  "event_category": "politics|crypto|sports|legal|economics|science|other",
  "affected_market_ids": [],
  "confidence_score": 0.0,
  "direction": "YES|NO|ABSTAIN",
  "reasoning": "max 50 words"
}
Rules:
- confidence_score: 0.0 to 0.88 maximum
- direction YES: event makes something more likely to happen on prediction markets
- direction NO: event makes something less likely to happen
- direction ABSTAIN: genuinely uncertain
- affected_market_ids: always empty list []
  Market matching happens in a later layer.
- Be conservative. Wrong signals cost money. Missed signals cost nothing.
"""

        import time
        start_time = time.time()
        prompt = f"Headline: {headline}\nSource: {source}"
        choice_content = None

        # 1. Attempt Primary: OpenRouter (Gemma 4 12B free)
        or_key = os.environ.get("OPENROUTER_API_KEY")
        if or_key and or_key != "placeholder":
            url = f"{config.PROVIDER_OPENROUTER}/chat/completions"
            model = getattr(config, "MODEL_NEWS_ANALYST", "google/gemma-4-12b-it:free")
            try:
                logger.info(f"[NEWS_ANALYST] Calling primary OpenRouter ({model})...")
                choice_content = await asyncio.wait_for(
                    _execute_news_call(url, or_key, model, system_content, prompt, is_fallback=False),
                    timeout=config.NEWS_ANALYST_TIMEOUT_SECONDS
                )
            except asyncio.TimeoutError:
                logger.warning(f"[NEWS_ANALYST] Primary OpenRouter call timed out (limit={config.NEWS_ANALYST_TIMEOUT_SECONDS}s).")

        # 2. Attempt Fallback: NVIDIA NIM (Qwen3-32B)
        if not choice_content:
            nv_key = os.environ.get("NVIDIA_API_KEY")
            if nv_key and nv_key != "placeholder":
                url = f"{config.PROVIDER_NVIDIA}/chat/completions"
                model = getattr(config, "MODEL_NEWS_ANALYST_FALLBACK", "qwen/qwen3-32b")
                
                elapsed = time.time() - start_time
                fallback_timeout = max(10.0, config.NEWS_ANALYST_TIMEOUT_SECONDS - elapsed)
                
                try:
                    logger.info(f"[NEWS_ANALYST] Calling fallback NVIDIA NIM ({model}) with timeout={fallback_timeout:.1f}s...")
                    choice_content = await asyncio.wait_for(
                        _execute_news_call(url, nv_key, model, system_content, prompt, is_fallback=True),
                        timeout=fallback_timeout
                    )
                except asyncio.TimeoutError:
                    logger.error(f"[NEWS_ANALYST] Fallback NVIDIA NIM call timed out (limit={fallback_timeout:.1f}s).")
            else:
                logger.error("[NEWS_ANALYST] NVIDIA API key missing, fallback unavailable.")

        if not choice_content:
            logger.error("[NEWS_ANALYST] Empty response from both primary and fallback providers")
            return None
                        
        try:
            # Clean up markdown wrapping if OpenRouter model insists on adding it
            cleaned = choice_content.strip()
            if cleaned.startswith("```json"):
                cleaned = cleaned[7:]
            if cleaned.startswith("```"):
                cleaned = cleaned[3:]
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3]
                
            parsed_json = json.loads(cleaned.strip())
            if isinstance(parsed_json, list) and len(parsed_json) > 0:
                parsed_json = parsed_json[0]
        except json.JSONDecodeError as e:
            logger.error(f"[NEWS_ANALYST] JSON decode failed: {e} | Content: {choice_content}")
            return None
            
        try:
            validated = NewsAnalystOutput(**parsed_json)
            
            # Database logging
            try:
                from memory.supabase_client import get_client
                
                async def _log_to_supabase():
                    client = await get_client()
                    client.table('market_signals').insert({
                        'raw_headline': headline,
                        'source_name': source,
                        'source_url': source,
                        'category': validated.event_category,
                        'event_type': validated.event_category,
                        'passed_fast_path': False,
                        'confidence_score': validated.confidence_score,
                        'affected_market_ids': validated.affected_market_ids,
                        'action_taken': 'pending' if validated.confidence_score >= 0.75 else 'discarded',
                        'discard_reason': 'low confidence' if validated.confidence_score < 0.75 else None,
                        'detected_at': datetime.now(timezone.utc).isoformat(),
                        'processed_at': datetime.now(timezone.utc).isoformat()
                    }).execute()

                db_timeout = getattr(config, "SUPABASE_TIMEOUT_SECONDS", 2)
                await asyncio.wait_for(_log_to_supabase(), timeout=db_timeout)
            except asyncio.TimeoutError:
                logger.error(f"[NEWS_ANALYST] Failed to log signal to Supabase: timed out after {db_timeout}s")
            except Exception as db_err:
                logger.error(f"[NEWS_ANALYST] Failed to log signal to Supabase: {db_err}")
                
            return validated
        except ValidationError as e:
            logger.error(f"[NEWS_ANALYST] Pydantic validation failed: {e} | Content: {choice_content}")
            return None

    except Exception as e:
        logger.error(f"[NEWS_ANALYST] Unexpected error: {e}")
        return None

_models_validated = False

async def validate_models() -> None:
    """
    Validate that the primary or fallback News Analyst model is reachable and working.
    Runs once at startup only. Raises RuntimeError if both fail.
    """
    global _models_validated
    if _models_validated:
        return

    logger.info("[NEWS_ANALYST] Running startup model validation probe...")
    
    # 1. Probe Primary: OpenRouter
    or_key = os.environ.get("OPENROUTER_API_KEY")
    primary_ok = False
    primary_err = "API Key Missing"
    primary_model = getattr(config, "MODEL_NEWS_ANALYST", "google/gemma-4-31b-it:free")
    
    if or_key and or_key != "placeholder":
        url = f"{config.PROVIDER_OPENROUTER}/chat/completions"
        try:
            payload = {
                "model": primary_model,
                "messages": [{"role": "user", "content": "Reply OK"}],
                "max_tokens": 5
            }
            headers = {
                "Authorization": f"Bearer {or_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/zeroalpha",
                "X-Title": "Zero Alpha Agent"
            }
            async with asyncio.timeout(12.0):
                async with aiohttp.ClientSession() as session:
                    async with session.post(url, json=payload, headers=headers) as response:
                        if response.status == 200:
                            primary_ok = True
                            logger.info(f"[NEWS_ANALYST] Model validated: {primary_model}")
                            _models_validated = True
                            return
                        else:
                            primary_err = f"Status {response.status}: {await response.text()}"
        except asyncio.TimeoutError:
            primary_err = "Timeout after 12 seconds"
        except Exception as e:
            primary_err = f"Exception: {type(e).__name__}: {e}"

    logger.warning(f"[NEWS_ANALYST] Primary model validation failed: {primary_err}")

    # 2. Probe Fallback: NVIDIA NIM
    nv_key = os.environ.get("NVIDIA_API_KEY")
    fallback_ok = False
    fallback_err = "API Key Missing"
    fallback_model = getattr(config, "MODEL_NEWS_ANALYST_FALLBACK", "qwen/qwen3-next-80b-a3b-instruct")

    if nv_key and nv_key != "placeholder":
        url = f"{config.PROVIDER_NVIDIA}/chat/completions"
        try:
            payload = {
                "model": fallback_model,
                "messages": [{"role": "user", "content": "Reply OK"}],
                "max_tokens": 5
            }
            headers = {
                "Authorization": f"Bearer {nv_key}",
                "Content-Type": "application/json",
            }
            async with asyncio.timeout(12.0):
                async with aiohttp.ClientSession() as session:
                    async with session.post(url, json=payload, headers=headers) as response:
                        if response.status == 200:
                            fallback_ok = True
                            logger.info(f"[NEWS_ANALYST] Model validated: {fallback_model}")
                            _models_validated = True
                            return
                        else:
                            fallback_err = f"Status {response.status}: {await response.text()}"
        except asyncio.TimeoutError:
            fallback_err = "Timeout after 12 seconds"
        except Exception as e:
            fallback_err = f"Exception: {type(e).__name__}: {e}"

    logger.warning(f"[NEWS_ANALYST] Fallback model validation failed: {fallback_err}")
    
    # Both failed
    raise RuntimeError(
        f"Model validation failed. Primary ({primary_model}): {primary_err}. Fallback ({fallback_model}): {fallback_err}."
    )

