# Phase 5: Contract Parser — Context

**Gathered:** 2026-05-27
**Status:** Ready for planning
**Source:** System Architecture Alignment

<domain>
## Phase Boundary

Build and verify the **Contract Parser Agent** (Layer 5) which parses Polymarket resolution criteria on first market discovery into structured JSON rules and caches them for 24 hours to accelerate the fast-path routing of subsequent signals.

</domain>

<decisions>
## Implementation Decisions

### 1. Model & Provider
- **Model:** DeepSeek V3
- **Provider:** OpenRouter
- **Cost Target:** ~$0.40/month
- **Latency Target:** ~3 seconds
- **Role:** Non-hot-path background parsing (runs once per market discovery, not per trade).

### 2. Output Schema
Every parser execution must return a JSON object with this exact schema (enforced via Pydantic or direct JSON validation):
```json
{
  "resolution_source": "string",
  "resolution_condition": "string",
  "key_entities": ["string"],
  "resolution_keywords": ["string"],
  "ambiguity_score": 0.0,
  "resolution_type": "string"
}
```

### 3. Database Cache (resolution_keyword_cache)
- **Table name:** `resolution_keyword_cache`
- **Columns:**
  - `id` (uuid primary key)
  - `market_id` (text unique constraint)
  - `market_question` (text)
  - `resolution_keywords` (text[])
  - `resolution_conditions` (jsonb)
  - `resolution_type` (text)
  - `ambiguity_score` (decimal)
  - `cached_at` (timestamptz default now)
  - `last_used_at` (timestamptz)
- **Degradation / Timeout Policy:** Synchronous database read/write with a 2-second timeout wrapper. If reading times out, skip fast path and use the full pipeline.

### 4. Cache TTL & Staleness Rules
- **TTL Duration:** 24 hours.
- **Stale Entries:** Entries older than 24 hours (tested via `cached_at > NOW() - INTERVAL '24 hours'`) must NOT be used by the fast path. The signal will fall back to the full pipeline and trigger a silent background cache refresh.

### 5. Execution Wrapper & Timeout
- **Timeout:** Hard 18-second timeout on the DeepSeek V3 API call.
- **Graceful Error Handling:** If the call times out or fails, catch the exception, log the timeout gracefully, and do NOT propagate the exception to the caller.

### Claude's Discretion
- **Logging:** All actions must log with timestamps, component name (`[CONTRACT_PARSER]`), and log level. No `print()` statements.
- **API Call wrapping:** Native implementation in `/llm/contract_parser.py` using standard OpenRouter client settings and dotenv loading.
- **Test File:** Unit tests in `/tests/test_parser.py` (or similar) mocking out API responses to verify caching and parsing logic.

</decisions>

<specifics>
## Specific Ideas
- Keywords like `"Federal Reserve"`, `"rate cut"`, `"basis points"` must be extracted cleanly. stopwords and generic words like `"the"`, `"will"`, `"yes"`, `"no"` must be automatically stripped out.

</specifics>

<deferred>
## Deferred Ideas
- Cross-market Bayesian networks are deferred to Milestone 2 (Correlation Arbitrage strategy).

</deferred>

---
*Phase: 05-contract-parser*
*Context gathered: 2026-05-27 via System Architecture Alignment*
