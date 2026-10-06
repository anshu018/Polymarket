# List A.md — Part A: "Edge and Truth" — Master Plan & Progress Tracker

> **This file is the single source of truth for Part A work.** Read it fully before doing any
> work on List A. Update the Master Tracker, per-step checkboxes, and Session Log every session.
> If context is ever lost, this file is the recovery point.

---

## STATUS DASHBOARD

| Field | Value |
|---|---|
| Created | 2026-10-06 |
| Last updated | 2026-10-06 |
| Current phase | Phase 0 — Steps 0–1 complete |
| Current step | **Step 1 DONE** — next action is **Step 2** (A6 signal instrumentation + novelty detection — ⏱ TIME-GATED) |
| Overall status | **IN PROGRESS — Steps 0–1 landed** (estimator fail-closed + honest net-edge gate; every entry logs a full cost breakdown) |
| Live-pipeline safety state | ✅ SAFE: no fake edge, no constant estimates, and entries now require net_edge > 2¢ after verified fees + spread + slippage from a live book. |

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
| 2 | A6 `signal_outcomes` instrumentation + novelty detection | — | NOT STARTED | — | — | ⏱ TIME-GATED: every week not running = data lost forever |
| 3 | A7 Attribution columns + edge-fade monitor (CUSUM) | — | NOT STARTED | — | — | ⏱ TIME-GATED: ledger should compound from first paper trade |
| 4 | A4 Kelly on real probability + shrinkage | Steps 0, 1 | NOT STARTED | — | — | Copy Class B gets real Kelly immediately |
| 5 | A5 Book-aware paper fills + market baseline + gate report | Steps 1–4 | NOT STARTED | — | — | Gate must judge on printed numbers |
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
- [ ] Migration SQL for `signal_outcomes` (pattern: `scratch/migration_*.sql`)
- [ ] Sampler task in `main.py` + tests (timeouts, NULL handling)
- [ ] `data/novelty.py` + unit tests (hash stability, Jaccard, factor rules)
- [ ] Wire novelty into pipeline drop logic + counters
- [ ] `scripts/signal_drift_report.py` + a synthetic-data test
- [ ] Run ≥1 week in paper mode before evaluating results (do not judge early)

**Definition of Done.** Every signal has forward prices (or explicit NULLs); drift report runs;
novelty counters live in pipeline stats. **Data clock starts the day this lands — schedule early.**
Status: **NOT STARTED**.

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
- [ ] Migration SQL (both tables + backfill `strategy` where derivable from existing rows)
- [ ] Write columns at entry (pipeline + copytrade executor write paths)
- [ ] `risk/edge_monitor.py` + synthetic tests (no-false-trigger, catches-fade)
- [ ] Allocation rule in config + enforcement point (sizing read)
- [ ] Dashboard script + Telegram send
- [ ] Ledger clock note: first paper trade after this lands = first attributed row

**Definition of Done.** Every closed trade self-describes its edge; CUSUM tested; dashboard prints.
Status: **NOT STARTED**.

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
- [ ] Rewire sizing call site(s) in `pipeline.py` (standard + `copy_edge_class_b` branch)
- [ ] Shrinkage helper (pure; belongs in `risk_engine.py` or `estimator.py` — pure math either way)
- [ ] Config: `SHRINKAGE_PRIOR_STRENGTH`
- [ ] Tests: different p_models → materially different sizes; confidence NEVER reaches Kelly
      (regression test); `kelly → 0` when net_edge ≤ 0; shrinkage monotone in n; side-adjustment correct
- [ ] Grep gate: no `win_probability=clamped` in live paths

**Definition of Done.** Kelly output varies with estimate quality; copy Class B sized by real
wallet hit rates immediately; regression tests lock it in. Status: **NOT STARTED**.

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
- [ ] Book-aware paper fill simulator + tests (spread crossing, depth rejection, maker queue model)
- [ ] Market-baseline recording (evaluated-markets table or market_signals columns) + resolution join
- [ ] Bootstrap CI in report script (numpy ok offline; runtime stays pure)
- [ ] `scripts/gate_report.py` + Telegram
- [ ] Decision-vs-fill logging wired → slippage curve feeds back into `cost_model.expected_slippage_units`

**Definition of Done.** Report runs weekly on real paper data; fill simulation proven against
book snapshots; baseline recorded from day one. Status: **NOT STARTED**.

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
| `CUSUM_THRESHOLD` / `CUSUM_DRIFT` | tuned once in Step 3 | 3 |
| `REALLOC_MIN_SAMPLES` | 30 | 3 |
| `GATE_MIN_RESOLVED_SAMPLES` | 200 | 5 |

## 7. NEW TABLES SUMMARY

| Table | Step | Purpose |
|---|---|---|
| `signal_outcomes` | 2 | Forward prices after each signal → post-delay alpha measurement |
| `market_history` | 6a | Resolved-market metadata + outcomes (backfill + incremental) |
| `market_price_history` | 6a | Price paths (daily + final-48h hourly) |
| `calibration_curves` | 6a | Pre-fitted base rates; runtime reads only |
| columns on `closed_trades` / `open_positions` | 3 | Attribution: strategy, thesis_id, p_model_at_entry, net_edge_at_entry, costs, order_type |

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
