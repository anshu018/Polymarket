# Deferred Items & Live-Capital Blockers

This document tracks technical debt, architectural deferred items, and mandatory blockers that must be resolved prior to deploying real capital (transitioning from `PAPER_TRADING=true` to live CLOB order execution).

---

## 1. Live-Capital Blockers

### 1.1 Synthetic Calibration Probability in Pipeline (`coordinator/pipeline.py:747`)
- **Current Behavior**:
  ```python
  estimated_probability = market_price + 0.10  # Simulating calibration model probability
  ```
- **Risk**: Hardcoded synthetic offset of $+0.10$ creates an artificial 10-cent edge on every single market regardless of reality, completely bypassing empirical calibration. In live trading, this would execute trades on false edge calculations.
- **Resolution Required**: Connect empirical calibration model / Brier calibration service to calculate real probability estimates (`agent_estimate`), clamped to `[0.01, 0.99]`, before live order placement.

### 1.2 Defensive Mode Pipeline Wiring Audit
- **Current Behavior**: `PLAN.md` defines Defensive Mode: when `health_score < 65`, the bot must cut position sizes by 50% and raise the minimum confidence threshold to $0.90$. If `health_score < 40`, a full halt is required. Currently, `coordinator/pipeline.py` does not query the daily health score or apply defensive multipliers.
- **Resolution Required**: Query `daily_performance.health_score` during startup or hourly caching, and adjust `position_size_check` and `MIN_CONFIDENCE_THRESHOLD` dynamically.

### 1.3 Event Loop Offloading for Synchronous Database Calls
- **Current Behavior**: While `coordinator/market_state.py` correctly uses `asyncio.to_thread(_query)` under `asyncio.wait_for`, other legacy Supabase helpers in `coordinator/pipeline.py` (such as `fetch_open_positions_exposure` and `check_pre_order_idempotency`) call synchronous `.execute()` directly inside the event loop coroutine.
- **Resolution Required**: Refactor all synchronous Postgrest `.execute()` calls in `coordinator/pipeline.py` to use `asyncio.to_thread` to guarantee zero event loop blocking during slow database queries.

### 1.4 Gross & Correlated Exposure Cap Semantics
- **Rule Verification**: `risk_engine.check_correlation_exposure` enforces a strict 20% cap (`total > 0.20` blocks). Exposure up to and including exactly 20.0% is permitted (e.g. four 5% allocations), while any proposed trade that would push exposure beyond 20.0% is blocked. This behavior is verified and tested (`test_exactly_20pct_allowed`). Must remain strictly untouched.

---

## 2. Exit-Feature Design Constraints

### 2.1 Ad-Hoc / Rogue Position Closing Scripts Prohibited
- **Ledger Invariant**: `closed_trades` is an immutable, permanent audit ledger. Manual scripts that force-close open positions before market resolution write synthetic records with `exit_reason="resolved"`, fictitious notes, and arbitrary Brier score contributions (e.g., forcing a 0.05 estimate to win or lose produces 0.9025 vs 0.0025 Brier penalty).
- **Rule**: Open paper positions (such as the four historical Mamdani 665460 positions) must remain open until actual market resolution occurs via official resolution reconciliation. No manual close scripts may be executed against Supabase.

### 2.2 Future Autonomous Exit Engine Requirements
When an automated position-exit engine is implemented, it must satisfy:
1. **Resolution Polling**: Continually query Polymarket Gamma API for official market resolution flags (`closed=True`, `winner=outcome`).
2. **True Settlement P&L**: Compute P&L using actual settlement payout ($1.00 for win, $0.00 for loss) or actual executed exit order fill price.
3. **Idempotent Migration**: Atomically remove row from `open_positions` and insert authoritative row into `closed_trades` within a verified transaction.
4. **Liquidity Floor Exits**: Only execute market exits if liquidity drops below `AUTO_EXIT_LIQUIDITY_FLOOR = 3000.0`, with proper slippage tolerance.

---

## 3. Concurrency & Locking Architecture Notes

### 3.1 In-Process Asyncio Locks vs Horizontal Scaling
- **Current Design**: `coordinator/market_state.get_market_lock(market_id)` maintains process-wide `asyncio.Lock` instances keyed on `market_id`. This provides complete mutual exclusion for concurrent async tasks within a single operating process (Hetzner CX22 daemon or single Railway container).
- **Multi-Instance Constraint**: If the trading agent is ever scaled horizontally across multiple containers, serverless instances, or separate OS worker processes, in-process `asyncio.Lock` will not share state across processes.
- **Future Solution**: Migrate `get_market_lock` to distributed locking (e.g. Postgres advisory locks via `pg_advisory_xact_lock(hashtext(market_id))` or Redis Redlock) prior to running multiple simultaneous bot processes.
