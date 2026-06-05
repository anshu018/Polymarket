# /paper-trade-check — Paper Trading Health Report
# Run this every 7 days during the paper trading period.
# Run this before considering live deployment.

## STEP 1 — Retrieve last 7 days of resolved trades

Query Supabase:
  SELECT market_id, market_question, direction,
         agent_estimate, outcome, brier_contribution,
         strategy, category, confidence_at_entry,
         was_memoryless, exit_reason, closed_at
  FROM closed_trades
  WHERE closed_at > NOW() - INTERVAL '7 days'
  ORDER BY closed_at DESC

Report: total trades resolved in last 7 days.
If fewer than 3 resolved trades: note insufficient data
for statistical conclusions. Report what is available.

## STEP 2 — Brier score calculation

Brier score formula:
  BS = (1/N) × Σ(agent_estimate - outcome)²
  Where outcome = 1 for YES resolution, 0 for NO resolution

Calculate:
  7-day Brier score (last 7 days only)
  30-day Brier score (all paper trades to date)
  Brier score by category (politics, crypto, sports,
  legal, economics — any category with 3+ trades)

Report each score.

Gate threshold: 0.23
  If any score exceeds 0.23: flag as FAILING.
  If all scores below 0.23: flag as PASSING.

Lower is better. A perfect forecaster scores 0.
A random forecaster on binary markets scores 0.25.
Target: below 0.23.

## STEP 3 — Strategy performance breakdown

For each strategy (velocity, recalibration,
correlation, resolution):
  - Trades executed
  - Win rate (%)
  - Average edge at entry (cents)
  - Average P&L per trade (USDC)
  - Brier score contribution

Flag any strategy with:
  Win rate below 52% over 10+ trades
  Average edge at entry below 4 cents
  Negative average P&L per trade

## STEP 4 — Circuit breaker and failure review

Query:
  SELECT count(*) FROM daily_performance
  WHERE circuit_breaker_fires > 0
  AND date > NOW() - INTERVAL '7 days'

Report: number of days with circuit breaker fires.
For each fire: what triggered it (drawdown, health,
liquidity floor, or code error).

Code error fires are FAILING regardless of count.
Market condition fires are acceptable but note the cause.

## STEP 5 — Latency verification

Query market_signals for last 7 days:
  Fast path trades: confirm passed_fast_path = true
  Report average processing time if logged.

Gate thresholds:
  Fast path: must be confirmed < 5 seconds
  Full pipeline: must be confirmed < 22 seconds

If latency data not logged: flag as gap to fix.

## STEP 6 — Memoryless trade review

Query:
  SELECT count(*) FROM closed_trades
  WHERE was_memoryless = true
  AND closed_at > NOW() - INTERVAL '7 days'

If memoryless trades > 10% of total: Supabase
agent_memory reads are timing out too frequently.
Flag as reliability concern.

## STEP 7 — Paper trading gate status

Report pass/fail for all PT criteria:
  PT.1  Minimum 2 weeks completed         PASS / FAIL / PENDING
  PT.2  Minimum 20 resolved trades        PASS / FAIL / PENDING
  PT.3  Brier score < 0.23               PASS / FAIL
  PT.4  Zero circuit breaker code errors  PASS / FAIL
  PT.5  Zero unexpected failure modes     PASS / FAIL
  PT.6  Reconciliation succeeding         PASS / FAIL
  PT.7  Fast path < 5 seconds             PASS / FAIL
  PT.8  Full pipeline < 22 seconds        PASS / FAIL
  PT.9  Idempotency verified              PASS / FAIL
  PT.10 Supabase fallbacks verified       PASS / FAIL

Overall gate status:
  ALL PASS → CLEARED FOR LIVE DEPLOYMENT
  ANY FAIL → EXTEND PAPER TRADING. DO NOT DEPLOY.
