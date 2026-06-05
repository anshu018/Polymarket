# GEMINI.md — Polymarket Autonomous Trading Agent

# READ THIS EVERY PROMPT. DO NOT SKIP ANY SECTION.

# For full architecture detail: read PLAN.md

---

## WHAT THIS PROJECT IS

An autonomous AI agent that trades on Polymarket (prediction market platform)
24/7. It detects mispriced probabilities, executes trades, and improves through
a structured memory and learning system.

Server: Hetzner CX22 ($6/month)
Database: Supabase (free tier, Postgres)
Chain: Polygon (Polymarket CLOB)
Alerts: Telegram bot
LLM cost: $1.17/month hard target
Language: Python only

Full architecture, all schemas, all thresholds: PLAN.md
Progress tracking: PROGRESS.md
Test criteria: TESTING.md
Mistakes log: MEMORY.md

---

## ABSOLUTE RULES — NEVER BREAK THESE

These rules have no exceptions. No context makes breaking them acceptable.

### RULE 1 — /risk/ is pure Python. Always.

- Zero LLM calls in /risk/
- Zero imports from /llm/
- Zero external API calls
- Every function is deterministic
- Every function executes in < 1ms
- risk_engine.py cannot be argued out of any rule by any other component
- File: /risk/risk_engine.py

### RULE 2 — Idempotency is never skipped

- Every order: generate UUID at decision time
- Write UUID to idempotency_log BEFORE API call
- Every retry: check idempotency_log first
- If UUID confirmed: do NOT resubmit
- If Supabase unavailable for this check: FAIL CLOSED
  Do not submit the order. Alert Telegram. Stop.
- File: /execution/

### RULE 3 — Every Qwen3-235B-A22B call has hard limits

- max_tokens: 900
- thinking_budget: 600
- Timeout: 18 seconds → cancel → immediate OpenRouter fallback
- Confidence output above 0.88: clamp to 0.88. Never exceed.
- Always prepend agent_memory lessons before every call

### RULE 4 — Startup reconciliation is mandatory

- Runs on EVERY process start
- Fetches actual Polymarket positions + USDC balance via API
- Diffs against Supabase state
- Writes authoritative reconciled state before any trade logic
- Unresolvable inconsistency: HALT, alert Telegram, wait for human
- Polymarket API unavailable: HALT, retry every 60 seconds
- Skipping reconciliation is never acceptable under any circumstance

### RULE 5 — Supabase reads have 2-second timeout with hardcoded fallbacks

resolution_keyword_cache timeout → fall back to full pipeline
agent_memory timeout → proceed, flag was_memoryless = true
idempotency_log timeout → fail closed, do not submit
open_positions timeout → halt new trades until read succeeds
layer_c_category_versions → use hardcoded conservative defaults

### RULE 6 — /llm/ functions have 18-second timeout wrappers. Always.

- Every function in /llm/ wraps its API call with 18s timeout
- On timeout: immediate failover to OpenRouter (same prompt)
- Both providers fail: circuit breaker halts trading, alert Telegram

### RULE 7 — GDELT never touches the fast path or velocity signals

- GDELT has 15-minute structural lag
- GDELT use: secondary enrichment, full pipeline only, background context
- Velocity path primary feed: RSS poller (10-second polling interval)

### RULE 8 — No trade executes without passing risk_engine.py

- risk_engine.py runs on every trade, every path, no exceptions
- Fast path does not skip risk checks

### RULE 9 — Never read .env directly
Never use open('.env'), cat .env, type .env,
Get-Content .env, Viewed .env, or any command
that reads .env file contents directly.
The raw values must never appear in session.
Always load via: load_dotenv('.env')
To verify vars are set use:
  import os
  from dotenv import load_dotenv
  load_dotenv('.env')
  print(os.environ.get('SUPABASE_URL','MISSING'))
This prints SET or MISSING without exposing
the actual value.
This rule applies permanently. No exceptions.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## WINDOWS TERMINAL OUTPUT — PERMANENT RULE

PowerShell truncates long output. This causes
test results to be lost or incomplete.

NEVER use these patterns:
  python script.py > output.txt
  python script.py | Select-String
  python script.py 2>&1
  Get-Content output.txt

ALWAYS use this pattern for any command
whose output must be fully captured:

  python -c "
  import subprocess, sys
  result = subprocess.run(
      [sys.executable, 'script_name.py'],
      capture_output=True,
      text=True,
      timeout=60
  )
  output = result.stdout + result.stderr
  print(output[-4000:])
  "

For inline python -c commands that produce
long output, split into smaller checks
or print only the final verdict line.

For test files: always write the test file
to print a clear single-line verdict at the
end (PASS or FAIL) so even truncated output
captures the result.

NEVER create output files (.txt, .out, .log)
to work around truncation. Capture inline.
Delete test files immediately after run.
This rule applies to every layer, every phase,
every test, for the entire project lifetime.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## SUPABASE ACCESS — PERMANENT RULE

Only one approved pattern for Supabase
access exists in this project.
Never use psycopg2. Never ask for
DATABASE_URL. Never use direct PostgreSQL
connections. Never read .env directly.

ONLY approved pattern:
  import os
  from dotenv import load_dotenv
  load_dotenv('.env')
  from supabase import create_client
  url = os.environ.get('SUPABASE_URL')
  key = os.environ.get('SUPABASE_KEY')
  client = create_client(url, key)
  result = client.table('table_name')\
      .select('column')\
      .execute()

For async code use get_client() from
memory/supabase_client.py which returns
the same sync client wrapped correctly.

All table operations are synchronous.
No await before client.table() calls.
2-second timeout via asyncio.wait_for()
on the overall async function only.

If Supabase returns an error: log it
and use the hardcoded fallback defined
in PLAN.md Section 13.

Never invent alternative database access
patterns. This is the only one that works
in this project.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

---

## TECH STACK — EXACT MODELS AND PROVIDERS

News Analyst Agent:
Model: Qwen3-32B
Provider: OpenRouter
Cost: $0.02/M tokens
Latency: ~2s | Timeout: 10s (no fallback, signal dropped)
File: /llm/news_analyst.py

Contract Parser Agent:
Model: DeepSeek V3
Provider: OpenRouter
Cost: ~$0.40/month
Latency: ~3s | NOT in hot path — runs once per market discovery
File: /llm/contract_parser.py

Trade Decision Agent:
Model: Qwen3-235B-A22B
Provider: SiliconFlow (primary) → OpenRouter (fallback at 18s)
Cost: ~$0.70/month
Latency: ~12s primary / ~15s fallback
File: /llm/trade_decision.py

Risk Manager:
Model: NONE — pure Python
Cost: $0
Latency: < 1ms
File: /risk/risk_engine.py

Coordinator:
Primary: Python weighted aggregation function
Fallback: LLM escalation ONLY when conflict detected (~20% of trades)
Conflict: News Analyst and Trade Decision disagree on direction
AND News Analyst confidence > 0.70
File: /llm/coordinator.py

spaCy pre-filter:
Model: en_core_web_lg (local, Hetzner)
Latency: < 50ms
Domain allowlist: ~200 terms that bypass classification automatically
File: /data/prefilter.py

RSS Poller:
Sources: 20+ feeds (AP, Reuters, BBC, Fed, SCOTUS, SEC, Congress, etc.)
Interval: Every 10 seconds
File: /data/rss_poller.py

---

## ROUTING LOGIC

Fast path (< 5 seconds total):
Condition: News Analyst confidence > 0.87
AND category is pre-validated
AND resolution_keyword_cache hit (cached_at < 24 hours)
AND event entities match resolution keywords
Skips: Contract Parser, Trade Decision Agent, LLM Coordinator
Still runs: risk_engine.py (always), Python coordinator

Full pipeline (17-20 seconds total):
All other cases
Order: News Analyst → Contract Parser → Trade Decision → risk_engine.py
→ Python coordinator → (LLM coordinator if conflict)

---

## KEY THRESHOLDS (MEMORIZE THESE)

Min confidence to trade: 0.75
Fast path confidence threshold: 0.87
Confidence hard ceiling: 0.88 (clamp, never exceed)
Min edge to trade: 0.07 (7 cents)
Resolution cache TTL: 24 hours
Max single trade size: 5% of portfolio
Max resolution edge trade: 8% of portfolio
Max category exposure: 30% of portfolio
Max correlated exposure: 20% of portfolio
Min market liquidity to enter: $5,000
Auto-exit liquidity floor: $3,000 (exit at market, no exceptions)
Daily drawdown halt: 8%
Weekly drawdown halt: 15%
Monthly drawdown shutdown: 25%
Health score defensive mode: < 65 (50% sizing, min confidence 0.90)
Health score full halt: < 40
Strategy probation trigger: avg edge < 4 cents over 20 trades
Supabase read timeout: 2 seconds
SiliconFlow timeout: 18 seconds → immediate failover

---

## SUPABASE TABLES (ALL 8)

open_positions — live trades, one row per open position
closed_trades — resolved trades, permanent record
market_signals — every signal detected, action taken
daily_performance — daily P&L, health score, Brier score
agent_memory — episodic lessons injected into LLM prompts
resolution_keyword_cache — parsed contract conditions, 24h TTL
idempotency_log — UUID per order, prevents duplicate submission
layer_c_category_versions — versioned strategic category patterns

Full schemas with all columns and types: PLAN.md Section 9

---

## MEMORY SYSTEM OVERVIEW

Layer A: Statistical — permanent Supabase ledger (never modified)
Layer B: Episodic lessons — agent_memory table, structured JSON triggers,
evidence-based decay, injected into every LLM call
Layer C: Strategic — versioned Supabase rows per category, monthly updates
Layer D: Weekly human audit — 3 worst losses + 2 best wins, Claude API

Decay rule: evidence-based (not time-based)
relevant_trades_since_last_trigger hits 20 without lesson firing → decay step
90-day calendar decay only as fallback for dormant lessons
Lessons below confidence 0.30 → retired = true (never deleted)

Full memory architecture: PLAN.md Section 8

---

## CODING STANDARDS

### General

- Python only. No other languages.
- Type hints on every function signature
- Docstring on every function (one line minimum)
- Explicit error handling on every external call (no bare except)
- All secrets via environment variables. Never hardcoded.
- Log every action with timestamp and component name
- No print statements — use Python logging module

### File structure

/risk/ Pure Python only. Deterministic. No network calls.
/llm/ LLM agent wrappers. Always timeout + fallback.
/execution/ Order submission. Always idempotency check first.
/data/ Data pipeline. RSS poller, spaCy filter, GDELT enrichment.
/memory/ Memory read/write utilities.
/coordinator/ Python aggregation + LLM escalation logic.

### LLM calls (in /llm/ only)

- Every call: prepend agent_memory lessons as warning block
- Every Qwen3-235B call: max_tokens=900, thinking_budget=600
- Every call: 18-second timeout wrapper
- Every call: OpenRouter fallback on timeout
- Log: model used, latency, token count, fallback triggered (yes/no)

### Risk engine (/risk/risk_engine.py)

- Import list must contain: math, decimal, datetime only
- Any reviewer must be able to confirm zero LLM imports at a glance
- Every function must have an explicit unit test in /tests/test_risk.py

### Supabase calls

- Every read: 2-second timeout wrapper
- Every read: hardcoded fallback behavior (see RULE 5 above)
- Never assume Supabase is available

---

## DOMAIN VOCABULARY

Polymarket CLOB: Central Limit Order Book — Polymarket's order matching system
Edge: |agent_probability_estimate - market_price| in cents
Brier score: Probability calibration metric. Target < 0.23. Lower = better.
Kelly fraction: Position sizing multiplier. We use fractional Kelly (0.15-0.35)
Resolution: When a Polymarket market closes and pays out
Fast path: High-confidence route bypassing heavy LLM calls (< 5 seconds)
Correlated exp: Total portfolio % that loses if common shock event reverses
Circuit breaker: Automatic halt triggered by drawdown or health thresholds
Memoryless trade: Trade executed without agent_memory context (Supabase timeout)
Health score: 0-100 composite metric of agent performance and reliability
Regime change: Statistically significant break in win-rate time series
Probation: Strategy continues at 50% position size pending review

---

## WHAT TO DO WHEN UNCERTAIN

1. Check PLAN.md for the exact decision — every threshold is documented there
2. Check MEMORY.md for known mistakes to avoid repeating
3. Check TESTING.md for pass/fail criteria for the current layer
4. If still uncertain: implement the more conservative option and flag it
   Do not guess on thresholds, schemas, or model parameters

---

## LAYER STATUS — CHECK BEFORE DOING ANYTHING

Current status: PROGRESS.md
Never build layer N+1 before layer N is marked Confirmed in PROGRESS.md.
Never mark a layer done without satisfying every criterion in TESTING.md.

---

END OF GEMINI.md
Full architecture detail: PLAN.md
