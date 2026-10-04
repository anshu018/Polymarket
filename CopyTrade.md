# Strategy 5: Copy Edge — Design Document

**Status:** Design finalized. Supersedes the earlier CopyTrade.md and the "Copy Edge" PRD.
**Relationship to prior docs:** This document keeps everything from the original PRD that still holds
(Class A/B split, Gamma API polling, limit-order slippage guard, non-goals) and replaces the two things
that were undefined: **how a wallet earns the right to be copied**, and **how much to risk once it has**.
Everything here is written to slot into the existing architecture in `PLAN.md` — it reuses your existing
Kelly sizing formula, your existing risk_engine.py gates, your existing idempotency pattern, and (this is
the important part) the _exact same probation/retirement state machine_ your other four strategies already
use in `PLAN.md` Section 12. Nothing new was invented where something proven already existed.

---

## 1. Purpose

Track a curated set of high-performing Polymarket wallets, mirror their trades through a risk-gated
pipeline, and let the _system itself_ decide — from evidence, not from operator judgment — how much
to trust each wallet and how much capital to risk on it. A wallet that keeps winning gets bigger. A
wallet that stops winning gets smaller automatically, then benched automatically. No manual tuning
required after the wallet is added.

---

## 2. Non-Goals (carried over, still correct)

- **No universal copying.** Every signal is filtered before it becomes a trade.
- **No real-time leaderboard scraping.** Wallet _candidates_ are discovered periodically by a
  semi-automated tool (Section 9) and approved by the operator — not scraped continuously. This
  avoids Gamma API rate-limit risk and avoids blindly trusting an unverified address.
- **No cross-exchange arbitrage** (Polymarket vs. Kalshi, etc.) as part of this feature.

---

## 3. Wallet Lifecycle & Trust Scoring — the core mechanism

### 3.1 Why raw win count isn't enough (and how we keep your idea intact)

"Most wins" is the right starting signal — but two failure modes have to be guarded against, or the
bot will eventually get fooled:

1. **Small-sample luck.** 3 wins out of 3 trades is not a 100% win rate, statistically — it's an
   unproven wallet that got lucky so far.
2. **Winning small, losing big.** A wallet can have a great win rate on binary markets by betting
   favorites, and still be a losing wallet in dollar terms if its rare losses are large.

The fix keeps win rate as the _primary_ signal but smooths it against sample size, and adds one
safety check against dollar losses. Nothing exotic — this is standard practice for evaluating any
track record with a limited number of observations.

### 3.2 Trust Score Formula

For each tracked wallet, using **resolved copy-trades only** (trades where the market has settled):

```
bayesian_win_rate = (wins + 5) / (wins + losses + 10)
```

The `+5 / +10` terms are a weak prior — they assume 50% until real evidence overrides it. A wallet
with 2 wins and 0 losses scores `7/12 = 0.58`, not `1.0`. A wallet with 18 wins and 2 losses scores
`23/30 = 0.77` — real evidence, real trust. This is exactly "most wins," just protected from noise.

**Trust Score = `bayesian_win_rate`.** This single number drives position sizing (Section 4).

### 3.3 Safety Override: Average Realized ROI

Independent of trust score, track:

```
avg_roi_per_trade = mean(pnl_percent) across all resolved copy-trades for this wallet
```

If `avg_roi_per_trade < -2%` **despite** an acceptable win rate, the wallet is flagged — this is
the "winning small, losing big" pattern. It forces demotion regardless of what the win-rate number
says (Section 3.5).

### 3.4 Wallet States

| State         | Trigger                                                           | Sizing Effect                                                                               |
| ------------- | ----------------------------------------------------------------- | ------------------------------------------------------------------------------------------- |
| **NEW**       | < 20 resolved copy-trades                                         | Forced 0.5× size multiplier regardless of trust score — sample too small to trust fully     |
| **ACTIVE**    | ≥ 20 resolved trades, win rate ≥ 52%, avg ROI ≥ -2%               | Full size, scaled by trust score                                                            |
| **PROBATION** | Win rate drops below 52% OR avg ROI drops below -2%, while ACTIVE | 0.5× size multiplier — must earn back trust                                                 |
| **RETIRED**   | Stays in PROBATION for 20 more resolved trades without recovering | `is_active = false`. Never deleted — kept for audit, same pattern as `agent_memory.retired` |

**Reinstatement (hysteresis, to prevent flapping):** A PROBATION wallet returns to ACTIVE only if,
over its next 10 resolved trades, win rate recovers above **55%** (not just back to the 52% floor —
the buffer prevents a wallet oscillating in and out every few trades).

These thresholds (52%, 20-trade windows) are **not new numbers** — they're the same ones your
existing Strategy Probation/Retirement system already uses in `PLAN.md` Section 12. Reusing them
means one mental model, one piece of logic to test, for both wallets and strategies.

### 3.5 State Diagram

```
        20+ resolved trades,
        win_rate >= 52%,
   ┌──── avg_roi >= -2%  ─────┐
   │                          ▼
[NEW] ──20+ resolved──▶ [ACTIVE] ──win_rate<52% OR──▶ [PROBATION]
  0.5x size              full size   avg_roi<-2%        0.5x size
                                                             │
                              10 resolved trades,            │ 20 more resolved
                              win_rate >= 55%  ◀──────────────┘  trades, still
                                     │                            below floor
                                     ▼                            │
                                [ACTIVE]                          ▼
                                                             [RETIRED]
                                                          is_active=false
                                                          (audit trail kept)
```

### 3.6 Priority Wallet Designation

The audit of the existing `copytrade/classifier.py` found it already has an **in-flight conflict
resolution map** — a 15-second TTL window that, when two wallets fire signals on the same market at
once, picks the one with the higher trust score. That mechanism is correct and stays as-is. This
section adds one refinement on top of it, not a replacement.

**The gap:** a narrow numeric tiebreak (0.81 vs. 0.79) treats a wallet with an exceptional,
long-proven record the same as one that's merely slightly better today. A wallet that has been
right almost every time deserves to win conflicts decisively, not by a rounding margin — and deserves
to be visible as such, not buried in a raw score.

**Mechanism:**

```
is_priority = (trust_score >= 0.80) AND (resolved_trades_count >= 30)
```

Note the stricter sample-size bar than the standard ACTIVE floor (30 resolved trades, not 20) —
"almost always profitable" is a stronger claim than "currently active," and should require more
evidence before the system trusts it.

- `is_priority` is **recomputed automatically** on every trade resolution, alongside `trust_score` —
  it is a derived label, not a separately maintained flag. If a Priority wallet's performance drops
  (Section 3.4 PROBATION trigger), `is_priority` is cleared in the same update, no separate logic path.
- **Conflict resolution rule (extends the existing classifier.py mechanism):**
  - If exactly one of the conflicting wallets is Priority → it wins automatically, full stop,
    regardless of the exact trust-score gap.
  - If more than one conflicting wallet is Priority → fall back to highest trust score, scoped to
    just the Priority wallets (same tiebreak logic as today, smaller pool).
  - If none are Priority → unchanged: highest trust score wins, as it does today.
- Every entry in `copytrade_log` for a trade taken during a conflict window records
  `was_priority_pick: true/false` — this is the audit trail you asked for: not just "we followed
  this wallet," but "we followed this wallet _over_ a competing signal, because its track record
  earned it that."

**Calibration note:** this only changes outcomes when two signals collide in the same 15-second
window on the same market — a relatively rare event. The main lever for "does a good wallet get
more weight" remains the continuous trust-score sizing in Section 4, which applies to every trade,
not just contested ones. Priority status is a refinement for the contested-signal edge case, not
the primary mechanism.

---

## 4. Sizing Logic — "how much to invest" follows directly from trust

Position size is **never a flat $10 or $50**. It's derived, in this order:

```
class_ceiling      = $10 USDC (Class A) or $50 USDC (Class B)   — hard absolute cap, unchanged from original PRD
state_multiplier   = 0.5 if wallet.state in (NEW, PROBATION) else 1.0
trust_multiplier   = wallet.trust_score                            — e.g. 0.77
kelly_scalar       = KELLY_FRACTION_COPY (0.10, Class B only; Class A stays fixed-fraction, see 5.2)

raw_size           = class_ceiling * state_multiplier * trust_multiplier
final_size         = risk_engine.position_size_check(raw_size, portfolio_value, strategy="copy_edge_class_a" or "copy_edge_class_b")
```

`position_size_check()` is the **existing** function in `risk/risk_engine.py` — no new risk logic is
introduced. It still enforces `MAX_SINGLE_TRADE_PCT`, category exposure, and correlated exposure
exactly as it does for the other four strategies. If Strategy 1–4 already hold a position in a market
a copy signal targets, this same call is what blocks or shrinks the copy trade — no special-case
code needed, the existing exposure gates already do this correctly.

**Worked example:** A Class B wallet with trust_score 0.77, state ACTIVE:
`raw_size = $50 * 1.0 * 0.77 = $38.50`, then passed through the same 5%/8% caps as everything else.

A brand-new Class A wallet (state NEW, trust_score not yet meaningful):
`raw_size = $10 * 0.5 * (whatever score) ≈ $5 max`, until it accumulates 20 resolved trades.

---

## 5. Class A / Class B Routing (unchanged from original PRD, confirmed correct)

- **Class type is a fixed per-wallet tag**, set by the operator when the wallet is added (not
  computed dynamically per trade). Simpler, auditable, and matches how a human would categorize a
  trader they're already watching ("this one's a fast scalper," "this one's a macro position-holder").
- **Class A stays permanently LLM-free.** This isn't an MVP shortcut — the sub-500ms latency target
  is structurally incompatible with a ~12-second Trade Decision LLM call. Class A is, and will always
  be, a pure deterministic mirror.
- **Class B routes through the full coordinator pipeline** (News Analyst → Contract Parser → Trade
  Decision → risk engine), same as Strategies 1–4. This is what catches offline hedges being
  mistakenly copied — the original PRD's stated reason for the split, and it still holds.

---

## 6. Strategy-Level Probation & Retirement (Copy Trade as a whole)

Separately from individual wallet lifecycle (Section 3), Copy Trade as an entire strategy is subject
to the **same rule your other four strategies already follow**, verbatim, from `PLAN.md` Section 12:

- Average edge < 4 cents over 20 consecutive copy-trades → **strategy probation** (all copy-trade
  position sizes halved, on top of any individual wallet-level halving already in effect)
- Continues for 20 more trades without recovery → **strategy suspended**, human review required
- Retirement requires all three: out-of-sample win rate < 52% over 30+ trades, AND EV/trade < 4
  cents, AND a causal explanation identified — one condition alone is not sufficient, exactly as the
  existing rule states

This gives you the two-layer answer to "if it's not doing well it gets slowed down": a bad **wallet**
gets benched without affecting the rest of Copy Trade; a bad **Copy Trade strategy overall** gets
throttled without you having to touch the other four strategies.

---

## 7. Data Model

### 7.1 `tracked_wallets` (extends the existing table — audit found it has only 5 columns)

| Column                  | Type           | Notes                                                                                                                                                        |
| ----------------------- | -------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `wallet_address`        | `VARCHAR(42)`  | Primary key                                                                                                                                                  |
| `trader_name`           | `VARCHAR(100)` | Human alias                                                                                                                                                  |
| `class_type`            | `VARCHAR(1)`   | `'A'` or `'B'`, fixed at creation                                                                                                                            |
| `state`                 | `TEXT`         | `'NEW'` \| `'ACTIVE'` \| `'PROBATION'` \| `'RETIRED'`, default `'NEW'`                                                                                       |
| `is_active`             | `BOOLEAN`      | `false` only when `state = 'RETIRED'`                                                                                                                        |
| `resolved_trades_count` | `INTEGER`      | Default 0, increments on each settled copy-trade                                                                                                             |
| `wins_count`            | `INTEGER`      | Default 0                                                                                                                                                    |
| `losses_count`          | `INTEGER`      | Default 0                                                                                                                                                    |
| `trust_score`           | `DECIMAL(6,4)` | Cached Bayesian win rate, recomputed on each resolution                                                                                                      |
| `avg_roi_per_trade`     | `DECIMAL(6,4)` | Rolling average `pnl_percent`, recomputed on each resolution                                                                                                 |
| `probation_entered_at`  | `TIMESTAMPTZ`  | Nullable — set when entering PROBATION, cleared on exit                                                                                                      |
| `is_priority`           | `BOOLEAN`      | Default `false`. Derived: `trust_score >= 0.80 AND resolved_trades_count >= 30` (Section 3.6). Recomputed alongside `trust_score`, not maintained separately |
| `added_at`              | `TIMESTAMPTZ`  | `DEFAULT NOW()`                                                                                                                                              |
| `last_updated_at`       | `TIMESTAMPTZ`  | Updated on every recompute                                                                                                                                   |

### 7.2 `copytrade_log` — confirm it records `wallet_address` per trade

Every row must be traceable to the source wallet so `wins_count` / `losses_count` above can be
recomputed on resolution. Add `wallet_address` as a foreign key to `tracked_wallets` if not already
present (audit did not confirm this column exists — verify before build).

Add one further column: `was_priority_pick BOOLEAN DEFAULT false` — set to `true` only when this
trade was taken _because_ its source wallet won a same-market signal conflict under the rule in
Section 3.6. `false` (not null) on every trade taken outside a conflict window. This is the audit
trail: which trades were a plain follow, and which were a follow chosen _over_ a competing signal.

### 7.3 `trader_performance`

If this table (found in migrations by the audit) already duplicates fields now living in
`tracked_wallets` above, **consolidate into one table** — two sources of truth for the same wallet's
performance is exactly the kind of drift that caused the market-discovery cache bug. One table, one
source of truth.

---

## 8. Execution & Safety Integration

- **Idempotency**: unchanged — UUID written to `idempotency_log` as `pending` before any Polymarket
  API call, for both classes, exactly as the audit confirmed already happens.
- **Risk engine**: unchanged — every sized trade, from both classes, passes through
  `risk_engine.py`'s existing exposure/drawdown/liquidity checks. No bypass.
- **Per-Market Tranche Gate & Concurrency Lock**: All copy-trade orders (Class A and Class B)
  acquire the process-wide market_locks[market_id] and evaluate market_position_check(...)
  before submitting orders or logging simulated paper fills. Repeat copy trades require >= 0.87
  confidence, maximum 2 tranches per market, and cumulative single-market exposure <= 8%.
- **Paper trading fix (required, not optional):** the audit found that in paper mode, Class A
  currently **no-ops silently** — it doesn't log a simulated fill anywhere. This must be fixed before
  Copy Trade can ever be evaluated: paper mode must write a simulated fill to `open_positions` /
  `closed_trades` on resolution, exactly like Strategies 1–4 do. Right now, Copy Trade is invisible
  to your Brier score and to its own paper-trading gate (Section 12) — that's a bug, not a feature.
- **Live order execution** (currently a `TODO` stub in both classes, per audit): **do not build yet.**
  Sequence: (1) confirm the market-discovery cache regression (`84abb88`) is fixed and validated on
  Railway, (2) let Strategies 1–4 clear the existing Paper Trading Gate in `PROGRESS.md`, (3) only
  then implement live execution for Copy Trade — and even then, Copy Trade runs its _own_ paper period
  first (Section 12) before touching live capital, the same discipline applied to every other strategy.

---

## 9. Wallet Candidate Discovery (semi-automated, human-gated)

Not continuous scraping (explicit non-goal), not fully manual either (too slow to find good wallets).
A middle path:

1. Operator periodically runs a discovery script (manual trigger, not a background job) that pulls
   public Polymarket leaderboard/trade-history data for candidate addresses.
2. Script computes historical win rate and average ROI for each candidate from public resolved-trade
   data — the same formula as Section 3.2/3.3, applied retroactively before the wallet is ever added.
3. Script presents a ranked shortlist. **Operator manually approves** which wallets get inserted into
   `tracked_wallets`, with `state = 'NEW'` — nothing is auto-added.
4. Once added, the wallet enters the normal NEW → ACTIVE lifecycle in Section 3, evaluated only on
   trades made _after_ being added (its pre-existing public history informs the discovery ranking, but
   does not pre-populate `resolved_trades_count` — every wallet starts its trust score from zero
   inside our own system).

---

## 10. Capital Allocation

**Now:** Copy Trade gets its own fixed ceiling — **10% of total active portfolio capital**, matching
the same conservative slice already given to Resolution Criteria Exploitation (your least-proven
existing strategy) in `PLAN.md` Section 5. This is additive; it does not require re-splitting the
existing 35/40/15/10 allocation across Strategies 1–4.

**Future (explicitly out of scope right now):** you flagged wanting a system where capital allocation
across _all_ strategies — present and future — shifts dynamically toward whichever is performing
best, using the same evidence-based logic as wallet trust scoring, generalized up one level. That is
a real, coherent idea and the wallet-trust mechanism in this document is intentionally built so it can
be reused for that later (same Bayesian smoothing, same probation/retirement shape). But it is **not**
being built now — noting it here so it isn't silently designed around while building Copy Trade, and
so the next feature (Strategy 6, whatever it is) has a clear place to plug into.

---

## 11. Milestones

- **Phase 1 — Schema & Scoring**: Migrate `tracked_wallets` to the schema in Section 7.1. Implement
  trust score / state-transition logic as pure functions (testable in isolation, no network calls —
  same discipline as `risk_engine.py`).
- **Phase 2 — Sizing Integration**: Wire Section 4's sizing formula into the existing executor, replacing
  the current fixed $10/$50 logic. Fix the paper-mode no-op bug (Section 8).
- **Phase 3 — Discovery Tool**: Build the semi-automated candidate discovery script (Section 9).
- **Phase 4 — Paper Validation**: Run Copy Trade in paper mode only, accumulate resolved trades per
  wallet, confirm state transitions fire correctly at the 20-trade boundaries before any live
  execution work begins.

---

## 12. Success Metrics (Copy-Trade-specific Paper Gate)

Mirrors your existing `PROGRESS.md` Paper Trading Gate structure, applied to this strategy alone
before it's allowed to request live capital:

- [ ] Minimum 20 resolved copy-trades across all wallets combined
- [ ] Brier score < 0.20 on resolved copy-trades (matches original PRD goal)
- [ ] At least one wallet has reached ACTIVE state (proving the NEW→ACTIVE transition works)
- [ ] Zero circuit breaker fires caused by copy-trade logic errors (market-condition fires acceptable)
- [ ] Paper-mode fills confirmed logged to `open_positions`/`closed_trades` (Section 8 fix verified)

---

## 13. Known Risks (carried over, still valid)

- **Liquidity slippage** — mitigated via limit orders at `tracker_entry_price + 0.5¢`, not market orders.
- **Copying exit dumps** — Class A does not copy exits; exits are managed independently via existing
  time-decay/trailing-profit rules.
- **Gamma API dependency** — if Gamma API goes down, the whole ingestion pipeline goes dormant. No
  fallback data source currently planned; acceptable for a single-operator, budget-constrained system.

---

## 14. Open Questions

- **Private/OTC order flow**: if a tracked wallet starts routing through private RPC (Flashbots-style)
  or OTC desks, their trades become invisible to Gamma API polling. No detection mechanism exists for
  this today — current assumption is that Polymarket volume still settles on-chain via the public CLOB.
  Revisit if a previously-active wallet's `resolved_trades_count` suddenly stops growing without being
  deactivated — that's the practical symptom to watch for, since we won't detect the cause directly.
