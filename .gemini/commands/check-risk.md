# /check-risk — Dedicated risk_engine.py Audit
# Run this every time risk_engine.py is modified.
# This is the most critical file in the codebase.

## STEP 1 — Import audit (most important check)

Read every import statement in risk_engine.py.
List them all.

Permitted imports (and ONLY these):
  import math
  import decimal
  from decimal import Decimal
  import datetime
  from datetime import datetime, timezone
  import logging

Any import not on this list is a critical violation.
Name it. Flag it. Do not suggest keeping it for any reason.

## STEP 2 — Network call audit

Search the entire file for:
  requests, httpx, aiohttp, urllib, http.client,
  socket, asyncio (if used for networking),
  openai, anthropic, openrouter, siliconflow,
  any function from /llm/

Any match is a critical violation.
risk_engine.py must have zero network capability.

## STEP 3 — Kelly formula verification

Locate the Kelly sizing function.
Verify the formula is: f = (b × p - q) / b
Where b = odds, p = win probability, q = (1 - p)

Verify fractional Kelly applied correctly:
  Velocity:      multiply f by 0.15
  Recalibration: multiply f by 0.25
  Correlation:   multiply f by 0.25
  Resolution:    multiply f by 0.35

Verify position size = fractional_f × portfolio_value

Run mental calculation:
  p=0.65, b=1.0, portfolio=$10,000, velocity strategy
  f = (1.0 × 0.65 - 0.35) / 1.0 = 0.30
  fractional_f = 0.30 × 0.15 = 0.045
  position = 0.045 × 10,000 = $450
  Confirm function returns $450 (±$1)

## STEP 4 — Threshold verification

Verify every threshold is hardcoded correctly:
  Confidence ceiling:         0.88
  Min confidence:             0.75
  Min edge:                   0.07
  Max single trade:           0.05 (0.08 for resolution)
  Max category exposure:      0.30
  Max correlated exposure:    0.20
  Min liquidity enter:        5000
  Auto-exit liquidity floor:  3000
  Daily drawdown halt:        0.08
  Weekly drawdown halt:       0.15
  Monthly shutdown:           0.25
  Health defensive:           65
  Health halt:                40

Flag any value that differs by any amount.

## STEP 5 — Determinism verification

Every function must return the same output for the
same input every time.

Flag any use of: random, time.time() as decision input,
datetime.now() as decision input (logging only is fine),
any global mutable state that affects return values,
any function that reads from a file or database.

## STEP 6 — Unit test coverage

Count all exported functions in risk_engine.py.
Count all test functions in /tests/test_risk.py.
Every exported function must have at least one test.

Report: X functions found, Y tests found.
List any function without a corresponding test.

## STEP 7 — Report format

CRITICAL VIOLATIONS (block Confirmed status):
  File, line number, exact violation, correct behavior.

FINDINGS (fix before next layer):
  File, line number, issue, correction.

If zero findings: write "None found."
