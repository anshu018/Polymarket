# llm/GEMINI.md — Rules for Every LLM Call in This Folder
# READ BEFORE WRITING ANY FUNCTION IN /llm/

## RULE 1 — Every function has an 18-second timeout wrapper

Every function that calls an LLM API must:
  - Wrap the API call with an 18-second timeout
  - On timeout: cancel immediately, do not wait
  - On timeout: retry on OpenRouter with identical prompt
  - Log: which model was used, latency, whether fallback triggered
  - Never let a timeout exception propagate uncaught to the caller

No exceptions to this rule. A function without a timeout
wrapper is not complete. It is broken.

## RULE 2 — Qwen3-235B-A22B calls have hard parameter limits

Every call to Qwen3-235B-A22B must include:
  max_tokens: 900
  thinking_budget: 600

These are not suggestions. They are hard limits that
prevent latency blowout on complex prompts. Never omit
them. Never increase them.

## RULE 3 — agent_memory lessons prepended before every call

Before every call to Trade Decision Agent or Contract Parser:
  1. Query agent_memory table for relevant non-retired lessons
  2. Filter by category and trigger_condition JSON fields
  3. Prepend all results as a warning block at the TOP
     of the system prompt
  4. Market context comes AFTER the lessons block

If the Supabase query times out (2 second limit):
  - Proceed without lessons
  - Set was_memoryless = true in the trade log
  - Do not block the trade

## RULE 4 — Provider routing

Trade Decision Agent:
  Primary:  SiliconFlow (Qwen3-235B-A22B)
  Fallback: OpenRouter (at exactly 18 seconds)

News Analyst:
  Primary:  OpenRouter (Qwen3-32B)
  Fallback: None — signal dropped on timeout, not critical

Contract Parser:
  Primary:  OpenRouter (DeepSeek V3)
  Fallback: None — not in hot path, retry acceptable

## RULE 5 — No risk logic lives in this folder

This folder handles LLM calls only.
Risk checks, position sizing, circuit breakers, and
threshold enforcement live exclusively in /risk/.
Never import /risk/ functions and call them from here.
The orchestration layer handles sequencing.
