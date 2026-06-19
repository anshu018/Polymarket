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

# A5: Module-level aiohttp session reuse
_session: Optional[aiohttp.ClientSession] = None


async def _get_session() -> aiohttp.ClientSession:
    """Return a reusable aiohttp session, creating it if needed."""
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession()
    return _session

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

class LLMFailFastError(Exception):
    def __init__(self, status: int, provider: str):
        self.status = status
        self.provider = provider
        super().__init__(f"Auth/quota error {status} on {provider}")

async def _execute_news_call(
    url: str,
    api_key: str,
    model: str,
    system_content: str,
    prompt: str,
    is_fallback: bool,
    provider_name: Optional[str] = None,
) -> Optional[str]:
    """Execute a single LLM API call and return the raw content string."""
    # A1: Disable thinking mode; add max_tokens
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_content},
            {"role": "user", "content": prompt}
        ],
        "response_format": {"type": "json_object"},
        "max_tokens": 200,
        "temperature": 0.1,
    }
    # Only add thinking controls for primary (SiliconFlow) calls, not fallback
    if not is_fallback:
        payload["enable_thinking"] = False

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if not is_fallback:
        headers["HTTP-Referer"] = "https://github.com/zeroalpha"
        headers["X-Title"] = "Zero Alpha Agent"

    resolved_provider = provider_name or ("SiliconFlow" if not is_fallback else "NVIDIA NIM")
    try:
        # A5: Reuse module-level session
        session = await _get_session()
        async with session.post(url, json=payload, headers=headers) as response:
            if response.status in config.FAIL_FAST_HTTP_CODES:
                logger.error(f"[LLM] Auth/quota error {response.status} on {resolved_provider} — immediate failover")
                raise LLMFailFastError(response.status, resolved_provider)
            if response.status != 200:
                text = await response.text()
                logger.error(f"[NEWS_ANALYST] {resolved_provider} returned {response.status}: {text}")
                return None
            data = await response.json()
            usage = data.get("usage", {})
            logger.info(f"[NEWS_ANALYST] Token usage: {usage} | Fallback: {is_fallback}")
            return data.get("choices", [{}])[0].get("message", {}).get("content", "")
    except LLMFailFastError:
        raise
    except Exception as e:
        logger.error(f"[NEWS_ANALYST] {resolved_provider} request failed: {e}")
        return None

async def classify_signal(headline: str, source: str, market_question: Optional[str] = None) -> Optional[NewsAnalystOutput]:
    """
    Classify a news headline into a trading action.
    Returns None on timeout or failure.
    """
    try:
        # A2: Rewritten system prompt — clearer schema, calibration examples, better direction definitions
        system_content = """You are a prediction market signal classifier.
Classify news headlines for relevance to binary prediction markets (YES/NO outcomes).
Respond ONLY in valid JSON. No preamble. No explanation. No markdown. JSON only.

Required JSON schema:
{
  "event_category": "politics|crypto|sports|legal|economics|science|other",
  "affected_market_ids": [],
  "confidence_score": 0.0,
  "direction": "YES|NO|ABSTAIN",
  "reasoning": "max 50 words"
}

Rules:
- confidence_score: 0.0 to 0.88 maximum
- direction YES: headline makes a binary outcome MORE likely to resolve YES
- direction NO: headline makes a binary outcome LESS likely to resolve YES
- direction ABSTAIN: ONLY when headline has zero relation to any binary market outcome
- affected_market_ids: always empty list []

Confidence calibration guide:
- 0.80-0.88: Direct named outcome ("X wins", "FDA approves Y", "Fed cuts by 25bps")
- 0.70-0.79: Clear causal signal ("polls show X leading by 10 points")
- 0.60-0.69: Indirect or background signal ("X campaign reports fundraising record")
- Below 0.60: Weak/ambiguous — still classify YES/NO if any market plausibly affected
- ABSTAIN: administrative notices, local news, sports scores with no market context

Examples:
- "Local city council meets Tuesday" → direction: ABSTAIN, confidence_score: 0.0, event_category: other
"""

        import time
        start_time = time.time()
        prompt = f"Headline: {headline}\nSource: {source}"
        if market_question:
            prompt += f"\nTarget Prediction Market Question: {market_question}"
        choice_content = None

        # 1. Attempt Primary: SiliconFlow (Qwen3-32B)
        sf_key = os.environ.get("SILICONFLOW_API_KEY")
        if sf_key and sf_key != "placeholder":
            url = f"{config.PROVIDER_SILICONFLOW}/chat/completions"
            model = getattr(config, "MODEL_NEWS_ANALYST", "qwen/qwen3-32b")
            try:
                logger.info(f"[NEWS_ANALYST] Calling primary SiliconFlow ({model})...")
                choice_content = await asyncio.wait_for(
                    _execute_news_call(url, sf_key, model, system_content, prompt, is_fallback=False),
                    timeout=config.NEWS_ANALYST_TIMEOUT_SECONDS
                )
            except LLMFailFastError as e:
                logger.warning(f"[NEWS_ANALYST] Primary SiliconFlow auth/quota error: {e}. Proceeding immediately to fallback.")
            except asyncio.TimeoutError:
                logger.warning(f"[NEWS_ANALYST] Primary SiliconFlow call timed out (limit={config.NEWS_ANALYST_TIMEOUT_SECONDS}s).")

        # 2. Attempt Fallback: NVIDIA NIM (Llama-3.3-70B)
        if not choice_content:
            nv_key = os.environ.get("NVIDIA_API_KEY")
            if not nv_key or nv_key in ("placeholder", ""):
                logger.warning("[NEWS_ANALYST] NVIDIA_API_KEY not set — skipping NVIDIA fallback. "
                               "Get a free key at build.nvidia.com to improve reliability.")
            else:
                url = f"{config.PROVIDER_NVIDIA}/chat/completions"
                model = getattr(config, "MODEL_NEWS_ANALYST_FALLBACK", "meta/llama-3.3-70b-instruct")
                
                elapsed = time.time() - start_time
                fallback_timeout = max(10.0, config.NEWS_ANALYST_TIMEOUT_SECONDS - elapsed)
                
                try:
                    logger.info(f"[NEWS_ANALYST] Calling fallback NVIDIA NIM ({model}) with timeout={fallback_timeout:.1f}s...")
                    choice_content = await asyncio.wait_for(
                        _execute_news_call(url, nv_key, model, system_content, prompt, is_fallback=True),
                        timeout=fallback_timeout
                    )
                except LLMFailFastError as e:
                    logger.error(f"[NEWS_ANALYST] Fallback NVIDIA NIM auth/quota error: {e}.")
                except asyncio.TimeoutError:
                    logger.error(f"[NEWS_ANALYST] Fallback NVIDIA NIM call timed out (limit={fallback_timeout:.1f}s).")

        # 3. Attempt Second Fallback: Google Gemini Flash (free tier — 1M tokens/day)
        if not choice_content:
            gemini_key = getattr(config, "GEMINI_API_KEY", "") or os.environ.get("GEMINI_API_KEY", "")
            if gemini_key and gemini_key not in ("placeholder", ""):
                gemini_url = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
                gemini_model = getattr(config, "MODEL_NEWS_ANALYST_FALLBACK_2", "gemini-2.0-flash")
                elapsed = time.time() - start_time
                gemini_timeout = max(8.0, 45.0 - elapsed)
                try:
                    logger.info(f"[NEWS_ANALYST] Calling second fallback Gemini Flash ({gemini_model})...")
                    choice_content = await asyncio.wait_for(
                        _execute_news_call(gemini_url, gemini_key, gemini_model, system_content, prompt, is_fallback=True),
                        timeout=gemini_timeout
                    )
                except asyncio.TimeoutError:
                    logger.error("[NEWS_ANALYST] Gemini Flash second fallback timed out.")
                except Exception as e:
                    logger.error(f"[NEWS_ANALYST] Gemini Flash second fallback failed: {e}")
            else:
                logger.debug("[NEWS_ANALYST] GEMINI_API_KEY not set — Gemini fallback unavailable.")


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
    
    # A4: Warn if NVIDIA fallback key is missing
    nv_key_check = os.environ.get("NVIDIA_API_KEY", "")
    if not nv_key_check or nv_key_check == "placeholder":
        logger.warning("[NEWS_ANALYST] NVIDIA_API_KEY is not set — fallback provider unavailable. Strongly recommended.")

    # 1. Probe Primary: SiliconFlow
    sf_key = os.environ.get("SILICONFLOW_API_KEY")
    primary_ok = False
    primary_err = "API Key Missing"
    primary_model = getattr(config, "MODEL_NEWS_ANALYST", "qwen/qwen3-32b")

    if sf_key and sf_key != "placeholder":
        url = f"{config.PROVIDER_SILICONFLOW}/chat/completions"
        try:
            payload = {
                "model": primary_model,
                "messages": [{"role": "user", "content": "Reply OK"}],
                "max_tokens": 5
            }
            headers = {
                "Authorization": f"Bearer {sf_key}",
                "Content-Type": "application/json",
            }
            async with asyncio.timeout(25.0):
                async with aiohttp.ClientSession() as session:
                    async with session.post(url, json=payload, headers=headers) as response:
                        if response.status in config.FAIL_FAST_HTTP_CODES:
                            logger.error(f"[LLM] Auth/quota error {response.status} on SiliconFlow — immediate failover")
                            raise LLMFailFastError(response.status, "SiliconFlow")
                        if response.status == 200:
                            primary_ok = True
                            logger.info(f"[NEWS_ANALYST] Model validated: {primary_model}")
                            _models_validated = True
                            return
                        else:
                            primary_err = f"Status {response.status}: {await response.text()}"
        except LLMFailFastError as fail_fast:
            primary_err = f"FailFast: {fail_fast}"
        except asyncio.TimeoutError:
            primary_err = "Timeout after 25 seconds"
        except Exception as e:
            primary_err = f"Exception: {type(e).__name__}: {e}"

    logger.warning(f"[NEWS_ANALYST] Primary model validation failed: {primary_err}")

    # 2. Probe Fallback: NVIDIA NIM
    nv_key = os.environ.get("NVIDIA_API_KEY")
    fallback_ok = False
    fallback_err = "API Key Missing"
    fallback_model = getattr(config, "MODEL_NEWS_ANALYST_FALLBACK", "meta/llama-3.3-70b-instruct")

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
            async with asyncio.timeout(25.0):
                async with aiohttp.ClientSession() as session:
                    async with session.post(url, json=payload, headers=headers) as response:
                        if response.status in config.FAIL_FAST_HTTP_CODES:
                            logger.error(f"[LLM] Auth/quota error {response.status} on NVIDIA — immediate failover")
                            raise LLMFailFastError(response.status, "NVIDIA")
                        if response.status == 200:
                            fallback_ok = True
                            logger.info(f"[NEWS_ANALYST] Model validated: {fallback_model}")
                            _models_validated = True
                            return
                        else:
                            fallback_err = f"Status {response.status}: {await response.text()}"
        except LLMFailFastError as fail_fast:
            fallback_err = f"FailFast: {fail_fast}"
        except asyncio.TimeoutError:
            fallback_err = "Timeout after 25 seconds"
        except Exception as e:
            fallback_err = f"Exception: {type(e).__name__}: {e}"

    logger.warning(f"[NEWS_ANALYST] Fallback model validation failed: {fallback_err}")
    
    # Both failed
    raise RuntimeError(
        f"Model validation failed. Primary ({primary_model}): {primary_err}. Fallback ({fallback_model}): {fallback_err}."
    )

