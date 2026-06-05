# Phase 5: Contract Parser — Research

**Status:** Research Complete
**Target:** DeepSeek V3 Integration and 24-hour Caching Cache

---

## 1. DeepSeek V3 & OpenRouter payload
- **Endpoint:** `https://openrouter.ai/api/v1/chat/completions`
- **Model name:** `deepseek/deepseek-chat` (DeepSeek V3) or custom OpenRouter model string.
- **Parameters:**
  - `response_format`: `{"type": "json_object"}` to enforce structured JSON output.
  - `temperature`: `0.0` to ensure deterministic, highly analytical parsing of resolution criteria.
- **Prompt Design:**
  - System prompt must explicitly define the 6 output fields (`resolution_source`, `resolution_condition`, `key_entities`, `resolution_keywords`, `ambiguity_score`, `resolution_type`).
  - Instruct the model to strip out punctuation and stopwords (like `"the"`, `"will"`, `"yes"`, `"no"`) from the `resolution_keywords` list, keeping only highly specific, high-entropy nouns and entities.

---

## 2. Supabase Cache Querying & TTL
- **Upsert Operation:** Use Supabase's `upsert` with the unique constraint on `market_id` to either write a new cache entry or update the existing one on discovery.
- **TTL Calculation:** 
  - To check if a cached entry is valid (not stale), run:
    ```python
    # Check if entry is older than 24 hours
    from datetime import datetime, timezone, timedelta
    is_valid = (datetime.now(timezone.utc) - cached_at) < timedelta(hours=24)
    ```
- **Timeout Wrapper:** Wrap Supabase database operations with `asyncio.wait_for` (2 seconds):
  ```python
  try:
      result = await asyncio.wait_for(
          supabase.table("resolution_keyword_cache")
          .select("*")
          .eq("market_id", market_id)
          .execute(),
          timeout=2.0
      )
  except asyncio.TimeoutError:
      # Fallback: log timeout and return None (triggers full pipeline)
  ```

---

## 3. Validation Architecture (Nyquist Compliance)
To ensure the parser is fully robust and error-free (invincible bot standards):
1. **JSON Verification:** The parser module must run a validation check on the DeepSeek response. If it contains missing keys or malformed JSON, it must raise a local error (or retry) rather than saving broken data to the cache.
2. **Double-Call Prevention Test:** Unit tests must mock the OpenRouter API and assert that querying the same market twice triggers exactly ONE API call (verifying the cache hit path).
3. **Staleness Expiry Test:** Unit tests must seed a mock entry with `cached_at = NOW - 25 hours` and verify that the pipeline correctly bypasses it and triggers a new parse.
