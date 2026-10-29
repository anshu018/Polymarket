# List A.md — Part A: "Edge and Truth" — Master Plan & Progress Tracker

> **This file is the single source of truth for Part A work.** Read it fully before doing any
> work on List A. Update the Master Tracker, per-step checkboxes, and Session Log every session.
> If context is ever lost, this file is the recovery point.

---

## STATUS DASHBOARD

| Field | Value |
|---|---|
| Created | 2026-10-06 |
| Last updated | 2026-10-29 |
| Current phase | Phase 0 — Steps 0–5 complete |
| Current step | **Step 5 DONE** — next action is **Step 6** (A3 historical data + calibration activation — the LAST step; full spec in §5 Step 6) |
| Overall status | **IN PROGRESS — Steps 0–5 landed** (fail-closed pipeline; honest net-edge gate; signals instrumented + novelty; attribution ledger + CUSUM + allocation; Kelly on shrunk real probability; paper fills now book-aware with a restart-safe maker monitor, the market baseline is recorded, and the honest gate report judges on printed numbers) |
| Live-pipeline safety state | ✅ SAFE: no fake edge, no constant estimates; entries require net_edge > 2¢ after verified costs from a live book; paper fills are what the book would actually give (takers cross the spread depth-checked, makers rest and fill only on a through-trade); Kelly never sees LLM confidence (regression-tested); a shrunk probability at/below the market price drops (sizing:kelly_zero); novelty dups drop pre-LLM; allocation throttles are advisory (hard gates fail-closed). |
| ⚠️ Operator action needed | **(1)** Apply the Step 3 attribution migration to the live Supabase if not yet done (scratch/migration_attribution_columns.sql, or restart the bot — migrations.py runs at startup). **(2)** Apply the Step 5 migration (scratch/migration_step5_fill_and_baseline.sql — fill/fill_type/resting_price on open_positions, fill/fill_type on closed_trades, resolved_outcome/resolved_at on signal_outcomes, unfilled-maker partial index) or restart the bot; until then fill/baseline writes fail best-effort. **(3)** Schedule the weekly gate report (python -m scripts.gate_report) owner-side, same as the other offline scripts. |

**How to resume work (any future session):**
1. Read this file top to bottom.
2. Check `STATUS DASHBOARD` + `Master Tracker` for the current step.
3. Read the current step's spec (Section 5) in full.
4. Read the last 3 entries of the `Session Log` (Section 11).
5. Work. Then update: dashboard, tracker, checkboxes, session log.

---

## 1. MISSION & THE SPINE

Part A replaces every fake number between "signal detected" and "order placed" with a
measured one. The unified formula every strategy converges to:

```
net_edge = p_model − price − fees − half_spread − expected_slippage      (signed by trade side)
size     = kelly(p_model_shrunk, odds, fraction) × portfolio_value, capped as today
```

Dependency chain: **data → estimate → costs → size → measured alpha → honest gate → durable allocation.**

**Agreed work order (A3 deliberately LAST — time-consuming, deferred by operator decision
2026-10-06; see Decision Log D-03):**

```
Step 0  A1-S  Estimator contract + fail-closed swap (kills the fake edge)
Step 1  A2    Cost model + net-edge gate (maker/taker)
Step 2  A6    Signal instrumentation + novelty detection (time-gated: starts data clock)
Step 3  A7    Attribution ledger + edge-fade monitor (time-gated: starts ledger clock)
Step 4  A4    Kelly on real probability (+ shrinkage)
Step 5  A5    Honest gate: book-aware paper fills, market baseline, gate report
Step 6  A3    Historical data + calibration activation (LAST — full spec in Step 6)
```

**Interim operating state (until Step 6 activates estimators):**
The main news pipeline runs **fail-closed and effectively dormant** (no estimate → no trade).
The only live-eligible strategy is **Copy Edge Class A** (trust-sized, estimate-free), with
Class B joinable once its wallet hit-rate estimator is wired (Step 0). This is intentional
and safe. The bot must NOT trade on a fake number while Part B bugs are being fixed.

---

## 2. VERIFIED ROOT CAUSES (evidence — re-verified 2026-10-06)

| # | Finding | Location | Evidence |
|---|---|---|---|
| E-1 | Fake estimate in live path (into LLM prompt) | `coordinator/pipeline.py:664` | `agent_estimate=market_price + 0.10,  # Simulate calibration model adding empirical edge` |
| E-2 | Fake estimate in live path (into edge gate) | `coordinator/pipeline.py:747` | `estimated_probability = market_price + 0.10  # Simulating calibration model probability` |
| E-3 | Simulated time-to-resolution | `coordinator/pipeline.py:655` | `time_to_res = 48.0  # Simulated` — while real `end_date_iso` IS fetched at `:641` and parsed only later at `:836` |
| E-4 | LLM shown the fake number as truth | `llm/trade_decision.py:315` | Prompt line: `Calibration Model Probability Estimate: {agent_estimate:.4f}` |
| E-5 | CalibrationModel exists but is ORPHANED | `strategies/calibration.py` (`get_category_estimate`) | Grep shows zero call sites outside tests/calibration.py itself |
| E-6 | Kelly fed LLM confidence, not probability | `coordinator/pipeline.py:765-788` | `kelly_size(win_probability=clamped_conf, ...)` — conf ∈ [0.75, 0.88] → fractional Kelly ≥ 5% → always binds the 5% cap ("flat 5%") |
| E-7 | Kelly function itself is correct | `risk/risk_engine.py:23-45` | Formula verified: `f = (odds*p − (1−p)) / odds`, fractional, × portfolio |
| E-8 | Edge gate is cost-blind and direction-blind | `risk/risk_engine.py:183-207` (`check_edge`) | Uses `abs(agent_estimate − market_price)`; no fees/spread/slippage anywhere in main pipeline |
| E-9 | Calibration trains only on own `closed_trades` | `strategies/calibration.py:116-158` | Table is empty (0 resolved records, per PROGRESS.md Layer 3 note) — and would train on the fake estimates (garbage loop) |
| E-10 | No fee model, no novelty detection, no forward-price measurement, no per-strategy attribution in main pipeline | various | Confirmed by read of `config.py`, `coordinator/pipeline.py`, `strategies/calibration.py` |

---

## 3. NON-NEGOTIABLE GROUND RULES (invariants — inherited from CLAUDE.md + Part A decisions)

1. `risk/risk_engine.py` stays **pure Python**: deterministic, <1ms, zero LLM/network imports.
   All new math (cost model, shrinkage, CUSUM) is pure deterministic functions or pre-fitted
   coefficients loaded from tables — never fitted at runtime.
2. **No constants for estimates. Anywhere. For any reason.** Tuning `0.10` to another constant is forbidden.
3. **The LLM never outputs the probability.** It *receives* the estimate as context and reasons about it.
4. **Fail-closed:** if no estimator has data for a (strategy, category) cell → drop the signal
   (`_increment_drop("estimate:no_data")`). Never trade on a guess.
5. Once Step 1 lands, **gross edge is retired as a metric** — net edge only.
6. Once Step 6 lands, calibration never trains on the fake-estimate-era rows of `closed_trades`
   (tag/purge them at backfill time).
7. **No raw dataset files in Supabase.** Raw parquet stays local; Supabase holds aggregates only (free-tier safe).
8. All thresholds via `config.py`. No hardcoding thresholds in modules.
9. Tests use `.env.test` only — `.env` is permanently off-limits to tests/scripts (CLAUDE.md rule).
10. Repo style: type hints, docstrings, `logging` (no `print`), explicit try/except with fallbacks,
    Supabase reads wrapped in 2s timeout, idempotency + startup reconciliation untouched.
11. Every step lands as its own small commit with its own tests. No drive-bys across steps.

---

## 4. MASTER TRACKER

Status vocabulary: `NOT STARTED` / `IN PROGRESS` / `BLOCKED` / `DONE` / `VERIFIED`.

| Step | Item | Depends on | Status | Started | Completed | Notes |
|---|---|---|---|---|---|---|
| 0 | A1-S Estimator contract + fail-closed swap + real `time_to_res` | — | **DONE** | 2026-10-06 | 2026-10-06 | Fake `+0.10` removed (grep-clean); 254 tests green (baseline 224 + 30 new); D-08 `wallet_address` kwarg |
| 1 | A2 Cost model + net-edge gate + maker/taker rule | Step 0 (contract only) | **DONE** | 2026-10-06 | 2026-10-06 | Fee schedule VERIFIED (D-09); `check_edge` deprecated, no live callers; 306 tests green (254 + 52 new) |
| 2 | A6 `signal_outcomes` instrumentation + novelty detection | — | **DONE** | 2026-10-07 | 2026-10-07 | Sampler + novelty live in pipeline; drift report shipped; 355 tests green (306 + 49 new). ⚠️ Owner must apply the migration to the live DB (or restart the bot to trigger migrations.py) — data clock starts then. Paper-mode ≥1 week observation still running |
| 3 | A7 Attribution columns + edge-fade monitor (CUSUM) | — | **DONE** | 2026-10-07 | 2026-10-07 | Ledger columns live on both tables (verified via Supabase MCP; both tables were 0 rows → strategy backfill = documented no-op); pipeline + Class A + close-path writes; CUSUM tuned once (D-10); mechanical allocation enforced at the sizing read; dashboard shipped. 421 tests green (355 + 66 new). ⚠️ Owner must apply the attribution migration / restart the bot — ledger clock starts then |
| 4 | A4 Kelly on real probability + shrinkage | Steps 0, 1 | **DONE** | 2026-10-07 | 2026-10-07 | Kelly consumes shrunk side-adjusted p (exact-value regression proves confidence is out); shrinkage K=20; kelly_zero drop for no-edge-at-odds; mandatory `[OBSERVABILITY][SIZING]` line with binding_constraint; grep gate clean. 435 tests green (421 + 14 new) |
| 5 | A5 Book-aware paper fills + market baseline + gate report | Steps 1–4 | **DONE** | 2026-10-29 | 2026-10-29 | Fill simulator (pure) + pipeline Step D + Class A wiring + restart-safe maker monitor + market baseline (signal_outcomes) + `scripts/gate_report.py` (cluster bootstrap, Brier-vs-market, printed-numbers verdict); dashboard Brier column live. 536 tests green (435 + 101 new). ⚠️ Owner must apply the Step 5 migration / restart the bot |
| 6 | A3 Historical data + calibration activation (LAST) | Steps 0–5 | NOT STARTED | — | — | Operator decision: deferred as time-consuming (D-03) |

Priority flags for scheduling: Steps 2 and 3 are cheap AND time-gated — schedule them
**early within the bug-fixing window**, even if done in small pieces.

---

## 5. STEP SPECS

### Step 0 — A1-S: Estimator contract + fail-closed swap

**Problem.** The bot's probability estimate is a constant (`market_price + 0.10`) fed to both
the LLM prompt (E-1, E-4) and the edge gate (E-2). The existing `CalibrationModel` is orphaned (E-5).
Until real estimates exist, the pipeline must be **structurally unable** to trade.

**Solution.**

1. New module `strategies/estimator.py`:

```python
@dataclass(frozen=True)
class EstimateResult:
    p_point: float | None      # None = "no data" → caller MUST drop the signal
    sample_size: int           # n behind the estimate (0 when p_point is None)
    method: str                # 'recalibration_base_rate' | 'velocity_drift' |
                               # 'resolution_analog' | 'copy_wallet_hitrate' | 'none'
    computed_at: datetime

async def get_estimate(strategy: str, category: str, market_id: str,
                       market_price: float, side: str) -> EstimateResult
```

2. Estimator registry (one function per strategy; each fail-closed):
   - `recalibration` → **stub until Step 6**: returns `p_point=None` (no data).
   - `velocity` → **stub until Step 2 data accumulates** (`signal_outcomes` drift): returns `None` for now.
   - `resolution` → **stub until Step 6** (historical analogs): returns `None` for now.
   - `copy_edge_class_b` → **live immediately**: wallet hit rate from `tracked_wallets`
     (`wins_count, losses_count` → Laplace-smoothed `(wins+1)/(wins+losses+2)`), `sample_size = wins+losses`.
3. Pipeline changes (`coordinator/pipeline.py`):
   - Line ~664: replace `agent_estimate=market_price + 0.10` with the estimator call. If
     `p_point is None` → drop signal **before** Contract Parser / Trade Decision (zero token burn):
     `_increment_drop("estimate:no_data")`.
   - Line ~747: replace `estimated_probability = market_price + 0.10` with the same
     `EstimateResult.p_point` (pass it down; do not recompute).
   - Line ~655: delete `time_to_res = 48.0`; parse the real `end_date_iso` (already fetched at
     `:641`) BEFORE the Trade Decision call and compute `time_to_resolution_hours` from it.
     Missing/invalid `end_date_iso` → conservative fallback (config `DEFAULT_TTR_HOURS = 720`)
     **plus** a drop-counter tag `estimate:ttr_fallback` so it stays visible.
4. Keep the LLM prompt field (`trade_decision.py:315`) — it now receives a real estimate or the
   signal never gets there. Rename the prompt label to `Model Probability Estimate (source: {method}, n={sample_size})`.

**Work plan.**
- [x] Create `strategies/estimator.py` with `EstimateResult` + `get_estimate` + registry
- [x] Implement `copy_edge_class_b` wallet hit-rate estimator (live)
- [x] Wire pipeline call site at ~664; drop-before-LLM on `None` with counter
- [x] Wire pipeline gate input at ~747 from the same `EstimateResult`
- [x] Fix `time_to_res` from real `end_date_iso`; add `estimate:ttr_fallback` counter
- [x] Update drop-counter stats reporter to expose `estimate:no_data`
- [x] Tests: estimator unit tests (contract, fail-closed, Laplace math, side adjustment `p_side = p` YES / `1−p` NO)
- [x] Tests: pipeline integration — no `+0.10` reachable; signal with no data is dropped pre-LLM; mock asserts LLM not called
- [x] Grep gate: `grep -rn "0\.10" coordinator/ llm/` returns no live-path hits (docs/comments ok)

**Definition of Done.** Grep clean; dormant-state integration test green (all main-pipeline
signals drop at `estimate:no_data`); copy Class B estimator live; `time_to_res` real; all tests green.
Status: **DONE (2026-10-06)** — 25 estimator unit tests + 5 pipeline integration tests added;
23 dormancy-affected existing tests repaired via an `estimator_has_data` fixture (intent preserved);
Class B e2e wallet given 18W/2L history so the REAL estimator runs end-to-end. Suite: 254 passed / 0 failed.

---

### Step 1 — A2: Cost model + net-edge gate + maker/taker rule

**Problem.** `check_edge` (E-8) is symmetric, direction-blind, cost-blind. At 2¢ with taker fees
≈ 3.9% of trade value, a "7¢ edge" can be fiction. No spread/slippage modeling exists in the main pipeline.

**Solution.**

1. New pure module `risk/cost_model.py` (deterministic, no I/O — book values passed in):
   - `taker_fee_units(price, fee_cfg) -> float` — fee converted to probability units.
     ⚠️ VERIFY item: confirm Polymarket's current fee schedule from official docs at
     implementation time and encode as config (`TAKER_FEE_RATE`, `MAKER_FEE_RATE`); operator
     reports ~3.9% of trade value at 2¢ — do not hardcode that number, parameterize it.
   - `half_spread_units(best_bid, best_ask) -> float` — taker pays `(ask − mid)`.
   - `expected_slippage_units(size_usd, book_depth_usd) -> float` — conservative default
     `full_spread × 1.2`; replace later with empirical curve from Step 5's decision-vs-fill logs.
   - `maker_fill_haircut_units(...) -> float` — queue-risk haircut for maker entries (config).
   - `net_edge(p_model, price, side, order_type, book, fee_cfg) -> float` — **signed** by side:
     YES buy: `p − price − costs`; NO buy: `(1 − p) − (1 − price) − costs` (equivalently `price − p − costs` on the YES line).
2. Gate replacement in `coordinator/pipeline.py`: swap `check_edge(estimated_probability, market_price)`
   for `check_net_edge(net_edge, ...)` → requires `net_edge > config.MIN_NET_EDGE_CENTS` (default
   `0.02`, range 0.02–0.03) **and** price inside `TRADEABLE_PRICE_BAND` (default `0.10–0.90`,
   bypassed only for maker orders). Add both to `config.py`; keep old `check_edge` in
   `risk_engine.py` marked deprecated for one release, then remove.
3. Maker/taker decision rule (per signal, config-driven):
   - `velocity`, `copy_edge_class_a` → **taker** (speed is the edge; queue wait exceeds signal half-life).
   - `recalibration`, `resolution`, `copy_edge_class_b` → **maker**, with fallback-to-taker if
     unfilled after `MAKER_FALLBACK_SECONDS` (default 20s) AND still net-positive as taker.
4. Mandatory decision log line per entry evaluation:
   `gross_edge, fee_units, spread_units, slippage_units, net_edge, order_type, price_band_ok`.

**Work plan.**
- [x] `risk/cost_model.py` + unit tests (fee math at 2¢/50¢/98¢; sign correctness YES/NO; zero-book fail-closed)
- [x] VERIFY Polymarket fee schedule from docs; set config values; record source in Decision Log (→ D-09)
- [x] Config keys: `TAKER_FEE_RATE`, `MAKER_FEE_RATE`, `MAKER_FILL_HAIRCUT`, `MIN_NET_EDGE_CENTS`,
      `TRADEABLE_PRICE_BAND`, `MAKER_FALLBACK_SECONDS` (+ `TAKER_FEE_RATE_BY_CATEGORY`, `SLIPPAGE_SPREAD_MULTIPLE`, `BOOK_DEPTH_TOP_LEVELS`, `MAKER_ORDER_STRATEGIES`)
- [x] Swap gate call site in `pipeline.py`; deprecate `check_edge`
- [x] Maker/taker rule module + per-strategy config map
- [x] Decision-log line wired into both pipeline paths (`[OBSERVABILITY][NET_EDGE]` at the shared gate)
- [x] Tests: gate blocks when net_edge ≤ MIN_NET_EDGE; low-price taker blocked; maker bypass of price band; log format

**Definition of Done.** No entry decision possible without a cost breakdown in logs; tests prove
the 2¢ taker-fee trap is blocked; old `check_edge` no longer called from live paths.
Status: **DONE (2026-10-06)** — 47 cost-model unit tests + 5 pipeline gate integration tests;
fixtures made direction-aware (the honest gate blocks trades whose final direction opposes the
estimate — the old direction-blind gross gate let those through). Suite: 306 passed / 0 failed.

---

### Step 2 — A6: Signal instrumentation + novelty detection

**Problem.** Nobody knows whether a headline still predicts a tradable move after OUR delay
(10s RSS poll + 0.5–2s analyst + ≤22s pipeline). No novelty detection: headline bursts on one
event look like fresh signals.

**Solution.**

1. New table `signal_outcomes`:
   `id, signal_id (FK market_signals), market_id, strategy, headline_hash, entities_json,
   t0 TIMESTAMPTZ, price_t0 DECIMAL, p_m1, p_m5, p_m15, p_m60 DECIMAL, confirmed_direction BOOL,
   created_at`. Index on `(market_id, t0 DESC)`.
2. Forward-price sampler: on every signal entering the pipeline, schedule 4 reads (CLOB midpoint
   or Gamma price) at +1m/+5m/+15m/+60m. Small async task in `main.py` supervisor; each read
   wrapped in its own timeout; misses recorded as NULL (never crash the pipeline).
3. Novelty module `data/novelty.py` (deterministic, zero LLM):
   - `headline_hash` = normalized headline (lowercase, strip punctuation, sort tokens) → sha1.
   - Entity-set Jaccard vs signals seen in trailing `NOVELTY_WINDOW_HOURS` (default 24) per market.
     `JACCARD_REPEAT_THRESHOLD = 0.5` → repeat.
   - `novelty_factor`: first = 1.0, repeat = `NOVELTY_REPEAT_FACTOR` (0.5), third+ within window = drop.
   - Counters: `novelty:novel`, `novelty:repeat`, `novelty:dup`.
4. Weekly analysis script `scripts/signal_drift_report.py`:
   per category × event-type: mean signed drift `sign(signal_direction) × (price_tX − price_t0)`
   for each horizon, sample count, and **velocity viability verdict** = drift vs Step 1 total costs.
   Output: markdown + Telegram. This is the empirical answer to "does the velocity edge survive our latency."

**Work plan.**
- [x] Migration SQL for `signal_outcomes` (pattern: `scratch/migration_*.sql`)
- [x] Sampler task in `main.py` + tests (timeouts, NULL handling)
- [x] `data/novelty.py` + unit tests (hash stability, Jaccard, factor rules)
- [x] Wire novelty into pipeline drop logic + counters
- [x] `scripts/signal_drift_report.py` + a synthetic-data test
- [ ] Run ≥1 week in paper mode before evaluating results (do not judge early) — **starts once the owner applies the migration / restarts the bot**

**Definition of Done.** Every signal has forward prices (or explicit NULLs); drift report runs;
novelty counters live in pipeline stats. **Data clock starts the day this lands — schedule early.**
Status: **DONE (2026-10-07)** — see Session Log for the design notes (hook point, fail-open
novelty reads, Class A copy signals not instrumented, migration pending owner application).

---

### Step 3 — A7: Attribution ledger + edge-fade monitor

**Problem.** No P&L attribution to strategy/edge-type; no statistical edge-fade detection;
no mechanical capital reallocation.

**Solution.**

1. Migration: `closed_trades` + `open_positions` gain columns:
   `strategy TEXT, thesis_id TEXT, p_model_at_entry DECIMAL, market_price_at_entry DECIMAL,
   net_edge_at_entry DECIMAL, costs_paid_usdc DECIMAL, order_type TEXT`.
   `thesis_id = market_id + ':' + strategy + ':' + event_key` (event_key = novelty entity-group;
   fallback = headline_hash). One row per thesis in significance math — anti-double-counting.
2. New pure module `risk/edge_monitor.py`:
   - `rolling_net_edge(trades, window=30)`.
   - One-sided **CUSUM** on per-trade net edge → fade flag. Tune threshold on synthetic series:
     a 30-trade zero-edge run must NOT trigger; a persistent small-negative drift must. Parameters
     in config (`CUSUM_THRESHOLD`, `CUSUM_DRIFT`), tuned once, recorded in Decision Log.
3. Allocation rule (config, mechanical — no judgment):
   - `REALLOC_MIN_SAMPLES = 30` per strategy before weighting.
   - Weight ∝ rolling net Sharpe; strategy in probation (existing rule: 20 trades, <4¢) → size ×0.5;
     two consecutive probations → `retired = true` (flag, never delete).
4. Weekly dashboard script `scripts/strategy_dashboard.py` → Telegram table per strategy:
   `n, net P&L, mean net edge, rolling Sharpe, CUSUM status, Brier-vs-market (feeds from Step 5)`.

**Work plan.**
- [x] Migration SQL (both tables + backfill `strategy` where derivable from existing rows) — `strategy` already existed on BOTH tables (verified live via Supabase MCP 2026-10-07) and both tables held 0 rows → the backfill is a documented no-op; only the 6 new columns + 1 index were needed
- [x] Write columns at entry (pipeline + copytrade executor write paths)
- [x] `risk/edge_monitor.py` + synthetic tests (no-false-trigger, catches-fade)
- [x] Allocation rule in config + enforcement point (sizing read) — enforcement point identified and documented: the sizing read path in `coordinator/pipeline.py` (after `kelly_size`, before `position_size_check`/`market_position_check`, keyed on `strategy_for_check`); Class A is exempt (estimate-free path — its throttle is the wallet state machine)
- [x] Dashboard script + Telegram send
- [x] Ledger clock note: first paper trade after this lands = first attributed row — recorded in the Session Log; **starts when the owner applies the attribution migration / restarts the bot**

**Definition of Done.** Every closed trade self-describes its edge; CUSUM tested; dashboard prints.
Status: **DONE (2026-10-07)** — 66 tests added across migration contract, event-key, entry-cost,
edge-monitor synthetic series, allocation derivation, entry-write integration, close passthrough
and dashboard rendering; Brier-vs-market renders as an explicit placeholder ("—") until Step 5.
Suite: 421 passed / 0 failed.

---

### Step 4 — A4: Kelly on real probability + shrinkage

**Problem.** `kelly_size(win_probability=clamped_conf, ...)` (E-6) — confidence is certainty of
the *call*, not probability of the *event*; with conf ∈ [0.75, 0.88] the 5% cap binds every time → flat 5%.

**Solution.**

1. `coordinator/pipeline.py` sizing: `win_probability = p_side` from `EstimateResult`
   (`p_model` for YES entry, `1 − p_model` for NO entry). `clamped_conf` stays ONLY for the
   confidence gates (min-confidence, repeat-entry) — never as Kelly input. Keep `net_odds`
   computation (line ~760) as-is — it is correct.
2. **Shrinkage** (principled humility, replaces arbitrary flatness):
   `p_used = w × p_model + (1 − w) × 0.5`, `w = n / (n + K)`, `K = SHRINKAGE_PRIOR_STRENGTH`
   (config, default 20). Thin-data estimates shrink toward neutral automatically.
3. Kelly consumes net edge: if `net_edge ≤ 0` → size 0 (Step 1 feeds this). Cost model becomes
   load-bearing, not advisory.
4. Logging per sizing decision: `p_model, n, w, p_used, kelly_raw, kelly_capped, binding_constraint`
   (one of `kelly_fraction | position_cap | market_ceiling | cash_reserve | net_edge_zero`).

**Work plan.**
- [x] Rewire sizing call site(s) in `pipeline.py` (standard + `copy_edge_class_b` branch) — both now feed `p_side = side_probability(shrink_probability(p_model, n), decision_direction)`
- [x] Shrinkage helper (pure; belongs in `risk_engine.py` or `estimator.py` — pure math either way) — `risk_engine.shrink_probability`, placed next to `kelly_size` (the sizing home); shrinks on the YES line (commutes with side-adjustment)
- [x] Config: `SHRINKAGE_PRIOR_STRENGTH` (= 20, own KELLY SIZING section with the rationale)
- [x] Tests: different p_models → materially different sizes; confidence NEVER reaches Kelly (regression test); `kelly → 0` when net_edge ≤ 0; shrinkage monotone in n; side-adjustment correct
- [x] Grep gate: no `win_probability=clamped` in live paths — verified CLEAN (only two `kelly_size` call sites exist, both feed `p_side`)

**Definition of Done.** Kelly output varies with estimate quality; copy Class B sized by real
wallet hit rates immediately; regression tests lock it in. Status: **DONE (2026-10-07)** —
regression proof is exact-value (0.625 < 0.75 min confidence): if confidence ever reached
Kelly again the assertion fails; Class B sized at 15/22 from the real 18W/2L wallet hit rate;
the kelly-≤-0 case (shrunk p at/below market price) drops as `kelly_zero_edge` instead of
submitting a zero-size order. Suite: 435 passed / 0 failed.

---

### Step 5 — A5: Honest gate — book-aware paper fills, market baseline, gate report

**Problem.** Gate counts trades/weeks with idealized fills, no baseline, no power argument, no
thesis-level dedup in significance math.

**Solution.**

1. **Book-aware paper fills.** Paper executor reads the live CLOB book:
   - Taker fill = cross the spread at `ask` (YES) / `bid` (NO) + Step 1 fees; reject if depth < size.
   - Maker fill = rest at level; fills only if the price trades through the level within the wait
     window (monitor loop checks); else maker-fallback rule from Step 1 applies.
   - Log `decision_price` vs `fill_price` per trade → this becomes the **empirical slippage
     dataset** that replaces Step 1's conservative default.
2. **Market baseline (the honest null).** For every market EVALUATED (traded or not): record the
   market's own probability at decision time; at resolution compute Brier for both agent and
   market on the identical sample. Skill = agent Brier < market Brier (with CI) + net P&L CI > 0.
3. **Power math → gate thresholds** (config): distinguishing a true 55% from a coin flip needs
   ~400 resolved samples at 95%; a 60% edge ~100. Gate = `GATE_MIN_RESOLVED_SAMPLES = 200`
   (composite: backtest + paper) AND bootstrap 95% CI of net P&L > 0 (10k resamples, offline in
   the report script) AND agent Brier < market Brier with non-overlapping CI or documented edge case.
4. **Thesis-level accounting**: the report aggregates by `thesis_id` (Step 3) — clusters count once.
5. **Gate report** `scripts/gate_report.py` → weekly markdown + Telegram:
   `n, net P&L + CI, agent vs market Brier, slippage stats, maker/taker ratio, thesis dedup count,
   per-strategy breakdown`. **The gate passes or fails on printed numbers — never on judgment.**

**Work plan.**
- [x] Book-aware paper fill simulator + tests (spread crossing, depth rejection, maker queue model) — `risk/fill_simulator.py` (pure); taker = cross at ask (YES) / 1−bid (NO), reject when depth < size; maker = rest at the touch, fills ONLY when the book trades through the persisted resting level (an unchanged book does NOT jump the queue); `MAKER_FALLBACK_MIN_NET_EDGE_CENTS` fallback rule
- [x] Market-baseline recording (evaluated-markets table or market_signals columns) + resolution join — reuses `signal_outcomes` (D-11): price_t0 has recorded the market's decision-time probability since Step 2; `resolved_outcome`/`resolved_at` backfilled offline by the gate report via idempotent Gamma updates (void/0.5 prices rejected)
- [x] Bootstrap CI in report script (numpy ok offline; runtime stays pure) — pure-Python seeded percentile bootstrap (D-12: no new dependency); P&L CI is a THESIS-CLUSTER bootstrap (mean per-thesis net P&L — clusters count once; total printed alongside)
- [x] `scripts/gate_report.py` + Telegram — GATE: PASS/FAIL strictly on printed conditions (n ≥ GATE_MIN_RESOLVED_SAMPLES, CI low > 0, agent Brier < market Brier with non-overlapping CIs; overlap renders as a documented EDGE_CASE, never a pass)
- [x] Decision-vs-fill logging wired → slippage curve feeds back into `cost_model.expected_slippage_units` — mandatory `[OBSERVABILITY][FILL]` line per trade (decision_price vs fill_price + signed slippage); `SLIPPAGE_EMPIRICAL_MULTIPLE` config key replaces the conservative 1.2 WHEN the owner sets it from the printed data (never auto-fitted)

**Definition of Done.** Report runs weekly on real paper data; fill simulation proven against
book snapshots; baseline recorded from day one. Status: **DONE (2026-10-29)** —
see the Session Log for the design decisions (Class A included — it is the only
strategy producing paper fills while the main pipeline is dormant; maker
resting_price persisted; restart-safe monitor), owner actions, and noticed issues.
Suite: 536 passed / 0 failed.

---

### Step 6 — A3 (LAST): Historical data + calibration activation

**Problem (E-9).** Calibration trains only on own empty `closed_trades` — and would train on fake
estimates (garbage loop). No historical base rates exist. Strategy 2 (Recalibration) is the
planned launch strategy and is entirely blocked without this.

**Agreed split (D-03/D-04):** all *code* below is written in this step; the heavy *data
download + backfill* is the final sub-phase. Nothing before this step depends on it. A trigger
also exists: if copy-edge paper results disappoint before this step starts, A3 may be pulled
forward (operator decision).

#### 6a — Schema (do this slowly — it is the contract; getting a column wrong = re-backfill)

- `market_history`: `market_id UNIQUE, condition_id, slug, question, category, outcomes_json,
  neg_risk BOOL, opened_at, closed_at, resolution_outcome SMALLINT (1 YES / 0 NO), 
  resolution_type TEXT (resolved|void|ambiguous|deactivated), resolved_at, source TEXT (gamma|hf_backfill|incremental)`.
- `market_price_history`: `market_id, ts, price_yes, best_bid, best_ask, liquidity_usd, volume_usd,
  granularity TEXT (daily|hourly_48h)`. Index `(market_id, ts)`.
- `calibration_curves`: `category, price_bin, ttr_bucket, n, base_rate, coef_json, valid_from` —
  runtime only ever READS this (keeps runtime pure & <1ms; fitting happens offline).
- Include void/ambiguous resolutions (survivorship-bias defense). YES/NO canonical: outcomes
  stored YES=1/0 at market level; NO-side trades handled as `1 − p_yes` everywhere; `side` explicit.

#### 6b — Incremental ingest job (forward-looking; runs for real once built)

- Nightly: Gamma API discovery → upsert `market_history`; CLOB `/prices-history`
  (params: `market=<clobTokenId>`, `interval`, `fidelity`; see docs.polymarket.com) →
  `market_price_history` (daily candles + final-48h hourly for recent markets).
- Idempotent upserts on `market_id` (+ `ts`); job crash-safe; runs under main.py supervisor.

#### 6c — HF backfill (the "data part" — deliberately last)

- Download (local disk, NOT Supabase):
  `hf download SII-WANGZJ/Polymarket_data markets.parquet trades.parquet --repo-type dataset`
  (0.5GB + 45.8GB; MIT license; coverage 2022-11-21 → 2026-10-04; 4.05M markets).
  Optional later: `quant.parquet` (44.7GB, pre-normalized YES view). Skip `orderfilled.parquet`
  (127GB raw) and `users.parquet` for now.
- `scripts/backfill_from_hf.py`: **DuckDB** SQL directly over parquet (no DB import; runs
  larger-than-RAM on the laptop) → aggregate to per-market daily candles + final-48h hourly →
  upsert into `market_price_history`; markets → `market_history` with `source='hf_backfill'`.
- **Purge/tag rule:** any `closed_trades` rows with fake-estimate-era `p_model_at_entry` are
  excluded from calibration training (flag column `estimate_source`).
- Velocity/news backtests (separate track, can lag): PolyBench (arXiv 2604.14199) provides
  point-in-time book+news cross-sections for 38.6k binary markets — use as the velocity
  backtest layer instead of building one. Cross-check sample: Jon-Becker/prediction-market-analysis.

#### 6d — CalibrationModel upgrade

- `CalibrationModel.refresh()` reads `calibration_curves` (+ market tables via derived curves);
  Laplace smoothing for sparse bins; side-aware; optionally offline-fitted logistic (Platt)
  coefficients in `coef_json` (fitted by an offline script — runtime applies a dot product only).

#### 6e — Validation before trust

- Join a random 100-market sample against live Gamma API — resolutions + timestamps must match.
- `scripts/validate_calib_data.py` → prints `READY` or `BLOCKED: <reason>` (row counts per
  category/bin, staleness, coverage). **Do not activate estimators without READY.**

#### 6f — Activation (the pipeline wakes up)

- `recalibration` estimator: category × price-bin × ttr-bucket base rate from curves →
  `EstimateResult(method='recalibration_base_rate', n=…)`. Pipeline un-dormants per strategy as
  each estimator gets data.
- `velocity` estimator: `signal_outcomes` drift conditioning + novelty factor (needs Step 2 data).
- `resolution` estimator: historical analogs matched on parsed `resolution_keywords`/`key_entities`.
- **First output that steers capital:** per-category market-Brier table (market-as-predictor) —
  categories where the market is already calibrated get zero recalibration capital (feeds Step 3 allocation).

**Work plan.**
- [ ] 6a Migrations + review pass on schema contract
- [ ] 6b Incremental ingest live + idempotency tests
- [ ] 6c Download executed; DuckDB script run; backfill complete; `estimate_source` tagging
- [ ] 6d Model upgrade + fixture tests
- [ ] 6e 100-market validation PASS; `validate_calib_data` → READY
- [ ] 6f Estimators activated in paper mode; market-Brier table produced; recalibration live

**Definition of Done.** ≥1,000 markets ingested (target 10k+); validation READY; per-category
market-Brier table exists; recalibration estimator live in paper mode; per-strategy estimates
logged with method + n. Status: **NOT STARTED** (deferred by operator decision — see D-03).

---

## 6. NEW CONFIG KEYS SUMMARY (all via `config.py`)

| Key | Default | Step |
|---|---|---|
| `DEFAULT_TTR_HOURS` | 720 | 0 |
| `SHRINKAGE_PRIOR_STRENGTH` | 20 | 4 |
| `TAKER_FEE_RATE` / `MAKER_FEE_RATE` | VERIFY from official docs at implementation | 1 |
| `MAKER_FILL_HAIRCUT` | TBD at implementation | 1 |
| `MIN_NET_EDGE_CENTS` | 0.02 | 1 |
| `TRADEABLE_PRICE_BAND` | (0.10, 0.90) | 1 |
| `MAKER_FALLBACK_SECONDS` | 20 | 1 |
| `NOVELTY_WINDOW_HOURS` | 24 | 2 |
| `JACCARD_REPEAT_THRESHOLD` | 0.5 | 2 |
| `NOVELTY_REPEAT_FACTOR` | 0.5 | 2 |
| `NOVELTY_MAX_HISTORY_ROWS` | 200 | 2 |
| `FORWARD_SAMPLER_HORIZONS_SECONDS` | (60, 300, 900, 3600) | 2 |
| `FORWARD_SAMPLER_READ_TIMEOUT_SECONDS` | 8 | 2 |
| `FORWARD_SAMPLER_QUEUE_MAXSIZE` | 500 | 2 |
| `DRIFT_VIABILITY_ASSUMED_SPREAD_CENTS` | 0.03 (report-only) | 2 |
| `CUSUM_THRESHOLD` / `CUSUM_DRIFT` | tuned once in Step 3 | 3 |
| `REALLOC_MIN_SAMPLES` | 30 | 3 |
| `GATE_MIN_RESOLVED_SAMPLES` | 200 | 5 |
| `SLIPPAGE_EMPIRICAL_MULTIPLE` | None (owner sets from printed data) | 5 |
| `PAPER_MAKER_CHECK_SECONDS` | 5.0 | 5 |
| `GATE_BOOTSTRAP_RESAMPLES` / `GATE_BOOTSTRAP_SEED` | 10000 / fixed | 5 |
| `MAKER_FALLBACK_MIN_NET_EDGE_CENTS` | 0.0 (strictly above) | 5 |

## 7. NEW TABLES SUMMARY

| Table | Step | Purpose |
|---|---|---|
| `signal_outcomes` | 2 | Forward prices after each signal → post-delay alpha measurement; **Step 5 adds `resolved_outcome`/`resolved_at` → the market baseline (Brier of the honest null)** |
| `market_history` | 6a | Resolved-market metadata + outcomes (backfill + incremental) |
| `market_price_history` | 6a | Price paths (daily + final-48h hourly) |
| `calibration_curves` | 6a | Pre-fitted base rates; runtime reads only |
| columns on `closed_trades` / `open_positions` | 3 | Attribution: strategy, thesis_id, p_model_at_entry, net_edge_at_entry, costs, order_type |
| columns on `open_positions` / `closed_trades` | 5 | Book-aware paper fills: fill_price, fill_type ('taker'/'maker_fill'/'taker_fallback') + resting_price (open_positions only — the monitor's persisted fill-detection key) |

## 8. COORDINATION WITH PART B (bugs — tracked separately by operator)

- **Step 0 should land before ANY live trading, regardless of Part B state** (it is the safety kill).
- Overlaps: B5 (exit engine) consumes Step 1's cost model; B2 (drawdown anchors) is independent
  of List A; B4 (DB-level guards) must not conflict with Step 3's migrations — sequence migrations.
- List A steps are safe to build in the fail-closed dormant state — none of Steps 0–5 requires
  live trading, and Step 0 *enforces* the dormant state.

## 9. DECISION LOG (append-only)

| ID | Date | Decision | Rationale |
|---|---|---|---|
| D-01 | 2026-10-06 | A3's heavy data work deferred to LAST in Part A | Operator: time-consuming; bugs first; bot must not trade on fake edge meanwhile |
| D-02 | 2026-10-06 | Interim state = fail-closed dormant main pipeline; copy-edge only | No estimate → no trade; structurally safe during bug-fixing |
| D-03 | 2026-10-06 | Historical source = SII-WANGZJ/Polymarket_data (HF, MIT) + own incremental ingest; NOT API scraping | 279GB/4.05M markets/2022→2026-10-04, verified+deduped; days not weeks; PolyBench (arXiv 2604.14199) for velocity/news backtests |
| D-04 | 2026-10-06 | Raw parquet stays local (DuckDB); Supabase gets aggregates only | Free-tier limits; runtime purity; aggregates are the derived truth |
| D-05 | 2026-10-06 | Velocity estimator stays stubbed until Step 2 drift data exists | Fail-closed; no velocity trades on assumption |
| D-06 | 2026-10-06 | Launch-strategy implication accepted: go-live menu is copy-trade-first until Step 6 | Recalibration (planned Strategy 2) requires A3 |
| D-07 | 2026-10-06 | Fee schedule values marked VERIFY — parameterized, never hardcoded | Schedule may change; operator's 3.9%-at-2¢ figure to be confirmed from official docs |
| D-08 | 2026-10-06 | `get_estimate()` gained optional keyword param `wallet_address` (not in the frozen spec signature) | copy_edge_class_b estimator must attribute the estimate to the tracked wallet behind the signal; the pipeline already holds `wallet_address` (run_pipeline param, set by executor.py); estimator fail-closes when absent; backward-compatible, unused by stub estimators |
| D-09 | 2026-10-06 | Fee schedule VERIFIED from official docs and encoded in config: `fee = shares × feeRate × p × (1−p)`; makers NEVER charged (15–25% rebates); taker feeRate per category (politics/finance/tech/mentions 0.04, crypto 0.07, sports/economics/culture/weather 0.05, default "Other" 0.05, geopolitics 0) | Source: https://docs.polymarket.com/polymarket-learn/trading/fees (fetched 2026-10-06; formula also matches Polymarket ctf-exchange docs). Sanity check: politics at 2¢ → 0.04×0.02×0.98 ≈ 3.9% of trade value — matches the operator's reported figure, closing D-07's VERIFY item |
| D-10 | 2026-10-07 | CUSUM fade monitor TUNED ONCE: `CUSUM_DRIFT = 0.005`, `CUSUM_THRESHOLD = 0.25` (zero-reference one-sided downward CUSUM on per-trade net edge; `faded` = S at the end of the fed window > threshold, `score` = max_t S_t) | Seeded Monte Carlo (500 series/point, σ = 3¢ per-trade noise, scratch/tune_cusum.py): 30-trade zero-edge run triggers ~2–3% (an exact-zero run NEVER triggers — increments are −drift), healthy +5¢ edge never triggers (0/500 over 100 trades), persistent −1¢/trade fade detected ~98% within 120 trades (mean latency ≈ 39 trades; flat −1¢ series crosses at trade 50). Monitor is a persistence detector — probation (20-trade window) is the fast throttle. Do not re-tune without re-recording this row |
| D-11 | 2026-10-29 | Market baseline reuses `signal_outcomes` — NO new evaluated-markets table; resolution join happens OFFLINE in `scripts/gate_report.py` (Gamma fetch for unresolved markets → idempotent `resolved_outcome`/`resolved_at` backfill, capped per run, void/0.5 prices rejected) | The spec offered "evaluated-markets table or market_signals columns" as alternatives; `price_t0` has recorded the market's decision-time probability for every instrumented signal since Step 2 — a new table would duplicate it. Runtime stays pure (no resolution I/O in the hot path); price_t0 is already recorded from day one |
| D-12 | 2026-10-29 | Bootstrap CIs are pure-Python percentile bootstraps with a fixed seed (`GATE_BOOTSTRAP_SEED`), NOT numpy; the P&L CI is a THESIS-CLUSTER bootstrap of the mean per-thesis net P&L (total printed alongside — a CI on a fixed total is degenerate) | numpy isn't in requirements and Railway would install a heavy image for one offline script; 10k resamples over ~200 samples is <1s in pure Python; fixed seed = reproducible reports. Cluster resampling enforces the Step 5d anti-double-counting rule statistically |
| D-13 | 2026-10-29 | Book-aware paper fills COVER THE CLASS A COPY PATH (operator-confirmed), and maker orders persist `resting_price DECIMAL(10,4)` on open_positions; cancelled maker orders delete the open_positions row and mark the idempotency entry 'cancelled' (a new allowed status — 'pending' never blocked future UUIDs) | The main pipeline is dormant (D-02) — Class A is the ONLY source of paper fills, so excluding it would leave the gate dataset empty. The monitor is DB-state-driven (order_type='maker' AND fill_price IS NULL) and therefore restart-safe with no in-memory registry; the persisted resting level is not recomputable from later books |

## 10. RISK REGISTER

| Risk | Mitigation |
|---|---|
| Scaffolding becomes zombie code before data arrives | Micro-validation built into Step 6e; every module ships with tests; `validate_calib_data` health check |
| Schema contract wrong → re-backfill | 6a gets a dedicated review pass before any backfill |
| Fee schedule changes silently | Config-parameterized (D-07); canary idea belongs to Part B drift checks |
| Steps 2/3 delayed "until bugs are done" and data clock never starts | Explicitly flagged TIME-GATED in tracker; schedule early in small pieces |
| Interim dormant state confuses paper-trading metrics | Expected behavior; document in paper reports ("main pipeline dormant by design") |
| Training calibration on fake-estimate-era rows | `estimate_source` tag + purge rule (Step 6c) |

## 11. SESSION LOG (append-only — newest on top)

| Date | Step | What was done | Next action |
|---|---|---|---|
| 2026-10-29 | 5 | **Step 5 (A5) DONE.** **Baseline: 435 passed/0 failed recorded first; suite finished 536 passed/0 failed (+101).** **(a) Pure fill simulator** `risk/fill_simulator.py`: `taker_fill_price` (YES=ask, NO=1−bid — mirrors cost_model's exec convention), `taker_depth_ok`, `maker_resting_price` (YES=bid, NO=1−ask), `maker_is_filled` (trades-THROUGH model: YES ask ≤ level, NO → YES bid ≥ 1−level; an UNCHANGED book does NOT fill a touch-resting order — you wait behind the queue; out-of-range level raises, never silently "no fill"), `maker_fallback_net_edge` (taker edge recomputed at the CURRENT book — the old breakdown is stale), `signed_slippage_units` (fill vs decision reference on the TRADED token's line; positive=adverse). Self-review catch BEFORE commit: my first NO-side slippage sign was inverted and 3 maker tests asserted queue-jumping fills on unchanged books — the tests were wrong, not the code; fixed both sides. **(b) Pipeline Step D**: takers fill at the book ask with a depth check placed BEFORE the idempotency insert (fail-closed counters `fill:insufficient_depth`/`fill:unpriceable`; uses the gate's already-fetched book — NO new Supabase call, so the sequence-sensitive 6_9 indices 1/6/9 are UNCHANGED and their tests pass unedited); makers rest at the touch with fill_price/fill_type NULL and persisted `resting_price`; mandatory `[OBSERVABILITY][FILL]` line per trade = the empirical slippage dataset; `log_to_open_positions` gained fill_price/fill_type/resting_price kwargs; run_pipeline return dict extended. Note: the pipeline-level taker depth check is DEFENSE-IN-DEPTH — the net-edge gate's worst-case-size slippage term already subsumes it for same-cap strategies (final size ≤ 5% worst case ≤ depth required to pass), and the one strategy with a higher cap ("resolution", 8% vs the 5% worst case) is a maker; it becomes load-bearing if that cap/worst-case relationship ever diverges (test locks the wiring + ordering, docstring states this honestly). **(c) Class A (operator-confirmed in scope, D-13)**: paper fill = live-book ask when ask ≤ limit (tracker+buffer), honest no-fill drops otherwise (`book_unavailable`/`insufficient_depth`/`limit_missed`) placed BEFORE the idempotency write; `entry_price` is now the price ACTUALLY paid (honest close-path P&L: qty=size/entry_price), decision mid rides in `market_price_at_entry`; Class B unaffected (flows through the pipeline). **(d) Restart-safe maker monitor** `execution/paper_fills.py` (supervisor task in main.py): state lives in open_positions (order_type='maker' AND fill_price IS NULL) — a restart re-scans the same rows, nothing in memory; inside the window fills on a through-trade (`maker_fill`), past MAKER_FALLBACK_SECONDS falls back to taker only if current-book net edge > `MAKER_FALLBACK_MIN_NET_EDGE_CENTS` AND depth carries the size (`taker_fallback`), else cancels (position row deleted — the trade never existed — and idempotency marked 'cancelled', a NEW allowed status that does not block future UUIDs); fail-closed cancel when p_model is unusable (never take blind); per-row exception isolation; fail-soft scan (2s timeout, retries next tick); corrupt rows (bad direction/level) are loudly logged and removed, never looped on forever. **(e) Migration** `scratch/migration_step5_fill_and_baseline.sql` + canonical DDL: fill_price/fill_type/resting_price on open_positions (resting_price open-only — closed trades never rest), fill_price/fill_type on closed_trades (close-path passthrough in reconciliation), resolved_outcome SMALLINT/resolved_at on signal_outcomes, partial index `idx_open_positions_unfilled_maker`; contract tests lock it. **OWNER ACTION: apply the migration / restart the bot — the FILL + BASELINE clocks start then.** **(f) Market baseline (D-11)**: reuses signal_outcomes.price_t0 (recorded since Step 2 for every instrumented signal, traded or dropped); resolutions backfilled OFFLINE by the gate report via idempotent Gamma updates (same resolved-predicate as reconciliation; outcomePrices/outcomes-as-strings handled; void/0.5 prices rejected — never ingested as YES/NO); runtime stays pure. **(g) Gate report** `scripts/gate_report.py` (dashboard pattern: .env.test before imports, pure core/I/O split, markdown → scratch/, Telegram `[ZERO-ALPHA] INFO | GATE_REPORT`, --days/--limit/--out/--no-telegram/--max-resolution-fetches, graceful DB failure → exit 1): net P&L total + THESIS-CLUSTER bootstrap 95% CI of mean per-thesis P&L (D-12: pure-Python, fixed seed, no numpy; clusters count once, NULL thesis = singleton); agent vs market Brier on the IDENTICAL resolved traded sample (p_model_at_entry vs market_price_at_entry, YES-line outcome mapped from direction+outcome; Class A NULL-estimate rows excluded WHOLE — never a fabricated probability); market-only Brier over ALL evaluated signals; separate bootstrap CIs with non-overlap check; slippage stats (mean/median/worst, DB decimals coerced through float() first — the clean_edges lesson); taker/maker_fill/taker_fallback ratio; thesis dedup counts; per-strategy breakdown; **GATE: PASS/FAIL strictly on printed conditions** (n ≥ 200, CI low > 0, agent Brier < market with non-overlapping CIs); agent<market with overlapping CIs renders as a documented EDGE_CASE — never silently a pass. Smoke-run from this sandbox hit a DNS failure → the graceful exit-1 path fired exactly as designed; the weekly run with network + real data is owner-side (same as the other offline scripts). **(h) Slippage feedback point**: `SLIPPAGE_EMPIRICAL_MULTIPLE` (None default) — when the owner sets it from the printed measured data, `expected_slippage_units` uses it instead of the conservative 1.2; never auto-fitted at runtime. **(i) Dashboard**: Brier-vs-market column now REAL — reuses the gate report's brier_pairs/mean_brier (the dashboard can never disagree with the gate); "—" only when the strategy has no Brier sample (Class A estimate-free rows honestly leave); select widened. **Tests (+101)**: 33 fill-simulator + 2 cost-model feedback + 8 pipeline (fill persistence, maker NULL+resting, depth-reject pre-idempotency ordering, [FILL] log fields) + 3 Class A (limit-miss/book-dead drops, fill recorded) + 7 schema contract + 12 monitor + 37 gate report + dashboard Brier computed/no-sample renders. Noticed issues (NOT fixed, per discipline): (a) pipeline rows still write entry_price = ARRIVAL market_price while Class A now writes the actual fill — inconsistent P&L reference between paths, pre-existing semantics for the pipeline (the fill is recorded separately); (b) signal_outcomes.price_t0 records the 0.50 DEFAULT price on no-price-fetched signals (pipeline's market_price default) — those rows pollute the evaluated-market baseline slightly and the report cannot distinguish them; a t0-source tag would be the fix (candidate for Step 6); (c) cancelled maker orders leave NO closed_trades trace (row deleted by design) so the maker/taker ratio counts only resolved fills — cancel counts live in logs only; (d) agent Brier needs p_model_at_entry — until Step 6 un-dormants the pipeline, the traded-sample Brier has effectively no rows (Class A is estimate-free by design); the gate will honestly print FAIL/empty until then; (e) the monitor's fallback re-reads the book per resting order per tick — fine at paper scale, revisit if resting volume grows; (f) `idempotency_log` gains a 'cancelled' status — check_pre_order_idempotency only blocks 'confirmed', verified. | Start **Step 6** (A3 LAST: historical data + calibration activation — schema 6a first, "do this slowly"; owner actions outstanding: apply the Step 5 migration / restart the bot, and schedule the weekly gate report). |
| 2026-10-07 | 4 | **Step 4 (A4) DONE.** **Shrinkage**: `risk_engine.shrink_probability(p_model, sample_size)` (pure, next to kelly_size, risk/GEMINI.md-clean): `p_used = w·p + (1−w)·0.5`, `w = n/(n+K)`, `K = config.SHRINKAGE_PRIOR_STRENGTH = 20` (new KELLY SIZING config section with rationale; n=20→w=0.5, n=100→w≈0.83). Guards: n ≤ 0 → exactly 0.5, p clamped into [0,1]. Shrink toward 0.5 COMMUTES with side-adjustment, so the pipeline shrinks on the YES line then flips — `p_side = side_probability(shrink_probability(estimate.p_point, estimate.sample_size), decision_direction)`. **Rewiring**: both `kelly_size` call sites (standard :1164 + copy_edge_class_b :1183) now feed `win_probability=p_side` — grep gate CLEAN (`win_probability=clamped` gone from coordinator/llm/risk/copytrade; exactly 2 production call sites, both verified). `clamped_conf` KEPT only where it belongs: min-confidence gate, `market_position_check` repeat-entry gate, `low_confidence_for_add` reason, `confidence_at_entry` audit column, logs. `net_odds` untouched per frozen spec. **Kelly consumes net edge**: `kelly_raw ≤ 0` ⇔ (algebraic identity) shrunk p_side ≤ arrival market_price — no edge at these odds — drops as `sizing:kelly_zero` / reason `kelly_zero_edge` with `binding_constraint=net_edge_zero` in the drop line, BEFORE the allocation multipliers (the Step 3 `allocation_zero_weight` check still covers weight-0). Reachable only for entries priced > 0.5, where shrinkage can pull a thin-data estimate below the price. **Mandatory sizing log**: `[OBSERVABILITY][SIZING]` — `p_model, n, w, p_used, p_side, kelly_raw, kelly_capped, binding_constraint` — emitted once per successful entry inside the market lock; binding_constraint tracked mechanically: `kelly_fraction` (unclamped) → `position_cap` if `position_size_check` shrank → `market_ceiling` if `market_position_check` shrank; `cash_reserve` logged on that BLOCK drop path; `net_edge_zero` on the kelly drop. **DoD tests** (all exact-value, no tolerance hand-waving): Kelly input = 0.625 for p=0.65/n=100 — BELOW the minimum reachable clamped confidence (0.75), so confidence reaching Kelly again structurally fails the assertion; NO-side input = 19/30 (p 0.30 @ n=40, coordinator flipped to NO); Class B input = 15/22 from the real 18W/2L wallet hit rate (19/22 shrunk at n=K=20) — DoD "copy Class B sized by real wallet hit rates immediately"; size varies materially with p_model (caps widened so only Kelly binds); shrinkage monotone in n + exact weight math + fixed point at p=0.5; kelly_zero drop proven end-to-end. **Cap-bound analysis** (why the suite stayed green unedited): fixture estimate 0.65/n=100 → raw $625 (recalib 0.25) vs $500 5% cap → final $500 = the old confidence-era final — cap-bound tests (dedupe 500/300) unchanged; velocity raw $375 — no test depends on velocity sizes. **Suite: 421 → 435 passed / 0 failed** (14 new: 8 shrink_probability unit + 5 kelly-input/log/pipeline + 1 Class B input). Noticed issues (NOT fixed, per discipline): (a) `net_odds` is computed from the ARRIVAL `market_price` while the net-edge gate uses `book.mid` — the two references can diverge in fast markets; the kelly-zero test had to align them (`market_price=0.80` vs book mid 0.80); spec froze "keep net_odds as-is", so recorded, not changed; (b) entries blocked at category/correlation exposure emit `[NET_EDGE]` + drop counters but not the full `[SIZING]` line (the sizing math ran but the trade died before the lock — the SIZING line rides the success path by design); (c) Class B with a zero trust ceiling → raw_size 0 → drops under the `allocation_zero_weight` label (Step 3 counter; cause is the ceiling, not the ledger weight — distinct-drop labels are a candidate cleanup); (d) the estimator is fetched with the ANALYST's side but side-adjusted with `decision_direction` — correct by construction (p_point is YES-line regardless of fetch side; the coordinator can flip direction), now explicit in the Kelly comment. | Start **Step 5** (A5: book-aware paper fills + market baseline + gate report — the honest gate judges on printed numbers; needs `GATE_MIN_RESOLVED_SAMPLES = 200` config, decision-vs-fill logging → empirical slippage curve, market-baseline recording). Owner action still outstanding: apply the Step 3 attribution migration / restart the bot to start the ledger clock. |
| 2026-10-07 | 3 | **Step 3 (A7) DONE.** **Migration**: `scratch/migration_attribution_columns.sql` (SAFE TO RUN, banner/section format) — 6 columns (`thesis_id, p_model_at_entry, market_price_at_entry, net_edge_at_entry, costs_paid_usdc, order_type`) on BOTH `open_positions` + `closed_trades` + index `idx_closed_trades_strategy_closed_at (strategy, closed_at DESC)`; registered by appending idempotent ALTERs to both entries in `memory/migrations.py` (canonical CREATEs extended too, per the copytrade_log precedent) + a committed contract test locks the schema. LIVE SCHEMA VERIFIED via Supabase MCP before writing SQL: `strategy` already exists on both tables and both held 0 rows → the spec's "backfill strategy where derivable" is a documented no-op. **OWNER ACTION: apply the attribution migration / restart the bot — the LEDGER CLOCK starts with the first attributed paper trade.** **Entry writes**: `data/novelty.compute_event_key` (sorted normalized entity set joined `\|` = the identity novelty's Jaccard groups on; fallback headline_hash when no entities; Class A copy path third-fallback market_id — approximation documented: identical entity sets share a thesis, Jaccard-similar-but-different sets stay separate → significance math can over-count theses, never merge them). `_instrument_signal` widened to a 4-tuple (+ headline_hash, computed BEFORE any fallible I/O; exception path recomputes deterministically). `log_to_open_positions` gained the 6 optional kwargs; the call site writes thesis_id, p_model_at_entry=estimate.p_point, market_price_at_entry=book.mid (the reference the net-edge math used — distinct from entry_price=arrival price), net_edge_at_entry=breakdown.net_edge, costs_paid_usdc=new pure `cost_model.entry_cost_usdc` (shares=size/entry_price × Σ cost units — MODELED cost; Step 5's decision-vs-fill data replaces it), order_type. Class A executor writes honest NULLs for p_model/net_edge/costs (estimate-free by design — never fabricated) + decided order_type. Reconciliation close path passes all 6 through verbatim → every closed trade self-describes its edge. **Edge-fade monitor** `risk/edge_monitor.py` (pure, no I/O): `clean_edges` (NULL/NaN/unconvertible skipped — coerced through float() FIRST, a self-review fix so a "NaN" string can't poison means), `rolling_net_edge`, `rolling_net_sharpe` (per-trade, sample std; <2 points or exactly-constant series → 0.0 — the constant-series guard was a REAL bug caught by its own test: repeated identical floats give variance ≈1e-18 → astronomic Sharpe), `cusum_fade` (one-sided downward, ZERO reference: S_t = max(0, S_{t−1} + (−e_t − drift)); **faded = S at the END of the fed window > threshold** = fade ongoing as of the latest trade, score = max_t S_t = worst excursion; callers feed a trailing window so recovered fades age out). **D-10 tuned ONCE** (`scratch/tune_cusum.py`, seeded Monte Carlo, 500 series/point, σ=3¢): CUSUM_DRIFT=0.005, CUSUM_THRESHOLD=0.25 — exact-zero 30-trade run NEVER triggers (increments −drift); noisy zero-edge ~2–3%; healthy +5¢ edge 0/500 over 100 trades; persistent −1¢/trade fade ~98% detected within 120 trades (mean latency ≈39; flat −1¢ crosses at trade 50). The monitor is a persistence detector — probation is the fast throttle. **Allocation rule (mechanical)**: `derive_allocations` pure over the ledger — n < REALLOC_MIN_SAMPLES=30 → weight 1.0; eligible: weight = sharpe / mean(sharpe of positive-Sharpe eligibles) clamped to REALLOC_WEIGHT_MAX=2.0, Sharpe ≤ 0 → 0.0, and when NO eligible strategy is positive-Sharpe ALL fall back to 1.0 (probation/retirement then throttle); probation = last STRATEGY_PROBATION_TRADE_COUNT=20 trades mean net_edge < STRATEGY_PROBATION_EDGE_THRESHOLD_CENTS=4¢ (**these two config keys existed dead since the original config — Step 3 makes them live**); two consecutive COMPLETE failing windows → retired (flag, never delete — trades stay). ENFORCEMENT POINT (identified per assignment): the sizing read path in `coordinator/pipeline.py` — retired drops pre-sizing (counter `allocation:retired`); weight × PROBATION_SIZE_MULTIPLIER=0.5 applied AFTER kelly and BEFORE `position_size_check`/`market_position_check` (hard caps still bind — a cap-bound trade correctly shows no visible change); zero weight → drop `allocation:zero_weight` (never a $0 order). Ledger read = ONE query, 2s timeout, ALLOCATION_HISTORY_LIMIT=1000, **fail-open NEUTRAL + WARNING** (allocation is an advisory throttle, not a capital gate — the hard gates stay fail-closed). Class A exempt (no edge measurement on that path; the wallet state machine governs it). CUSUM fade flag is observability-only in Step 3 (dashboard + `[OBSERVABILITY][ALLOCATION]` log line). **Dashboard**: `scripts/strategy_dashboard.py` mirrors signal_drift_report exactly (.env.test before imports, pure core / I/O split, markdown → scratch/, Telegram `[ZERO-ALPHA] INFO | STRATEGY_DASHBOARD`, `--days/--limit/--out/--no-telegram`, graceful DB-failure) and REUSES `derive_allocations` — the dashboard can never disagree with enforcement; per-strategy n, net P&L, mean net edge, rolling Sharpe, CUSUM status, weight/probation/retired, thesis_count dedup visibility; **Brier-vs-market renders as an explicit "—" placeholder with a Step 5 note — never an invented number**. **Suite: 355 → 421 passed / 0 failed** (66 new: 32 edge_monitor + 14 dashboard + 7 migration contract + 3 event_key + 3 entry_cost + 1 entry-write integration + 1 Class A write + 1 close passthrough + 4 allocation enforcement). Test adaptations: test_integration 6_9 timeout indices +1 AGAIN (the allocation ledger read is now Supabase Call 6 — after the net-edge gate, before the exposure fetch; idempotency is Call 9) — intent preserved, comment documents the full sequence. Noticed issues (NOT fixed, per discipline): (a) Class A `agent_estimate` stays the legacy entry-price proxy — the new p_model_at_entry=NULL distinguishes it honestly, but a cleanup of legacy agent_estimate semantics is a candidate for Step 5; (b) costs_paid_usdc conversion divides by entry_price (arrival), while taker fee units were priced at exec_price — sub-half-spread approximation, documented in the helper; (c) thesis event-key does not merge Jaccard-SIMILAR (≥0.5) but not identical entity sets — Step 5 thesis-dedup may want a canonical group id (signal_outcomes would need the key persisted); (d) the sizing path now performs one extra Supabase read per entry (the ledger read) — acceptable at current entry rates, revisit if hot-path budget tightens; (e) dashboard "weekly" = manual/cron by owner (offline script, same as drift report). | Start **Step 4** (A4: Kelly on real probability + shrinkage — rewire sizing call sites to p_side, shrinkage helper, regression tests locking confidence OUT of Kelly; copy Class B gets real Kelly immediately). Before that: owner applies the attribution migration / restarts the bot to start the ledger clock. |
| 2026-10-07 | 2 | **Step 2 (A6) DONE.** `signal_outcomes` table: `scratch/migration_signal_outcomes.sql` (repo migration pattern, SAFE TO RUN) + registered in `memory/migrations.py` so the bot's own startup pass creates it on next restart — **owner must apply to the live DB** (defaulted to not touching live infra). `data/novelty.py` (pure: `compute_headline_hash` lowercase/strip-punct/sort-tokens→sha1; `jaccard` (empty∩empty=0); `novelty_verdict` 0/1/2+ similar priors → novel 1.0 / repeat `NOVELTY_REPEAT_FACTOR` / dup). `data/forward_sampler.py` (`SamplerJob` + `DirectionUpdate` queue messages; supervisor started in main.py inserts the row (client-generated UUID) and spawns one tracked sampling task per job; CLOB midpoint reads at +1/5/15/60m via `get_market_price`, each under `FORWARD_SAMPLER_READ_TIMEOUT_SECONDS`; misses stay NULL; `apply_signal_direction` records `confirmed_direction` (YES=True/NO=False/ABSTAIN=NULL) and backfills `signal_id` from market_signals via the log_final_signal_status lookup pattern). Pipeline hook placed EARLY — after market/price resolution, BEFORE the News Analyst and estimator gate — so dormant-dropped signals are measured (design choice per assignment note, recorded here as specified). Novelty: dup → drop pre-LLM (`novelty:dup`, zero token burn); repeat/novel counted; read failure → fail-OPEN + `novelty:check_failed` (novelty is a burst filter, not a capital gate — the honest gates downstream still protect). Sampler stats joined the 5-min stats reporter. `scripts/signal_drift_report.py`: per category × event-type mean signed drift + n + pass rate at each horizon, verdict = mean drift as gross edge for a velocity TAKER through the REAL Step 1 gate (cost_model + check_net_edge; fees exact, synthetic spread `DRIFT_VIABILITY_ASSUMED_SPREAD_CENTS`); markdown + Telegram; reads .env.test per the scripts rule; graceful DB-failure handling. **Suite: 306 → 355 passed / 0 failed** (18 novelty + 10 sampler + 6 wiring + 15 report). Test adaptations: test_integration 6_9 timeout indices +1 (novelty window read is now Supabase Call 1) — intent preserved. KEY FINDINGS (recorded): (1) Python 3.11 `asyncio.wait_for` swallows an external cancellation arriving after its inner future completed — a cancelled task survives and loops; new code uses `async with asyncio.timeout(...)` (repo precedent in main.py); (2) module-level `asyncio.Queue` binds to its first loop — `get_sampler_queue()` rebinds per running loop (single loop in prod; fresh queues in tests). Noticed issues (NOT fixed, per discipline): (a) **live PolyAgent Supabase has RLS disabled on all 11 tables** (Supabase advisor flags CRITICAL; enabling without policies locks the bot out — owner's call); (b) older scratch/*.py scripts `load_dotenv('.env')` contrary to the CLAUDE.md scripts rule (pre-existing); (c) Class A copy trades bypass coordinator/pipeline by design → NOT instrumented (candidate: hook `_execute_class_a` later); (d) same-market copy-signal bursts hit novelty before the tranche gate (consistent with MAX_MARKET_TRANCHES, noted); (e) market_signals.event_type == category today (classify_signal writes the same value) so report grouping degenerates gracefully; (f) pending sampler reads are in-memory — a restart loses them (recorded as NULLs; backfill out of scope); (g) `asyncio.wait_for` footgun exists in OTHER pre-existing modules' timeout wrappers (untouched here). | Start **Step 3** (A7: attribution columns migration + `risk/edge_monitor.py` CUSUM + allocation rule + `strategy_dashboard.py` — ⏱ TIME-GATED: ledger clock starts with the first paper trade after it lands). Before that: owner applies the signal_outcomes migration / restarts the bot to start the data clock. |
| 2026-10-06 | 1 | **Step 1 (A2) DONE.** Created `risk/cost_model.py` (pure: `BookSnapshot`/`FeeConfig`/`CostBreakdown`, `taker_fee_units` = rate×p×(1−p) per D-09, `half_spread_units`, `expected_slippage_units` with zero-depth/oversize fail-closed 1.0, `maker_fill_haircut_units`, `compute_cost_breakdown`/`net_edge` signed by side, `check_net_edge` (strict > 2¢; band 0.10–0.90 takers only), `decide_order_type` per `MAKER_ORDER_STRATEGIES`, unknown→taker fail-safe). Added `data/market_discovery.get_market_book` (fail-closed `Optional[BookSnapshot]`, depth = thinner side over top-10 levels). Pipeline: gross `check_edge` swapped for the net-edge gate — fetches the live book (`book_token_id` = cache token for news path, signal `market_id` for non-cache/copy path, the classifier-proven key), decides maker/taker, logs the mandatory `[OBSERVABILITY][NET_EDGE]` cost breakdown on every entry evaluation (both paths via the shared gate), blocks on `low_net_edge` / `price_band` / `book_unavailable` with new drop counters. `check_edge` deprecated (one-time warning, kept one release for tests, zero live callers). Config: verified fee schedule (D-09), `MIN_NET_EDGE_CENTS=0.02`, `TRADEABLE_PRICE_BAND=(0.10,0.90)`, `MAKER_FILL_HAIRCUT=0.01`, `SLIPPAGE_SPREAD_MULTIPLE=1.2`, `MAKER_FALLBACK_SECONDS=20`. Tests: 47 cost-model unit + 5 gate integration (incl. the DoD 2¢-trap test and log-format assert); fixtures made direction-aware + book-patched; 6_5 case-2 estimate overridden (honest gate blocks trades whose final direction opposes the estimate — correct new behavior). **Suite: 254 → 306 passed / 0 failed.** Noticed issues (NOT fixed): (1) classifier `_fetch_live_ask` passes the signal's `market_id` (a data-api condition id) as the CLOB `token_id` — if it ever stops resolving, Class B signals drop upstream at `DROP:price_fetch_failed`; pipeline book key intentionally mirrors it; (2) `MAKER_FALLBACK_SECONDS` enforcement (unfilled-maker → taker) is execution-phase work (Phase 3), config-ready now; (3) slippage is a conservative flat `full_spread×1.2` until Step 5's empirical curve. | Start **Step 2** (A6: `signal_outcomes` instrumentation + novelty detection — ⏱ TIME-GATED: data clock starts when this lands) |
| 2026-10-06 | 0 | **Step 0 (A1-S) DONE.** Created `strategies/estimator.py` (`EstimateResult` frozen contract, fail-closed registry, Laplace wallet hit-rate for `copy_edge_class_b`; `recalibration`/`velocity`/`resolution` stubs per D-02/D-05; `side_probability` helper for Step 4). Pipeline: estimator gate on BOTH fast+full paths — drop pre-LLM with `estimate:no_data`; edge gate + `open_positions.agent_estimate` consume the same `p_point` (no recompute); real `time_to_res` parsed from `end_date_iso` via `_parse_end_date` helper (deduped the deadline-gate parse); missing/invalid → `config.DEFAULT_TTR_HOURS=720` + `estimate:ttr_fallback` counter (visibility tag, not a drop). `decide_trade` prompt now shows `Model Probability Estimate (source: {method}, n={n})` via required `estimate_method`/`estimate_sample_size` kwargs. Tests: 25 estimator unit + 5 pipeline integration (no-constant reachable, zero-token-burn drop, fast-path dormancy, Laplace flow, TTR real+fallback); 23 dormancy-broken existing tests repaired with an `estimator_has_data` fixture (original intent preserved); Class B e2e wallet seeded 18W/2L so the REAL estimator runs end-to-end. **Suite: baseline 224 → 254 passed / 0 failed.** Grep gate clean (`0\.10` and `48.0` gone from live paths). Drift notes: E-1..E-5 line numbers all confirmed exactly. D-08 recorded (`wallet_address` kwarg). Noticed issues (NOT fixed, per discipline): (1) Class B copy signals often lack `end_date_iso` — metadata is only fetched when `matching_markets` non-empty, so `ttr_fallback` fires by design on that path (candidate improvement, out of Step 0 scope); (2) `test_all_four_paths_hit_the_gate` seeds the fast-path cache with a `keywords` key but `get_cached_keywords` reads `resolution_keywords` — path 1 actually routes full; pre-existing test quirk, untouched. | ~~Start Step 1~~ → done, see above |
| 2026-10-06 | — | Plan created; root causes verified in code (E-1..E-10); work order agreed (A3 last) | Await operator go for **Step 0** |

## 12. PART A EXIT CRITERIA (all must hold)

1. No constant, anywhere, in the estimate path (grep-clean, test-enforced).
2. Every entry decision logs: gross edge breakdown → net edge → order type → Kelly inputs/outputs.
3. Kelly never receives LLM confidence (regression test).
4. Every closed trade attributed to strategy + thesis; CUSUM monitor live; dashboard weekly.
5. Gate report passes on printed numbers: n ≥ 200 composite, net P&L CI > 0, agent Brier < market Brier.
6. Calibration curves trained on real history (source-tagged); recalibration estimator live; market-Brier table exists.
7. All existing invariants intact: risk engine purity, idempotency, reconciliation, `.env.test`, thresholds via config.

---

END OF List A.md
