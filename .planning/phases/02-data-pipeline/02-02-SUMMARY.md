---
phase: 02-data-pipeline
plan: 02
subsystem: llm
tags: [newsanalyst, openrouter, pipeline, queue]
provides:
  - llm/news_analyst.py event classification agent.
  - data/pipeline.py queue coordination.
affects: [calibration-engine]
tech-stack:
  added: [aiohttp]
  patterns: [Concurrent queue worker, LLM classification agent, graceful timeout fallback]
key-files:
  created:
    - llm/news_analyst.py
    - data/pipeline.py
  modified: []
key-decisions:
  - decision: "Implement a hard 10-second timeout on News Analyst calls with no OpenRouter fallback."
    rationale: "If the News Analyst fails to respond within 10 seconds, the signal's timeliness is lost, so it's safer to drop the signal rather than retry."
duration: 45 min
completed: 2026-04-03
---

# Phase 2 Plan 02: News Analyst & Pipeline Ingestion Queue Summary

**Ingestion worker pipeline and News Analyst classification agent implemented and validated.**

## Accomplishments
- Implemented `llm/news_analyst.py` leveraging Qwen3-32B via OpenRouter with a strict 10s timeout.
- Built `data/pipeline.py` which runs 3 concurrent workers processing feed signals via `asyncio.Queue`.
- Enforced discarding signals with confidence <0.75 and logging them in Supabase `market_signals` with `action_taken = 'discarded'`.

## Next Phase Readiness
Ready for Phase 3 (Calibration Engine).
