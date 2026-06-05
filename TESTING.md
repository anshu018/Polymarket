# TESTING.md — Layer Pass/Fail Criteria

# Every criterion must pass before a layer is marked Confirmed in PROGRESS.md

# No exceptions. No partial credit. All criteria = pass. One failure = not done.

---

## HOW TO USE THIS FILE

1. Complete all build work for the layer
2. Run every test listed under that layer
3. Every single criterion must show PASS
4. Only then: update PROGRESS.md from Built → Tested → Confirmed
5. Never proceed to next layer without Confirmed status

Gemini must run these tests explicitly and report each result as PASS or FAIL.
"It should work" is not a test result. Evidence is required for every criterion.

---

## LAYER 1 — FOUNDATION

### All Supabase tables created and verified

[ ] 1.1 — All 8 tables exist in Supabase
Test: Run SELECT table_name FROM information_schema.tables
WHERE table_schema = 'public'
Expected: Returns all 8 table names:
open_positions, closed_trades, market_signals,
daily_performance, agent_memory, resolution_keyword_cache,
idempotency_log, layer_c_category_versions
PASS: All 8 present
FAIL: Any table missing or named differently

[ ] 1.2 — open_positions schema matches PLAN.md Section 9.1 exactly
Test: SELECT column_name, data_type
FROM information_schema.columns
WHERE table_name = 'open_positions'
Expected: All columns present with correct types:
id (uuid), market_id (text), market_question (text),
direction (text), entry_price (numeric),
position_size_usdc (numeric), strategy (text),
agent_estimate (numeric), confidence_at_entry (numeric),
kelly_fraction_used (numeric), category (text),
idempotency_uuid (uuid), opened_at (timestamptz),
last_checked_at (timestamptz)
PASS: All columns present, correct types
FAIL: Any column missing, misnamed, or wrong type

[ ] 1.3 — closed_trades schema matches PLAN.md Section 9.2 exactly
Test: Same method as 1.2 on closed_trades
Expected columns: id, market_id, market_question, direction,
entry_price, exit_price, position_size_usdc, pnl_usdc,
pnl_percent, strategy, agent_estimate, confidence_at_entry,
brier_contribution, category, outcome, exit_reason,
opened_at, closed_at, was_memoryless, notes
PASS: All columns present, correct types
FAIL: Any column missing, misnamed, or wrong type

[ ] 1.4 — agent_memory schema matches PLAN.md Section 9.5 exactly
Test: Same method on agent_memory
Expected columns: id, category, lesson, trigger_condition (jsonb),
severity, confidence_score, relevant_trades_since_last_trigger,
reinforcement_count, recently_validated_at (timestamptz nullable),
retired, created_at, last_triggered_at, superseded_by
PASS: All columns present. trigger_condition is JSONB not TEXT.
recently_validated_at is nullable timestamptz not boolean.
FAIL: trigger_condition is TEXT. recently_validated_at is boolean.
Any column missing.

[ ] 1.5 — resolution_keyword_cache schema matches PLAN.md Section 9.6
Test: Same method on resolution_keyword_cache
Expected columns: id, market_id (unique), market_question,
resolution_keywords (text[]), resolution_conditions (jsonb),
resolution_type, ambiguity_score, cached_at, last_used_at
PASS: market_id has UNIQUE constraint. resolution_keywords is text[].
FAIL: Any column missing. market_id not unique.

[ ] 1.6 — idempotency_log schema matches PLAN.md Section 9.7
Test: Same method on idempotency_log
Expected columns: id (uuid primary key — IS the idempotency UUID),
market_id, direction, intended_size_usdc, status (default pending),
polymarket_order_id, created_at, confirmed_at, failure_reason
PASS: id column IS the idempotency UUID (not a separate auto-increment).
status has DEFAULT 'pending'.
FAIL: Separate auto-increment id added. Status has no default.

[ ] 1.7 — layer_c_category_versions schema matches PLAN.md Section 9.8
Test: Same method on layer_c_category_versions
Expected columns: id, category, avg_resolution_ambiguity_score,
recommended_confidence_threshold, known_resolution_traps (text[]),
historical_edge_percent, notes, valid_from, superseded_by
PASS: superseded_by is foreign key to same table. valid_from has default.
FAIL: superseded_by is plain text. Any column missing.

[ ] 1.8 — market_signals and daily_performance schemas correct
Test: Same method on both tables
Expected market_signals columns: id, raw_headline, source_url, source_name,
category, confidence_score, affected_market_ids (text[]),
event_type, passed_fast_path, action_taken, discard_reason,
detected_at, processed_at
Expected daily_performance columns: id, date (unique), starting_balance_usdc,
ending_balance_usdc, daily_pnl_usdc, daily_pnl_percent,
trades_executed, trades_won, trades_lost, brier_score_rolling,
health_score, circuit_breaker_fires, signals_detected,
signals_traded, created_at
PASS: Both tables complete and correct
FAIL: Any column missing

[ ] 1.9 — Supabase connection succeeds from Python
Test: Run connection test script from Hetzner server (or local dev):
Connect to Supabase, run SELECT 1, confirm response received.
PASS: Connection established, query returns result, no timeout
FAIL: Connection error, timeout, or authentication failure

[ ] 1.10 — All environment variables loaded correctly
Test: Python script confirms all required env vars are set:
SUPABASE_URL, SUPABASE_KEY, POLYMARKET_API_KEY,
OPENROUTER_API_KEY, SILICONFLOW_API_KEY,
TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
PASS: All 7 variables present and non-empty
FAIL: Any variable missing or empty

LAYER 1 CONFIRMED when: All 10 criteria show PASS

---

## LAYER 2 — DATA PIPELINE

### Live data flowing from RSS feeds through spaCy into Supabase

[ ] 2.1 — RSS poller connects to feeds without error
Test: Run RSS poller for 60 seconds. Check logs.
Expected: Feeds polled, zero connection errors logged
Development machine threshold:
  PASS if >= 8 feeds respond
  (Cloudflare/bot blocks expected on non-server IPs. This is not a bug.)
Production Oracle Cloud threshold:
  PASS if >= 15 feeds respond
  (Full threshold applies on server)
How to determine environment:
  Check config.ENVIRONMENT value.
  development = 8 feed minimum
  production = 15 feed minimum

[ ] 2.2 — spaCy pre-filter loads and classifies without error
Test: Pass 10 sample headlines through prefilter.py manually.
Include 3 financial headlines, 3 political, 3 sports, 1 irrelevant.
Expected: Financial and political pass. Irrelevant blocked.
PASS: No exceptions. Returns classification decision for all 10.
FAIL: Exception on any headline. Crashes on empty string.

[ ] 2.3 — Domain allowlist correctly bypasses spaCy classification
Test: Pass these exact terms through prefilter.py:
"FOMC minutes released ahead of schedule"
"NRC ruling on reactor license renewal"
"slip opinion handed down in Circuit Court"
"quantitative tightening pace adjusted"
Expected: All four PASS the filter regardless of spaCy classification
PASS: All four bypassed to next stage via allowlist
FAIL: Any of these blocked by spaCy classification

[ ] 2.4 — Signals reaching Supabase market_signals table
Test: Let pipeline run for 10 minutes. Query market_signals.
SELECT count(\*) FROM market_signals
WHERE detected_at > NOW() - INTERVAL '10 minutes'
Expected: At least 1 signal recorded (more on active news day)
PASS: Count >= 1. Rows contain non-null raw_headline, source_name,
detected_at. action_taken is populated.
FAIL: Count = 0. Null required fields. Rows not being written.

[ ] 2.5 — GDELT is NOT in the fast path or velocity signal flow
Test: Review prefilter.py and rss_poller.py source code.
Search for any GDELT reference in fast-path routing logic.
Expected: GDELT referenced only in full pipeline enrichment, not velocity.
PASS: Zero GDELT calls in fast path code. GDELT only in background enrichment.
FAIL: GDELT in rss_poller.py. GDELT called before News Analyst on any path.

[ ] 2.6 — RSS poller polling interval is 10 seconds or less
Test: Review rss_poller.py. Confirm polling interval setting.
Run for 30 seconds, confirm at least 2 polling cycles logged.
PASS: Interval <= 10 seconds. 2+ cycles visible in logs.
FAIL: Interval > 10 seconds. Single cycle in 30 seconds.

[ ] 2.7 — News Analyst agent returns valid structured output
Test: Pass 5 real headlines to news_analyst.py directly.
Expected output contains: event_category, affected_market_ids (list),
confidence_score (float 0.0-1.0), direction (YES/NO/ABSTAIN)
PASS: All 5 return valid structured JSON. confidence_score between 0 and 1.
No raw prose returned without structure.
FAIL: Returns unstructured text. confidence_score outside 0-1.
Missing any required field.

[ ] 2.8 — News Analyst has correct timeout behavior
Test: Mock the OpenRouter API to delay 11 seconds.
Call news_analyst.py and observe behavior.
Expected: Call times out at 10 seconds. Signal dropped gracefully.
Log entry written. No exception propagates to caller.
PASS: Timeout fires at <= 10 seconds. No crash. Log entry present.
FAIL: Call hangs past 10 seconds. Exception propagates. No log entry.

[ ] 2.9 — Signals with confidence below 0.75 are discarded and logged
Test: Inject a synthetic signal with confidence = 0.60 into the pipeline.
Expected: Signal reaches market_signals table with action_taken = 'discarded'
and discard_reason populated.
PASS: Row exists in market_signals. action_taken = 'discarded'.
discard_reason is not null.
FAIL: Signal passed to next stage. Row not written. action_taken null.

[ ] 2.10 — No API keys appear in any source file
Test: grep -r "sk-" /project/
grep -r "SUPABASE" /project/_.py
grep -r "Bearer " /project/_.py
Expected: Zero matches in .py files. All secrets via os.environ only.
PASS: Zero hardcoded secrets found in any Python file.
FAIL: Any API key, token, or secret found hardcoded in source.

LAYER 2 CONFIRMED when: All 10 criteria show PASS

---

## LAYER 3 — CALIBRATION ENGINE

### Probability calibration model flagging mispriced markets

[ ] 3.1 — Calibration model loads without error
Test: Import calibration module. Initialize model. Confirm no exceptions.
PASS: Model loads. No import errors. No missing dependency errors.
FAIL: Any exception on load or initialization.

[ ] 3.2 — Brier score computation is mathematically correct
Test: Run Brier score function on known inputs:
predictions = [0.9, 0.1, 0.8, 0.2]
outcomes = [1, 0, 1, 0 ]
Expected Brier score = mean([(0.9-1)², (0.1-0)², (0.8-1)², (0.2-0)²])
= mean([0.01, 0.01, 0.04, 0.04])
= 0.025
PASS: Function returns 0.025 (±0.0001)
FAIL: Any other value returned.

[ ] 3.3 — Model flags markets with edge above 7 cents
Test: Feed calibrator synthetic market data:
market_price = 0.45, agent_estimate = 0.58 (edge = 13 cents)
market_price = 0.70, agent_estimate = 0.74 (edge = 4 cents)
Expected: First market flagged as mispriced. Second not flagged.
PASS: market at 13 cent edge flagged. market at 4 cent edge not flagged.
FAIL: Both flagged. Neither flagged. Wrong market flagged.

[ ] 3.4 — Model does not flag markets with edge below 7 cents
Test: Feed 5 markets all with |estimate - market_price| < 0.07
Expected: Zero markets flagged.
PASS: Zero flags returned.
FAIL: Any market flagged. Threshold not enforced.

[ ] 3.5 — Calibration model trained on minimum dataset present
Test: Confirm resolved markets dataset exists.
SELECT count(\*) FROM closed_trades (or confirm flat file exists)
Note: At Layer 3, live data may be limited. Paper data is acceptable.
Minimum 50 paper records required for calibration validation.
PASS: >= 50 resolved market records present for calibration.
FAIL: Fewer than 50 records. Model training on empty dataset.

[ ] 3.6 — Calibration curves exist per category
Test: Confirm calibration model has separate curves for:
politics, crypto, sports, science, legal, economics
Expected: 6 separate calibration curves (or fallback to global curve
if category has < 20 resolved markets — acceptable at this stage)
PASS: Category curves present where data exists. No exceptions thrown
when querying any of the 6 categories.
FAIL: Single global curve only with no category routing.
Exception thrown for any category query.

[ ] 3.7 — Model correctly handles novel event types
Test: Feed market data for a category with no historical data
(simulate by passing category = 'unknown_category')
Expected: Model returns estimate using global fallback curve.
Does NOT throw exception. Does NOT return confidence above 0.50
for unknown category (uncertainty acknowledged).
PASS: Returns estimate <= 0.50 confidence. No exception. Logs fallback used.
FAIL: Exception thrown. Returns high confidence on unknown category.

[ ] 3.8 — Agent estimate combined with market price produces correct edge
Test: Verify edge calculation:
agent_estimate = 0.62, market_price = 0.51
Expected edge = 0.11 (11 cents, flagged as > 7 cents)
agent_estimate = 0.55, market_price = 0.51
Expected edge = 0.04 (4 cents, not flagged)
PASS: Both computed correctly. Flagging threshold applied correctly.
FAIL: Edge computed as percentage not decimal. Threshold wrong.

LAYER 3 CONFIRMED when: All 8 criteria show PASS

---

## LAYER 4 — RISK ENGINE

### Pure Python. Deterministic. Inviolable.

### This is the most critical layer. Every criterion carries equal weight.

[ ] 4.1 — Zero LLM imports in risk_engine.py
Test: cat /risk/risk_engine.py | grep -E "import|from" | head -50
Expected output contains ONLY these imports (any subset acceptable):
import math
import decimal
from decimal import Decimal
import datetime
from datetime import datetime, timezone
import logging
PASS: Zero references to openrouter, anthropic, requests (HTTP),
httpx, aiohttp, or any /llm/ module.
FAIL: Any LLM library import. Any HTTP client import. Any /llm/ import.

[ ] 4.2 — Kelly sizing mathematically correct (velocity strategy)
Test: Call kelly_size() with:
win_probability = 0.65
odds = 1.0 (even odds, Polymarket binary)
kelly_fraction = 0.15 (velocity)
portfolio_value = 10000
Expected:
f_full = (1.0 × 0.65 - 0.35) / 1.0 = 0.30
f_fractional = 0.30 × 0.15 = 0.045
position_size = 0.045 × 10000 = $450
PASS: Function returns $450 (±$1)
FAIL: Any other value. Wrong fraction applied. Formula inverted.

[ ] 4.3 — Kelly sizing mathematically correct (recalibration strategy)
Test: Same as 4.2 but kelly_fraction = 0.25
Expected position_size = 0.30 × 0.25 × 10000 = $750
PASS: Returns $750 (±$1)
FAIL: Any other value.

[ ] 4.4 — 5% single trade hard cap enforced
Test: Call position_size_check() with:
proposed_size = 600
portfolio_value = 10000 (5% = $500)
Expected: Function returns 500, not 600. Cap applied.
Test 2: proposed_size = 400, portfolio_value = 10000
Expected: Function returns 400. No cap needed.
PASS: Both cases return correct values. Cap enforced silently.
FAIL: Returns 600 (cap not enforced). Raises exception instead of capping.

[ ] 4.5 — 8% resolution edge cap enforced correctly
Test: Call position_size_check() with strategy = 'resolution',
proposed_size = 850, portfolio_value = 10000 (8% = $800)
Expected: Returns 800.
Test 2: strategy = 'velocity', proposed_size = 550,
portfolio_value = 10000 (5% = $500)
Expected: Returns 500.
PASS: Resolution edge gets 8% cap. All other strategies get 5% cap.
FAIL: Resolution edge gets 5% cap (wrong). Velocity gets 8% cap (wrong).

[ ] 4.6 — Daily drawdown circuit breaker triggers at exactly 8%
Test: Call check_drawdown() with:
starting_balance = 10000
current_balance = 9199 (8.01% drawdown)
Expected: Returns HALT signal. Logs circuit breaker fire.
Test 2: current_balance = 9201 (7.99% drawdown)
Expected: Returns CONTINUE. No halt.
PASS: HALT at 8.01%. CONTINUE at 7.99%. Exact threshold enforced.
FAIL: HALT at wrong threshold. No distinction between 8.01 and 7.99.

[ ] 4.7 — Weekly drawdown circuit breaker triggers at exactly 15%
Test: check_drawdown() with weekly_drawdown = 0.1501
Expected: HALT
Test 2: weekly_drawdown = 0.1499
Expected: CONTINUE
PASS: Exact threshold enforced.
FAIL: Wrong threshold. Off by more than 0.001.

[ ] 4.8 — Monthly drawdown shutdown triggers at exactly 25%
Test: check_drawdown() with monthly_drawdown = 0.2501
Expected: SHUTDOWN (stronger signal than HALT)
Test 2: monthly_drawdown = 0.2499
Expected: CONTINUE
PASS: SHUTDOWN at 25.01%. SHUTDOWN signal is distinct from HALT signal.
FAIL: SHUTDOWN at wrong threshold. SHUTDOWN and HALT are same signal.

[ ] 4.9 — Minimum liquidity check enforced
Test: check_liquidity() with available_liquidity = 4999
Expected: BLOCK trade.
Test 2: available_liquidity = 5001
Expected: ALLOW trade.
PASS: Block at 4999. Allow at 5001. Exact threshold.
FAIL: Allow at 4999. Block at 5001. Wrong threshold.

[ ] 4.10 — Auto-exit floor enforced
Test: check_liquidity() with current_market_liquidity = 2999
Expected: Returns EXIT_NOW signal.
Test 2: current_market_liquidity = 3001
Expected: Returns HOLD. No exit signal.
PASS: EXIT_NOW at $2,999. HOLD at $3,001.
FAIL: No auto-exit signal returned. Wrong threshold.

[ ] 4.11 — Confidence ceiling enforced at 0.88
Test: apply_confidence_ceiling(0.95)
Expected: Returns 0.88
Test 2: apply_confidence_ceiling(0.82)
Expected: Returns 0.82 (unchanged)
PASS: Values above 0.88 clamped to 0.88. Values below returned unchanged.
FAIL: 0.95 returned unchanged. 0.88 not enforced.

[ ] 4.12 — Minimum confidence gate enforced at 0.75
Test: check_min_confidence(0.74)
Expected: Returns BLOCK
Test 2: check_min_confidence(0.76)
Expected: Returns ALLOW
PASS: Exact threshold enforced.
FAIL: Wrong threshold. 0.74 allowed.

[ ] 4.13 — Minimum edge gate enforced at 7 cents
Test: check_edge(agent_estimate=0.55, market_price=0.49)
edge = 0.06 (6 cents)
Expected: BLOCK
Test 2: agent_estimate=0.55, market_price=0.47
edge = 0.08 (8 cents)
Expected: ALLOW
PASS: 6 cent edge blocked. 8 cent edge allowed.
FAIL: 6 cent edge allowed. Wrong threshold.

[ ] 4.14 — Category exposure cap enforced at 30%
Test: check_category_exposure() with:
current_politics_exposure = 0.28 (28%)
proposed_trade_size = 0.04 (4%) in politics
portfolio = 10000
28% + 4% = 32% > 30%
Expected: BLOCK
Test 2: current_politics_exposure = 0.25, proposed = 0.04 (29% total)
Expected: ALLOW
PASS: Trade blocked at 32% total. Allowed at 29% total.
FAIL: Trade allowed at 32%. Cap not enforced.

[ ] 4.15 — Correlated exposure cap enforced at 20%
Test: check_correlation_exposure() with correlated_exposure = 0.21
Expected: BLOCK new trade
Test 2: correlated_exposure = 0.19
Expected: ALLOW
PASS: Exact threshold enforced. Correlation check documented as mandatory.
FAIL: 21% allowed. Check skippable.

[ ] 4.16 — Health score computed correctly
Test: Call compute_health_score() with:
win_rate_score = 70
brier_score_score = 80
slippage_score = 60
feed_latency_score = 90
drawdown_score = 85
correlation_score = 75
Expected: health_score = mean([70,80,60,90,85,75]) = 76.67
PASS: Returns 76.67 (±0.01)
FAIL: Any other value. Weighted differently than equal weights.

[ ] 4.17 — Health score thresholds trigger correct modes
Test: interpret_health_score(64)
Expected: DEFENSIVE_MODE
Test 2: interpret_health_score(66)
Expected: NORMAL
Test 3: interpret_health_score(39)
Expected: FULL_HALT
Test 4: interpret_health_score(41)
Expected: DEFENSIVE_MODE
PASS: All four return correct mode.
FAIL: Any threshold off. Modes not distinguished.

[ ] 4.18 — All risk_engine.py functions execute in under 1ms
Test: Time each exported function with timeit (100 iterations each).
Expected: Every function p99 latency < 1ms (1000 microseconds)
PASS: All functions under 1ms at p99.
FAIL: Any function exceeds 1ms at p99.

[ ] 4.19 — Every function in risk_engine.py has a unit test
Test: Count exported functions in risk_engine.py.
Count test functions in /tests/test_risk.py.
Expected: test count >= function count. Every function testable in isolation.
PASS: All functions covered. Tests runnable with pytest.
FAIL: Any function untested. Test file missing.

LAYER 4 CONFIRMED when: All 19 criteria show PASS

---

## LAYER 5 — CONTRACT PARSER

### DeepSeek V3 parsing real Polymarket resolution criteria

[ ] 5.1 — Contract Parser returns valid structured JSON on all 10 test contracts
Test: Run contract_parser.py on 10 real Polymarket markets.
Use markets from: politics (3), crypto (2), sports (2),
legal (2), economics (1).
Expected output for each:
{
"resolution_source": string (non-empty),
"resolution_condition": string (non-empty),
"key_entities": [string] (at least 1),
"resolution_keywords": [string] (at least 3),
"ambiguity_score": float between 0.0 and 1.0,
"resolution_type": string (non-empty)
}
PASS: All 10 return valid JSON matching this schema.
Zero raw prose responses. Zero null fields.
FAIL: Any response is raw prose. Any required field null or missing.
JSON parse error on any response.

[ ] 5.2 — resolution_keywords are meaningful and specific
Test: Review keywords from 5.2.1 manually.
Example: "Will the Fed cut rates at the May 2026 meeting?"
Expected keywords include: ["Federal Reserve", "rate cut",
"May 2026", "FOMC", "basis points"] or similar specific terms.
PASS: Keywords are specific to the market. Not generic words like
"the", "will", "market", "yes", "no".
FAIL: Generic stopwords in keyword list. Keywords identical across
unrelated markets.

[ ] 5.3 — ambiguity_score is calibrated reasonably
Test: Compare ambiguity scores across 3 market types:
Clear market: "Will BTC close above $100k on Dec 31 2026
according to Coinbase?" (expect low score < 0.3)
Ambiguous: "Will the economy be in recession by Q3 2026?"
(expect high score > 0.6)
PASS: Clear market scores lower than ambiguous market.
Scores are between 0.0 and 1.0.
FAIL: Both markets score the same. Score outside 0-1 range.

[ ] 5.4 — Parsed results saved to resolution_keyword_cache correctly
Test: After parsing 10 contracts, query resolution_keyword_cache.
SELECT count(\*) FROM resolution_keyword_cache
Expected: 10 rows. Each row has market_id, resolution_keywords (array),
cached_at timestamp, ambiguity_score.
PASS: 10 rows present. All required fields populated. cached_at is recent.
FAIL: Fewer than 10 rows. Null required fields. cached_at is null.

[ ] 5.5 — Contract Parser is NOT called on markets already in cache
Test: Parse 5 markets. Then trigger parsing for same 5 markets again.
Expected: Second run makes zero DeepSeek API calls.
Reads from resolution_keyword_cache instead.
PASS: Zero API calls on second run. Cache hit logged.
FAIL: API called again for cached markets. Cache not checked.

[ ] 5.6 — Cache entries older than 24 hours trigger refresh
Test: Manually set cached_at = NOW() - INTERVAL '25 hours'
on one cache entry. Trigger pipeline for that market.
Expected: Contract Parser called again for stale market.
New entry written. Old entry updated or replaced.
PASS: API call made. New cached_at timestamp written.
FAIL: Stale entry used without refresh. API not called.

[ ] 5.7 — Contract Parser has 18-second timeout
Test: Mock OpenRouter API to delay 19 seconds.
Call contract_parser.py and observe behavior.
Expected: Times out at 18 seconds. Exception caught gracefully.
Log entry written with timeout noted. No crash.
PASS: Timeout at <= 18 seconds. Graceful handling. Log entry present.
FAIL: Hangs past 18 seconds. Unhandled exception. No log.

[ ] 5.8 — Contract Parser output triggers fast path correctly
Test: After parsing a market, simulate fast path routing for that market.
Inject signal with entities that match resolution_keywords.
Expected: Fast path proceeds for this market (cache hit + keyword match).
Test 2: Inject signal with entities that do NOT match keywords.
Expected: Market excluded from fast path for this signal.
PASS: Correct routing in both cases.
FAIL: Fast path fires on keyword mismatch. Fast path blocked on keyword match.

LAYER 5 CONFIRMED when: All 8 criteria show PASS

---

## LAYER 6 — INTEGRATION

### All layers communicating correctly end to end

[ ] 6.1 — Full pipeline runs end to end without exception
Test: Inject a synthetic high-confidence signal into the pipeline.
Let it run through: spaCy → News Analyst → Contract Parser →
Trade Decision → risk_engine.py → Python coordinator.
Use paper trading mode (no real order submission).
Expected: Pipeline completes. Decision logged to market_signals.
No unhandled exceptions at any stage.
PASS: Full run completes. Decision recorded. No crashes.
FAIL: Exception at any stage. Pipeline stalls. No decision recorded.

[ ] 6.2 — Fast path runs end to end in under 5 seconds
Test: Inject signal with confidence = 0.91, pre-validated category,
market with fresh cache entry (cached_at < 1 hour).
Time from signal injection to decision output.
Expected: Total time < 5 seconds.
PASS: Confirmed < 5 seconds. Fast path routing logged.
FAIL: Exceeds 5 seconds. Full pipeline used instead of fast path.

[ ] 6.3 — Full pipeline runs in under 22 seconds
Test: Inject signal requiring full pipeline (confidence = 0.80).
Time from signal injection to decision output.
Expected: Total time < 22 seconds (target 17-20, hard limit 22).
PASS: Confirmed < 22 seconds.
FAIL: Exceeds 22 seconds. Latency regression identified.

[ ] 6.4 — agent_memory lessons prepended on every Trade Decision call
Test: Insert 2 test lessons into agent_memory for category 'politics'
with trigger_condition matching the test signal.
Run full pipeline with politics signal.
Inspect prompt sent to Trade Decision Agent.
Expected: Prompt contains warning block with both lessons at the top.
Prompt structured as: [lessons block] then [market context].
PASS: Both lessons present in prompt. Placed before market context.
FAIL: Lessons absent from prompt. Lessons placed after market context.

[ ] 6.5 — Conflict detection triggers LLM coordinator correctly
Test: Mock News Analyst to return direction=YES, confidence=0.80.
Mock Trade Decision Agent to return direction=NO.
Run coordinator.
Expected: LLM Coordinator triggered (disagreement + confidence > 0.70).
Test 2: Mock News Analyst direction=YES, confidence=0.60.
Mock Trade Decision direction=NO.
Expected: Trade Decision Agent wins. LLM Coordinator NOT triggered.
PASS: LLM triggered in case 1. Not triggered in case 2.
FAIL: LLM triggered when it shouldn't be. Not triggered when it should.

[ ] 6.6 — risk_engine.py called on every trade, both paths
Test: Run fast path trade. Confirm risk_engine.py log entry present.
Test 2: Run full pipeline trade. Confirm risk_engine.py log entry present.
Expected: risk_engine.py logs show it was called in both runs.
PASS: Log entries present for both. Risk check not skipped on fast path.
FAIL: Risk check absent on fast path. Log entries missing.

[ ] 6.7 — Circuit breaker halts pipeline correctly
Test: Set daily_drawdown to 9% in risk_engine.py state.
Inject new signal into pipeline.
Expected: Pipeline blocked at risk_engine.py stage.
Signal logged as discarded (reason: circuit_breaker).
Telegram alert would fire (verify log, not actual Telegram at this stage).
PASS: Pipeline blocked. Discard reason logged. No order attempted.
FAIL: Pipeline continues past circuit breaker. No block.

[ ] 6.8 — Idempotency check fires before order submission in both paths
Test: Run integration test with paper order. Check logs.
Expected: Logs show UUID generated BEFORE order submission.
idempotency_log shows 'pending' entry created before submission.
Test 2: Manually insert UUID with status='confirmed' for a market.
Trigger same trade again.
Expected: Second submission blocked. Log shows "UUID already confirmed".
PASS: Both cases correct. UUID always precedes submission.
FAIL: UUID written after submission. Duplicate submission not blocked.

[ ] 6.9 — Supabase timeout fallbacks work under simulated failure
Test: Mock Supabase to timeout after 2.5 seconds.
Run full pipeline.
Expected for each dependency:
resolution_keyword_cache timeout → falls back to full pipeline
agent_memory timeout → trade proceeds, was_memoryless = true in log
idempotency timeout → order NOT submitted, Telegram alert logged
PASS: All three fallbacks behave as specified.
FAIL: Any fallback panics, crashes, or behaves incorrectly.

[ ] 6.10 — SiliconFlow failover to OpenRouter works
Test: Mock SiliconFlow to delay 19 seconds (beyond 18s timeout).
Run Trade Decision Agent call.
Expected: Call cancelled at 18 seconds.
Immediately retried on OpenRouter.
OpenRouter response used. Fallback logged.
Total latency < 18 + 15 = 33 seconds.
PASS: Failover fires at 18s. OpenRouter used. Logged correctly.
FAIL: Hangs past 18 seconds. No failover. Crash on timeout.

LAYER 6 CONFIRMED when: All 10 criteria show PASS

---

## LAYER 7 — DEPLOYMENT

### Hetzner deployment, Telegram alerts, startup reconciliation

[ ] 7.1 — Agent process running continuously on Hetzner CX22
Test: SSH into Hetzner. Check process status.
systemctl status polymarket-agent (or equivalent)
Expected: Process active and running. Start time > 30 minutes ago.
No recent restart loops.
PASS: Active status. Stable runtime. No crash loops.
FAIL: Process inactive. Repeated restarts. Not found.

[ ] 7.2 — Startup reconciliation completes before any trade logic
Test: Restart agent process. Monitor logs in real time.
Expected log sequence (exact order): 1. "[RECONCILIATION] Starting startup reconciliation" 2. "[RECONCILIATION] Fetching Polymarket positions..." 3. "[RECONCILIATION] Fetching USDC balance..." 4. "[RECONCILIATION] Diffing against Supabase state..." 5. "[RECONCILIATION] Reconciliation complete. State authoritative." 6. "[AGENT] Beginning signal processing."
Signal processing must NOT appear before step 5.
PASS: Exact sequence. Signal processing only after reconciliation complete.
FAIL: Signal processing starts before reconciliation. Steps out of order.

[ ] 7.3 — Reconciliation halts correctly on Polymarket API unavailability
Test: Block Polymarket API access (firewall rule or mock).
Restart agent. Monitor logs.
Expected: Agent halts at reconciliation. Logs show retry every 60 seconds.
Telegram alert fires after 5 minutes of unavailability.
Agent does NOT begin signal processing.
PASS: Halt confirmed. 60-second retry confirmed. No trade logic runs.
FAIL: Agent starts trading without reconciliation. No retry. No halt.

[ ] 7.4 — Reconciliation halts on unresolvable inconsistency
Test: Manually create inconsistency:
Insert open_position in Supabase for a market_id that
Polymarket API has no record of.
Restart agent.
Expected: Reconciliation detects inconsistency.
Halts. Does NOT proceed.
Logs exact inconsistency details.
Telegram alert fires with inconsistency description.
PASS: Halt confirmed. Inconsistency logged with specific detail.
Telegram alert message contains market_id and nature of conflict.
FAIL: Agent proceeds despite inconsistency. No halt. No alert.

[ ] 7.5 — All Telegram alerts fire correctly
Test: Trigger each alert condition manually and verify Telegram message:
7.5.1 — Daily drawdown > 8%: trigger circuit breaker in test mode
7.5.2 — Health score < 65: set health_score = 60 in test
7.5.3 — Health score < 40: set health_score = 35 in test
7.5.4 — SiliconFlow failover: mock 19s delay
7.5.5 — Both providers down: mock both unavailable
7.5.6 — Idempotency fail closed: mock Supabase timeout on idempotency
7.5.7 — Agent restart: restart process
Expected: Telegram message received within 30 seconds for each.
Message format: [ZERO-ALPHA] {severity} | {trigger} | {timestamp}
PASS: All 7 alerts received. Correct format. Within 30 seconds.
FAIL: Any alert missing. Wrong format. Delayed > 30 seconds.

[ ] 7.6 — .geminiignore is present and correctly configured
Test: cat .geminiignore
Expected contents include:
node_modules/
.git/
**pycache**/
\*.pyc
.env
PASS: File exists. All 5 entries present.
FAIL: File missing. Any entry absent.

[ ] 7.7 — No secrets in environment on Hetzner
Test: env | grep -E "SUPABASE|OPENROUTER|SILICONFLOW|TELEGRAM|POLYMARKET"
on Hetzner server
Expected: All environment variables set.
Test 2: grep -r "sk-" /project/_.py
grep -r "API_KEY" /project/_.py (looking for hardcoded values)
Expected: Zero hardcoded secrets in any Python file.
PASS: All 7 env vars set on Hetzner. Zero hardcoded secrets in source.
FAIL: Any env var missing. Any hardcoded secret found.

[ ] 7.8 — Agent restarts automatically after crash
Test: Kill agent process manually (kill -9 PID).
Wait 60 seconds. Check process status.
Expected: Process restarts automatically within 60 seconds.
Startup reconciliation runs on restart (verified in logs).
PASS: Auto-restart confirmed. Reconciliation runs after restart.
FAIL: Process stays dead. Manual restart required. No reconciliation on restart.

[ ] 7.9 — Logs are being written with correct format
Test: tail -n 50 /var/log/polymarket-agent.log (or equivalent)
Expected: Every log entry contains:
timestamp (ISO 8601), component name, log level, message
Example: "2026-03-28T14:23:11Z [RISK_ENGINE] INFO Kelly size: $450"
PASS: All entries follow format. No print() output visible.
No bare exceptions logged without context.
FAIL: print() statements in logs. Missing timestamps. Missing component name.

[ ] 7.10 — Paper trading mode confirmed active before live deployment
Test: Confirm PAPER_TRADING = true in environment or config.
Confirm no real orders submitted to Polymarket during Layer 7 testing.
Check Polymarket wallet — zero new transactions from agent.
PASS: Paper mode confirmed active. Wallet unchanged during testing.
FAIL: Live orders submitted during Layer 7 testing. Paper mode not set.

LAYER 7 CONFIRMED when: All 10 criteria show PASS

---

## PAPER TRADING GATE — BEFORE LIVE DEPLOYMENT

All Layer 7 criteria confirmed PLUS all of the following:

[ ] PT.1 — Minimum 2 weeks of paper trading completed
[ ] PT.2 — Minimum 20 resolved paper trades logged in closed_trades
[ ] PT.3 — Brier score < 0.23 across all resolved paper trades
[ ] PT.4 — Zero circuit breaker fires caused by code logic errors
(market condition fires are acceptable, code errors are not)
[ ] PT.5 — Zero unexpected failure modes observed
[ ] PT.6 — Startup reconciliation completed successfully on every restart
during paper trading period
[ ] PT.7 — Fast path latency confirmed < 5 seconds on real signals
[ ] PT.8 — Full pipeline latency confirmed < 22 seconds on real signals
[ ] PT.9 — Idempotency verified: no duplicate orders under any condition
[ ] PT.10 — Supabase fallback behaviors verified under real timeout conditions

If ANY paper trading criterion fails: extend paper trading period.
Do NOT deploy live capital until all PT criteria pass.
These criteria are not negotiable.

---

## TOTAL CRITERIA COUNT

Layer 1: 10 criteria
Layer 2: 10 criteria
Layer 3: 8 criteria
Layer 4: 19 criteria (most critical layer)
Layer 5: 8 criteria
Layer 6: 10 criteria
Layer 7: 10 criteria
Paper: 10 criteria

TOTAL: 85 explicit pass/fail criteria before live deployment.

---

END OF TESTING.md
All criteria derived from architecture decisions in PLAN.md.
No criterion is optional. No criterion is subjective.
