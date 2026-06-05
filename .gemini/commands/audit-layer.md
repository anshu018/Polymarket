# /audit-layer — Full Layer Architecture Compliance Review
# Run this before marking any layer as Tested in PROGRESS.md

Perform a complete audit of the current layer's code.
Review every file in the layer. Report every finding.

## STEP 1 — Architecture compliance

Check every file against PLAN.md and the relevant
subfolder GEMINI.md rules.

For /risk/ files: confirm zero LLM imports, zero network
calls, all functions deterministic and under 1ms.

For /llm/ files: confirm 18s timeout wrapper present,
max_tokens=900 and thinking_budget=600 on all
Qwen3-235B-A22B calls, agent_memory prepended before
Trade Decision and Contract Parser calls.

For /execution/ files: confirm idempotency UUID written
before every API call, fail closed if Supabase unavailable.

## STEP 2 — Threshold verification

Verify every numeric threshold matches PLAN.md exactly:
  Min confidence to trade:         0.75
  Fast path confidence threshold:  0.87
  Confidence ceiling:              0.88
  Min edge to trade:               0.07
  Max single trade:                5% (8% resolution edge)
  Max category exposure:           30%
  Max correlated exposure:         20%
  Min market liquidity:            $5,000
  Auto-exit floor:                 $3,000
  Daily drawdown halt:             8%
  Weekly drawdown halt:            15%
  Monthly shutdown:                25%
  Health defensive mode:           < 65
  Health full halt:                < 40
  Supabase timeout:                2 seconds
  LLM timeout:                     18 seconds
  Kelly fractions: velocity=0.15, recalibration=0.25,
                   correlation=0.25, resolution=0.35

Flag any value that differs from this list.

## STEP 3 — Error handling review

Every external call must have explicit error handling.
Flag any bare except clause.
Flag any exception that is caught and silently ignored.
Flag any Supabase read without a 2-second timeout wrapper.
Flag any LLM call without an 18-second timeout wrapper.

## STEP 4 — Code standards

Flag any: missing type hints on function signatures.
Flag any: missing docstrings.
Flag any: print() used instead of logging module.
Flag any: hardcoded API key, token, or secret.
Flag any: log entry missing timestamp or component name.

## STEP 5 — Report format

Report findings in two sections:

CRITICAL (must fix before Confirmed):
  List each violation with file name, line number,
  exact rule violated, and correct behavior.

IMPORTANT (fix before next layer):
  List each issue with file name and correction needed.

If zero findings in a section: write "None found."
Do not omit a section. Do not combine sections.
