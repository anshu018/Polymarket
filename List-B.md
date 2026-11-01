# List-B.md — Part B: "Safety and Lifecycle" — Master Plan & Progress Tracker

> **This file is the single source of truth for Part B work.** Read it fully before doing any
> work on List B. Update the Master Tracker, per-step checkboxes, and Session Log every session.
> If context is ever lost, this file is the recovery point.
> Companion documents: `List A.md` (Part A — COMPLETE, kept as recovery point), `CLAUDE.md`
> (architecture invariants — read it second, always).

---

## 0. STATUS DASHBOARD

| Field | Value |
|---|---|
| Created | 2026-10-30 |
| Last updated | 2026-11-01 (Step 4 DONE locally — 678/678; NOT yet deployed) |
| Current phase | Part B execution — Steps 1–3 **DONE & DEPLOYED LIVE** (deployment c8731511); **Step 4 DONE locally** (678/678 tests; the ONE shared close path + runtime reconciliation + in-flight visibility) — one `railway up` activates it |
| Current step | Next action is **Step 5** (B5: exit engine — the biggest step; consumes Step 4's shared close path). Owner: `env -u RAILWAY_API_TOKEN railway up` to activate Step 4 live (no schema change — startup recon + the runtime loop come alive on restart) |
| Overall status | **IN PROGRESS** — Steps 1–3 complete and LIVE; Step 4 complete locally; Steps 5–7 not started |
| ✅ Crash watch (RESOLVED 2026-11-01, D-B16) | Root cause measured: the 708 MB spaCy-lg model + the old healthcheck spawning a ~200MB second Python every 60s (3 slow checks during a signal burst or the startup window = Docker killed the container — matches all 3 hard kills). Fixed: spaCy sm (10/10 identical filter decisions, ~620MB freed), in-process /health probe (curl, start-period 120s, true liveness), MALLOC_ARENA_MAX=2. Deployment 084671df live and stable 11+ min past every historical kill window. If a crash ever recurs, the new signature will be diagnostic (healthcheck events visible; endpoint silence = hang, not memory) |
| Part A state | Steps 0–5 **DONE & independently verified 2026-10-30** (536/536 tests, grep gates clean, wiring read-confirmed). Step 6 (A3 historical data) **PARKED** — operator lacks disk space for the ~47GB HF download; nothing in List B depends on it. |
| Live-pipeline safety state | ✅ SAFE: main news pipeline dormant by design (estimator fail-closed → `estimate:no_data`); Copy Edge Class A is the only strategy producing positions (paper, book-aware fills); all entries require net_edge > 2¢ after verified costs; Kelly never sees LLM confidence. **PAPER_TRADING=true everywhere.** Step 1 added: entries additionally fail closed on stale/unavailable equity (`equity:stale`/`equity:unavailable`). |
| 🔒 HARD OPERATOR DIRECTIVE (2026-10-30) | **No live capital until: ≥2 weeks of paper trading pass AND the paper-trading gate PT.1–PT.10 (PROGRESS.md) is fully checked AND the gate report prints PASS.** Every step below must preserve this. Real CLOB order code is written in Step 7 but stays unreachable while `PAPER_TRADING=true` (double interlock, D-B07). |
| ⚠️ Owner actions still open (carried from List A) | (1) Apply the Step 3 + Step 5 migrations to the live Supabase **or restart the bot** (migrations.py runs at startup). (2) Schedule the three weekly offline scripts owner-side: `python -m scripts.gate_report`, `python -m scripts.strategy_dashboard`, `python -m scripts.signal_drift_report`. (3) Decide on the RLS-disabled advisor finding (critical; enabling without policies locks the bot out). |

**How to resume work (any future session):**
1. Read this file top to bottom.
2. Check `STATUS DASHBOARD` + `Master Tracker` for the current step.
3. Read the current step's spec (Section 6) in full — including its DO-NOT list.
4. Read the last 3 entries of the `Session Log` (Section 12).
5. Re-verify the evidence rows you are about to touch (line numbers may have drifted).
6. Work. Then update: dashboard, tracker, checkboxes, session log.

---

## 1. HOW TO WORK ON THIS LIST — persona, discipline, flexibility

### 1.1 Persona

You are a **senior quantitative engineer and skeptical code reviewer** with production
trading-systems experience. You behave like the third engineer on a two-person team:
you plan before you type, you verify claims against the code instead of trusting docs,
you write production-grade code with tests, you review your own diff like an adversary
before committing, and you report outcomes plainly (including failures and skipped
work). You never ship a "temporary" fix — if a proper fix is out of scope, you record
it in the Session Log as a noticed issue instead.

### 1.2 Operating discipline (non-negotiable)

1. **ONE STEP AT A TIME**, in the Section 5 work order. Never start Step N+1 until
   Step N meets its full Definition of Done (or is explicitly BLOCKED with the owner
   informed — then only start a step the tracker marks independent).
2. Work through each step's work-plan checkboxes IN ORDER; tick them in this file as
   you complete each one.
3. **Baseline first**: run `pytest tests/` at session start (output redirected to a
   file — Windows terminals truncate) and record the count in the Session Log. Every
   new function gets a test. The suite must be 100% green before any step is declared
   done. Tests use `.env.test` ONLY — `.env` is permanently forbidden to all test files
   and scripts (violation = stop and report).
4. **Production-grade only**: type hints, docstrings, `logging` (no `print`), explicit
   try/except with documented fallbacks, all thresholds from `config.py`, no magic
   numbers, no placeholders, no TODO-left-behind. Fail-closed on anything that gates
   capital; fail-soft only on telemetry/observability paths, explicitly documented.
5. **Re-verify before you trust**: line numbers in Section 3 were verified 2026-10-30.
   Before editing around them, re-confirm with grep/read. If code reality differs from
   this file, adapt carefully and record the correction in the Session Log.
6. **Spec conflicts**: if a spec here seems wrong, unsafe, or contradicts CLAUDE.md,
   STOP — propose the change, record it in the Decision Log (Section 11) first, get
   the owner's approval if it alters capital safety. Never improvise silently.
7. **After every session — even an incomplete one** — update: Status Dashboard,
   Master Tracker row, step checkboxes, and append a Session Log row (date, step,
   what was done, next action). Continuity depends on this.
8. **Commits**: one commit (or a small series) per completed work-plan item,
   conventional-commit style matching `git log` (e.g. `feat(equity): ...`,
   `fix(locks): ...`). Never commit `.env`, secrets, or unrelated untracked files
   (leave `.hermes/`, `_research/`, `CODING_AGENT_PROMPT.md`, `.zcode*/` alone).
   Work on `main` as the owner does. Do not push unless asked.
9. **Supabase call-order tests**: `tests/test_integration.py` asserts exact Supabase
   call indices (List A precedent: every new DB read shifted indices and tests were
   updated with intent preserved + a comment documenting the sequence). Expect and
   manage this every time you add a read to the hot path.
10. **Migrations**: any schema change = new `scratch/migration_*.sql` (SAFE TO RUN,
    idempotent) **and** the equivalent DDL registered in `memory/migrations.py`
    (keep the two in sync — there are cross-reference comments to maintain). The
    owner applies to live DB, or the bot applies on restart.
11. **Dormancy is sacred**: the main news pipeline is dormant by design (`estimate:no_data`).
    Never "fix" dormancy, never reintroduce an estimate, never make tests pass by
    weakening a gate.
12. **Owner-blocked work**: if a step needs something only the owner can do (apply a
    migration, restart the bot, provide access), mark it BLOCKED in the tracker with
    the exact owner action, and continue with the next independent step if safe.

### 1.3 Global DO-NOT list

- Do NOT bypass or weaken the paper gate; do not set `PAPER_TRADING=false` anywhere,
  including tests; do not write code that could place a live order while
  `PAPER_TRADING=true`.
- Do NOT put I/O, LLM calls, or network calls in `risk/risk_engine.py` (purity rule).
  New pure math goes there or in a pure module; I/O lives in coordinator/execution layers.
- Do NOT hardcode thresholds — everything through `config.py`.
- Do NOT delete or rewrite List A artifacts (estimator contract, cost model, fill
  simulator, edge monitor, gate report). Extend, don't replace.
- Do NOT close positions outside the single shared close path (created in Step 4).
- Do NOT let exits be blocked by entry-side halts (a trading halt must never trap capital).
- Do NOT spam Telegram (cooldowns required on repeating alerts).
- Do NOT re-tune List A's CUSUM parameters (List A.md D-10: tuned once, recorded).
- Do NOT touch `temp_*.py`, existing scratch SQL, or docs other than tracker/log
  updates in `List A.md` and the files this plan names.
- Do NOT run the bot against the live Supabase from tests. Ever.

---

## 2. MISSION & THE SPINE

Part A made the bot honest (every number between signal and order is measured).
Part B makes it **survivable and governable**: a drawdown breaker that can actually
trip, a health score that is actually computed and enforced, concurrency that holds
across processes, truth that refreshes at runtime instead of only at startup, an exit
engine (P&L in binary markets is decided at exits), a correlated-exposure cap that
means something, and a governed, kill-switched path toward live — gated by paper proof.

Dependency chain: **equity truth → health enforcement → cross-process safety →
runtime truth → exits → real correlation → governed live-readiness.**

Work order (operator-confirmed 2026-10-30; the operator's original B-numbers in brackets):

```
Step 1  [B2]  Drawdown anchors + mark-to-market portfolio equity   (foundation for 2,5,7)
Step 2  [B1]  Wire the health score: producers, enforcement, alerts (consumes Step 1)
Step 3  [B4]  Lock & concurrency hardening: bounded waits + DB lease (cross-process)
Step 4  [B3]  Runtime reconciliation + in-flight order visibility   (uses Step 3 lease)
Step 5  [B5]  Exit engine: paper sells, monitor loop, six exit rules, cooldown
Step 6  [B6]  Real event-correlation exposure (replaces total-exposure fake)
Step 7  [B7]  Copy Edge live-readiness: CLOB gateway (paper-gated), kill switches,
              two-way Telegram, migration gap fix, ROLLOUT plan
```

Rationale for the order: Step 1 creates the equity truth that health (Step 2) and
exits (Step 5) consume; Step 3's cross-process guard must exist before Steps 4/5 add
runtime loops that mutate position state; Step 5 needs Step 4's shared close path;
Step 6 is cheap once exposure fetching is unified; Step 7 is last because it is the
go-live gate and benefits from everything before it.

---

## 3. VERIFIED EVIDENCE (explored & verified 2026-10-30 — re-confirm line numbers before editing)

| # | Finding | Location | Evidence |
|---|---|---|---|
| E-B1 | Health score ORPHANED: zero production call sites; no code produces the six component scores; `daily_performance.health_score` column exists but is never written | `risk/risk_engine.py:373` (`compute_health_score`), `:414` (`interpret_health_score`); column at `memory/migrations.py:140` | Grep across repo: only definitions + `tests/test_risk.py` |
| E-B2 | Drawdown breakers structurally ALWAYS-ZERO. Pipeline defaults start=current=same snapshot; the production caller passes nothing | `coordinator/pipeline.py:714-721` (defaults), `:1031-1056` (consumption), `data/pipeline.py:77` (production call with no balances) | `starting_balances = {"daily": portfolio_value, ...}` == `current_balances` → `(s−c)/s == 0` |
| E-B3 | Class A drawdown literally self-compares; alert hardcodes zero | `copytrade/executor.py:279-295` (same value to both args), `:290` (`current_pct=0.0`) | `check_drawdown(starting_balance=portfolio_value, current_balance=portfolio_value, ...)` |
| E-B4 | No period-start equity anywhere; `daily_performance` (built for exactly this: `starting_balance_usdc`, `ending_balance_usdc`, `daily_pnl_usdc`, `health_score`, `circuit_breaker_fires`) is never read or written by production code | grep `period_start|start_of_day|equity` → zero hits; table only in `memory/migrations.py:126-144` + tests | Schema-only table |
| E-B5 | Paper portfolio is a STATIC constant, never marked to market | `coordinator/pipeline.py:531-550` (`get_live_portfolio_value`: paper → `PAPER_TRADING_PORTFOLIO_USDC` env, default 10000; live → CLOB collateral + Σ open_positions; exception → env constant) | Drawdown can never trip in paper mode; sizing caps computed off a frozen number |
| E-B6 | "Correlated" exposure == TOTAL exposure: `corr_total` sums every open position; Class A version is coarser and divides by a fixed constant | `coordinator/pipeline.py:244-289` (`fetch_open_positions_exposure`: `corr_total += size` for ALL rows), call sites `:1157`, `:1259`; `copytrade/executor.py:208-230` (`_fetch_exposure`, divides by `config.PAPER_TRADING_PORTFOLIO_USDC` at `:217`) | `check_correlation_exposure` (`risk_engine.py:270-288`) receives total exposure, not correlated |
| E-B7 | Reconciliation is STARTUP-ONLY; no runtime re-reconciliation; in-flight visibility = idempotency statuses only (pending/confirmed/failed/cancelled) | `execution/reconciliation.py:158` (`reconcile_on_startup`), close path `:78-137` (`_supabase_move_to_closed`), pending-idempotency sweep `:375-415`; `main.py:71-76` (`sys.exit(1)` on failure) | Only caller is main.py:73 |
| E-B8 | Market locks are process-local asyncio.Locks with UNBOUNDED wait; no DB-level guard; critical sections span ~200 lines | `coordinator/market_state.py:34-38` (`get_market_lock`), sites `coordinator/pipeline.py:1311-1511`, `copytrade/executor.py:343-479`; zero advisory-lock usage repo-wide; only index is `idx_open_positions_unfilled_maker` (`migrations.py:53-55`) | Two processes (overlapping deploys) each hold their own lock → same market traded concurrently |
| E-B9 | EXIT MACHINERY DOES NOT EXIST. The four exit config keys are dead; no sell code anywhere; live CLOB submission itself is unwired; `EXIT_NOW` only drops NEW signals, never exits positions | `config.py:60-63` (POSITION_MONITOR_INTERVAL_MINUTES, PRICE_TARGET_EXIT_THRESHOLD_CENTS, TIME_DECAY_EXIT_HOURS_REMAINING, TIME_DECAY_POSITION_REDUCTION_PCT — referenced nowhere else); grep `create_order|post_order` → zero outside `.venv`; live TODO `copytrade/executor.py:440-446`; pipeline orders mocked `pipeline.py:1468-1473`; EXIT_NOW consumers `pipeline.py:1054-1058`, `data/pipeline.py:90-92`, `executor.py:299-308`; copy poller filters out SELLs (`poller.py:125,164`) | Position close happens ONLY via startup reconciliation on resolution |
| E-B10 | Telegram is OUTBOUND-ONLY (no getUpdates/handlers); two alerts orphaned; no health or probation alert exists; NO global kill switch anywhere | `monitoring/telegram_alerts.py:9-29` (`_send_to_telegram`); `alert_copy_trade_executed` (`:126`) and `alert_brier_score_exceeded` (`:92`) have zero callers; grep `HALT|KILL|TRADING_ENABLED` in config/coordinator → nothing | Only global stop = toggling PAPER_TRADING env or killing the process |
| E-B11 | MIGRATION GAP (fresh-DB breaker): `scratch/migration_copytrade_phase1.sql` is NOT registered in `memory/migrations.py` — `tracked_wallets` CREATE has only 5 base columns, `copytrade_log` CREATE lacks `was_priority_pick`/`pnl_percent`, yet `performance_tracker.py` writes them (`:316, :478-493`) and poller selects them (`:223-227`) | `memory/migrations.py:223-229, :238-259` vs `performance_tracker.py` | A fresh DB migrated only via migrations.py breaks the copy-trade close path |
| E-B12 | Runtime-loop precedent (List A Step 5) to copy: DB-state-driven scan, per-row exception isolation, fail-soft timeouts, whole-tick survival, `CancelledError` re-raise | `execution/paper_fills.py:241-269` (`check_pending_maker_orders_once`, `run_paper_fill_monitor`), scan cap `:43`, cancel flow `:106-150` | main.py:129-130 supervises it |
| E-B13 | Startup sequence & supervised tasks (7 background tasks; only `signal_pipeline` is awaited; reconciliation gates everything) | `main.py:50` (client) → `:56` (migrations) → `:71-76` (reconcile, fail=exit) → `:79-83` (alert) → `:86-98` (market cache) → tasks `:101,110-113,120-121,129-130` → `:134-137` (model validation) → `:144-145` (signal pipeline; crash → alert + exit `:153-161`) | Any new runtime loop joins this supervisor set |
| E-B14 | Wallet state machine (for exit-engine trust semantics): NEW→ACTIVE (≥20 resolved, WR≥0.52, ROI≥−2%); ACTIVE→PROBATION (WR<0.52 or ROI<−2%); PROBATION→RETIRED (20 failing trades); PROBATION→ACTIVE (≥10 trades, WR≥0.55); RETIRED terminal. Priority (trust≥0.80 AND ≥30 resolved) affects ONLY signal-conflict resolution | `copytrade/performance_tracker.py:224-274` (`_compute_new_state`), constants `:50-59`, trust `:75-88`, priority `:91-96`, applied in `_update_wallet` `:433-533`; classifier use `:188-231` | Early exits must NOT pollute wallet W/L (your exit ≠ the wallet's pick) |
| E-B15 | List A artifacts available for reuse (do not rebuild): estimator contract `strategies/estimator.py` (fail-closed, YES-line convention, `side_probability`); pure cost model `risk/cost_model.py` (fees = rate×p×(1−p), spread, slippage, `net_edge` signed by side); pure fill simulator `risk/fill_simulator.py` (taker/maker, depth, NO-side conventions); edge monitor `risk/edge_monitor.py` (CUSUM, `derive_allocations`); event keys `data/novelty.compute_event_key`; `signal_outcomes` incl. `resolved_outcome/resolved_at`; attribution columns (`thesis_id, p_model_at_entry, net_edge_at_entry, costs_paid_usdc, order_type`) on both position tables; gate report `scripts/gate_report.py` (thesis-cluster bootstrap) | See `List A.md` §5 Steps 0–5 and Decision Log D-08..D-13 | Reuse, extend, never duplicate |

---

## 4. GROUND RULES (inherited from List A + CLAUDE.md — all still binding)

1. `risk/risk_engine.py` stays pure Python: deterministic, <1ms, zero LLM/network imports.
   Permitted imports unchanged: math, decimal, datetime, logging, config.
2. No constants for estimates or probabilities. Anywhere. Fail-closed instead.
3. The LLM never outputs the probability; it receives the estimator's output as context.
4. Fail-closed on capital gates (drawdown, health halt, locks/leases, idempotency,
   kill switch); fail-soft only on telemetry/observability paths, explicitly documented.
5. Net edge only (gross edge is retired). The cost model is load-bearing.
6. All thresholds via `config.py`; new keys get comments explaining the value.
7. Tests use `.env.test` only; tests NEVER touch live Supabase or place orders.
8. Every schema change = scratch SQL + `memory/migrations.py` registration, idempotent.
9. Repo style: type hints, docstrings, logging, no print, explicit fallbacks,
   2s Supabase timeouts, `asyncio.timeout()` (NOT `asyncio.wait_for` — List A Step 2
   finding: wait_for swallows external cancellation).
10. One work-plan item ≈ one commit; conventional commits; no push unless asked.
11. The paper gate (Section 0 directive) outranks everything in this file.

---

## 5. MASTER TRACKER

Status vocabulary: `NOT STARTED` / `IN PROGRESS` / `BLOCKED` / `DONE` / `VERIFIED`.

| Step | Item | Depends on | Status | Started | Completed | Notes |
|---|---|---|---|---|---|---|
| 1 | B2 Drawdown anchors + mark-to-market equity | — | **DONE 2026-10-30** | 2026-10-30 | 2026-10-30 | `coordinator/equity.py` + both entry paths wired; e2e HALT proven on both; 561/561 green; decisions D-B12; owner restart still needed to activate on the running bot |
| 2 | B1 Health score: producers + enforcement + alerts | Step 1 | **DONE + LIVE 2026-10-31** | 2026-10-31 | 2026-10-31 | Deployed via `railway up`; live first+second ticks verified (66.7 → NORMAL, `health_score=66.67` in daily_performance); feed-latency spec corrected (stats reporter has no latency — pipeline instrumented) |
| 3 | B4 Lock & concurrency hardening | — | **DONE + LIVE 2026-11-01** | 2026-11-01 | 2026-11-01 | D-B13 (claim via dedicated `claim_market_lease` RPC function — execute_sql returns VOID); 650/650 green (baseline 620, `scratch/baseline_step3.txt`); two-process double-entry test + singleton guard proven; DoD grep gates clean; deployed + overlap guard live-verified; RLS incident on the new table fixed durably (c307056) |
| 4 | B3 Runtime reconciliation + in-flight visibility | Steps 1, 3 | **DONE 2026-11-01** | 2026-11-01 | 2026-11-01 | `execution/position_close.py` = the ONE close writer (D-B08); runtime loop + D-B14/D-B15; 651→678 green (`scratch/baseline_step4.txt`); grep gates clean; **owner: `railway up` to activate live** |
| 5 | B5 Exit engine | Steps 1, 3, 4 | NOT STARTED | — | — | Biggest step; the four dead config keys come alive |
| 6 | B6 Real event-correlation exposure | Steps 1 (equity), 4 (close path) | NOT STARTED | — | — | Reuses List A event keys; unifies exposure fetching |
| 7 | B7 Live-readiness: CLOB gateway (paper-gated) + kill switches + ROLLOUT | Steps 1–6 | NOT STARTED | — | — | Includes migration-gap fix (E-B11) + two-way Telegram |

Priority/parallelism notes: Step 3 is independent of Steps 1–2 — if the owner is
blocked on applying Step 1-related migrations, do Step 3. Steps 4 and 5 both touch
the close path — do them in order, never concurrently.

---

## 6. STEP SPECS

### Step 1 — B2: Drawdown anchors + mark-to-market portfolio equity

**Problem.** Both live drawdown paths feed the same number as start and current balance
(E-B2, E-B3) → drawdown is always 0% and the breakers are decorative. There is no
period-start equity snapshot anywhere (E-B4), and the paper portfolio is a frozen
constant that never reflects P&L (E-B5) — so breakers, health, sizing caps, and the
gate report all compute off a fiction.

**Solution.**

1. New module `coordinator/equity.py` (I/O allowed; NOT in risk_engine):
   - `mark_to_market_value() -> float`: cash (paper: `PAPER_TRADING_PORTFOLIO_USDC`;
     live: CLOB collateral via `get_balance_allowance`) + Σ over `open_positions` of
     `shares × current_price(direction)`, where `shares = position_size_usdc / entry_price`
     and a NO-side position's value uses `1 − price_yes`. Per-position fetch failures →
     fall back to entry price (conservative: never fabricate gains), count them, expose
     them in the log line.
   - `get_period_balances() -> dict[period, (start, current)]` for daily/weekly/monthly:
     - Anchors live in `daily_performance` (one row per UTC day, upsert on date):
       `starting_balance_usdc` = first mark-to-market value observed that UTC day;
       `ending_balance_usdc` = latest observed; `daily_pnl_usdc` = ending − starting.
     - Weekly anchor = `starting_balance_usdc` of the earliest row in the current ISO
       week; monthly anchor = earliest row of the current calendar month. If the
       period's first day is missing (bot was down), use the earliest available row in
       that period and emit a WARNING (documented approximation).
     - **Bootstrap rule**: if no row exists for today, create it with anchor = current
       equity. If this happens MID-period after a data gap, that masks drawdown — it
       MUST fire a WARNING alert (`[ZERO-ALPHA] WARNING | EQUITY_ANCHOR_BOOTSTRAP`).
       A silent bootstrap is forbidden.
   - **Staleness rule**: a background **equity snapshot task** (supervisor pattern from
     E-B12; interval `EQUITY_SNAPSHOT_INTERVAL_SECONDS`, default 300) refreshes an
     in-process cached `(timestamp, period_balances)`. At trade time, if the cache is
     older than `EQUITY_STALE_HALT_MINUTES` (default 60) or equity computation fails →
     **fail-closed**: block the entry with drop counter `equity:stale` /
     `equity:unavailable` + WARNING alert. Blind breakers are the bug being fixed —
     an equity system that silently returns zeros is worse than no trading.
2. Wire BOTH broken paths to the new service:
   - `coordinator/pipeline.py:714-721` — replace the same-snapshot defaults with
     `get_period_balances()`; the drawdown loop at `:1031-1056` keeps its structure
     (`risk_engine.check_drawdown` is correct — it was being fed lies).
   - `copytrade/executor.py:279-295` — replace the self-comparison and delete the
     hardcoded `current_pct=0.0` (`:290`); use the same service.
3. `get_live_portfolio_value()` is refactored to return mark-to-market equity in paper
   mode too (cash + marked positions), so sizing caps and the gate report see real P&L.
   All existing sizing semantics (5%/8% caps, tranche ceilings) apply to the new value
   unchanged. **Expect test updates**: fixtures that assume the static 10000 must inject
   equity explicitly (add a parameter/fixture — never read live state in tests).
   Note for Step 6: `executor.py:217` divides exposure by the fixed constant — that
   specific fix lands in Step 6 (exposure unification); recorded here so it isn't lost.
4. Supabase call-order tests will shift (new read in the entry path) — manage indices
   per Discipline rule 9.
5. Config keys: `EQUITY_SNAPSHOT_INTERVAL_SECONDS=300`, `EQUITY_STALE_HALT_MINUTES=60`
   (+ comments). The supervisor task is registered in `main.py` alongside the others
   (E-B13), named `equity_snapshot`.

**Work plan.**
- [x] `coordinator/equity.py`: `mark_to_market_value`, `get_period_balances`, snapshot cache + staleness
- [x] Equity snapshot supervisor task in `main.py` (paper_fills pattern: fail-soft tick, CancelledError re-raise)
- [x] `daily_performance` upsert/read path (table exists — E-B4; no migration needed)
- [x] Replace `pipeline.py:714-721` defaults; keep the `check_drawdown` loop structure
- [x] Replace `executor.py:279-295` self-comparison; delete hardcoded `current_pct=0.0`
- [x] Refactor `get_live_portfolio_value()` to marked equity (paper included)
- [x] Config keys + main.py task registration
- [x] Tests: marked-value math (YES/NO positions, fallback-to-entry on price failure), anchor bootstrap + mid-period WARNING, weekly/monthly derivation incl. missing-first-day, staleness fail-closed drop, breaker TRIPS end-to-end in a paper test (inject equity drop → `check_drawdown` HALT fires with a REAL pct), call-order indices updated

**Definition of Done.** A paper test demonstrates a drawdown HALT firing with a non-zero
percentage; grep gate: no same-snapshot `starting_balances = {"daily": portfolio_value, ...}`
default and no same-value `check_drawdown` call remains; `daily_performance` rows are
written and read; suite green. Status: **DONE 2026-10-30** (both e2e HALT tests assert a
real 10% `current_pct`; grep gates verified clean; `risk/risk_engine.py` untouched).

**DO NOT (this step):** change drawdown thresholds; put equity I/O in risk_engine;
compute equity per-LLM-call (use the snapshot cache); bootstrap anchors silently;
touch `executor.py:217`'s exposure denominator (Step 6).

---

### Step 2 — B1: Wire the health score (producers, enforcement, alerts)

**Problem.** `compute_health_score`/`interpret_health_score` exist but nothing computes
the six components or calls them (E-B1). Defensive mode and full halt exist as thresholds
(`HEALTH_SCORE_DEFENSIVE_THRESHOLD=65`, `HEALTH_SCORE_HALT_THRESHOLD=40`) but are never
enforced. No health or probation alerts exist.

**Solution.**

1. New module `monitoring/health_score.py` (I/O allowed) — one producer per component,
   each returning 0–100, each **fail-soft to a neutral 50 when its data is absent**
   (a missing input degrades the signal; it must not crash the loop or fake health):
   - `win_rate_score`: win rate of the last 20 `closed_trades` × 100 (<2 resolved → 50).
   - `brier_score_score`: rolling 30-day agent Brier from `closed_trades`; map
     0.00→100, `BRIER_SCORE_THRESHOLD` (0.23)→0, linear between (<10 resolved → 50).
   - `slippage_score`: mean signed slippage from List A fill data (`closed_trades`/
     `open_positions` fill vs decision price); 0→100, `HEALTH_SLIPPAGE_MAX_UNITS`
     (default 0.02)→0, linear; no fills → 50.
   - `feed_latency_score`: pipeline latency stats vs targets (fast path 5s, full 22s —
     reuse the existing stats-reporter data); at target→100, at 2× target→0; no stats → 50.
   - `drawdown_score`: worst of daily/weekly/monthly drawdown (from Step 1 equity) vs
     its halt threshold; 0%→100, at-threshold→0.
   - `correlation_score`: INTERIM = 100 − (max category exposure / 30% cap)×100
     (no exposure → 100). Upgraded to event-cluster exposure in Step 6. The interim
     definition is recorded in the Decision Log (D-B11) and must be revisited, not forgotten.
2. A **health evaluation task** (supervisor pattern; interval
   `HEALTH_CHECK_INTERVAL_SECONDS=300`): computes the score via
   `risk_engine.compute_health_score` (the pure function, finally fed for real), maps
   via `interpret_health_score`, upserts `daily_performance.health_score` (today's row —
   extend Step 1's upsert, never double-write), and caches the current mode in-process.
3. **Enforcement point (entries only — NEVER exits):**
   - `FULL_HALT`: `run_pipeline` and the Class A executor drop at the TOP with counter
     `health:full_halt` + `alert_system_halt` (exists). No new entries; exits keep running.
   - `DEFENSIVE_MODE`: sizing multiplier `HEALTH_DEFENSIVE_SIZE_MULTIPLIER=0.5` applied
     alongside the existing allocation multiplier (multiply; both are safety multipliers
     — document the stacking in the sizing log).
   - Kill-switch interplay: Step 7's `TRADING_ENABLED=false` supersedes everything for
     entries; health halt is the graded version of the same gate.
4. **Alerts** (new functions in `monitoring/telegram_alerts.py`, format-consistent):
   - `alert_health_score(level, score, mode, components)` on mode TRANSITIONS only
     (NORMAL→DEFENSIVE→FULL_HALT and recoveries), with
     `HEALTH_ALERT_COOLDOWN_SECONDS=3600` anti-spam.
   - `alert_strategy_probation(strategy, window_stats)` — fire when List A's
     `derive_allocations` (risk/edge_monitor.py) marks a probation/retired transition
     (the health task diffs last-known vs current allocation states).
   - Wire the ORPHANED `alert_brier_score_exceeded` (`telegram_alerts.py:92`) to the
     rolling-Brier producer crossing `BRIER_SCORE_THRESHOLD` (same cooldown mechanism).
5. Tests: each component mapping (boundaries), neutral-50 fallbacks, score persistence,
   FULL_HALT blocks entries on both paths (counters), DEFENSIVE halves sizes, exits NOT
   affected (write the test now so it guards Step 5), alert transitions + cooldown,
   integration call-order indices updated.

**Definition of Done.** Health score computed on a schedule, persisted to
`daily_performance.health_score`, enforced at both entry paths, mode-transition alerts
fire with cooldown, suite green. Status: **DONE 2026-10-31** (baseline 563 → 620/620
green, `scratch/baseline_step2.txt`; grep gates verified: `compute_health_score` has a
real production caller (`monitoring/health_score.py`), zero health references on any
exit path (locked by a guard test), `risk/risk_engine.py` untouched; no schema change).

**DO NOT (this step):** make the health score gate exits (halts trap capital — forbidden);
put component I/O in risk_engine (the pure function just consumes numbers); reweight the
six components (CLAUDE.md fixes equal weights); let a missing component read fake a
healthy score (neutral 50, logged); skip the probation alert (part of this step).

---

### Step 3 — B4: Lock & concurrency hardening (bounded waits + DB lease)

**Problem.** Market locks are process-local asyncio.Locks with unbounded waits and
~200-line critical sections (E-B8). Two processes — overlapping Railway deploys, or
Hetzner + a stray container — each hold their own lock registry and can trade the same
market concurrently. There is no DB-level guard.

**Solution.**

1. **Bounded lock wait**: wrap `lock.acquire()` in `asyncio.timeout(LOCK_ACQUIRE_TIMEOUT_SECONDS)`
   (default 10) at BOTH sites (`pipeline.py:1311`, `executor.py:343`). Timeout → drop
   with counter `lock:timeout` + WARNING (fail-closed; never queue-jump, never proceed
   unguarded). Keep `async with` semantics for release.
2. **DB lease table** (PostgREST-compatible cross-process mutex — session-level
   `pg_advisory_lock` is UNSAFE here because supabase-py pools connections; each RPC runs
   in its own transaction, so a session lock can never be released reliably — D-B02):
   - Table `market_leases (market_id TEXT PRIMARY KEY, owner TEXT NOT NULL,
     lease_until TIMESTAMPTZ NOT NULL)` + scratch migration + migrations.py registration.
   - `coordinator/lease.py`: `acquire_lease(market_id, owner, ttl)` — ONE atomic SQL
     statement via the existing `execute_sql` RPC (the same RPC migrations use):
     `INSERT ... ON CONFLICT (market_id) DO UPDATE SET owner = $owner,
     lease_until = now() + $ttl WHERE market_leases.lease_until < now()
     OR market_leases.owner = $owner` → claimed iff the row came back;
     `renew_lease`, `release_lease` (delete where owner = me).
   - `owner` = instance identity (hostname + process boot id, logged at startup).
   - Acquire the lease at the TOP of the same critical sections that take the asyncio
     lock (defense in depth: process-local lock first, lease second). TTL
     `LEASE_TTL_SECONDS` default 45. Release in a `finally`. Lease failure → drop with
     counter `lease:conflict` (fail-closed).
   - Residual risk, documented in the module docstring: a hung holder's lease expires
     and another process may enter; the DB-level re-checks inside the section (tranche
     count, idempotency) remain the second line of defense. This is the honest trade
     for PostgREST compatibility.
3. **Singleton instance guard** (the overlapping-deploy fix): at startup, claim a global
   lease `market_id='__singleton__'` with `SINGLETON_LEASE_TTL_SECONDS=90`; a renew task
   keeps it alive every 30s. If the claim fails → another instance is alive → retry
   every 30s up to `SINGLETON_CLAIM_MAX_WAIT_SECONDS=600`, then `alert_system_halt` +
   `exit(1)`. Deploy overlap becomes safe: the old instance holds the lease until it
   dies; the new one waits and takes over.
4. Tests: lease claim/conflict/expiry/steal/release (including atomicity of the
   conditional upsert), asyncio lock timeout drop, singleton guard (second instance
   waits then exits), lease release on exception paths, call-order indices.

**Definition of Done.** Both critical sections are bounded-wait + lease-guarded; a test
simulating two processes cannot double-enter the same market; singleton guard proven;
suite green. Status: **DONE 2026-11-01** (baseline 620 → 650/650 green,
`scratch/baseline_step3.txt`; DoD grep gates verified: zero `pg_advisory*` usage
repo-wide (docstring mentions of the prohibition only), the asyncio per-market lock is
still acquired at both sites (`pipeline.py:1417`, `executor.py:395`), both lease call
sites release in a `finally` (`pipeline.py:1656`, `executor.py:566`); the DoD
two-process test proves proc-B cannot double-enter proc-A's market (drops fail-closed
at the bounded lock wait, writes nothing, steals nothing) and that same-owner re-entry
after release succeeds; the singleton guard is proven behaviorally
(`tests/test_lease.py`: claim free / wait-then-exit(1) when held / takeover after
holder expiry / renewer success, transient-error tolerance, split-brain exit) and its
startup wiring by contract (`tests/test_singleton_wiring.py`: claim precedes
reconciliation and all trading tasks; renewer registered after the claim). Line-drift
note: the spec's `pipeline.py:1311` / `executor.py:343` anchors were re-located by
grep (`pipeline.py:1407→1414`, `executor.py:385→392`) as anticipated).

**DO NOT (this step):** remove the asyncio per-market lock (it stays; the lease is
additive); use session-level `pg_advisory_lock` via RPC (pooling makes it un-releasable
— documented); make lease failure fail-open; set TTL so long that a crashed holder
blocks a market for minutes (45s default).

---

### Step 4 — B3: Runtime reconciliation + in-flight order visibility

**Problem.** Reconciliation runs once at startup (E-B7): positions that resolve mid-run
stay open until the next restart; `idempotency_log` entries stuck in `pending` (a crash
between write and confirm) strand invisibly; there is no in-flight visibility.

**Solution.**

1. **One shared close path FIRST** (this step's structural deliverable): extract the
   close logic from `execution/reconciliation.py:78-137` (`_supabase_move_to_closed`)
   + the copy-trade trust updates (`resolve_copy_trade`, `performance_tracker.py:338`)
   into `execution/position_close.py`:
   `close_position(position_row, exit_price, exit_type, exit_reason, resolved_at) -> CloseResult`.
   Handles: PnL computation (shares × (exit − entry), direction-correct), `closed_trades`
   insert, `open_positions` delete, `copytrade_log` close + wallet trust update for copy
   strategies, attribution-column passthrough. `reconciliation.py` is refactored to call
   it (behavior preserved — its tests must still pass unedited except call-order indices).
   **Step 5's exit engine uses the same function — one close path, ever (D-B08).**
2. **Runtime reconciliation loop** `execution/runtime_reconciliation.py` (supervisor
   pattern E-B12; interval `RUNTIME_RECON_INTERVAL_SECONDS=900`). Each tick runs three
   independent, fail-soft checks with per-item isolation:
   - **Resolution sweep**: for each `open_positions` row, apply the SAME resolved-market
     predicate the startup reconciliation and gate report use (Gamma fetch; void/0.5
     prices rejected — List A D-11 convention); resolved → `close_position(...,
     exit_type='resolved')` under the market lease (Step 3). Rate-limit aware: cap
     Gamma fetches per tick (`RECON_MAX_RESOLUTION_FETCHES=50`).
   - **Stuck-pending sweep**: `idempotency_log` rows with `status='pending'` older than
     `PENDING_STUCK_MINUTES=15` → if the row corresponds to a RESTING maker position
     (`open_positions.order_type='maker' AND fill_price IS NULL` — a legitimate rest
     state, List A D-13), skip; else mark `'failed'` + WARNING alert (paper mode; when
     live orders exist, reuse reconciliation's `get_order` logic from `:375-415` instead
     of blind-failing — the branch must check `PAPER_TRADING`).
   - **In-flight visibility**: one `[OBSERVABILITY][INFLIGHT]` log per tick (plus a
     daily Telegram digest — reuse the startup-alert transport): counts of pending
     idempotency, resting makers, and (after Step 5) pending exits.
3. Startup reconciliation is UNCHANGED and still gates all trading (E-B13). The runtime
   loop is additive.
4. Config keys: `RUNTIME_RECON_INTERVAL_SECONDS=900`, `PENDING_STUCK_MINUTES=15`,
   `RECON_MAX_RESOLUTION_FETCHES=50`.
5. Tests: resolution sweep (fixture rows → closed via the SHARED path; void excluded),
   resting-maker exclusion from stuck-pending, stuck-pending → failed + alert, close-path
   refactor leaves reconciliation tests green (behavior preserved), loop fail-softness,
   lease usage, call-order indices.

**Definition of Done.** A resolved position closes WITHOUT a restart in a paper test;
stuck pending entries are swept with the resting-maker exclusion proven; all closes flow
through `execution/position_close.py` (grep: no other writer of `closed_trades` inserts);
suite green. Status: **DONE 2026-11-01** (baseline 651 → 678/678 green,
`scratch/baseline_step4.txt`; DoD tests: `test_resolved_position_closes_without_restart_via_shared_path`
proves the end-to-end paper close through the SHARED path incl. copy-trade trust; the
stuck-pending sweep proves all three D-B14 classifications + the freshness guard; grep
gate verified: `position_close.py:142` is the ONLY `closed_trades` insert writer
(every other reference repo-wide is a read); startup recon suite 8/8 green UNEDITED
except one patch target (intent documented); ZERO entry-hot-path changes → call-order
ordinals untouched; decisions D-B14/D-B15 recorded before building).

**DO NOT (this step):** delete or weaken startup reconciliation; auto-resolve
void/ambiguous markets (reject 0.5 prices — List A convention); close positions the
exit engine owns decisions for (runtime recon handles RESOLUTION only); run unbounded
Gamma fetches per tick.

---

### Step 5 — B5: Exit engine (the biggest step)

**Problem.** There is no exit machinery at all (E-B9): no sell path, the four exit
config keys are dead, `EXIT_NOW` from the liquidity floor never exits anything — it
only drops new signals. In binary markets P&L is decided at exits; today the bot can
only ride every position to 0/1 or a liquidity death.

**Solution.**

1. **Paper sell simulation** — extend `risk/fill_simulator.py` (pure, mirrors List A
   conventions):
   - `taker_exit_price(book, direction)`: selling YES hits the YES bid; selling NO
     fills at `1 − YES_ask` (mirror of the NO-entry convention).
   - `taker_exit_depth_ok(book, size_shares, direction)`: mirrored depth check.
   - Exit fees: taker fee formula applies on sells (same `cost_model` schedule).
   - `signed_exit_slippage(fill, mark)`: positive = adverse.
2. **Monitor loop** `execution/exit_engine.py` (supervisor; interval
   `EXIT_MONITOR_INTERVAL_SECONDS=300`; per-position isolation; book unavailable →
   fail-soft retry, never a blind exit; book dead beyond `EXIT_BOOK_DEAD_HOURS=6` →
   alert, still no blind exit). For each `open_positions` row, evaluate rules in
   priority order; first hit wins:
   - **R1 liquidity auto-exit**: market liquidity < `AUTO_EXIT_LIQUIDITY_FLOOR_USDC`
     (3000) → exit, reason `exit_liquidity`. (Finally makes `EXIT_NOW` mean exit.)
   - **R2 probability/edge-decay stop**: INTERIM (until List A Step 6 provides
     estimators — D-B09): adverse price move vs entry beyond `PROB_STOP_MOVE_CENTS`
     (default 0.15) AND held ≥ `MIN_HOLD_HOURS` (default 24) → exit, reason
     `exit_stop`. When estimators wake up, this rule upgrades to a real shrunk-p_model
     stop (the upgrade hook is documented in the module).
   - **R3 profit-taking**: gain ≥ `TAKE_PROFIT_GAIN_CENTS` (0.25) OR price ≥
     `TAKE_PROFIT_MIN_PRICE` (0.90) with days-to-resolution ≥ 1 (SELL INTO liquidity —
     never into the final-24h illiquid window when behind; documented rule) → exit,
     reason `exit_profit`.
   - **R4 time-decay**: days-to-resolution < `TIME_DECAY_EXIT_HOURS_REMAINING` (72h —
     the existing dead key comes alive) with the thesis unconfirmed (price within
     `TIME_DECAY_BAND` (0.10) of entry) → reduce the position by
     `TIME_DECAY_POSITION_REDUCTION_PCT` (0.50, existing key) — partial exit, reason
     `exit_time_decay` (position row updated, not closed; the reduction sells half the
     shares at the simulated exit price).
   - **R5 exit-weakest-for-stronger**: when a NEW signal passes every gate EXCEPT the
     category-exposure cap, evaluate the weakest same-category open position (lowest
     `net_edge_at_entry`, worst allocation state) via these same rules; if selling it
     frees room AND its current net edge (mark vs model — price-move proxy interim)
     trails the incoming signal's net edge by more than `SWAP_MARGIN_CENTS` (0.02) →
     exit it (`exit_swap`) and admit the new signal. Correlation-cap swaps wait for
     Step 6 clusters. This is the only rule that runs on the ENTRY path — strictly
     bounded: one swap attempt per signal, never recursive.
   - **R6 re-entry cooldown**: after ANY exit on `(market_id, direction)`, block
     re-entry for `REENTRY_COOLDOWN_HOURS=24` — enforced in BOTH entry paths (pipeline
     + Class A) by reading the last `closed_trades` exit for that key (no new table).
     Cooldown checks fail-closed (read failure → block).
3. **Close mechanics**: every exit goes through `execution/position_close.py` (Step 4)
   with the rule's `exit_type`. Copy-trade positions: wallet trust is **NOT** updated
   on early exits (D-B06: an early exit is YOUR trade management, not the wallet's
   pick — `copytrade_log` records `exit_type`; wallet W/L move only on true resolution).
   New columns on `closed_trades`: `exit_type TEXT`, `exit_reason TEXT` (+ scratch
   migration + migrations.py registration; `open_positions` needs no new column —
   exits delete the row; partial exits update it).
4. **Honest Brier on early exits** (D-B05): early-exited trades are NOT
   agent-vs-market Brier observations (a forecast at exit price is not a resolution
   forecast); they carry `exit_type` and count in P&L. Update `scripts/gate_report.py`
   to bucket `exit_type != 'resolved'` separately in both the Brier section and the
   per-strategy breakdown (printed, never silently merged).
5. Config: the new keys above; the four revived keys get updated comments stating
   their now-real semantics. `PRICE_TARGET_EXIT_THRESHOLD_CENTS` is superseded by
   `TAKE_PROFIT_MIN_PRICE` and `POSITION_MONITOR_INTERVAL_MINUTES` by
   `EXIT_MONITOR_INTERVAL_SECONDS` — mark both deprecated in comments (prefer the new
   keys; note the replacement).
6. Tests: fill-simulator exits (YES/NO, depth, fees, slippage sign); each rule's
   trigger AND no-trigger boundaries (esp. R3's days-to-resolution guard, R4's band);
   priority order; partial exit (R4) updates the row correctly; every close goes
   through the shared path; wallet trust untouched on early exits (regression);
   cooldown blocks both entry paths; R5 swap selection + margin + no-recursion;
   gate-report bucketing; call-order indices.

**Definition of Done.** In paper mode, a position exits end-to-end (rule → simulated
sell → shared close → ledger rows) without a restart; all six rules have boundary
tests; cooldown enforced on both paths; gate report separates early exits; suite green.
Status: **NOT STARTED**.

**DO NOT (this step):** let any halt (health, drawdown, kill switch) block EXITS; exit
into the final-24h illiquid window when behind; invent a p_model for Class A positions
(the price-move proxy is the documented interim — D-B09); close outside
`execution/position_close.py`; make R5 recursive or multi-swap; touch wallet W/L on
early exits.

---

### Step 6 — B6: Real event-correlation exposure

**Problem.** The "correlated" cap receives TOTAL exposure — every open position is
summed regardless of relationship (E-B6) — so `MAX_CORRELATED_EXPOSURE_PCT=0.20` is
really a second total-exposure cap, and Class A computes exposure against a frozen
constant.

**Solution.**

1. **Event-cluster key on positions**: add `event_key TEXT` to `open_positions`
   (scratch migration + migrations.py; backfill = parse from the existing `thesis_id`'s
   third segment where present, else `market_id` as its own singleton cluster). Write it
   at entry in BOTH paths (pipeline + Class A) from `data/novelty.compute_event_key`
   (List A) — the same grouping novelty already computes.
2. **Cluster exposure**: refactor `fetch_open_positions_exposure` into a shared module
   (`coordinator/exposure.py`) used by BOTH the pipeline and Class A (delete
   `executor.py:208-230 _fetch_exposure`):
   - `category_exposure(category)` — unchanged semantics.
   - `max_event_cluster_exposure()` — Σ marked position value per `event_key` cluster
     (NULL/absent keys = singleton clusters keyed by market_id), divided by
     mark-to-market portfolio value (Step 1 — never the fixed constant; E-B5's second
     read path dies here).
   - Return shape extended; the pipeline's `check_correlation_exposure(...)` call now
     passes the PROPOSED trade's own cluster contribution (the proposed trade joins the
     cluster of ITS event key), not a flat `proposed_pct`.
3. `risk_engine.check_correlation_exposure` is UNTOUCHED — the pure gate was always
   correct; it was fed the wrong number.
4. Upgrade path (documented in the module, NOT built): when List A Step 6 lands, the
   cluster key upgrades to an empirical correlation grouping; the health score's
   `correlation_score` interim definition (Step 2, D-B11) switches to cluster exposure
   at the same time — record both in the Decision Log when done.
5. Tests: clustering math (shared key sums, NULL→singleton), gate trips on cluster
   (not total) exposure — two uncorrelated clusters at 15% each PASS while one cluster
   at 21% BLOCKS, Class A unified fetch (fixed constant gone — grep gate),
   proposed-trade cluster attribution, call-order indices.

**Definition of Done.** `MAX_CORRELATED_EXPOSURE_PCT` demonstrably binds per event
cluster; no caller divides by `PAPER_TRADING_PORTFOLIO_USDC` anymore; suite green.
Status: **NOT STARTED**.

**DO NOT (this step):** hardcode category-to-category correlations; change the pure
gate's signature; count the proposed trade twice (it joins its own cluster once);
break Class B's path while unifying (its exposure flows through the pipeline already).

---

### Step 7 — B7: Copy Edge live-readiness — CLOB gateway (paper-gated), kill switches, ROLLOUT

**Problem.** Live CLOB order submission is an explicit TODO (E-B9) — the bot cannot
actually trade live, cannot cancel, has no global kill switch, Telegram is
outbound-only (E-B10), the copytrade migration gap breaks fresh databases (E-B11),
and two alerts are orphaned. B7 is the go-live gate: it makes live *possible* while
keeping it *unreachable* until the operator's 2-week paper proof passes.

**Solution.**

1. **Order gateway** `execution/order_gateway.py` — the SINGLE choke point for all
   order placement and cancellation (pipeline and executor call nothing else):
   - `place_limit_order(token_id, side, price, size_shares) -> OrderResult`
   - `cancel_order(order_id) -> bool`
   - `get_order(order_id)` (reuses the client from `execution/polymarket_auth.py` —
     L2 creds already derived there; never re-authenticate).
   - `PAPER_TRADING=true` → routes to the existing paper paths (fill simulator /
     resting semantics) EXACTLY as today — behavior-preserving.
   - `PAPER_TRADING=false` → real py_clob_client calls (`create_order`/`post_order`,
     GTC). ⚠️ VERIFY at implementation time against current py_clob_client docs:
     exact OrderArgs/PostOrdersArgs signatures, `get_tick_size(token_id)` for price
     rounding (0.001/0.01 ticks — a mis-ticked price is rejected by the CLOB),
     negative-risk market handling, minimum order size. Record findings in the
     Decision Log (List A D-09 precedent).
   - The live branch enforces the SAME guards paper has: book fetch, depth check,
     limit price (tracker + `COPY_LIMIT_PRICE_BUFFER`), classifier slippage thresholds.
     No guard may exist only in paper.
   - Hard interlock (D-B07): `place_limit_order` raises unless `PAPER_TRADING` is
     explicitly False AND `TRADING_ENABLED` is True — two independent flags must agree.
2. **Maker fallback goes live-capable**: the List A maker monitor
   (`execution/paper_fills.py`) gains a live branch — cancel via the gateway, then
   taker-fallback or cancel per the existing net-edge rule. Paper behavior unchanged.
3. **Global kill switch + two-way Telegram**:
   - `TRADING_ENABLED` (runtime state, DB-persisted so it survives restarts): new table
     `system_state (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TIMESTAMPTZ)`
     + migration. Key `trading_enabled` = 'true'/'false'; read through a short-TTL
     cache (5s); read failure → fail-closed for ENTRIES (treat as disabled) — exits
     exempt (D-B10).
   - Entry gates check `TRADING_ENABLED` at the top of `run_pipeline` + the Class A
     executor (counter `kill_switch:disabled`). Exits and reconciliation are ALWAYS exempt.
   - `monitoring/telegram_commands.py`: getUpdates long-poll supervisor task (offset
     persisted in `system_state`); commands from `TELEGRAM_CHAT_ID` ONLY (others
     ignored + logged): `/halt` (trading_enabled=false + alert), `/resume` (true +
     alert), `/status` (positions, marked equity, health mode, in-flight counts),
     `/flatten <market_id>` (Step 5 exit, reason `exit_manual`). No shell-out, no eval
     — commands map to fixed functions only.
4. **Migration gap fix (E-B11)**: register the `migration_copytrade_phase1.sql` columns
   (tracked_wallets: state, resolved_trades_count, wins_count, losses_count, trust_score,
   avg_roi_per_trade, is_priority, probation_entered_at, probation_resolved_at_entry,
   last_updated_at; copytrade_log: was_priority_pick, pnl_percent) as idempotent ALTERs
   in `memory/migrations.py` + extend the canonical CREATEs. Contract test added.
5. **Wire the orphaned alerts**: `alert_copy_trade_executed` on Class A fills (it
   already handles the `[PAPER]` tag); `alert_brier_score_exceeded` was wired in
   Step 2 — verify, don't duplicate.
6. **ROLLOUT plan** — write `ROLLOUT.md` (repo root) as part of this step's DoD:
   - **Preconditions to flip live** (ALL required, owner-verified): paper gate
     PT.1–PT.10 (PROGRESS.md) fully checked; ≥2 weeks paper trading (operator
     directive 2026-10-30); gate report prints PASS (n≥200 composite, net P&L CI > 0,
     agent Brier < market Brier); zero unexplained circuit-breaker fires;
     reconciliation clean on every restart.
   - **Capital ladder**: start $1,000 proof-of-concept (PROGRESS.md), max position $50;
     scale ×2 only after every +30 closed trades with CI-positive net P&L; never scale
     during DEFENSIVE health mode or an open CUSUM fade flag.
   - **Custody**: the trading wallet is a HOT wallet holding working capital only
     (≈2 weeks max drawdown + open positions); weekly automated profit sweep to cold
     storage; key on the server only, never on the dev laptop.
   - **Runbook**: one row per Telegram alert → pre-decided human action (alert list
     per CLAUDE.md + the List B additions).
   - **Pre-committed kill criteria** (decided NOW, in writing): 30 rolling closed
     trades with net Sharpe ≤ 0 → back to paper; two consecutive strategy probations →
     that strategy retired (already mechanical); monthly drawdown 25% → full stop
     (already coded); operator kill date: review 90 days after go-live — if no
     strategy has CI-positive attribution, shut down and keep the capital.
   - **Flip procedure**: exact ordered checklist (migrations applied → gate report PASS
     attached → `PAPER_TRADING=false` in the server env only → first-day size cap
     `LIVE_FIRST_DAY_MAX_USDC` (config, default 50) → monitor first 24h → revert
     procedure = set `PAPER_TRADING=true` and `TRADING_ENABLED=false`, reconcile).
7. Tests: gateway routing (paper unchanged — behavior-preservation suite; live branch
   against a MOCKED client only — never a real order in tests), tick-size validation,
   interlock (paper=true + trading_enabled=true cannot place a live order), kill-switch
   gates on both entry paths (exits exempt), command handler auth + each command,
   `system_state` persistence across a simulated restart, migration contract.

**Definition of Done.** The gateway is the only order path (grep gate: no direct
py_clob_client order calls outside the gateway); paper behavior preserved (suite green
except call-order indices); kill switch + commands live and DB-persisted; migration
gap closed with a contract test; `ROLLOUT.md` exists and this dashboard links it;
suite green. Status: **NOT STARTED**.

**DO NOT (this step):** place a real order under any circumstances (tests use mocks
only); default `PAPER_TRADING` to false anywhere; make `/halt` effective only in
memory (it must survive restarts — DB-persisted); let `/flatten` bypass the shared
close path; skip the VERIFY items on py_clob_client signatures; deploy to the live
bot before the owner applies migrations.

---

## 7. NEW CONFIG KEYS SUMMARY (all via `config.py`, with comments)

| Key | Default | Step |
|---|---|---|
| `EQUITY_SNAPSHOT_INTERVAL_SECONDS` | 300 | 1 |
| `EQUITY_STALE_HALT_MINUTES` | 60 | 1 |
| `HEALTH_CHECK_INTERVAL_SECONDS` | 300 | 2 |
| `HEALTH_SLIPPAGE_MAX_UNITS` | 0.02 | 2 |
| `HEALTH_DEFENSIVE_SIZE_MULTIPLIER` | 0.5 | 2 |
| `HEALTH_ALERT_COOLDOWN_SECONDS` | 3600 | 2 |
| `LOCK_ACQUIRE_TIMEOUT_SECONDS` | 10 | 3 |
| `LEASE_TTL_SECONDS` | 45 | 3 |
| `SINGLETON_LEASE_TTL_SECONDS` | 90 | 3 |
| `SINGLETON_CLAIM_MAX_WAIT_SECONDS` | 600 | 3 |
| `LEASE_RENEW_INTERVAL_SECONDS` | 30 | 3 |
| `RUNTIME_RECON_INTERVAL_SECONDS` | 900 | 4 |
| `PENDING_STUCK_MINUTES` | 15 | 4 |
| `RECON_MAX_RESOLUTION_FETCHES` | 50 | 4 |
| `RECON_ALERT_COOLDOWN_SECONDS` | 3600 | 4 (supporting) |
| `EXIT_MONITOR_INTERVAL_SECONDS` | 300 | 5 |
| `EXIT_BOOK_DEAD_HOURS` | 6 | 5 |
| `PROB_STOP_MOVE_CENTS` | 0.15 | 5 |
| `MIN_HOLD_HOURS` | 24 | 5 |
| `TAKE_PROFIT_GAIN_CENTS` | 0.25 | 5 |
| `TAKE_PROFIT_MIN_PRICE` | 0.90 | 5 |
| `TIME_DECAY_BAND` | 0.10 | 5 |
| `SWAP_MARGIN_CENTS` | 0.02 | 5 |
| `REENTRY_COOLDOWN_HOURS` | 24 | 5 |
| `LIVE_FIRST_DAY_MAX_USDC` | 50 | 7 |

Revived-from-dead keys (E-B9): `TIME_DECAY_EXIT_HOURS_REMAINING` and
`TIME_DECAY_POSITION_REDUCTION_PCT` get real semantics in Step 5;
`PRICE_TARGET_EXIT_THRESHOLD_CENTS` is superseded by `TAKE_PROFIT_MIN_PRICE`;
`POSITION_MONITOR_INTERVAL_MINUTES` is superseded by `EXIT_MONITOR_INTERVAL_SECONDS`
— both marked deprecated in comments.

## 8. NEW TABLES / SCHEMA CHANGES SUMMARY

| Change | Step | Purpose |
|---|---|---|
| `market_leases (market_id PK, owner, lease_until)` | 3 | Cross-process mutex (PostgREST-compatible) |
| `execution/position_close.py` shared close path | 4 | ONE writer of closed_trades (startup recon refactored onto it) |
| `closed_trades` += `exit_type`, `exit_reason` | 5 | Honest exit accounting + gate-report bucketing |
| `open_positions` += `event_key` | 6 | Event-cluster exposure |
| `system_state (key PK, value, updated_at)` | 7 | DB-persisted kill switch + Telegram poll offset |
| `tracked_wallets`/`copytrade_log` += copytrade_phase1 columns (GAP FIX) | 7 | Fresh-DB breaker (E-B11) |
| `daily_performance` | 1–2 | NO schema change — the existing table finally gets written/read |

## 9. COORDINATION WITH LIST A

- List A Steps 0–5 stand as built. List B extends them; it does not modify their specs.
- Dormant main pipeline: List B does not un-dormant it. Until List A Step 6 lands,
  exits operate on the only positions that exist (Class A copy trades) — the exit
  engine must handle estimate-free positions honestly (price-move proxy, D-B09).
- The List A Step 6 (A3) parked trigger still stands: if copy-edge paper results
  disappoint, A3 may be pulled forward — operator decision, recorded in List A.md D-03.
- Supabase call-order test indices WILL shift again (Steps 1, 2, 4, 5 add reads) —
  the List A precedent documents how to manage this (Discipline rule 9).
- The owner actions in Section 0 remain prerequisites for List A's data/fill/ledger
  clocks; List B code must tolerate their absence (fail-soft writes) as List A did.

## 10. DECISION LOG (append-only)

| ID | Date | Decision | Rationale |
|---|---|---|---|
| D-B01 | 2026-10-30 | Work order B2→B1→B4→B3→B5→B6→B7 | Equity truth first (health/exits consume it); cross-process guard before runtime loops that mutate state; shared close path before exits; live gateway last (go-live gate) |
| D-B02 | 2026-10-30 | Cross-process guard = `market_leases` DB lease rows, NOT session-level pg_advisory_locks | supabase-py/PostgREST pools connections; each RPC is its own transaction — a session advisory lock can never be reliably released. An atomic conditional upsert via the existing `execute_sql` RPC is PostgREST-safe |
| D-B03 | 2026-10-30 | Equity anchors = `daily_performance` daily rows (no new table); weekly/monthly derived; mid-period bootstrap allowed ONLY with WARNING alert; stale/unavailable equity = fail-closed for ENTRIES | The table was built for this and never used; silently masking drawdown is the bug being fixed |
| D-B04 | 2026-10-30 | Health FULL_HALT blocks entries only — NEVER exits; DEFENSIVE halves sizes (multiplier stacks with the allocation multiplier) | A halt that traps capital is worse than the risk it prevents; exits are always allowed |
| D-B05 | 2026-10-30 | Early exits are excluded from agent-vs-market Brier; tagged via `exit_type`; counted in P&L; gate report prints buckets separately | A forecast at exit price is not a resolution forecast — mixing them corrupts the skill metric |
| D-B06 | 2026-10-30 | Early exits do NOT update copy-trade wallet W/L/trust; only true resolutions do (copytrade_log records exit_type) | Wallet trust measures the WALLET's picks; the operator's exit management is a separate skill — polluting W/L would mis-rank wallets |
| D-B07 | 2026-10-30 | Real CLOB gateway built in Step 7 but interlocked: requires `PAPER_TRADING=false` AND `TRADING_ENABLED=true` simultaneously | Operator directive: 2 weeks paper + PT gate before live; two independent flags = no single-toggle accident |
| D-B08 | 2026-10-30 | ONE shared close path (`execution/position_close.py`) for startup recon, runtime recon, and the exit engine | Multiple close writers = double-close races and inconsistent P&L; a single path is auditable |
| D-B09 | 2026-10-30 | Probability stops use a price-move proxy until List A Step 6 provides estimators; upgrade hook documented in the exit engine | Honest interim: Class A positions have no p_model; a fabricated one is forbidden (List A rules) |
| D-B10 | 2026-10-30 | Kill switch state is DB-persisted (`system_state`) with a 5s read cache; control-plane read failure = fail-closed for entries; exits exempt | /halt must survive restarts; an unreachable control plane must never silently re-enable trading |
| D-B11 | 2026-10-30 | Interim `correlation_score` = category concentration; switches to event-cluster exposure when Step 6 lands | Honest placeholder, explicitly logged so it is revisited, not forgotten |
| D-B12 | 2026-10-30 | Paper cash in `mark_to_market_value` = `PAPER_TRADING_PORTFOLIO_USDC` + Σ `closed_trades.pnl_usdc` − Σ cost of open filled positions (NOT the bare constant); resting makers contribute 0; any snapshot-tick failure (read or write) leaves the cache untouched (fail-closed) | The literal constant makes every position resolution look like an equity drop (a win counted as drawdown) and realized P&L never counts; the netted formula is continuous across entry→move→resolution so drawdown math is exact. Anchor writes are capital-safety-adjacent — no telemetry fail-soft |
| D-B13 | 2026-11-01 | Step 3 lease claim = a dedicated SQL function `claim_market_lease(p_market_id TEXT, p_owner TEXT, p_ttl_seconds INTEGER) RETURNS BOOLEAN` — atomic `INSERT … ON CONFLICT (market_id) DO UPDATE … WHERE market_leases.lease_until < now() OR market_leases.owner = EXCLUDED.owner RETURNING true` — created by the market_leases migration and called via `client.rpc("claim_market_lease", {...})`; NOT via the `execute_sql` RPC | SPEC CORRECTION (owner pre-authorized): the live `execute_sql` RPC returns VOID (body `BEGIN EXECUTE sql; END;` — re-verified in the live DB 2026-11-01 via pg_get_functiondef), so the Step 3 spec's "claimed iff the row came back" cannot work through it. A scalar-returning function keeps the same single-statement atomicity (ON CONFLICT DO UPDATE re-evaluates its WHERE under row lock) and the same fail-closed semantics; capital behavior unchanged (lease failure still drops the entry). The `owner = $owner` match branch doubles as atomic same-owner renewal |
| D-B14 | 2026-11-01 | Runtime stuck-pending classification (Step 4): pending idempotency log older than PENDING_STUCK_MINUTES matching a RESTING maker position → skip (legitimate rest, D-13); matching a FILLED open position → mark **'confirmed'** (mirrors startup reconciliation's position-match branch :408-411); no match → mark 'failed' + cooldown WARNING (paper; live branch reuses the extracted order-status logic behind the PAPER_TRADING check) | The spec's literal "else mark failed" would mislabel a filled entry: 'failed' does NOT block resubmission (`check_pre_order_idempotency` blocks only 'confirmed'), so mislabeling a real fill licenses a duplicate live order in Step 7. Unreachable in paper today (both entry paths confirm-before-log), but the branch must be honest before live orders exist. Strictly more conservative than the spec; capital behavior unchanged |
| D-B15 | 2026-11-01 | Runtime resolution sweep (Step 4): a FILLED position on a cleanly resolved market (price exactly 0.0/1.0) closes via the shared close path (`exit_type='resolved'`); a NEVER-FILLED maker on a resolved/void market is **CANCELLED** (paper_fills cancel semantics: row deleted + idempotency 'cancelled'), never closed as a trade; a void-resolved position (0.5 price) is **SKIPPED + counted + cooldown WARNING** — never auto-resolved | A never-filled order owns no shares — closing it would fabricate P&L (D-13 extension to the resolution case). Void markets: the DO-NOT forbids auto-resolving (List A D-11 convention rejects 0.5 prices); a human decides. The resolution predicate is ported verbatim from scripts/gate_report.py:market_yes_outcome (the established D-11 implementation); startup reconciliation's own inline path is intentionally UNCHANGED this step (behavior preserved) — its void-closes-as-loss and resting-maker-closes-as-trade quirks are recorded as noticed issues for the owner to align later |
| D-B16 | 2026-11-01 | CRASH FIX — (a) `SPACY_MODEL` switched `en_core_web_lg` → `en_core_web_sm` (config knob unchanged, value changed; supersedes the GEMINI.md lg-alignment note until re-measured); (b) the Docker HEALTHCHECK replaced: `python -c "import main"` (a full second Python importing the whole app every 60s) → `curl` of a new in-process `127.0.0.1:8077/health` endpoint served by the bot's own event loop, started FIRST in main(), with `--start-period=120s`; (c) `ENV MALLOC_ARENA_MAX=2` | MEASURED, not guessed: `en_core_web_lg` alone = **708 MB RSS** (392 MB static vector table the junk filter never uses — it only needs NER); `import main` (the old probe) = 2.47s WARM on a fast dev machine, slower cold+contended in the container → 3 slow checks during a signal burst or the startup window = Docker kills the container: the exact hard-kill/no-traceback signature of all three crashes (2026-10-30 23:12 UTC ~80s in, 2026-10-31 09:33:31 UTC during a 605-signals/5min burst, 2026-10-31 23:40:53 UTC during a Polymarket-hack news burst). sm verified functionally: 10/10 IDENTICAL pass/block decisions vs lg on real headlines (the gate only checks valid-type entity PRESENCE; entity texts match). The new probe is TRUE liveness: it runs on the shared loop, so a hung loop cannot answer → restart; it answers from the very top of startup, covering the initialization window that killed the startup-phase crash |


## 11. RISK REGISTER

| Risk | Mitigation |
|---|---|
| Equity marking fetches prices for every open position each tick | Paper scale is tiny (few positions); per-position fail-soft + fallback-to-entry; cap fetches; revisit if position count grows |
| Lease table adds latency to every entry | One RPC per entry inside an already-multi-call critical section; TTL/renew cost bounded; measure in Step 3 tests |
| Hang + lease expiry → two processes in one critical section | Documented residual risk (Step 3); DB re-checks (tranche, idempotency) remain the second line; idempotency blocks duplicate order UUIDs |
| Swap logic (R5) churns the portfolio | One swap per signal, same-category only, `SWAP_MARGIN_CENTS` + re-entry cooldown; swap exits visible in the dashboard |
| Exit rules fire on stale books | Fail-soft retry + `EXIT_BOOK_DEAD_HOURS` alert; never a blind exit |
| Call-order test indices drift with every new read | Discipline rule 9; List A precedent documented in test comments |
| RLS still disabled on live DB (owner decision open) | Listed in Section 0 owner actions; List B adds no new broad grants |
| Migration gap (E-B11) bites before Step 7 lands | Pre-existing; Step 7 fixes it; if a fresh DB is needed earlier, apply `scratch/migration_copytrade_phase1.sql` manually (owner action) |
| Two-week paper gate vs pressure to flip live | Section 0 directive + D-B07 interlock; ROLLOUT.md preconditions are owner-verified checklists, not code flags alone |

## 12. SESSION LOG (append-only — newest on top)

| Date | Step | What was done | Next action |
|---|---|---|---|
| 2026-11-01 | ops/crash-fix | **ROOT CAUSE FOUND (measured, not guessed) + FIXED + DEPLOYED LIVE** (owner: "fix it smartly, not a temporary solution"). Forensics: THREE hard kills, no traceback, all SIGKILL-class — 2026-10-30 23:12 UTC (~80s after start), 2026-10-31 09:33:31 UTC (during a 605-signals/5min burst), 2026-10-31 23:40:53 UTC (during the Polymarket-hack news burst; the c8731511 deployment lived 2h08m) — every restart after each kill produced ZERO logs (died pre-flush), burning Railway's 10-restart budget → the repeated multi-hour outages. **Measured locally**: (a) `en_core_web_lg` alone = **708 MB RSS** (392 MB static vector table the junk filter NEVER uses — it only needs `doc.ents` NER) — the single largest allocation in the container; (b) the Docker `HEALTHCHECK` ran `python -c "import main"` every 60s = a full second Python importing the whole app chain (~200MB + 2.47s WARM on a fast dev machine, slower cold+contended) competing with the running bot AND probing nothing about it; (c) 3 slow checks (10s timeout) during a burst or the spaCy-loading startup window = Docker kills the container — the exact signature of all three crashes. **Fix (D-B16)**: (1) `SPACY_MODEL` lg → sm — verified 10/10 IDENTICAL pass/block decisions on real headlines (the gate checks valid-type entity PRESENCE only; entity texts match), ~620 MB freed, NER several times faster (GEMINI.md lg-alignment superseded until re-measurement; config knob unchanged so it's a one-line revert); (2) the probe is now `curl` of a NEW in-process `127.0.0.1:8077/health` endpoint (`monitoring/health_server.py`) served by the bot's own event loop and started FIRST in main() — TRUE liveness (a hung loop cannot answer → restart), zero second-process cost, `--start-period=120s` covers initialization, and the payload (status/uptime/paper_trading/environment) rides in the deployment logs; (3) `MALLOC_ARENA_MAX=2` (glibc arena fragmentation hardening for threaded spaCy executors). Docker image also downloads sm now (much smaller). 3 new endpoint tests; **681/681 green**. **Deployed (084671df) and verified live**: liveness endpoint is the first task (06:00:21 UTC), startup fully clean (singleton claimed immediately, reconcile → Telegram → equity 10000 / health 66.7 NORMAL → signal processing), spaCy sm NER making live pass/block decisions, **11+ min stable past every historical kill window with the healthcheck green throughout**. | **Step 5** (B5 exit engine) awaits the owner's go. Watch: if a crash EVER recurs, the new signature will differ (healthcheck failures now visible in deploy events; the endpoint's absence = process-level hang, not memory) — the fix is diagnostic-friendly. Owner items unchanged: IaC cutover checklist (before 2026-12-01), RLS decision, push when wanted |
| 2026-11-01 | 4 (B3) | **DONE (local — one `railway up` from the owner activates it; no schema change).** Baseline 651 → final **678/678** green (`scratch/baseline_step4.txt`). Plan first approved by the owner, then executed. **Structural deliverable — the ONE shared close path** (`execution/position_close.py`, D-B08): the close logic extracted VERBATIM from startup reconciliation (`_supabase_move_to_closed` + the inline copy-trade trust block, now deleted there) into one function every closer uses — startup recon (behavior preserved: its suite 8/8 green UNEDITED except one patch target, intent documented), the runtime sweep, and Step 5's exit engine from here. Two deliberate improvements documented in the module: (1) **claim-then-record** — the open row is deleted FIRST and the returned rows inspected (postgrest-py `delete()` defaults to `returning=representation`, VERIFIED in the installed client); an empty claim = another closer won → nothing written, killing the race-based double-close whose duplicate ledger row the equity anchor would double-count (the old insert-first order failed toward a silent duplicate on retry; the new one fails toward a loudly-CRITICAL missing row); (2) **the Supabase client is injected** — an explicit I/O boundary that also kept the reconciliation tests green unedited. **Runtime loop** (`execution/runtime_reconciliation.py`, paper_fills supervisor pattern, strict to_thread I/O): resolution sweep (shared D-11 predicate ported verbatim from `gate_report.market_yes_outcome` into `execution/resolution.py` as a three-state classifier — a failed fetch is UNAVAILABLE, never mistaken for NOT_RESOLVED; clean resolutions close UNDER the Step 3 market lease with a fresh row re-read inside it since paper_fills doesn't take the lease; never-filled makers on resolved/void markets CANCELLED via the new public `paper_fills.cancel_unfilled_maker` wrapper, never closed as trades — D-B15; void positions skipped + cooldown WARNING — the DO-NOT honored; Gamma capped at 50/tick with a ROTATING start offset so a full cap cannot starve the newest positions); stuck-pending sweep (D-B14: resting→skip / filled→confirm / no-match→failed + cooldown WARNING; live branch reuses the extracted `fetch_order_status` behind the PAPER_TRADING check; freshness guard in Python — no new mock-builder methods needed); in-flight visibility (`[OBSERVABILITY][INFLIGHT]` per tick + daily Telegram digest, UTC-date change only, restarts don't re-send). Counters `recon:*` in the service module. Config: 3 spec keys + RECON_ALERT_COOLDOWN_SECONDS. **Self-review caught a wedge**: a poisoned close (NULL entry_price → float(None)) would have aborted EVERY tick — per-row isolation added + regression test; also `_mark_idempotency_status` now mirrors startup's fields incl. confirmed_at. **Spec refinements recorded BEFORE building**: D-B14 (filled-position pending → confirm — 'failed' does not block resubmission, a duplicate-order hole for Step 7) and D-B15 (never-filled maker on resolved/void → cancelled, never closed; void positions never auto-resolved). **DoD grep gate**: `position_close.py` is the ONLY `closed_trades` insert writer repo-wide (all other references are reads). ZERO entry-hot-path changes — call-order ordinals untouched this step. **Noticed issues (NOT fixed)**: startup recon still closes void (0.5) markets as "loss" and closes resting makers as trades (pre-existing, behavior preserved — the runtime sweep does both correctly; owner may align startup later); the digest's closed_by_runtime_today counter resets on restart (in-memory; the ledger is the truth). | **Step 5** (B5 exit engine — the biggest step; consumes the shared close path; the four dead exit config keys come alive). Owner: `railway up` to activate Steps 3+4 live, then the go for Step 5. Crash-watch + IaC cutover checklist + RLS decision still open (owner) |
| 2026-11-01 | 3 (B4) — DEPLOY + ops | **Step 3 LIVE + two live incidents found & fixed.** (0) **Found the bot CRASHED on arrival** (● Crashed): last log 2026-10-31 09:33:31 UTC, abrupt end mid-normal-operation, NO app traceback — same hard-kill signature as the 2026-10-30 23:12 crash → now REPRODUCED (2 occurrences); Railway's restart budget (ALWAYS × maxRetries 10) was exhausted → **down ~11h48m**. Suspect host/OOM; the current deployment is stable 8+ min past the historical ~80-90s window; needs Railway memory metrics (owner). (1) **Deployed Step 3** (`railway up`, deployment 1d67368c → after the incident below, final c8731511). (2) **LIVE INCIDENT — RLS on the new table**: `claim_market_lease` failed 42501 "new row violates row-level security policy" — the platform had enabled RLS on the freshly created market_leases (bare, ZERO policies; every pre-existing table is RLS-off) — a bare RLS gate blocks the bot's anon-key writes entirely. The singleton guard did exactly its job: fail-closed, 30s retry loop, would have alert+exit(1) at 600s (it never came to that — fixed within the window). Fixed live via `ALTER TABLE market_leases DISABLE ROW LEVEL SECURITY` (matches project-wide state; the deployment then claimed and completed startup). **Durable fix committed (c307056)**: the market_leases migration now carries an RLS convergence guard — disables RLS ONLY when the table is RLS-on with ZERO policies (a bare platform default), and leaves it untouched when real policies exist, so the owner's future RLS enablement is never fought at startup. Future new-table migrations (Step 7 system_state) must copy this pattern. (3) **REDEPLOY (c8731511) live-verified the overlap guard end-to-end**: migrations 13/13 EXECUTED (market_leases smoke INSERT/SELECT/DELETE PASS — anon writes work), new instance hit `lease:conflict` (old instance held `__singleton__`) and retried every 30s, absorbed one transient claim timeout (`lease:error`, the to_thread+2s bound working live), then claimed at 21:32:51 and took over (~2min handover); reconcile → Telegram alert → equity 10000.00 / health 66.7 NORMAL persisted → signal processing; `__singleton__` held by bc2d36b2c900-2944124168bd, renewing every 30s (verified in DB, is_live=true, RLS=false). The deploy-overlap scenario from the Step 3 spec is now PROVEN IN PRODUCTION, not just in tests. (4) **IaC migration PREPARED, CUTOVER DEFERRED** (deliberate): `.railway/railway.ts` generated via `railway config migrate --variables unmanaged --apply` and committed (ea74845; filesystem-only, zero behavior change). Cutover is BLOCKED on three findings: the IaC DSL cannot express `deploy.restartPolicyType ALWAYS`/maxRetries (the IaC reference lists no restart-policy field and a cutover apply DELETES omitted settings — cutting over would drop the crash-resilience policy exactly while the bot is crash-prone); cutover itself REDEPLOYS the service; the generated `github(...)` source pins vs the `railway up` local-upload workflow are undocumented. railway.json keeps working until 2026-12-01. **Cutover checklist (owner/agent, before 2026-12-01)**: re-check the IaC reference for restart-policy support → if absent, decide whether the restart policy can live server-side (set once in the dashboard) → `railway config migrate cutover` → `railway config plan` (review deletions!) → `railway config apply` → verify a `railway up` deploy still works → only then delete railway.json. (5) Telegram: two startup alerts dispatched during the deploys — expected. Tests 650/650 unchanged (no runtime code changed beyond the migration guard + its contract test). | **Step 4** (B3) — all dependencies (Steps 1+3) done and live; **owner go to start**. Owner items: crash investigation via Railway memory metrics (bot stability); IaC cutover checklist before 2026-12-01; RLS advisor decision still open (unchanged); push main if/when wanted |
| 2026-11-01 | 3 (B4) | **DONE.** Baseline 620 → final **650/650** green (`scratch/baseline_step3.txt`; 620/620 confirmed at HEAD bb5be02 first). Live `execute_sql` RPC re-verified VOID via pg_get_functiondef → **D-B13 recorded**: the lease claim is a dedicated `claim_market_lease(p_market_id, p_owner, p_ttl_seconds) RETURNS BOOLEAN` SQL function (atomic `INSERT … ON CONFLICT … WHERE lease_until < now() OR owner = EXCLUDED.owner RETURNING true`; PostgreSQL re-evaluates the WHERE under row lock → exactly one winner under concurrency; the `owner = $owner` branch doubles as atomic same-owner renewal), created by the market_leases migration and called via `client.rpc(...)` — NOT through execute_sql. Six commits: (1) schema + `migrations.py` registration (test_table gained a market_leases smoke branch — PK is market_id, no id column) + `tests/test_schema_lease.py` contract tests + scratch mirror (`scratch/migration_market_leases.sql`, gitignored); (2) `coordinator/lease.py` (tri-state LeaseOutcome — CLAIMED/CONFLICT/ERROR — because entries drop on either failure but the singleton renewer must tolerate transient errors and exit only on definitive loss; owner = hostname + boot-unique suffix, logged at startup; release is the one fail-soft path — TTL self-heals, never masks a finally's in-flight exception; drop counters `lock:timeout`/`lease:conflict`/`lease:error` live in the service module per the equity/health precedent) + 5 config keys (`LEASE_RENEW_INTERVAL_SECONDS=30` added as the spec's "every 30s" named home) + `tests/lease_mock.py` (shared claim-semantics mock — one implementation across every test file so the conditional-upsert mirror cannot drift) + 18 tests; (3) pipeline critical section: bounded `asyncio.timeout(10)` lock acquire (timeout → `lock:timeout` fail-closed drop BEFORE any claim; asyncio.Lock cannot strand the lock on a cancelled acquire), lease claimed second inside the lock, both released in a hardened `finally` on every blocked/success/exception path; 5 test files gained rpc mocks + `coordinator.lease.get_client` patches; call-order ordinal 9→10 with the documented sequence; test_dedupe_gate's mock now honors DELETE (attribute was silently ignored — the release path needs honest delete semantics; no prior test relied on the no-op); (4) executor section: same guard pair + the DoD two-process test (proc-B drops fail-closed at the bounded wait against proc-A's held lock+lease, writes nothing, steals nothing; same-owner re-entry after release succeeds); (5) main.py singleton guard after migrations/connection-verify, BEFORE reconciliation (two instances must never reconcile concurrently — placement documented), renewer task joins the supervised set immediately after the claim; (6) adversarial self-review ×2 → `fix(lease)` hardening commit. **Self-review caught two bugs the tests missed**: (a) supabase-py execute() is blocking HTTP, so asyncio.timeout around a direct call cannot cancel it — lease claim/release now run execute via `asyncio.to_thread` (the stricter `load_market_state` pattern, our neighbor in the same section; ground rule 9's asyncio.timeout preserved); a timed-out orphaned claim leaks a row under our owner — self-heals via TTL and reads as same-owner renewal next claim (fail-closed direction, documented); (b) a CancelledError inside the lease release (shutdown-only) would strand the in-process lock — both finallys now free the lock before propagating. **Documented tradeoffs**: a hard-killed holder makes the next instance wait out the remaining ≤90s TTL by design (restarts, not just deploy overlaps); the singleton claim counts an unverifiable claim as not-claimed (Supabase down at boot → alert + exit(1) after 600s, Railway restarts — slower than the old reconciliation-exit but the same terminal state, fail-closed); lease adds ~1 RPC per entry (risk register accepted). **Noticed issues (NOT fixed, per discipline)**: (a) the loose-timeout pattern (asyncio.timeout around DIRECT supabase-py execute calls without to_thread) is codebase-wide — equity.py/health_score.py/etc. only truly bound calls that await (e.g. the mocked get_client sleep in tests); lease.py now uses the strict pattern, the rest is pre-existing (candidate for a Step 4+ cleanup); (b) `alert_system_halt` is fire-and-forget (create_task) — a sys.exit(1) immediately after dispatch can cancel the Telegram send before it completes (pre-existing alert pattern, main.py:179-190 has the same property); (c) SystemExit from the singleton renewer task propagates through the event loop and kills the process (verified semantics, matches main.py's crash path) — a future hardening could route it through a supervisor instead. | **Step 4** (B3: runtime reconciliation + in-flight visibility — depends on Steps 1+3, both done). Superseded same-day: Step 3 DEPLOYED LIVE (see the newer row above) — RLS incident found & fixed durably, overlap guard live-proven, IaC prepared with cutover deferred (checklist there) |
| 2026-10-31 | ops/verify | **Step 2 DEPLOYED LIVE + owner checklist executed autonomously (owner studying for an exam — delegated).** (1) **Deploy**: `env -u RAILWAY_API_TOKEN railway up` → Online; live logs verified: `[EQUITY] Snapshot: equity=10000.00 anchors={'daily': 10000.0, 'weekly': 10000.0, 'monthly': 10000.0}` and `[HEALTH] score=66.7 mode=NORMAL components={50,50,50,50,100,100} persisted=True` on the first tick, second tick at +300s confirms the loop sustains; no CRITICAL/restart. **`daily_performance.health_score=66.67` verified in the live table** — the full Step 2 chain is proven on production. (2) **LIVE BUG FOUND + FIXED: the poisoned 2026-10-30 anchor row (starting 114,181.99) was NOT harmless** — it sat inside the current ISO week (Mon 10-26) and current month, so the Step 1 equity gate had been computing ~91% weekly/monthly drawdown → weekly HALT + monthly SHUTDOWN were permanently tripped since the deploy-session restart (no entries attempted since, so nothing visibly failed). Deleted via SQL (owner-legacy-cleanup precedent; row was a pre-instrumentation artifact); all three anchors now derive from the clean 2026-10-31 row (10,000.00) and one cooldown-gated EQUITY_ANCHOR_BOOTSTRAP WARNING per period is the designed honest behavior. Lesson recorded: "harmless" claims about period-spanning data must be checked against weekly/monthly windows, not just daily. (3) **Stale RAILWAY_API_TOKEN deleted** from the owner's Windows User env — staleness PROVEN first (`railway whoami` with token → Unauthorized; without → logged in as Anshu); fresh terminals are clean, already-open shells keep the old inherited value until reopened. (4) **Telegram VERIFIED end-to-end**: one test message sent via the bot's own Railway-env credentials → HTTP 200, ok:true, message_id 330 delivered to the owner's chat; zero "Telegram alert failed" lines in live logs. Owner will see 2 messages when he checks his phone (startup alert + this test). (5) **Weekly reports automated** on the owner's laptop: 3 Windows scheduled tasks (Sundays 09:00/09:20/09:40 local, StartWhenAvailable + battery allowances, current user) running `C:\Python314\python.exe -m scripts.{gate_report,strategy_dashboard,signal_drift_report}` with output appended to scratch/weekly_reports.log; gate_report smoke-run executed for real. (6) **`.env.test` SUPABASE_URL/KEY fixed** — it pointed at `fake.supabase.co` (why List A's smoke-runs "hit a DNS failure"); now points at the real PolyAgent project (REST domain, IPv4 — note: `db.*.supabase.co` direct-postgres hosts are IPv6-only and unresolvable from this laptop, irrelevant to supabase-py REST). Suite re-run after the change: 620/620 green (proves tests genuinely mock the DB layer). (7) **Noticed issue (NOT fixed)**: `.env.test` Telegram creds stay FAKE placeholders deliberately — real creds there would let any unmocked fire-and-forget alert path in test runs send real messages; consequence: weekly reports from the laptop write scratch/ markdown only and log a Telegram 404/skip line (harmless noise). Revisit with Step 7's Telegram work. Railway deployment log note: config-as-code (railway.json) deprecation warning repeats — migrate to `.railway/` IaC before 2026-12-01. | **Step 3** (B4: lock & concurrency hardening) — all owner checklist items now DONE except the RLS decision and the Railway GitHub App reconnect (both optional; `railway up` works) |---|
| 2026-10-31 | 2 (B1) | **DONE.** Baseline 563 → final **620/620** green (scratch/baseline_step2.txt). Built `monitoring/health_score.py`: six producers (win rate last-20, rolling 30-day agent Brier with the gate-report YES-line outcome mapping — Class A NULL-estimate rows excluded whole, slippage via `fill_simulator.signed_slippage_units` over closed+open fills incl. the NO-side mirror, feed latency vs 5s/22s targets, worst-period drawdown vs each period's own threshold, D-B11 interim category concentration), each fail-soft to a LOGGED neutral 50; evaluation supervisor (`health_check` task in main.py, paper_fills pattern) scores via the pure `risk_engine.compute_health_score`, persists into TODAY's `daily_performance.health_score` (UPDATE-only — the equity anchor task owns row creation; a date-only insert is forbidden, the 2026-10-31 orphan-row lesson), caches the mode in-process, fires mode-transition alerts (`alert_health_score` NEW; cooldown PER TARGET MODE so a DEFENSIVE→FULL_HALT escalation can never be suppressed; booting degraded alerts, booting NORMAL seeds silently), diffs `derive_allocations` states for probation/retired transitions (`alert_strategy_probation` NEW; a strategy first seen already failing alerts too), and wires the long-orphaned `alert_brier_score_exceeded` to the rolling-Brier crossing of 0.23. Entering FULL_HALT also fires the existing `alert_system_halt` with REAL values (reason carries the score; optional `detail` line added backward-compatibly). **Enforcement (entries only, D-B04)**: FULL_HALT drops at the TOP of both entry paths (pipeline body + Class A executor Risk Gate 0) with counter `health:full_halt`, pre-LLM, cache-only — ZERO new Supabase reads on the trade path, call-order ordinals unchanged; DEFENSIVE applies `HEALTH_DEFENSIVE_SIZE_MULTIPLIER=0.5` stacked multiplicatively after the probation multiplier, documented in the `[OBSERVABILITY][SIZING]` log (health_mode/health_mult) and the executor Sizing log. **SPEC CORRECTION**: the feed-latency spec said "reuse the existing stats-reporter data" — the stats reporter holds only counters (no latency exists anywhere in the repo), so `run_pipeline` became a thin timing wrapper (body → `_run_pipeline_impl`) recording (duration, fast_path) for analyst-reached signals into a bounded deque (cancelled + pre-analyst-dropped signals excluded — they don't exercise the feed). `coordinator/equity.py` gained a read-only `get_cached_snapshot()` (Step 1 artifact extended, not rewritten). Config: 4 spec keys + 8 supporting producer thresholds, all commented; no schema change (`health_score` column existed); `risk_engine` untouched. **Test-caught bugs fixed pre-ship** (commit 2a49216): sync feed_latency/drawdown producers would have been permanently pinned at neutral 50 (await bug); transition cooldown could suppress a FULL_HALT escalation; eager coroutine construction on suppressed alerts; slippage producer crashed on NULL fills (`signed_slippage_units` raises TypeError on None input — noticed issue below); a strategy first seen already on probation never alerted. **Noticed issues (NOT fixed, per discipline)**: (a) the close path's `brier_contribution` column is unusable for the agent Brier — it squares the legacy `agent_estimate` (entry-price proxy on Class A) against a SIDE-line outcome; the health producer uses `p_model_at_entry` + YES-line mapping instead; candidate cleanup for Step 4's shared close path. (b) `fill_simulator.signed_slippage_units` docstring says "None on invalid inputs" but a None input raises TypeError; all its callers pass floats (my producer now skips NULLs first). (c) Mode cache reads NORMAL until the first tick (~seconds after boot) — documented; the equity gate independently fail-closes entries during that window. (d) Supabase down at boot → all producers neutral-50 → score 50 → DEFENSIVE_MODE (half sizes) until data returns — the spec's own fail-soft design, self-heals. | **Step 3** (B4: lock & concurrency hardening — tracker-independent). Owner: `env -u RAILWAY_API_TOKEN railway up` to activate Step 2 on the live bot (with empty closed_trades the all-neutral data scores 66.7 → NORMAL — no behavior change until real degradation); verify Telegram alerts arrive; the earlier owner actions (GitHub App, stale RAILWAY_API_TOKEN, RLS decision, weekly scripts) remain open |
| 2026-10-31 | 1 (B2) — DEPLOY | **Bot is LIVE on Railway with Step 1.** Owner-authorized push of main (39 commits: List A 0–5 + List B Step 1). Four live blockers found and fixed: (1) Supabase PolyAgent was PAUSED (free-tier) — restored via MCP, ACTIVE_HEALTHY; (2) Railway service had NO GitHub repo trigger (push did not deploy) — deployed via `railway up` (CLI auth via owner browserless login; note: a stale `RAILWAY_API_TOKEN` in the owner's system env was breaking all Railway CLI calls); (3) the `execute_sql` RPC that `memory/migrations.py` runs EVERY migration through never existed in the live DB → all migrations failed silently at every startup (base tables dated to March) → created the RPC (idempotent, one-time; SECURITY NOTE: SECURITY DEFINER + EXECUTE to anon = arbitrary SQL via the bot's anon key — acceptable only while RLS stays disabled; revisit at the RLS advisor decision) → all 12 migrations EXECUTED at startup; (4) smoke-test bug: the migrations INSERT/SELECT/DELETE integrity test selected by `id` but keyed by `date` → cleanup never ran → every startup orphaned a date-only `daily_performance` row whose NULL anchor fail-closed the equity tick (fixed: test uses 1970-01-01 + correct key; equity bootstraps unusable anchors with EQUITY_ANCHOR_BOOTSTRAP WARNING instead of blocking forever). Also fixed live: the NO-side price-fetch fallback would fabricate a near-max payout (legacy rows at entry 0.019 → valued 0.981, +100k fake equity) — fallback is now COST basis for both directions. **Legacy data CLEANED (owner decision, no backup kept)**: 4 phantom open_positions (July, dead market "665460", books 404) + 2 old closed_trades (+12,320.51 "realized" was a manual-liquidation script marking exits at a placeholder 0.50) deleted; both tables verified 0; **fresh 2026-10-31 anchor bootstrapped at exactly 10,000.00 / pnl 0.0000**. **Crash + hardening**: the 23:12 UTC deploy died ~80s in (hard kill, NO app traceback — suspect OOM/host, one-off) and stayed DOWN 5.5h because Railway had no restart policy → added `railway.json` `restartPolicyType: ALWAYS` (CLAUDE.md Layer 7 auto-restart requirement); redeployed, Online 17+ min past the crash window. Note: Railway deprecates railway.json config-as-code 2026-12-01 (migrate to .railway/ IaC before then). Today's poisoned 2026-10-30 anchor row (starting 114,181.99) is historical and harmless. Owner: reconnect Railway GitHub App for push-to-deploy (or keep `railway up`); delete stale `RAILWAY_API_TOKEN` from system env; confirm Telegram alerts arrive; watch for another crash (if it reproduces, investigate memory) | **Step 2 (B1 health score)** — now unblocked: closed_trades is empty so the health producers start from clean data |
| 2026-10-30 | 1 (B2) | **DONE.** Baseline 536 → final **561/561** green (scratch/baseline_step1.txt). Built `coordinator/equity.py` (mark-to-market: YES/NO marking, fallback-to-entry counted, book-fetch cap; `daily_performance` anchors with never-moving starting anchor; weekly/monthly derivation; snapshot cache + fail-closed `get_entry_gate` with `equity:stale`/`equity:unavailable` counters and cooldown-gated WARNINGs; paper_fills-pattern supervisor). Registered `equity_snapshot` task in main.py. Wired BOTH drawdown paths (pipeline defaults replaced; executor self-comparison + `current_pct=0.0` + always-DAILY-threshold bug killed — the threshold fix is an adjacent bug repaired in the same lines). `get_live_portfolio_value` now returns marked equity in paper mode (E-B5). DoD e2e: injected book drop → `check_drawdown` HALT with a REAL 10% on BOTH paths (test_6_7b + test_class_a_drawdown_halt_from_real_equity_gate). Grep gates clean; `risk/risk_engine.py` untouched; no schema change (no migration). Recorded D-B12 (paper cash = constant + realized PnL − committed cost; resting makers contribute 0). Notes: equity drop counters live in `equity.get_drop_counters()` (not the pipeline registry); on the dormant news path the equity gate runs before the estimator drop, so a stale-equity window shifts the drop counter from `estimate:no_data` to `equity:*` (correct fail-closed); equity gate adds ZERO Supabase reads to the trade path (cache-only), so call-order ordinals are unchanged. **Owner action: restart the bot** to activate the supervisor and both wired gates on the running instance | Begin **Step 2** (B1 health score — consumes Step 1 equity). Owner: bot restart (also activates List A migrations), weekly scripts, RLS decision — all still open |
| 2026-10-30 | — | List-B.md created from a 3-agent verified exploration of the codebase (evidence E-B1..E-B15); work order fixed; decisions D-B01..D-B11 recorded; owner actions carried from List A | Await operator go for **Step 1** (B2: drawdown anchors + mark-to-market equity). Owner: apply pending List A migrations / restart the bot when convenient — the List A instrumentation clocks start then |

## 13. PART B EXIT CRITERIA (all must hold)

1. A paper test demonstrates a REAL drawdown HALT (non-zero pct); no same-value/same-snapshot drawdown call remains; `daily_performance` is written and read.
2. Health score computed on schedule, persisted, enforced (FULL_HALT blocks entries on both paths, DEFENSIVE halves sizes, exits unaffected); transition alerts fire with cooldown; probation alerts live.
3. Both entry critical sections bounded-wait + lease-guarded; a two-process simulation cannot double-enter; the singleton guard prevents overlapping-deploy double-trading.
4. Runtime reconciliation loop live: resolved positions close without restart through the ONE shared close path; stuck pending swept (resting-maker exclusion proven); in-flight digest visible.
5. Exit engine live in paper: all six rules boundary-tested; exits flow through the shared close path; re-entry cooldown enforced on both entry paths; gate report separates early exits; wallet trust untouched by early exits.
6. Correlated exposure is event-cluster based (test proves two clusters pass / one blocks); no fixed-constant exposure denominators remain.
7. The order gateway is the single order path; the live branch is interlocked behind `PAPER_TRADING=false` + `TRADING_ENABLED=true`; kill switch + /halt /resume /status /flatten live and DB-persisted; migration gap closed with a contract test; `ROLLOUT.md` written (preconditions, capital ladder, custody, runbook, kill criteria, flip/revert procedures).
8. All List A invariants intact (risk-engine purity, idempotency, reconciliation-first startup, `.env.test`, thresholds via config, dormant pipeline preserved); full suite green; every Supabase call-order change documented.

---

## 14. SUGGESTED KICKOFF MESSAGE (paste into the new chat)

```
You are the senior quant engineer owning this repo's Part B work. Read List-B.md
(project root) FULLY first — it is the single source of truth: status dashboard,
verified evidence table (E-B1..E-B15 with file:line), operating discipline and the
global DO-NOT list (Section 1.3), master tracker, per-step specs with work plans
and Definition of Done, decision log, and session log. Then read CLAUDE.md fully
(architecture invariants), and skim List A.md Section 0 + Decision Log for context
(Part A is complete; its artifacts — estimator, cost model, fill simulator, edge
monitor, gate report — are yours to reuse, never rebuild).

Then: verify the baseline (pytest tests/ output to a file; record the count in the
List-B.md Session Log), re-confirm the Step 1 evidence lines with grep, briefly
state your Step 1 implementation plan, and start working. One step at a time,
production-grade only, every work-plan item committed and ticked off in List-B.md,
suite green before anything is declared done, session log updated every session.
The paper-trading gate in Section 0 is absolute: no code path may place a live
order while PAPER_TRADING=true. If a spec seems wrong or unsafe, stop and record
it in the Decision Log instead of improvising.
```

---

END OF List-B.md
