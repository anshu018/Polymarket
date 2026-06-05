# ZERO-ALPHA: Autonomous Polymarket Trading Agent
## Master PRD & Build Handoff Document
### Version: Production Ready | Date: June 2026 | Status: Layer 7 Confirmed (Ready for Paper Trading)

---

## WHO YOU ARE TALKING TO

This document is for a developer or agent continuing work on an autonomous Polymarket trading agent. The developer is **Anshu**, a B.Tech CSE student at JBIT Dehradun, India. The project has been built across many sessions inside the Antigravity IDE.

**Your role:** Senior architect and code reviewer. You write prompts for Anshu to send to the AI coding agent inside Antigravity IDE. You review responses, catch violations, and write the next steps. You do NOT write code directly.

**Antigravity agent:** Always use **Claude Sonnet** or **Gemini 3.5 Flash** inside Antigravity. Ensure they strictly follow instructions, verify tests, and never bypass sandbox constraints.

---

## THE VISION

Build the most powerful autonomous AI agent that trades on Polymarket prediction markets 24/7. It detects mispriced probabilities faster than humans or slow bots, executes trades with mathematically proven risk controls, and gets smarter every week through self-learning.

**The five real edges on Polymarket:**
1. **Speed asymmetry** — news takes 90-480 seconds to fully price in.
2. **Probability miscalibration** — retail systematically misprices base rates.
3. **Correlation blindness** — markets price events independently when they are highly correlated.
4. **Resolution mechanics exploitation** — reading and parsing criteria humans ignore.
5. **Liquidity timing** — wide spreads at market open and 24h before resolution.

---

## INFRASTRUCTURE

| Component | Choice | Reason |
|-----------|--------|--------|
| **Server** | Oracle Cloud Free Tier ARM Ubuntu 22.04 | 4 vCPUs, 24GB RAM, free forever, 24/7 bot execution. |
| **Database** | Supabase free tier (Postgres) | RLS **DISABLED** on all 8 tables. |
| **Chain** | Polygon (Polymarket CLOB) | Fast, low-fee execution. |
| **Alerts** | Telegram bot | Instant 24/7 notifications. |
| **Language** | Python 3.12+ | async/await for all network and database I/O. |
| **Dev machine** | Windows laptop | PowerShell environment. |
| **IDE** | Antigravity | Integrated agent pair programming. |

**Monthly costs:** ~$1.17 LLM + $0 server (Oracle free) = ~$1.17/month total.

---

## IMPLEMENTED AI MODEL STACK (PRODUCTION READY)

To optimize LLM costs and latency, the system utilizes a hybrid model strategy using OpenRouter (primary) and NVIDIA NIM (fallback) for free-tier and low-cost models:

| Agent | Primary Model | Fallback Model | Provider Routing | Cost | Timeout |
|-------|---------------|----------------|------------------|------|---------|
| **News Analyst** | `google/gemma-4-12b-it:free` | `qwen/qwen3-32b` | OpenRouter → NVIDIA NIM | Free / $0.02/M | 10s (6s primary / 4s fallback) |
| **Contract Parser** | `moonshotai/kimi-k2.6:free` | `deepseek-ai/deepseek-v4-flash` | OpenRouter → DeepSeek API → NVIDIA NIM | Free / ~$0.40/mo | 18s (9s / 5s / 4s) |
| **Trade Decision** | `qwen/qwen3-235b-a22b` | `qwen/qwen3-235b-a22b` | NVIDIA NIM → OpenRouter | Free / ~$0.70/mo | 18s (18s primary / 15s fallback) |
| **Risk Manager** | Pure Python | None | Local execution (no API calls) | $0 | <1ms |
| **Coordinator** | Python Aggregation | `qwen/qwen3-32b` (escalation) | NVIDIA NIM → OpenRouter | Free / ~$0.05/mo | 18s |

**Total LLM Cost:** ~$1.17/month (paid fallback headroom).

---

## 7 BUILD LAYERS — EXACT CURRENT STATE

All 7 layers are fully built, tested, and confirmed:

```
Layer 1 — Foundation:        CONFIRMED ✓ (All 8 Supabase tables created and verified)
Layer 2 — Data Pipeline:     CONFIRMED ✓ (RSS feed poller, spaCy pre-filter, and News Analyst)
Layer 3 — Calibration:       TESTED     (Calibration curves mapped, 3.5 Brier score pending PT data)
Layer 4 — Risk Engine:       CONFIRMED ✓ (Pure Python controls, 19/19 pass, 63 tests pass)
Layer 5 — Contract Parser:   CONFIRMED ✓ (Kimi K2/DeepSeek parser + 24h keyword cache)
Layer 6 — Integration:       CONFIRMED ✓ (Dual-path integration pipeline, 10/10 pass)
Layer 7 — Deployment:        CONFIRMED ✓ (Continuous service, startup reconciliation, Telegram alerts)
```

---

## COMPLETE CODEBASE FILE TREE

The project contains the following file structure:

```
Polymarket/
├── config.py                    ← 57 constants, environment configs, and thresholds
├── main.py                      ← Startup entry point, reconciliation, & pipeline launch
├── requirements.txt             ← Python dependencies
├── Dockerfile                   ← Deployment configuration
├── polymarket-agent.service     ← systemd service configuration for 24/7 run
├── run_wrapper.py               ← Test execution subprocess wrapper
├── check_env.py                 ← Environment validation utility
├── verify_l5.py                 ← L5 Cache & parsing verification script
├── .env.example                 ← Secrets configuration template
├── .env.test                    ← Test environment variable overrides
├── .gitignore                   ← Protecting .env and dependencies
├── .geminiignore                ← Directing Antigravity file reads
├── data/
│   ├── __init__.py
│   ├── pipeline.py              ← RSS -> spacy_filter -> News Analyst async queue
│   ├── rss_poller.py            ← 19 feeds poller, 10s polling interval
│   └── spacy_filter.py          ← Dev passthrough / prod spaCy entity pre-filter
├── llm/
│   ├── __init__.py
│   ├── news_analyst.py          ← Gemma 4 / Qwen3-32B OpenRouter classification
│   ├── contract_parser.py       ← Kimi K2 / DeepSeek V3 parsing + 24h keyword cache
│   ├── trade_decision.py        ← Qwen3-235B decision agent + agent_memory warnings
│   └── coordinator.py           ← Python weighted aggregator / LLM escalation
├── coordinator/
│   ├── __init__.py
│   └── pipeline.py              ← Dual-path integration, risk gates, & idempotency
├── execution/
│   ├── __init__.py
│   ├── polymarket_auth.py       ← Single source of truth for clob-client derivation
│   └── reconciliation.py        ← Startup balance & position reconciliation
├── memory/
│   ├── __init__.py
│   ├── migrations.py            ← Automated Supabase table schemas creation
│   └── supabase_client.py       ← Thread-safe sync client wrapped in async getters
├── monitoring/
│   ├── __init__.py
│   └── telegram_alerts.py       ← HTML-formatted Telegram alert dispatcher
├── strategies/
│   ├── __init__.py
│   └── calibration.py           ← Brier score, empirical edge, category curves
├── risk/
│   ├── __init__.py
│   └── risk_engine.py           ← 11 pure Python deterministic functions (<1ms)
├── tests/
│   ├── __init__.py
│   ├── run_layer1.py            ← Foundation test runner
│   ├── test_alerts.py           ← Telegram alert triggers verification
│   ├── test_integration.py      ← End-to-end routing, timeouts, fallbacks (10 tests)
│   ├── test_parser.py           ← Contract parsing JSON output schema verification
│   ├── test_reconciliation.py    ← Startup reconciler diff & resolved trade tests
│   └── test_risk.py             ← Pytest unit tests for all 11 risk controls
└── .planning/                   ← GSD workflow configuration and roadmap tracking
    ├── config.json
    ├── PROJECT.md
    ├── ROADMAP.md
    └── STATE.md
```

---

## DATABASE SCHEMAS & TABLES

The Supabase database has all 8 tables configured with Row Level Security (RLS) disabled for internal access:

1. **`open_positions`** — Live active positions, one row per trade.
2. **`closed_trades`** — Resolved trade records (immutable historical ledger).
3. **`market_signals`** — Raw news signals ingested, categorized, and acted upon.
4. **`daily_performance`** — Daily rolling P&L, Health score, Brier score.
5. **`agent_memory`** — Episodic memory warnings used in LLM prompts.
6. **`resolution_keyword_cache`** — Parsed criteria cache (TTL: 24 hours).
7. **`idempotency_log`** — UUID-to-order-ID mapping to block duplicates.
8. **`layer_c_category_versions`** — Versioned strategic category metrics.

---

## DETAILED ROUTING FLOWS & LOGIC

### 1. Fast Path Routing (Target: <5 seconds)
* **Trigger Conditions:**
  * News Analyst confidence score > 0.87.
  * Event category is pre-validated (e.g. `politics`, `crypto`, `sports`, `legal`, `economics`, `science`).
  * `resolution_keyword_cache` hit (fresh entry <24 hours old).
  * Headline entities match cached keywords.
* **Flow:** News Analyst → Risk Engine → Python Coordinator → Order Submission.
* **Skips:** Contract Parser, Trade Decision Agent, and LLM Coordinator.

### 2. Full Pipeline Routing (Target: 17–20 seconds, hard cap 22 seconds)
* **Trigger Conditions:** All other incoming classified signals.
* **Flow:** News Analyst → Contract Parser → Trade Decision Agent → Risk Engine → Python Coordinator → LLM Coordinator (if conflict escalates) → Order Submission.

### 3. Conflict Escalation (Coordinator)
* Escalates to the LLM Coordinator (`qwen/qwen3-32b`) under an 18-second timeout if and only if:
  * News Analyst and Trade Decision Agent disagree on trade direction.
  * News Analyst confidence is > 0.70.
* Otherwise, defaults to Trade Decision Agent (if News Analyst confidence <= 0.70) or applies standard Python weighted confidence aggregation (`0.4 * news + 0.6 * trade`).

---

## DEGRADATION & SAFETY CONTROLS (THE ABSOLUTE RULES)

### RULE 1 — Pure Python risk_engine.py
No external dependencies or LLMs. Allowed imports: `math`, `decimal`, `datetime`, `logging`, `config`. Evaluates under 1ms.

### RULE 2 — Pre-Order Idempotency
Every trade generates a unique UUID. The UUID is written to `idempotency_log` as `pending` BEFORE the API call. Any retry checks the log first; if already `confirmed`, the trade is skipped. Fail closed (halt order) if Supabase is unavailable.

### RULE 3 — Trade Decision Constraints
Qwen3-235B is restricted to `max_tokens = 900` and `thinking_budget = 600`. Confidence scores are capped at `0.88` to prevent epistemic hubris.

### RULE 4 — Startup Reconciliation
Runs on process startup before any signal ingestion. Queries actual wallet balances and shares from Polymarket, diffs against Supabase `open_positions`, reconciliation is written, and halts on unresolvable inconsistencies.

### RULE 5 — Supabase 2s Timeout Degradation
* `resolution_keyword_cache` read times out → Fall back to Full Pipeline.
* `agent_memory` read times out → Proceed with trade, flag `was_memoryless = True`.
* `idempotency_log` read/write times out → FAIL CLOSED (halt order).
* `open_positions` read times out → Halt trading until database succeeds.
* `layer_c_category_versions` read times out → Use conservative hardcoded defaults.

### RULE 6 — LLM 18s Timeout Failover
NVIDIA NIM (primary) has an 18s timeout wrapper. On timeout, immediately cancel, log, and failover to OpenRouter (exact same prompt).

### RULE 7 — Windows Terminal Output Capture
All shell runs of python test scripts must use the subprocess capture pattern to prevent PowerShell output truncation.

---

## TELEGRAM ALERTS MATRIX

The `telegram_alerts.py` module dispatches alerts matching these 10 events:

| Severity | Event Type | Trigger | Detail |
|----------|------------|---------|--------|
| **CRITICAL** | `SYSTEM_HALT` | Startup credential derivation crash / DB errors | HALT trading immediately. |
| **CRITICAL** | `CIRCUIT_BREAKER` | Daily drawdown >8%, Weekly >15%, Monthly >25% | HALT trading. No new trades. |
| **CRITICAL** | `RECONCILIATION_FAILURE` | Inconsistencies between Supabase and Wallet | HALT startup sequence. |
| **WARNING** | `DB_DEGRADATION` | Supabase read times out (>2s) | Table name + fallback applied. |
| **WARNING** | `LLM_FAILOVER` | Primary LLM timeout (>18s) | Latency in ms + fallback model. |
| **WARNING** | `DUPLICATE_ORDER_BLOCKED` | Idempotency retry checks | Blocked duplicate order info. |
| **WARNING** | `KPI_DEGRADATION` | Rolling Brier score > 0.23 | Retraining trigger flag. |
| **ERROR** | `COMPONENT_CRASH` | Continuous queue worker crash | Error traceback + restart. |
| **INFO** | `AGENT_STARTUP` | Process restart | Environment + PaperTrading flags. |

---

## WHAT NEEDS TO HAPPEN NEXT (THE ROADMAP)

Now that Layers 1–7 are complete and confirmed, the codebase is structurally intact and fully verified by unit and integration tests.

### 1. NEXT — Build Market Discovery Module
The final missing link is matching classified news signals with live Polymarket markets. Create `/data/market_discovery.py` using the `py_clob_client` and Polymarket Gamma API to:
* `async def get_open_markets(query_text: str) -> list[dict]` — Fetch active markets matching the signal keywords.
* `async def get_market_price(token_id: str) -> float` — Query the current CLOB midpoint price.
* `async def get_market_metadata(market_id: str) -> dict` — Fetch market question and resolution criteria.

### 2. THEN — Seed Calibration Data
Layer 3 (Calibration) needs 50+ historical records in `closed_trades` to compute empirical calibration curves. This will be seeded during the initial phase of paper trading.

### 3. FINALLY — Enter Paper Trading Gate (Minimum 2 Weeks)
Configure `config.py` with `PAPER_TRADING = True` and run the continuous loop on the Oracle/Hetzner server for a minimum of 2 weeks. The gate to deploy live capital ($1,000 POC) requires:
* Minimum 20 resolved paper trades.
* Composite Brier score < 0.23.
* Zero process crashes.
* Verified execution speeds (Fast Path <5s, Full Pipeline <22s).
