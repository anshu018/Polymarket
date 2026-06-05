# Polymarket Autonomous Trading Agent

## What This Is

An autonomous AI agent that trades on Polymarket (prediction market platform) 24/7. It monitors open markets, detects mispriced probabilities using a multi-layer data and AI stack, executes trades with deterministic risk controls, and improves its own performance through a structured memory and learning system.

## Core Value

Zero trade executes without passing the pure Python deterministic `risk_engine.py` with fractional Kelly sizing and absolute circuit breakers.

## Requirements

### Validated

<!-- Shipped and confirmed valuable. -->

- ✓ **[REQ-01]** All 8 Supabase schemas created and connection verified — Phase 1
- ✓ **[REQ-02]** Live news data flowing from RSS poller (20+ feeds) into market_signals table — Phase 2
- ✓ **[REQ-03]** News Analyst agent returning structured event classification with timeout behavior — Phase 2
- ✓ **[REQ-04]** Calibration model flagging empirical probability edge >= 7 cents on paper data — Phase 3
- ✓ **[REQ-05]** Pure Python risk manager with Kelly sizing, hard position limits, drawdown circuit breakers, and zero LLM imports — Phase 4
- ✓ **[REQ-06]** DeepSeek V3 Contract Parser returns valid JSON resolution criteria cached for 24 hours — Phase 5
- ✓ **[REQ-07]** Fast path completes under 5 seconds, full pipeline under 22 seconds — Phase 6
- ✓ **[REQ-08]** Pre-trade idempotency log UUID matching prevents duplicate orders — Phase 6
- ✓ **[REQ-09]** Hetzner CX22 running agent continuously with automatic systemctl restart — Phase 7
- ✓ **[REQ-10]** Startup reconciliation runs on every start to diff actual wallet/positions with Supabase state — Phase 7
- ✓ **[REQ-11]** Seven Telegram alerts fire on drawdowns, health score drops, reconciliation halts, and failovers — Phase 7

### Active

<!-- Current scope. Building toward these. -->

- [ ] **[REQ-12]** Minimum 2 weeks paper trading, 20 resolved trades, and rolling Brier score < 0.23 — Paper Trading Gate

### Out of Scope

<!-- Explicit boundaries. Includes reasoning to prevent re-adding. -->

- **[GDELT in Fast Path]** — GDELT has a 15-minute structural lag and is relegated to background recalibration only (RSS is velocity trigger).
- **[Direct .env Reading]** — permanently off-limits to all tests and scripts to prevent raw API keys from appearing in session context (load_dotenv only).
- **[psycopg2 / Direct SQL]** — all database table operations must be synchronous supabase-py client calls with 2-second timeouts.

## Context

- **Developer Guidelines & Rules:** [CLAUDE.md](file:///c:/Users/ash74/OneDrive/Desktop/Polymarket/CLAUDE.md) (Architecture contexts and audit focus), [GEMINI.md](file:///c:/Users/ash74/OneDrive/Desktop/Polymarket/GEMINI.md) (Inviolable Absolute Rules), and [TESTING.md](file:///c:/Users/ash74/OneDrive/Desktop/Polymarket/TESTING.md) (Verification criteria per layer).
- **Episodic Mistake Log:** [MEMORY.md](file:///c:/Users/ash74/OneDrive/Desktop/Polymarket/MEMORY.md) (Living memory of past mistakes. Must be read before writing a single line of code).
- **Server environment:** Hetzner CX22 server ($6/month) running Linux.
- **Database:** Supabase Postgres (free tier).
- **APIs:** SiliconFlow (primary) and OpenRouter (fallback).
- **Alerts:** Telegram bot API.
- **Downtime protection:** Idempotency checks to prevent double order submissions under blockchain/network congestion.

## Constraints

- **LLM Budget:** Total monthly spending hard target of $1.17/month.
- **Execution Safety:** /risk/risk_engine.py is non-negotiable pure Python (under 1ms, zero imports from /llm/, math/decimal/datetime only).
- **SiliconFlow Timeout:** Hard 18-second timeout on trade decisions before failover to OpenRouter.
- **Daily Drawdown:** Circuit breaker halts trading if drawdown exceeds 8%.

## Key Decisions

<!-- Decisions that constrain future work. Add throughout project lifecycle. -->

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| Pure Python Risk Engine | Enforce absolute safety, deterministic controls, and prevent LLM hallucination | ✓ Good |
| RSS poller velocity triggering | Replaces stale GDELT feed on fast path with 10s poller | ✓ Good |
| Pre-submission idempotency log | Generates UUID at decision time and writes it to DB before API call | ✓ Good |
| Local GSD isolated activation | Bridges global GSD tool to project-local `.gemini` folder without symlink wikitasks | ✓ Good |

---
*Last updated: 2026-05-27 after local GSD activation*
