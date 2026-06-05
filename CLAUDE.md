# CLAUDE.md — Architecture Context for Code Review

# This file is pasted into Claude alongside code for audit purposes.

# Primary use: Layer 4 (risk_engine.py) and Layer 7 (deployment) review.

# Claude: read this entire file before reviewing any code. Then review.

---

## WHAT THIS SYSTEM IS

Autonomous Polymarket prediction market trading agent running 24/7.
Stack: Python, Supabase (Postgres), Polygon blockchain, Hetzner CX22,
Telegram alerts, OpenRouter + SiliconFlow LLM APIs.
Monthly LLM budget: $1.17. Monthly infra: $6.00. Student-owned project.

---

## THE SINGLE MOST IMPORTANT RULE IN THIS CODEBASE

/risk/risk_engine.py is pure Python. Always. Non-negotiable.

Zero LLM calls. Zero HTTP calls. Zero external API calls.
Zero imports from /llm/. Zero imports from requests, httpx, aiohttp,
openai, anthropic, openrouter, or any network library.

Permitted imports in risk_engine.py (and only these):
import math
import decimal
from decimal import Decimal
import datetime
from datetime import datetime, timezone
import logging

Every function in risk_engine.py must be:

- Deterministic (same input always produces same output)
- Executable in under 1ms
- Independently unit testable
- Incapable of being overridden by any other component's reasoning

If you see ANY import in risk_engine.py that is not on the list above:
flag it immediately as a critical violation. Do not suggest keeping it.

---

## FULL AI MODEL STACK

News Analyst:
Model: Qwen3-32B | Provider: OpenRouter | Cost: $0.02/M
Latency: ~2s | Timeout: 10s (no fallback, signal dropped on timeout)
File: /llm/news_analyst.py
Output: event_category, affected_market_ids[], confidence_score, direction

Contract Parser:
Model: DeepSeek V3 | Provider: OpenRouter | Cost: ~$0.40/month
Latency: ~3s | Timeout: 18s | NOT in hot path
Runs: once per market discovery, populates resolution_keyword_cache
File: /llm/contract_parser.py
Output: resolution_source, resolution_condition, key_entities[],
resolution_keywords[], ambiguity_score, resolution_type

Trade Decision Agent:
Model: Qwen3-235B-A22B
Provider: SiliconFlow (primary) → OpenRouter (failover at exactly 18s)
Cost: ~$0.70/month | Latency: ~12s primary / ~15s fallover
File: /llm/trade_decision.py
Hard limits (non-negotiable):
max_tokens: 900
thinking_budget: 600
Timeout: 18 seconds → cancel → immediate OpenRouter retry (same prompt)
Confidence output clamped to 0.88 ceiling (risk_engine enforces this)
agent_memory lessons must be prepended to every call

Risk Manager:
Model: NONE — pure Python only
File: /risk/risk_engine.py
Latency: < 1ms | Cost: $0
(See critical rule above)

Coordinator:
Primary: Python weighted aggregation (< 1ms)
Escalation: LLM call only when conflict detected
File: /llm/coordinator.py + /coordinator/aggregator.py
Conflict condition (exact — encoded as Python conditional):
if (news_analyst_direction != trade_decision_direction
and news_analyst_confidence > 0.70):
→ LLM Coordinator
elif news_analyst_confidence <= 0.70:
→ Trade Decision Agent wins, no escalation
else: # both agree
→ Python weighted aggregation only

---

## ROUTING LOGIC

Fast path (target < 5 seconds):
Triggers when ALL of: - News Analyst confidence > 0.87 - Category is pre-validated - resolution_keyword_cache hit with cached_at < 24 hours - Signal entities match resolution keywords (microsecond cache lookup)
Skips: Contract Parser, Trade Decision Agent, LLM Coordinator
Never skips: risk_engine.py (always runs on every path)

Full pipeline (target 17-20s, hard limit 22s):
All other cases
Order: spaCy → News Analyst → Contract Parser → Trade Decision →
risk_engine.py → Python coordinator → (LLM if conflict)

---

## ALL HARD THRESHOLDS

Memorize these. Flag any code that uses different values.

Min confidence to trade: 0.75
Fast path confidence threshold: 0.87
Confidence hard ceiling: 0.88 (clamp, never allow higher)
Min edge to trade: 0.07 (7 cents)
Resolution cache TTL: 24 hours
Max single trade (standard): 5% of portfolio
Max single trade (resolution edge): 8% of portfolio
Max category exposure: 30% of portfolio
Max correlated exposure: 20% of portfolio
Min market liquidity to enter: $5,000
Auto-exit liquidity floor: $3,000 (exit at market, no exceptions)
Daily drawdown halt: 8% (> 8% = halt new trades)
Weekly drawdown halt: 15%
Monthly drawdown shutdown: 25%
Health score defensive mode: < 65
Health score full halt: < 40
Strategy probation edge threshold: < 4 cents over 20 consecutive trades
Supabase read timeout: 2 seconds
Trade Decision Agent timeout: 18 seconds → immediate failover
SiliconFlow health check interval: 5 minutes
Telegram alert delay for API down: 5 minutes

Kelly fractions by strategy:
Velocity trades: 0.15
Recalibration: 0.25
Correlation arb: 0.25
Resolution edge: 0.35

Kelly formula: f = (b × p - q) / b
Where: b = odds, p = win probability, q = (1 - p)
Applied as: position_size = (f × kelly_fraction) × portfolio_value

---

## SUPABASE TABLES (ALL 8)

open_positions live trades
closed_trades resolved trades, permanent (never modified)
market_signals every detected signal, action taken
daily_performance daily P&L, health_score, brier_score_rolling
agent_memory episodic lessons injected into LLM prompts
resolution_keyword_cache parsed contract conditions, 24h TTL
idempotency_log UUID per order, prevents duplicate submission
layer_c_category_versions versioned strategic category patterns

Critical schema details:
agent_memory.trigger_condition: JSONB (not TEXT)
agent_memory.recently_validated_at: TIMESTAMPTZ nullable (not boolean)
idempotency_log.id: IS the idempotency UUID (not a separate column)
idempotency_log.status: DEFAULT 'pending'
resolution_keyword_cache.market_id: UNIQUE constraint required
layer_c_category_versions.superseded_by: FK to same table

---

## SUPABASE DEGRADATION POLICY

Every Supabase read has a 2-second timeout. Hardcoded fallbacks:
resolution_keyword_cache timeout → skip fast path, use full pipeline
agent_memory timeout → proceed, set was_memoryless = true
idempotency_log timeout → FAIL CLOSED, do not submit order,
alert Telegram
open_positions timeout → halt new trades until read succeeds
layer_c_category_versions timeout→ use hardcoded conservative defaults

These fallbacks must be explicit try/except blocks.
"It probably won't time out" is not an acceptable substitute.

---

## IDEMPOTENCY — NEVER SKIPPED

Every order submission:

1. Generate UUID at trade decision time
2. Write UUID to idempotency_log with status='pending' BEFORE API call
3. On any retry: query idempotency_log first
4. If status = 'confirmed': do NOT resubmit under any circumstances
5. If Supabase unavailable for this check: FAIL CLOSED
   Do not submit. Alert Telegram. Stop.

Flag any code path that submits an order without first checking idempotency.
Flag any code that retries without querying Supabase first.

---

## STARTUP RECONCILIATION — MANDATORY

Runs on every process start. No exceptions. No skipping.
Steps must execute in this exact order:

1. Fetch actual open positions from Polymarket API
2. Fetch actual USDC wallet balance from Polymarket API
3. Fetch known state from Supabase open_positions
4. Diff actual vs known
5. Write authoritative reconciled state to Supabase
6. If unresolvable inconsistency: HALT, alert Telegram, wait for human

Signal processing must not begin until step 5 completes successfully.
If Polymarket API unavailable: halt, retry every 60 seconds, never skip.

Flag any code where signal processing can begin before reconciliation completes.
Flag any code that catches reconciliation errors and continues anyway.

---

## TELEGRAM ALERT TRIGGERS (COMPLETE LIST)

Alerts must fire immediately on:

- Daily drawdown > 8%
- Weekly drawdown > 15%
- Monthly drawdown > 25%
- Health score < 65 (WARNING)
- Health score < 40 (CRITICAL)
- Startup reconciliation halt (unresolvable inconsistency)
- Polymarket API unavailable > 5 minutes during startup
- Supabase idempotency check unavailable (fail closed triggered)
- SiliconFlow failover to OpenRouter triggered
- Both SiliconFlow AND OpenRouter unavailable
- Strategy entering probation
- Strategy suspended
- Regime change detected
- Weekly Brier score > 0.23
- Agent process restart (every restart, always)

Message format: [ZERO-ALPHA] {severity} | {trigger} | {timestamp} | {detail}
Severity levels: INFO | WARNING | CRITICAL

Flag any alert condition from this list that has no corresponding Telegram call.

---

## MEMORY SYSTEM (FOR AUDIT CONTEXT)

Layer B (agent_memory) injection rule:
Before every Trade Decision Agent call and every Contract Parser call:
Query agent_memory WHERE category matches AND retired = false
Using structured JSON WHERE clauses on trigger_condition JSONB fields
Prepend results as warning block at TOP of system prompt
Maximum 3-5 lessons returned (structured query prevents bloat)

Decay rule: evidence-based
relevant_trades_since_last_trigger hits 20 → one decay step (−0.10)
recently_validated_at within 90 days → decay rate halved
confidence_score < 0.30 → retired = true (never deleted)

Layer C (layer_c_category_versions) read:
Always: SELECT WHERE category = $1 AND superseded_by IS NULL
ORDER BY valid_from DESC LIMIT 1

---

## WHAT TO FLAG IN CODE REVIEW

Flag immediately (critical violations):

- Any import in risk_engine.py not on the permitted list
- Any LLM call, HTTP call, or external API call in /risk/
- Any order submission without prior idempotency check
- Any signal processing before startup reconciliation completes
- Any Supabase read without a timeout wrapper
- Any Trade Decision call without max_tokens=900 and 18s timeout
- Any confidence value above 0.88 passed to execution layer
- Any threshold that differs from the values in this document
- Any bare except clause that silently swallows errors
- Any hardcoded API key, token, or secret in source code
- Any retry logic that doesn't check idempotency_log first
- GDELT referenced in fast path or velocity signal routing
- risk_engine.py called conditionally (it must be called always)

Flag as important (non-critical but must fix):

- Missing type hints on any function signature
- Missing docstring on any function
- print() used instead of logging module
- Log entry missing timestamp or component name
- Function in risk_engine.py without corresponding unit test
- LLM call missing fallback on timeout
- Supabase read missing hardcoded fallback behavior

---

## LAYER-SPECIFIC AUDIT FOCUS

### When auditing Layer 4 (risk_engine.py):

Primary question: Is this pure Python with zero external dependencies?
Check every import. Check every function for network calls.
Verify Kelly formula implementation mathematically.
Verify every threshold matches values in this document exactly.
Verify every function has a unit test.
Verify every function executes deterministically.
Time concern: every function must complete in < 1ms.

### When auditing Layer 7 (deployment):

Primary question: Does startup reconciliation run before anything else?
Check process startup sequence in logs.
Verify all Telegram alerts are wired to correct triggers.
Verify auto-restart is configured.
Verify paper trading mode is active before live capital.
Verify no secrets are hardcoded.
Verify .geminiignore is present with correct entries.

---

## PAPER TRADING GATE (DO NOT REMOVE FROM CONTEXT)

Live capital deployment requires ALL of:

- Minimum 2 weeks paper trading
- Minimum 20 resolved paper trades
- Brier score < 0.23 across all resolved paper trades
- Zero circuit breaker fires from code logic errors
- Zero unexpected failure modes
- Fast path confirmed < 5 seconds on real signals
- Full pipeline confirmed < 22 seconds on real signals
- Idempotency verified under simulated timeout conditions

---

RULE: .env IS PERMANENTLY OFF-LIMITS TO ALL TEST FILES AND SCRIPTS.
Tests use .env.test only. load_dotenv() must always be called with
dotenv_path=".env.test" and override=True, before any project import.
Violation = stop the session immediately and report.

---

END OF CLAUDE.md
This file is self-contained. No other files needed for code review context.
