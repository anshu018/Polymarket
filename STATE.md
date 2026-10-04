# STATE.md — Agent State and Validation Report

## 1. RSS Feeds Configuration
- **Total Feeds**: 23 (target: 15–25)
- **Status**: Checked and verified active (all return 200 OK and have entries).
- **Cleanup Details**:
  - Removed low-signal/noisy feeds (Federal Register, PACER dockets, environmental/fisheries notices, Coast Guard zone alerts).
  - Retained high-signal feeds: AP News (via Google), Federal Reserve (FOMC announcements), sports (ESPN), crypto (CoinDesk updated to feedburner link, CoinTelegraph), and platform search feeds (Polymarket, Kalshi, Metaculus via Google).
  - Added new high-signal feeds: BBC News World, Politico, NYT World, WSJ World, The Hill.

## 2. Test Verification
- **Total Tests Run**: 220
- **Passed**: 220
- **Failed**: 0
- **Coverage**: 100% green across unit and integration tests (83 risk, 12 tranche gate, 15 pipeline integration, 75 copytrade, 35 parser/discovery)
- **Verification Result**: PASS

## 3. Fallback Model Update
- **Observation**: The original fallback model `qwen/qwen3-next-80b-a3b-instruct` on NVIDIA NIM was unresponsive (timed out at 25s/35s), causing the Railway container startup validation probe to fail.
- **Action**: Updated `MODEL_NEWS_ANALYST_FALLBACK` to `meta/llama-3.3-70b-instruct` in `config.py` and `llm/news_analyst.py`. Verified that its latency is exceptionally fast (~1.74s) and it successfully runs model validation and signal processing on Railway.
- **Unit Test Coverage**: Adjusted mock model matchers in `tests/test_integration.py` to match the new Llama model name, ensuring all 104 tests pass successfully.

## 4. Confidence Score Distribution of Last 50 Signals
- **Count with conf = None (Error/Timeout)**: 2
- **Count with conf = 0.0**: 4
- **Count with conf 0.01-0.74**: 41
- **Count with conf >= 0.75**: 3 (6.0%)
- **Top Recent High-Confidence Signals**:
  - `[2026-06-15T22:07:48] conf=0.80 | headline: U.S. Open: Ranking favorites, contenders, more`
  - `[2026-06-15T22:05:54] conf=0.85 | headline: CFTC sues New Mexico over prediction market jurisdiction`
  - `[2026-06-15T22:04:32] conf=0.80 | headline: Bitcoin shoots higher on Iran peace deal, with Strait of Hormuz set to open`
- **Result Details**: High-signal feeds are successfully filtering out low-relevance noise, resulting in significantly fewer `0.0` confidence signals than before (which were previously 99%+ of all signals).

## 5. Diagnostic Instrumentation Pass
- **Status**: Completed
- **Tasks**:
  - Task 1: Fix market cache background loop in `main.py` to run as a repeating loop every 300 seconds and check cache size at startup. [x]
  - Task 2: Add stage-by-stage drop counters with `[PIPELINE][DROP:*]` tags in `coordinator/pipeline.py`. [x]
  - Task 3: Add pipeline stats counter and stats reporter task in `data/pipeline.py`. [x]
  - Task 4: Log OpenRouter HTTP status and rate-limiting warnings in `llm/news_analyst.py`. [x]

## 6. Strategy 5: Copy Edge — CopyTrade Phase 2 Complete
- **Status**: Phase 2 COMPLETE. Phase 3 = paper validation (need 20 resolved copy-trades before live).
- **Test Results**: **75/75** tests pass — `test_copytrade.py` (25 tests) + `test_copytrade_trust.py` (50 tests).
- **Supabase Migration Applied (live, 2026-07-05)**:
  - `tracked_wallets` +10 columns: `state TEXT DEFAULT 'NEW'`, `resolved_trades_count INT DEFAULT 0`, `wins_count INT DEFAULT 0`, `losses_count INT DEFAULT 0`, `trust_score DECIMAL DEFAULT 0.5000`, `avg_roi_per_trade DECIMAL DEFAULT 0.0`, `is_priority BOOL DEFAULT false`, `probation_entered_at TIMESTAMPTZ`, `probation_resolved_at_entry INT DEFAULT 0`, `last_updated_at TIMESTAMPTZ`.
  - `copytrade_log` +3 columns: `was_priority_pick BOOL DEFAULT false`, `pnl_percent DECIMAL`, `wallet_address TEXT`.
  - Existing rows backfilled: `state='NEW'`, `trust_score=0.5000`, `is_priority=false`.
- **Core Logic Rewrites**:
  - `performance_tracker.py` — Bayesian formula `(wins+5)/(wins+losses+10)`, 4-state machine (NEW/ACTIVE/PROBATION/RETIRED), Priority at trust≥0.80 AND resolved≥30, single source of truth (tracked_wallets only).
  - `executor.py` — `raw_size = COPY_CLASS_A_MAX_SIZE_USDC × state_multiplier × trust_score` (line 285); paper-mode no-op bug fixed (writes to open_positions, line 347).
  - `classifier.py` — Priority §3.6 conflict resolution; `was_priority_pick` audit flag on signals.
  - `poller.py` — fetches full wallet row including state, trust, priority, counts.
- **Write Order (Class A execution)**:
  1. `idempotency_log` → status=`pending` (line 322) — BEFORE order
  2. Paper fill (line 325–335) / live order (line 336–342)
  3. `idempotency_log` → status=`confirmed` (line 345)
  4. `open_positions` row inserted (line 347)
  5. `copytrade_log` row inserted (line 358)
  - On resolution: `copytrade_log` row closed (exit_price/pnl_usdc/pnl_percent), `tracked_wallets` atomically updated (wins/losses/trust/state).
- **dead code**: `memory/migrations.py:219` creates `trader_performance` table at startup but nothing reads or writes it (kept to avoid breaking Railway cold-start migration runner).
- **Files Created/Modified**: `copytrade/performance_tracker.py` (rewrite), `copytrade/executor.py`, `copytrade/classifier.py`, `copytrade/poller.py`, `tests/test_copytrade_trust.py` (rewrite), `scratch/migration_copytrade_phase1.sql`.
 
## 7. Per-Market Tranche Gate (Dedupe & Anti-Concentration)
- **Status**: COMPLETE & VERIFIED (Commits `44587de` through `fea3a0a`).
- **Core Invariants**:
  - `MAX_MARKET_TRANCHES = 2`: At most 2 entries per market; third entry strictly blocked.
  - `REPEAT_MIN_CONFIDENCE = 0.87`: High-conviction threshold required for repeat entries.
  - `REPEAT_ENTRY_PCT = 0.03`: Second entry strictly capped at 3% total portfolio.
  - `MIN_ADD_TICKET_USDC = 25.0`: Enforces $25 minimum add ticket on repeats only (first entries unaffected).
  - `MAX_MARKET_TRADE_PCT = 0.08`: Cumulative exposure per market capped at 8%.
  - `opposite_direction_open`: Blocks opposite-direction trades on existing active markets.
  - `get_market_lock(market_id)`: Process-wide `asyncio.Lock` keyed strictly on `market_id` alone, held through state query to `open_positions` insertion.
  - `load_market_state`: Supabase state query offloaded via `asyncio.to_thread` with strict 2-second timeout, failing closed (`None`).
  - Pre-checks before expensive LLM calls in `coordinator/pipeline.py` and `copytrade/executor.py` (`_execute_class_b`) to eliminate unnecessary token burn.
- **Paths Protected**: Fast Path, Full Pipeline, Copy Edge Class A, Copy Edge Class B.
- **Test Coverage**: 12 dedicated unit tests (`TestMarketPositionCheck` in `tests/test_risk.py`) + 12 end-to-end integration tests (`tests/test_dedupe_gate.py`). All 24 tests pass.

## 8. Active AI Model Pipeline & Budget Optimization
- **News Analyst**: `typesafe/jev-1.13` via TokenRouter primary (~535ms latency, ~$0.000025/call). Fallback: `qwen/qwen3.5-flash` (`enable_thinking=False`, max 200 tokens). Early drop on `ABSTAIN` cuts downstream token burn by ~60%.
- **Contract Parser**: `qwen/qwen3.8-flash` via TokenRouter (`enable_thinking=False`, 18s timeout, cached 24h in Supabase).
- **Trade Decision**: `deepseek/deepseek-v4.1-flash` via TokenRouter (`thinking: {"type": "disabled"}`, `max_tokens=300`, structured 3-step reasoning). Fallback: `qwen/qwen3.5-flash`.
- **Coordinator**: Pure Python weighted aggregation primary (<0.1ms), LLM escalation only on high-confidence conflicts (>0.70).
- **Burn Rate**: Optimized to ~$0.012 - $0.015/day (~$0.40/month), safely under the $0.75/month hard budget.
