# Phase 9: Hermes-Adapted Self-Learning & Memory System - Context

**Gathered:** 2026-06-07
**Status:** Ready for planning
**Source:** PRD Express Path (POLYMARKET_LEARNING_SYSTEM_PRD.md)

<domain>
## Phase Boundary

This phase implements a self-learning memory system directly adapted from the NousResearch/hermes-agent codebase. It ports the frozen snapshot memory pattern, background trade curation review, threat sanitization, and pipeline injection hooks to optimize agent decision quality based on resolved trade outcomes. Only Steps 1–11 (Memory System and Trade Curation) are within the scope of this phase. Pattern Manager implementation is deferred to subsequent work.

</domain>

<decisions>
## Implementation Decisions

### Core Memory Architecture
- **Frozen Snapshot Pattern:** Load memory from Supabase once at pipeline start, format, and inject frozen into each agent's prompt. It never changes mid-run.
- **Entry Delimiter:** Use `\n§\n` to separate memory entries.
- **Character Limits:** Hard limits enforced in code:
  - `memory_content`: 2,200 characters.
  - `market_profile`: 1,375 characters.
  - `similar_trade_context`: 500 characters.
- **Surgical Patch preference:** Use substring find-and-replace (`old_text` -> `new_content`) rather than full rewrites.

### Storage Backend
- All memory is persisted in Supabase (`agent_memories` table), not on local disk.
- If Supabase is unavailable, fail gracefully (degrade to empty memory `""` so pipeline continues).
- Schema of `agent_memories`:
  - `id` UUID PRIMARY KEY DEFAULT gen_random_uuid()
  - `agent_name` TEXT NOT NULL UNIQUE
  - `memory_content` TEXT NOT NULL DEFAULT ''
  - `market_profile` TEXT NOT NULL DEFAULT ''
  - `version` INTEGER NOT NULL DEFAULT 1
  - `last_updated` TIMESTAMPTZ DEFAULT NOW()
  - `total_updates` INTEGER NOT NULL DEFAULT 0

### Trade Curator
- Executes in the background (using `asyncio.create_task()`) after a trade resolves. Never blocks the hot path.
- Runs outcome-based reviews of resolved trades for all 4 agents using the cheapest available LLM (no forked agent needed, use existing HTTP client).
- Prompts: Use adapted prompts for memory updates and market category profiles.
- Enforce character limits, and run security sanitization before saving.

### Security Sanitization
- Scan memory entries before injection using regex pattern checking to prevent prompt injection.

### Pipeline Integration
- In `llm/news_analyst.py`, `llm/contract_parser.py`, `llm/trade_decision.py`, and `llm/coordinator.py`, add optional `memory_context: str = ""` argument without changing existing core logic.
- In `coordinator/pipeline.py`, fetch all agent memories at start, inject them, and trigger the background trade curator task post-resolution.

### Claude's Discretion
- Internal class name is `TradingMemoryStore` to replace the ported `MemoryStore`.
- Use existing project logging module (no print statements).
- Choice of cheapest LLM model for curation (defaulting to the News Analyst model Qwen3-32B or fallback).

</decisions>

<specifics>
## Specific Ideas
- Cold-start seeds are provided in the PRD for `news_analyst`, `contract_parser`, `trade_decision`, and `coordinator`. These must be seeded into Supabase on deployment.
- Delimiter: `\n§\n`.
- File structure: `learning/` containing `trading_memory.py`, `trade_curator.py`, `sanitizer.py`, `seeds.py`.
- Automated test file `tests/test_learning.py` to verify the memory store, snapshot generation, curation prompts, and injection hooks.

</specifics>

<deferred>
## Deferred Ideas
- **Pattern Manager and Extractor (Steps 12+):** Deferred until 2 weeks of paper trading data is gathered.
- Supabase table `trading_patterns` and files `learning/pattern_manager.py`, `learning/pattern_extractor.py` are explicitly out of scope for this phase.

</deferred>

---

*Phase: 09-hermes-adapted-self-learning-memory-system*
*Context gathered: 2026-06-07 via PRD Express Path*
