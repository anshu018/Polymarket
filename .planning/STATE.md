---
gsd_state_version: 1.0
milestone: v1.0
milestone_name: milestone
status: active
last_updated: "2026-07-05T13:58:00.000Z"
progress:
  total_phases: 9
  completed_phases: 8
  total_plans: 9
  completed_plans: 9
---

# Project State

## Project Reference

See: .planning/PROJECT.md (updated 2026-06-06)

**Core value:** Zero trade executes without passing pure Python deterministic risk_engine.py with fractional Kelly and absolute circuit breakers.
**Current focus:** Hermes-Adapted Self-Learning & Memory System

## Current Position

Phase: 9 of 9 (Hermes-Adapted Self-Learning & Memory System)
Plan: 0 of 0 in current phase
Status: Active
Last activity: 2026-06-07 — Added Phase 9 for adapting NousResearch/hermes-agent self-learning memory system.

Progress: [████████░░] 88%

## Performance Metrics

**Velocity:**
- Total plans completed: 9
- Average duration: TBD
- Total execution time: TBD

**By Phase:**

| Phase | Plans | Total | Avg/Plan | Status |
|-------|-------|-------|----------|--------|
| Phase 1 | 1 | — | — | Complete |
| Phase 2 | 2 | — | — | Complete |
| Phase 3 | 1 | — | — | Complete |
| Phase 4 | 1 | — | — | Complete |
| Phase 5 | 1 | — | — | Complete |
| Phase 6 | 1 | — | — | Complete |
| Phase 7 | 1 | — | — | Complete |
| Phase 8 | 1 | — | — | Complete |
| Phase 9 | 0 | — | — | Active |

*Updated after each plan completion*

## Accumulated Context

### Roadmap Evolution

- Phase 9 added: Hermes-Adapted Self-Learning & Memory System

### Decisions

Decisions are logged in PROJECT.md Key Decisions table.
Recent decisions affecting current work:

- [Phase 4]: risk_engine.py implemented as deterministic pure-Python with 100% unit test coverage.
- [Phase 5]: Local project isolated GSD commands copies chosen over Windows junctions to prevent accidental global data loss and clean git trees.
- [Phase 8]: Cache-based market discovery module implemented to map signals using in-memory entity overlap scoring and real-time CLOB midpoint pricing checks.
- [Phase 9]: Wired ingestion pipeline to coordination/trading pipeline. Resolved pipeline disconnect bug, end-to-end routing active, model IDs corrected, startup validation added. First live signals confirmed 2026-06-15, pipeline end-to-end verified.
- [Strategy 5 / Copy Trade — Phase 2, commit 142bc53, 2026-07-05]: Trust scoring, wallet state machine, and Priority conflict resolution implemented and committed. Key details:
  - Bayesian trust formula: `(wins + 5) / (wins + losses + 10)` — weak prior dampens small-sample luck.
  - 4-state wallet lifecycle: NEW (0.5× size) → ACTIVE (full size) → PROBATION (0.5× size) → RETIRED (is_active=false, audit trail kept).
  - `is_priority` derived flag: trust_score ≥ 0.80 AND resolved_trades_count ≥ 30 — wins conflicts automatically in classifier.
  - Schema migration applied live: `tracked_wallets` +10 columns (state, resolved_trades_count, wins_count, losses_count, trust_score, avg_roi_per_trade, is_priority, probation_entered_at, probation_resolved_at_entry, last_updated_at); `copytrade_log` +3 columns (was_priority_pick, pnl_percent, wallet_address). 13 new columns total.
  - Files rewritten: `copytrade/performance_tracker.py`, `copytrade/executor.py`, `copytrade/classifier.py`, `copytrade/poller.py`, `tests/test_copytrade_trust.py`.
  - Test suite: 75/75 pass — `test_copytrade.py` (25 tests) + `test_copytrade_trust.py` (50 tests).
  - Single source of truth: `tracked_wallets` only (trader_performance table kept as dead code — never read or written, preserved to avoid breaking Railway migration runner).
- [Strategy 5 / Copy Trade — Phase 2 bug fixes, commit c530c02, 2026-07-05]: Two post-Phase-2 live bugs found and fixed. 80/80 tests pass.
  - **Bug 1 (critical):** `resolve_copy_trade()` in `performance_tracker.py` had zero call sites — trust scores never updated after any market resolution. Fixed by wiring it into `execution/reconciliation.py` immediately after `_supabase_move_to_closed()` for `copy_edge_class_*` positions. Lookup uses `idempotency_uuid` (not `market_id+direction`) to prevent multi-wallet attribution error. Call is `await` / inline — not fire-and-forget — so trust scores are current before next trade sizes.
  - **Bug 2 (risk control gap):** `copytrade/executor.py` re-implemented the 5% portfolio cap inline instead of calling `risk_engine.position_size_check()`. Fixed: class ceiling ($10) still applied in executor (copy-trade business logic), then final size routed through `position_size_check(..., strategy="copy_trade")` — same as `coordinator/pipeline.py:733` for all other strategies.
  - **New test:** `test_copy_trade_resolution_updates_trust_score` in `test_reconciliation.py` — integration test that seeds open_positions + copytrade_log + tracked_wallets, runs paper-mode reconciliation, and asserts `wins_count` and `trust_score` actually changed. Would have caught Bug 1 before commit.
- [Strategy 5 / Copy Trade — naming + guard hardening, commit 5c7ff40, 2026-07-05]: Three follow-on fixes. 82/82 tests pass.
  - **Naming:** `copytrade/executor.py` `position_size_check()` call now uses `strategy="copy_edge_class_a"` (the canonical label already written to `open_positions` at line 161) instead of the orphan `"copy_trade"` string that existed nowhere else. Behaviour unchanged (still falls to 5% cap).
  - **Guard hardened:** `performance_tracker.resolve_copy_trade()` silent `market_id+direction` fallback removed. Missing `idempotency_uuid` now logs CRITICAL and returns early — a silent guess would corrupt a specific wallet's trust score without any visible signal. `idempotency_uuid` is always set on copy-trade `open_positions` rows (generated pre-order, written at `executor.py:166`), so the fallback was dead code in practice.
  - **Negative tests added:** (1) `test_non_copy_trade_resolution_does_not_call_resolve_copy_trade` — loops over all four standard strategies (`velocity`, `resolution`, `recalibration`, `fast_path`), patches `_resolve_copy_trade` with a call counter, asserts zero calls each time. (2) `test_resolve_copy_trade_missing_uuid_logs_critical_and_skips` — calls `resolve_copy_trade(idempotency_uuid=None)` directly and asserts CRITICAL is logged before any DB access.
- [Strategy 5 / Copy Trade — Class B pipeline integration + documentation, commit 70cfade, 2026-07-05]: Fixed strategy tagging and logging for Class B copy trades. 97/97 tests pass.
  - **Class B Strategy Tagging:** Wired a strategy override path through `coordinator/pipeline.py`. If a signal is a Class B copy trade, it overrides strategy as `"copy_edge_class_b"`. This ensures the startswith("copy_edge_class_") reconciliation guard resolves Class B copy trades (previously, they were logged as "velocity"/"recalibration" and ignored on resolution, corrupting trust scores).
  - **Class B Sizing & Logging:** Fixed Class B copy trades bypassing the `copytrade_log` database table completely. They now correctly log to `copytrade_log` upon successful order execution, allowing future resolutions to calculate ROI/trust. Sizing logic uses the correct `config.KELLY_FRACTION_COPY` (0.10) and clamps the raw Kelly size under the wallet's trust-dampened ceiling ($50 max).
  - **E2E Integration Test:** Added `test_copy_trade_class_b_end_to_end_flow` to `tests/test_integration.py`. This test seeds tracked_wallets, runs the pipeline with the copy trade signal (which tags strategy correctly and writes to copytrade_log), simulates paper-mode resolution, and asserts trust score updates. Also updated `MockTableBuilder` in `test_integration.py` to support `delete()` and auto-generate UUID `id` to avoid KeyErrors.
  - **Documentation:** Updated Section 4 sizing pseudocode in `CopyTrade.md` to reflect canonical `copy_edge_class_a` and `copy_edge_class_b` strategy labels.


### Pending Todos

- **Phase 3 paper validation (Copy Trade)** — gated on 20 resolved copy-trades before any live execution work begins. Must confirm: (1) state transitions fire correctly at 20-trade boundaries, (2) Brier score < 0.20 on resolved copy-trades, (3) at least one wallet reaches ACTIVE state, (4) zero circuit-breaker fires from copy-trade logic errors, (5) paper-mode fills confirmed logged to open_positions/closed_trades.

### Blockers/Concerns

- **Market discovery gap — FIXED (2026-06-15)**: Root cause was Gamma API returning only 20 markets by default (API hard limit: 100/page, no pagination in original code). Fixed by adding offset-based pagination loop in `refresh_market_cache()`. EU-West confirmed working: `[MARKET_DISCOVERY] Cache refreshed: 4642 active markets.`

- **Entity matching fix — DEPLOYED (2026-06-15)**: Generic stop words (price, surges, court, gas, execution, nitrogen, etc.) were diluting match scores below 0.30 threshold. Added `_ENTITY_STOP_WORDS` frozenset in `coordinator/pipeline.py`. Verified: Alabama/Bitcoin/Trump headlines now score >0.30, fisheries noise still scores 0.00. No new dependencies.

- **News analyst timeout — FIXED (2026-06-15)**: 52% of signals timing out with `conf=None`. Root cause: hardcoded 6s timeout vs. free-tier Gemma 4 31B taking 8-12s. Changed `NEWS_ANALYST_TIMEOUT_SECONDS = 15` in config.py. Both primary and fallback calls now use config value. Awaiting first `idempotency_log` entry to confirm full trade path is live.

- **RSS feed quality — FIXED (2026-06-16)**: 38% of active feeds were LOW VALUE (Federal Register, PACER dockets, ClinicalTrials, Congress bills, DOJ releases, UN notices) flooding pipeline with regulatory noise. Replaced with 17 verified high-signal feeds. All 17 URLs verified live before commit.

- **News Analyst Fallback Timeout — FIXED (2026-06-16)**: High rate of `conf=None` (timeout/abstain) signals persisted even after increase of primary timeout. Discovered fallback NVIDIA NIM requests sometimes take 5-8 seconds. Since fallback timeout was hardcoded to `NEWS_ANALYST_TIMEOUT_SECONDS / 2` (7.5s), it frequently timed out when primary OpenRouter returned 429 immediately. Implemented dynamic fallback timeout using remaining budget (`max(10.0, limit - elapsed)`), and increased startup model validation probe timeout from 5.0s to 12.0s.

- **Market Discovery pagination concurrency, SiliconFlow integration & Fail-Fast — FIXED (2026-06-19)**:
  - Switched `data/market_discovery.py` page loop to `asyncio.gather` for concurrent fetching, increasing page limit to 500 across 5 pages. Fixed empty Telegram crash detail formatting.
  - Implemented free-only routing (SiliconFlow as primary News Analyst, NVIDIA NIM as fallback; NVIDIA NIM as primary Trade Decision/Coordinator, OpenRouter as fallback).
  - Added immediate fail-fast handling on HTTP status `[401, 402, 403]` raising `LLMFailFastError` in `/llm/` wrappers for rapid failover.
  - Standardized integration test suites to clear caching contamination and mock `asyncio.sleep` to run in 11s instead of 100s.

- **Market cache regression from commit 84abb88 — FIXED (2026-06-29)**:
  - **Root cause:** Commit `84abb88` (2026-06-20) changed page-level exception handling to silently swallow failures and commit partial results. Before it, any single page failure aborted the whole refresh (too aggressive). After it, partial failures were swallowed at WARNING level (not aggressive enough). On Railway, intermittent Gamma API failures caused some pages to fail silently, producing a cache of ~3 markets instead of 4,600+. The cache was committed without any CRITICAL-level indication.
  - **Timeline:** Phase 8 confirmed 4,642 markets on 2026-06-15. Commit `84abb88` landed 2026-06-20 — 5 days after Phase 8 verification. Regression introduced AFTER Phase 8 was confirmed working.
  - **Fix 1 — Floor guard (Option A):** Added `MIN_MARKET_CACHE_SIZE = 100` in `config.py`. In `refresh_market_cache()`, if a healthy prior cache exists (size > 0, non-None timestamp) and the new result falls below the floor, the refresh is rejected (CRITICAL logged) and the prior cache is kept intact. Cold-boot (no prior cache) accepts any non-empty result. Cache mutation is in-place (`.clear()` + `.extend()`) — no reassignment — so no module holding a reference to the cache object goes stale.
  - **Fix 2 — Page-level CRITICAL logging:** Upgraded per-page exception logging from `WARNING` to `CRITICAL` with full `type(e).__name__: {str}` detail. All pages that fail now produce prominent log lines instead of being silently skipped.
  - **Fix 3 — Network self-test:** Added `_network_self_test()` that runs at the start of every `refresh_market_cache()` call. DNS-resolves `gamma-api.polymarket.com` and `clob.polymarket.com`, logs resolved IPs, and tests TCP port 443 connectivity with a 3-second timeout. Runs regardless of what follows — provides immediate diagnosis of DNS hijack / ISP blocking (confirmed on dev machine: both resolve to `49.44.79.236`, an Indian-ISP IP, not Polymarket infra).
  - **Tests:** 109 passed before, 109 passed after. No test changes.
  - **Local dev observation:** Dev machine cannot reach Polymarket APIs at all (DNS hijack to `49.44.79.236`). All diagnostics log correctly. Railway deployment needed for production verification.

## Session Continuity

Last session: 2026-07-05
Stopped at: Class B pipeline integration, database logging, and testing complete (commit 70cfade). Class B now tags strategy=copy_edge_class_b and logs to copytrade_log, E2E integration test added, MockTableBuilder updated, documentation synchronized. 97/97 tests pass. Phase 3 paper validation is the next gate before live execution.
Resume file: None
