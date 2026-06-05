# PLAN.md — Polymarket Autonomous Trading Agent

# Complete Architecture Reference

# Last updated: after final architecture review (all 7 components cleared)

# Status: PRODUCTION READY — cleared for paper trading then live deployment

---

## TABLE OF CONTENTS

1. Project Overview
2. Seven Build Layers
3. AI Model Stack
4. Data Pipeline
5. Strategy Stack
6. Risk Engine (Pure Python)
7. Execution Layer
8. Memory Architecture
9. Supabase Schema (All Tables)
10. Latency Targets
11. Cost Budget
12. Circuit Breakers and Thresholds
13. Failover and Degradation Policies
14. Startup Reconciliation
15. Agent Decision Flow
16. Conflict Resolution Logic
17. Weekly Learning Cycle
18. Telegram Alert Triggers
19. Health Score System
20. Paper Trading Success Criteria
21. Scaling Plan

---

## 1. PROJECT OVERVIEW

An autonomous AI agent that trades on Polymarket (prediction market platform)
24/7. It monitors open markets, detects mispriced probabilities using a
multi-layer data and AI stack, executes trades with deterministic risk controls,
and improves its own performance through a structured memory and learning system.

Infrastructure:

- Server: Hetzner CX22 (~$6/month)
- Database: Supabase (free tier)
- Blockchain: Polygon (Polymarket's CLOB)
- Alerts: Telegram bot
- Total monthly LLM cost: ~$1.17/month
- Total monthly infrastructure: ~$6/month

Operator: student budget, maximum $5/month LLM spend.

---

## 2. SEVEN BUILD LAYERS

Layer 1 — Foundation
Purpose: All Supabase tables created, connections verified.
Done when: Every table listed in Section 9 exists and connection test passes.

Layer 2 — Data Pipeline
Purpose: Live data flowing from RSS feeds, spaCy pre-filter, GDELT enrichment.
Done when: Live signals confirmed flowing into market_signals table.

Layer 3 — Calibration Engine
Purpose: Probability calibration model flagging mispriced markets.
Done when: Calibrator correctly flags mispriced markets on paper data.

Layer 4 — Risk Engine
Purpose: Pure Python risk_engine.py. Kelly sizing, circuit breakers,
correlation checks, liquidity floors.
Done when: Circuit breakers trigger at exact thresholds, Kelly sizing
mathematically verified, zero LLM imports confirmed.

Layer 5 — Contract Parser
Purpose: DeepSeek V3 parsing resolution criteria into structured JSON.
Done when: 10 real Polymarket contracts parsed with correct structured output.

Layer 6 — Integration
Purpose: All layers communicating correctly end to end.
Done when: Full integrated test passes with all components.

Layer 7 — Deployment
Purpose: Hetzner deployment, Telegram alerts, startup reconciliation.
Done when: Telegram alerts fire correctly, startup reconciliation completes
before any trade logic, agent running 24/7 on Hetzner.

---

## 3. AI MODEL STACK

### 3.1 News Analyst Agent

Model: Qwen3-32B
Provider: OpenRouter
Cost: $0.02/M tokens
Latency: ~2 seconds
Role: Real-time news classification and event flagging.
Receives pre-filtered signals from spaCy layer.
Outputs: event category, affected markets, confidence score.
Rules: - max_tokens: 500 (output is classification, not prose) - 10-second timeout, no fallback (if News Analyst fails, signal is dropped,
not a critical path failure)

### 3.2 Contract Parser Agent

Model: DeepSeek V3
Provider: OpenRouter
Cost: ~$0.40/month
Latency: ~3 seconds
Role: Parses Polymarket resolution criteria on first market discovery.
Extracts structured resolution conditions.
Populates resolution_keyword_cache table.
NOT in hot path — runs once per market discovery, not per trade.
Output format (exact):
{
"resolution_source": string,
"resolution_condition": string,
"key_entities": [string],
"resolution_keywords": [string],
"ambiguity_score": float (0.0-1.0),
"resolution_type": string
}

### 3.3 Trade Decision Agent

Model: Qwen3-235B-A22B
Provider: SiliconFlow (primary), OpenRouter (fallback)
Cost: ~$0.70/month
Latency: ~12 seconds (primary), ~15 seconds (fallback)
Role: Final YES/NO decision on entering a trade.
Receives: signal summary, agent_memory lessons prepended,
market data, own probability estimate vs market price.
HARD RULES — never override: - max_tokens: 900 - thinking_budget: 600 - Timeout: 18 seconds → immediate failover to OpenRouter - Always prepend agent_memory lessons before every call - Confidence hard cap: 0.88 (agent cannot express higher confidence
than this regardless of model output)

### 3.4 Risk Manager

Model: NONE — pure Python
Provider: Hetzner (local execution)
Cost: $0
Latency: < 1ms
File: /risk/risk_engine.py
Role: Kelly sizing, circuit breakers, correlation checks,
liquidity floors, drawdown tracking.
ABSOLUTE RULE: Zero LLM calls. Zero external API calls. Zero imports
from /llm/. Every function is deterministic. Cannot be argued out
of any rule by any other component.

### 3.5 Coordinator

Type: Python weighted aggregation function (primary)
LLM escalation (conflict cases only, ~20% of trades)
Cost: ~$0.05/month (conflict escalation only)
Latency: < 1ms (Python) / ~12 seconds (LLM escalation)
Role: Synthesizes News Analyst and Trade Decision Agent outputs
into final trading decision.
Conflict trigger — LLM Coordinator fires IF AND ONLY IF: - News Analyst and Trade Decision Agent disagree on direction (one YES,
one NO or abstain) - AND News Analyst confidence > 0.70
Non-conflict routing: - Trade Decision Agent wins without escalation if News Analyst
confidence < 0.70 - Both agents agree → Python aggregation only, no LLM call

### 3.6 Monthly LLM Cost Breakdown

News Analyst (Qwen3-32B OpenRouter): ~$0.02/month
Contract Parser (DeepSeek V3 OpenRouter): ~$0.40/month
Trade Decision (Qwen3-235B SiliconFlow): ~$0.70/month
Coordinator LLM escalation (~20% of trades): ~$0.05/month
TOTAL: ~$1.17/month

---

## 4. DATA PIPELINE

### 4.1 RSS Poller (Velocity Path — Primary)

Purpose: Real-time event detection for velocity trades.
Replaces GDELT on the fast path entirely.
Polling: Every 10 seconds
Sources (minimum 20):
- AP News RSS
- Reuters RSS
- BBC News RSS
- Federal Reserve press releases RSS
- Supreme Court slip opinions RSS
- SEC EDGAR filings RSS
- Congress.gov bill tracker RSS
- SCOTUS blog RSS
- DOJ press releases RSS
- FDA announcements RSS
- ClinicalTrials.gov RSS
- Federal Register RSS
- Treasury.gov RSS
- WhiteHouse.gov briefings RSS
- NATO press RSS
- UN press RSS
- Polymarket blog RSS
- Kalshi market feed RSS
- Metaculus new questions RSS
- Predictit RSS (if available)

RSS FEED ENVIRONMENT NOTES:
Reuters RSS permanently discontinued.
Removed from feed list.
AP News URL updated to apnews.com/hub/rss
Cloudflare-protected feeds (Metaculus,
ClinicalTrials, Kalshi, FDA) may require
server-level IP on Oracle Cloud to access.
Dev machine threshold: 8+ feeds = PASS.
Production threshold: 15+ feeds = PASS.

Latency: Zero structural lag. Events surface within polling interval.

### 4.2 spaCy Pre-filter

Purpose: Reduces raw RSS volume before paid API calls.
Model: en_core_web_lg (on Hetzner, local execution)
Latency: < 50ms
CRITICAL: Domain allowlist of ~200 terms bypasses spaCy classification
automatically. This prevents silent dropping of financial,
legal, and regulatory signals that spaCy misclassifies.
Domain allowlist includes (non-exhaustive):
resolution, settlement, FOMC, basis points, bps, at-the-money,
CLOB, NRC ruling, slip opinion, cloture, reconciliation,
appropriations, continuing resolution, sanctions, indictment,
acquittal, mistrial, certiorari, mandate, injunction, stay,
preliminary injunction, CPI, PCE, NFP, taper, quantitative,
on-chain, mempool, hash rate, liquidation, open interest,
funding rate, perpetual, spot ETF, SEC filing, 8-K, 10-Q,
proxy statement, merger agreement, acquisition, spin-off,
[expand to 200 terms during Layer 2 build]

### 4.3 GDELT

Purpose: Secondary enrichment only — full pipeline background context.
NOT USED: In velocity path or fast path.
Reason: 15-minute structural update lag. By the time GDELT surfaces
an event, Polymarket has already priced it.
Usage: Recalibration strategy background signal. Historical pattern
enrichment. Never as primary trade trigger.

### 4.4 Parsec API

Tier: Free tier
Purpose: Additional market data enrichment.

### 4.5 Government Feeds

Free feeds: All sources listed in RSS poller section.
No paid government data sources required.

---

## 5. STRATEGY STACK

### Strategy 1: Velocity / News Catalyst

Core insight: News takes 90-480 seconds to fully price on Polymarket.
Capital: 35% of active capital
Max position: 5% of portfolio per trade
Kelly: 0.15 fractional Kelly (lower due to time-pressure uncertainty)
EV estimate: 12-18% per trade when triggered correctly
Entry trigger: Signal confidence > 0.75, expected move > 8 cents
Time stop: Exit within 4 hours if no price movement
Thrives: High-frequency news (election weeks, Fed decisions, crises)
Fails: Quiet news periods, when price has already moved
Path: Fast path if confidence > 0.87 AND pre-validated category

### Strategy 2: Probability Recalibration (Statistical Arbitrage)

Core insight: Market-implied probabilities are systematically biased.
Agent's calibration model trained on 5,000+ resolved markets
detects when market price diverges from empirical base rate.
Capital: 40% of active capital
Max position: 3% of portfolio per trade
Kelly: 0.25 fractional Kelly
EV estimate: 7-12% per trade
Entry trigger: |agent_estimate - market_price| > 7 cents
Thrives: Any market environment. Most robust strategy.
Fails: Genuinely unprecedented events with no historical analogues.
Path: Full pipeline always

### Strategy 3: Cross-Market Correlation Arbitrage

Core insight: Related markets cannot hold internally inconsistent prices.
Agent maintains Bayesian network of 200+ linked markets.
Capital: 15% of active capital
Kelly: 0.25 fractional Kelly
EV estimate: 5-9% per trade
Entry trigger: Correlation inconsistency detected in daily optimization pass
Thrives: Complex political environments with many interdependent markets
Fails: Markets more independent than they appear
Path: Full pipeline, basket positions
Activation: Not activated until $10,000 capital scale

### Strategy 4: Resolution Criteria Exploitation

Core insight: Resolution criteria are legal documents. Most traders don't
read them. Agent reads every single one and checks whether
current reality already satisfies YES condition.
Capital: 10% of active capital
Max position: 8% of portfolio per trade (high conviction, rare)
Kelly: 0.35 fractional Kelly
EV estimate: 15-25% per trade when triggered
Entry trigger: Criteria checked against live data sources by Contract Parser
Thrives: Technical markets (regulatory, legal, economic data releases)
Fails: Ambiguous or subjective resolution criteria
Path: Full pipeline, Contract Parser mandatory

---

## 6. RISK ENGINE

File: /risk/risk_engine.py
ABSOLUTE RULE: Pure Python only. No LLM calls. No OpenRouter imports.
No external API calls. Every function deterministic. Every function < 1ms.

### 6.1 Kelly Position Sizing

Formula: f = (bp - q) / b
Where: b = odds, p = win probability, q = 1 - p
Applied fractional Kelly by strategy:
Velocity trades: 0.15 × Kelly
Recalibration trades: 0.25 × Kelly
Correlation arb: 0.25 × Kelly
Resolution edge trades: 0.35 × Kelly
Rationale: Fractional Kelly reduces variance while sacrificing
modest expected returns. Protects against imperfect
probability estimates.

### 6.2 Hard Position Limits (NEVER OVERRIDDEN)

Single trade maximum: 5% of total portfolio
Exception (resolution edge only): 8% of total portfolio
Category exposure maximum: 30% of portfolio per category
Correlated exposure maximum: 20% of portfolio
Minimum market liquidity to trade: $5,000 available at target price
Maximum position as % of market: 5% of total market liquidity
Auto-exit trigger: If market liquidity drops below $3,000,
exit at market price regardless of P&L

### 6.3 Drawdown Circuit Breakers

Daily drawdown > 8%: Halt all new trades. Alert Telegram. Human review.
Weekly drawdown > 15%: Full stop. No new trades. Human review required
before resuming.
Monthly drawdown > 25%: Shut down agent entirely. Full diagnostic required.

### 6.4 Confidence Rules

Minimum confidence to trade: 0.75
Fast path confidence threshold: 0.87
Confidence hard ceiling: 0.88 (model output above this is clamped
to 0.88 — epistemic humility enforced)
Minimum edge to trade: 7 cents (|agent_estimate - market_price|)

### 6.5 Correlation Management

Before every new trade, compute total portfolio exposure that would be
affected if the common shock behind this trade reversed.
If correlated exposure would exceed 20%: block trade regardless of
individual merit. This check is mandatory, never skipped.

### 6.6 Health Score System

Score range: 0-100
Components (weighted equally): 1. Recent win rate (last 20 trades) 2. Brier score rolling 30-day 3. Average slippage vs expected 4. Data feed latency 5. Drawdown trend 6. Strategy correlation
Thresholds:
Health < 65: Defensive mode.
Position sizing reduced 50%.
Only trades with confidence > 0.90 execute.
Alert Telegram.
Health < 40: Full halt. No new trades. Alert Telegram.
Human review required before resuming.

### 6.7 No-Trade Windows

Never trade within 60 minutes before OR after a market category-wide
shock event (election night, major court ruling, Fed announcement).
Spreads too wide, prices chaotic.

---

## 7. EXECUTION LAYER

File: /execution/

### 7.1 Idempotency (NEVER SKIPPED)

Flow: 1. Trade decision made → generate UUID immediately 2. Write UUID to idempotency_log in Supabase BEFORE API call 3. On every retry: query idempotency_log first 4. If UUID has status = 'confirmed': do NOT resubmit 5. If Supabase unavailable for idempotency check: FAIL CLOSED
Do not submit order. Alert Telegram.
Purpose: Prevents doubled positions under Polygon congestion or
HTTP timeout during order submission.

### 7.2 Order Submission

Platform: Polymarket CLOB API
Chain: Polygon
After submission: poll for confirmation, update idempotency_log
status to 'confirmed' when confirmed.

### 7.3 Post-Trade Monitoring

Frequency: Every 15 minutes on all open positions
Exit conditions (any one triggers exit): 1. Confidence reversal: subsequent signal > 0.70 strength pushes
opposite direction → exit immediately regardless of P&L 2. Price target: market price within 3 cents of agent's estimate
(edge realized) → take profit 3. Time decay: > 50% of original expected edge unrealized AND
< 72 hours to resolution AND confidence not increased
→ reduce position by 50%

### 7.4 Post-Resolution Logging

Within 30 minutes of market resolution: - Log outcome to closed_trades - Compute Brier score contribution - Update calibration model with new data point - Tag trade for weekly strategy attribution review - Increment relevant_trades_since_last_trigger on matching
agent_memory lessons

---

## 8. MEMORY ARCHITECTURE

### Layer A — Statistical Memory (Permanent Ledger)

Storage: Supabase
Tables: open_positions, closed_trades, daily_performance
Rule: Never modified after write. Permanent raw performance record.
This is the ground truth. Nothing overwrites it.

### Layer B — Episodic Lesson Memory

Storage: Supabase agent_memory table
Purpose: Structured mistake and pattern memory injected into LLM
prompts at runtime.
Full schema: See Section 9.5
Query logic: Before every Trade Decision Agent and Contract Parser call,
query agent_memory WHERE category matches AND retired = false,
using structured JSON WHERE clauses on trigger_condition fields.
Returns 3-5 precisely relevant lessons maximum.
Prepend all results as warning block at top of system prompt.

### Decay System (Evidence-Based, Not Time-Based)

Counter: relevant_trades_since_last_trigger - Increments every time any trade resolves in matching category,
regardless of whether lesson fired - When counter hits 20 without lesson triggering: apply one decay step
(reduce confidence_score by 0.10) - Counter resets to 0 after decay step applied
Calendar fallback: 90-day decay only for dormant lessons where zero
relevant trades occurred in that period
Validation bonus: When lesson fires AND trade wins: - Set recently_validated_at to current timestamp - While recently_validated_at within last 90 days: decay rate halved
Retirement: confidence_score drops below 0.3 → set retired = true
(history preserved, never deleted)
Reinforcement: Same mistake repeats → reinforcement_count increments
→ confidence_score reset to 1.0
→ severity may escalate to hard_rule

### Layer C — Strategic Category Memory (Versioned)

Storage: Supabase layer_c_category_versions table
Purpose: Category-level patterns too broad for individual lessons.
Contents: category name, average resolution ambiguity score,
recommended confidence threshold, known resolution traps,
historical edge percentage
Updates: Monthly — inserts new row (never overwrites)
Rollback: Single UPDATE to superseded_by field
Agent reads: Always latest valid_from row per category

### Layer D — Weekly Human Audit

Trigger: Weekly, Claude API call
Reviews: 3 worst losses of the week + 2 best wins of the week
Actions: - Writes new lessons into agent_memory in structured JSON format - Reviews lessons with confidence_score < 0.40: decide retire or keep - Migrates any remaining plain-text trigger_conditions to structured JSON - Diffs current Layer C version against previous version
Purpose: Human judgment layer. Prevents system from reinforcing its own
wrong beliefs unsupervised. Required for trustworthy operation.

---

## 9. SUPABASE SCHEMA (ALL TABLES)

### 9.1 open_positions

id UUID PRIMARY KEY DEFAULT gen_random_uuid()
market_id TEXT NOT NULL
market_question TEXT
direction TEXT NOT NULL -- 'YES' or 'NO'
entry_price DECIMAL(10,4) NOT NULL
position_size_usdc DECIMAL(10,4) NOT NULL
strategy TEXT NOT NULL -- 'velocity'|'recalibration'|
-- 'correlation'|'resolution'
agent_estimate DECIMAL(10,4)
confidence_at_entry DECIMAL(6,4)
kelly_fraction_used DECIMAL(6,4)
category TEXT
idempotency_uuid UUID
opened_at TIMESTAMPTZ DEFAULT NOW()
last_checked_at TIMESTAMPTZ

### 9.2 closed_trades

id UUID PRIMARY KEY DEFAULT gen_random_uuid()
market_id TEXT NOT NULL
market_question TEXT
direction TEXT NOT NULL
entry_price DECIMAL(10,4)
exit_price DECIMAL(10,4)
position_size_usdc DECIMAL(10,4)
pnl_usdc DECIMAL(10,4)
pnl_percent DECIMAL(10,4)
strategy TEXT
agent_estimate DECIMAL(10,4)
confidence_at_entry DECIMAL(6,4)
brier_contribution DECIMAL(10,6)
category TEXT
outcome TEXT -- 'win' | 'loss' | 'push'
exit_reason TEXT -- 'resolved'|'confidence_reversal'|
-- 'price_target'|'time_decay'|
-- 'circuit_breaker'
opened_at TIMESTAMPTZ
closed_at TIMESTAMPTZ DEFAULT NOW()
was_memoryless BOOLEAN DEFAULT FALSE
notes TEXT

### 9.3 market_signals

id UUID PRIMARY KEY DEFAULT gen_random_uuid()
raw_headline TEXT
source_url TEXT
source_name TEXT
category TEXT
confidence_score DECIMAL(6,4)
affected_market_ids TEXT[]
event_type TEXT
passed_fast_path BOOLEAN DEFAULT FALSE
action_taken TEXT -- 'traded'|'discarded'|'pending'
discard_reason TEXT
detected_at TIMESTAMPTZ DEFAULT NOW()
processed_at TIMESTAMPTZ

### 9.4 daily_performance

id UUID PRIMARY KEY DEFAULT gen_random_uuid()
date DATE UNIQUE NOT NULL
starting_balance_usdc DECIMAL(12,4)
ending_balance_usdc DECIMAL(12,4)
daily_pnl_usdc DECIMAL(12,4)
daily_pnl_percent DECIMAL(10,4)
trades_executed INTEGER DEFAULT 0
trades_won INTEGER DEFAULT 0
trades_lost INTEGER DEFAULT 0
brier_score_rolling DECIMAL(10,6)
health_score DECIMAL(6,2)
circuit_breaker_fires INTEGER DEFAULT 0
signals_detected INTEGER DEFAULT 0
signals_traded INTEGER DEFAULT 0
created_at TIMESTAMPTZ DEFAULT NOW()

### 9.5 agent_memory

id UUID PRIMARY KEY DEFAULT gen_random_uuid()
category TEXT NOT NULL
-- 'politics'|'crypto'|'sports'|
-- 'science'|'legal'|'economics'|'all'
lesson TEXT NOT NULL
trigger_condition JSONB NOT NULL
severity TEXT NOT NULL
-- 'warning' | 'hard_rule'
confidence_score DECIMAL(6,4) DEFAULT 1.0
relevant_trades_since_last_trigger INTEGER DEFAULT 0
reinforcement_count INTEGER DEFAULT 0
recently_validated_at TIMESTAMPTZ -- nullable, NULL by default
retired BOOLEAN DEFAULT FALSE
created_at TIMESTAMPTZ DEFAULT NOW()
last_triggered_at TIMESTAMPTZ
superseded_by UUID REFERENCES agent_memory(id)

trigger_condition JSON structure:
{
"time_to_resolution_hours": {"max": 72},
"probability_range": [0.3, 0.7],
"resolution_type": "vote_based",
"signal_source": ["news_velocity"],
"category": "politics"
}
All fields optional. Query matches only specified fields.

### 9.6 resolution_keyword_cache

id UUID PRIMARY KEY DEFAULT gen_random_uuid()
market_id TEXT UNIQUE NOT NULL
market_question TEXT
resolution_keywords TEXT[]
resolution_conditions JSONB
resolution_type TEXT
ambiguity_score DECIMAL(6,4)
cached_at TIMESTAMPTZ DEFAULT NOW()
last_used_at TIMESTAMPTZ

TTL rule: entries older than 24 hours are NOT used by fast path.
Fast path falls back to full pipeline and triggers silent refresh.
Staleness check: WHERE cached_at > NOW() - INTERVAL '24 hours'

### 9.7 idempotency_log

id UUID PRIMARY KEY -- this IS the idempotency UUID,
-- generated at trade decision time
market_id TEXT NOT NULL
direction TEXT NOT NULL
intended_size_usdc DECIMAL(10,4)
status TEXT NOT NULL DEFAULT 'pending'
-- 'pending'|'submitted'|'confirmed'|'failed'
polymarket_order_id TEXT
created_at TIMESTAMPTZ DEFAULT NOW()
confirmed_at TIMESTAMPTZ
failure_reason TEXT

### 9.8 layer_c_category_versions

id UUID PRIMARY KEY DEFAULT gen_random_uuid()
category TEXT NOT NULL
avg_resolution_ambiguity_score DECIMAL(6,4)
recommended_confidence_threshold DECIMAL(6,4)
known_resolution_traps TEXT[]
historical_edge_percent DECIMAL(6,4)
notes TEXT
valid_from TIMESTAMPTZ DEFAULT NOW()
superseded_by UUID REFERENCES layer_c_category_versions(id)

Agent always reads: SELECT \* FROM layer_c_category_versions
WHERE category = $1
AND superseded_by IS NULL
ORDER BY valid_from DESC LIMIT 1

---

## 10. LATENCY TARGETS

Component Target Hard Limit
─────────────────────────────────────────────────────────
spaCy pre-filter (local) < 50ms 100ms
News Analyst (Qwen3-32B) ~2s 10s
Resolution cache lookup < 10ms 50ms
Contract Parser (DeepSeek V3) ~3s 10s (non-hot-path)
Trade Decision (Qwen3-235B) ~12s 18s → failover
risk_engine.py < 1ms 5ms
Python coordinator < 1ms 5ms
LLM coordinator (conflict only) ~12s 18s
Supabase read < 500ms 2s → fallback
Order submission (Polymarket API) ~2s 10s

FAST PATH total: < 5s
FULL PIPELINE total: 17-20s
Previous architecture (resolved): 35-55s

---

## 11. COST BUDGET

Monthly hard limit: $5.00 LLM + $6.00 Hetzner = $11.00/month total

LLM costs:
Qwen3-32B (OpenRouter): ~$0.02/month
DeepSeek V3 (OpenRouter): ~$0.40/month
Qwen3-235B-A22B (SiliconFlow): ~$0.70/month
LLM Coordinator (conflict cases): ~$0.05/month
Weekly audit (Claude API): ~$0.00/month (negligible)
TOTAL LLM: ~$1.17/month

Headroom for high-load events (elections, Fed days): ~$0.40 extra per event.
This remains within budget.

---

## 12. CIRCUIT BREAKERS AND THRESHOLDS (COMPLETE LIST)

TRADING LIMITS:
Max single trade size: 5% of portfolio (8% for resolution edge)
Max category exposure: 30% of portfolio
Max correlated exposure: 20% of portfolio
Min market liquidity to enter: $5,000
Max position as % of market: 5% of market's total liquidity
Auto-exit liquidity floor: $3,000 (exit at market, no exceptions)
Min edge to trade: 7 cents
Min confidence to trade: 0.75
Fast path confidence threshold: 0.87
Confidence hard ceiling: 0.88

DRAWDOWN CIRCUIT BREAKERS:
Daily drawdown > 8%: Halt new trades + Telegram alert
Weekly drawdown > 15%: Full stop + Telegram + human review
Monthly drawdown > 25%: Shut down agent + Telegram + full diagnostic

HEALTH SCORE:
Health < 65: Defensive mode (50% sizing, > 0.90 only)
Health < 40: Full halt + Telegram

STRATEGY PROBATION:
Average edge < 4 cents over 20 consecutive trades: strategy enters probation
(position sizes halved)
Continues for 20 more trades: strategy suspended, human review

STRATEGY RETIREMENT:
Out-of-sample win rate < 52% over 30+ consecutive trades
AND EV per trade < 4 cents
AND causal explanation identified
All three conditions required. One alone is not sufficient.

NO-TRADE WINDOW:
60 minutes before and after category-wide shock event.

---

## 13. FAILOVER AND DEGRADATION POLICIES

### SiliconFlow → OpenRouter Failover

Trigger: Trade Decision Agent call exceeds 18 seconds
Action: Cancel SiliconFlow request immediately
Route to OpenRouter with same prompt
Health: 5-minute health check ping to SiliconFlow
If latency > 20s on health ping: route all calls to OpenRouter
until next health check passes
If both fail: circuit breaker halts trading. Alert Telegram.

### Supabase Degradation (2-second timeout on every read)

resolution_keyword_cache timeout: Fall back to full pipeline (skip fast path)
agent_memory timeout: Proceed without memory context.
Flag trade as was_memoryless = true
in closed_trades.
idempotency_log timeout: FAIL CLOSED. Do not submit order.
Alert Telegram.
layer_c_category_versions timeout: Use hardcoded conservative defaults.
open_positions timeout: Halt new trades until read succeeds.

### Polymarket API Unavailable (Startup)

Action: Halt completely. Retry every 60 seconds.
Never skip reconciliation. Never proceed on stale data.
Alert Telegram after 5 minutes of unavailability.

---

## 14. STARTUP RECONCILIATION

Runs on EVERY process start, before any signal processing begins.
Order is mandatory. No steps may be skipped for any reason.

Step 1: Fetch actual open positions from Polymarket API
Step 2: Fetch actual USDC wallet balance from Polymarket API
Step 3: Fetch agent's known state from Supabase (open_positions table)
Step 4: Diff actual vs known: - Markets resolved during downtime → move to closed_trades,
log outcome, update Brier score - Orders partially filled → update open_positions with actual size - Orders in 'pending' idempotency status → check Polymarket API
for actual status, update idempotency_log accordingly
Step 5: Write authoritative reconciled state back to Supabase
Step 6: If unresolvable inconsistency detected:
HALT. Do not proceed. Alert Telegram with exact inconsistency.
Wait for human review.

If Polymarket API unavailable at any step: halt completely.
Retry every 60 seconds until reconciliation succeeds.
Skipping reconciliation is never acceptable under any circumstances.

---

## 15. AGENT DECISION FLOW

Signal detected (RSS poller or GDELT enrichment)
↓
spaCy pre-filter (<50ms)
↓ [domain allowlist bypasses classification]
News Analyst — Qwen3-32B (~2s)
↓
Confidence gate: confidence > 0.75?
NO → discard signal, log to market_signals (action='discarded')
YES ↓
Market discovery: fetch all open related markets from Supabase/Polymarket API
↓
Resolution cache check: cached_at < 24 hours?
NO → route to full pipeline, trigger Contract Parser refresh
YES ↓
ROUTING DECISION:
IF confidence > 0.87 AND category is pre-validated:
FAST PATH:
Check resolution_keyword_cache for market relevance (<10ms)
IF event entities don't match resolution keywords: exclude market
→ risk_engine.py (<1ms)
→ Python coordinator (<1ms)
→ Order submission
TOTAL: < 5 seconds
ELSE:
FULL PIPELINE:
Contract Parser — DeepSeek V3 (~3s)
Trade Decision Agent — Qwen3-235B (~12s)
[agent_memory lessons prepended]
risk_engine.py (<1ms)
Python coordinator (<1ms)
IF conflict (News Analyst vs Trade Decision disagree on direction
AND News Analyst confidence > 0.70):
LLM Coordinator escalation (~12s)
→ Order submission
TOTAL: 17-20 seconds

Pre-submission:
Generate UUID → write to idempotency_log (status='pending') → submit order

Post-submission:
Poll for confirmation → update idempotency_log (status='confirmed')
Log to open_positions

Post-resolution (within 30 minutes):
Log to closed_trades
Compute Brier contribution
Increment relevant_trades_since_last_trigger on matching lessons
Tag for weekly audit

---

## 16. CONFLICT RESOLUTION LOGIC

Python conditional (exact):

if (news_analyst_direction != trade_decision_direction
and news_analyst_confidence > 0.70):
route to LLM Coordinator
elif news_analyst_confidence <= 0.70:
use trade_decision_direction (Trade Decision Agent wins)
else: # both agree
use python_weighted_aggregation(news_analyst_output,
trade_decision_output)

---

## 17. WEEKLY LEARNING CYCLE

Sunday 23:00 UTC:

1. Compute Brier scores by category
   → retrain calibration models if any category score has risen
2. Run strategy attribution analysis
   → retire strategy-category combinations with negative EV over 30 trades
3. Claude API weekly audit:
   → review 3 worst losses + 2 best wins
   → write new lessons to agent_memory in structured JSON format
   → review lessons with confidence_score < 0.40 (retire or keep)
   → migrate any plain-text trigger_conditions to structured JSON
4. Run regime change detection (PELT algorithm on win-rate time series)
   → if statistically significant break detected: cut position sizes 40%,
   run full diagnostic, alert Telegram
5. Diff Layer C current vs previous version
   → update if category-level patterns have shifted
6. Update correlation matrix weights
7. Generate weekly performance report

Monday 06:00 UTC:
Updated models and calibration go live.

---

## 18. TELEGRAM ALERT TRIGGERS (COMPLETE LIST)

Alert fires immediately on:

- Any drawdown circuit breaker firing (8% daily, 15% weekly, 25% monthly)
- Health score dropping below 65 (defensive mode triggered)
- Health score dropping below 40 (full halt)
- Startup reconciliation halt (unresolvable inconsistency)
- Startup reconciliation: Polymarket API unavailable > 5 minutes
- Supabase idempotency check unavailable (fail closed triggered)
- SiliconFlow failover to OpenRouter triggered
- Both SiliconFlow AND OpenRouter unavailable (trading halted)
- Strategy entering probation
- Strategy suspended (human review required)
- Regime change detected (weekly cycle)
- Weekly Brier score above 0.23
- Agent process restart (always)

Alert format:
[ZERO-ALPHA] {severity} | {trigger} | {timestamp} | {detail}
severity: INFO | WARNING | CRITICAL

---

## 19. HEALTH SCORE SYSTEM

Computed continuously. Stored in daily_performance.health_score.

Components (each scored 0-100, averaged):

1. Recent win rate: last 20 trades win% (100 = 100% win rate)
2. Brier score: (1 - brier_score/0.25) × 100 (calibrated against 0.25 baseline)
3. Average slippage: (1 - actual_slippage/expected_slippage) × 100
4. Data feed latency: (1 - avg_feed_latency/10s) × 100
5. Drawdown trend: based on 7-day rolling drawdown direction
6. Strategy correlation: based on inter-strategy P&L correlation

health_score = mean(all six components)

Thresholds:
≥ 65: Normal operation
< 65: Defensive mode. Position sizes × 0.5. Min confidence raised to 0.90.
Telegram alert (WARNING).
< 40: Full halt. No new trades. Telegram alert (CRITICAL).

---

## 20. PAPER TRADING SUCCESS CRITERIA

Minimum duration: 2 full weeks
Minimum trades: 20 resolved trades

Gate criteria (ALL must pass before live deployment):
✓ Brier score < 0.23 on all resolved paper trades
✓ No circuit breaker fires from logic errors
(market condition fires acceptable, code errors are not)
✓ No unexpected failure modes observed
✓ Startup reconciliation completing successfully on every restart
✓ Telegram alerts firing correctly on every trigger
✓ Supabase fallback behaviors verified under simulated timeout
✓ Fast path latency confirmed < 5 seconds on real signals
✓ Full pipeline latency confirmed < 22 seconds on real signals
✓ Idempotency verified: no duplicate orders on simulated timeout

If ANY criterion fails: extend paper trading. Do not deploy live capital.

---

## 21. SCALING PLAN

$1,000 (Proof of Concept / Paper Trading):
Active strategies: Strategy 2 only (Recalibration)
Max position: $50 (5% of $1,000)
Goal: Data collection and calibration validation, not profit
Target trades: 3-5 per week

$10,000 (Early Live):
Active strategies: Strategy 1 (Velocity) + Strategy 2 + Strategy 4 (Resolution)
Max position: $500
Target: 8-15 trades per week
Sharpe ratio target: > 1.5
Begin strategy attribution tracking

$50,000 (Full Deployment):
Active strategies: All four
Max position: $2,500
Target: 15-30 trades per week
Begin tracking agent's own market impact on slippage

$200,000+ (Scale Awareness):
Agent's own buying moves prices before positions are fully built.
Market impact must be modeled into EV calculations.
Scale in tranches: never increase capital > 3× without 2-week impact audit.
If slippage per trade increases > 30% after capital increase: stop scaling.

Strategy capacity ceilings:
Velocity: ~$500,000
Recalibration: ~$2,000,000
Correlation: ~$300,000
Resolution: ~$100,000
Combined: ~$800,000 - $1,200,000 before edge decay dominates

Minimum acceptable Sharpe ratio: 1.8 in normal conditions.
Below 1.8 sustained over 30 days: edge does not justify operational complexity.

---

END OF PLAN.md
Version: Final (post all architecture reviews)
All decisions locked. Do not deviate without updating this document.
