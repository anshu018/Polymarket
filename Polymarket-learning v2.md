# Implementation Plan: Hermes-Adapted Self-Learning & Memory System (Upgraded v2.1)

This plan details the surgical port and adaptation of the NousResearch/hermes-agent memory, background curation, and threat sanitization systems to the Polymarket bot pipeline. The system operates asynchronously, allowing the bot to learn from resolved trades, update memory notes, and evolve market profiles without introducing hot-path trading latency.

---

## User Review Required

> [!IMPORTANT]
> - **Database Seeding Strategy:** The new `agent_memories` table will be seeded automatically on startup via `memory/migrations.py` using the values from `learning/seeds.py`.
> - **Cheapest Curator LLM:** The curator will perform background LLM reviews using the cheaper `qwen/qwen3-32b` model via OpenRouter (approx. $0.02/M tokens) to satisfy the LLM budget targets.
> - **Robust Seeding / Offline Fallback:** If Supabase is down during pipeline startup, the bot will load the local cold-start seeds from `learning/seeds.py` rather than using empty contexts. This guarantees that core safety rules (like FDA false positive limits) are always active.

---

## Open Questions

> [!NOTE]
> - **Headline Retrieval:** Since the `closed_trades` table doesn't have a `headline` column, we will query the `market_signals` table for the matching `market_id` to retrieve the original `raw_headline` for curator review. If missing or query fails, we will fall back to using the `market_question` as the headline.
> - **Time to Resolution:** The `time_to_resolution_hours` will be calculated dynamically by finding the difference between `closed_at` and `opened_at` timestamps on the resolved trade, defaulting to `24.0` hours if either timestamp is missing.

---

## Proposed Changes

We will introduce a new directory `learning/` containing memory storage, curation, sanitization, and seeds. Then we will modify the agents in `llm/` to accept a `memory_context` and integrate memory injection and curation task dispatch in `coordinator/pipeline.py`.

---

### 1. Learning & Memory Modules

#### [NEW] [sanitizer.py](file:///c:/Users/ash74/OneDrive/Desktop/Polymarket/learning/sanitizer.py)
- Scans memory contents using regular expressions to detect prompt injection threat signatures (e.g. `ignore previous instructions`, `forget everything`).
- Used to sanitize loaded database contents before prepending to agent system prompts, as well as checking curator LLM outputs before saving.

#### [NEW] [seeds.py](file:///c:/Users/ash74/OneDrive/Desktop/Polymarket/learning/seeds.py)
- Defines cold-start memory content and market profiles for `news_analyst`, `contract_parser`, `trade_decision`, and `coordinator`.

#### [NEW] [trading_memory.py](file:///c:/Users/ash74/OneDrive/Desktop/Polymarket/learning/trading_memory.py)
- Implements `TradingMemoryStore` class (port of Hermes `MemoryStore`).
- Implements `add()`, `replace()`, and `remove()` to manage memory entries.
- Implements `load_from_supabase(agent_name)` and `save_to_supabase(agent_name, target, content)` under 2-second timeout wrappers (`asyncio.wait_for`).
- **Optimistic Concurrency Control (OCC):** `save_to_supabase` will perform a version-check update. If another writer updated the row in the meantime, it will fetch the new state, re-apply the patch, and retry up to 3 times.
- Enforces duplicate rejection and character limits (2,200 for `memory_content`, 1,375 for `market_profile`).
- Implements formatting methods `format_for_system_prompt()`, `_render_block()`, and threat filtration using `sanitizer.py`.

#### [NEW] [trade_curator.py](file:///c:/Users/ash74/OneDrive/Desktop/Polymarket/learning/trade_curator.py)
- Implements `run_post_resolution_curator(resolved_trade)`.
- Dispatched via `asyncio.create_task()` (non-blocking) on trade resolution.
- Compiles review prompts for the target agent using the trade outcome, P&L, and historical stats.
- Fetches the last 20 category trades from `closed_trades` to summarize category performance.
- Queries OpenRouter (Qwen3-32B) to generate updates or `NO_UPDATE`.
- Enforces character limits and sanitizer checks on generated responses before writing them back to Supabase.

---

### 2. Database Migrations

#### [MODIFY] [migrations.py](file:///c:/Users/ash74/OneDrive/Desktop/Polymarket/memory/migrations.py)
- Append the `agent_memories` table schema to `SQL_MIGRATIONS`.
- Add test coverage for `agent_memories` inside `test_table()`.
- Add programmatic seeding to insert the seeds from `learning/seeds.py` if the table is empty.

---

### 3. Agent Integration (llm/)

#### [MODIFY] [news_analyst.py](file:///c:/Users/ash74/OneDrive/Desktop/Polymarket/llm/news_analyst.py)
#### [MODIFY] [contract_parser.py](file:///c:/Users/ash74/OneDrive/Desktop/Polymarket/llm/contract_parser.py)
#### [MODIFY] [trade_decision.py](file:///c:/Users/ash74/OneDrive/Desktop/Polymarket/llm/trade_decision.py)
#### [MODIFY] [coordinator.py](file:///c:/Users/ash74/OneDrive/Desktop/Polymarket/llm/coordinator.py)
- Add optional `memory_context: str = ""` parameter to the main classification/decision/parsing functions.
- If `memory_context` is present and non-empty, prepends it to the system prompt.

---

### 4. Pipeline Orchestration

#### [MODIFY] [pipeline.py](file:///c:/Users/ash74/OneDrive/Desktop/Polymarket/coordinator/pipeline.py)
- **At start of `run_pipeline`:** Instantiate `TradingMemoryStore` and load memories for all 4 agents.
- **Agent calls:** Pass the loaded formatted memory contexts to each agent call.
- **Graceful degradation:** If database timeouts occur during memory fetch, logs the issue and falls back to using the baseline hardcoded seeds from `learning/seeds.py` instead of empty strings.
- **Post-resolution Hook:** Modify the place where trades resolve or close to spawn `run_post_resolution_curator` asynchronously as a background task.

---

## Verification Plan

### Automated Tests

#### [NEW] [test_learning.py](file:///c:/Users/ash74/OneDrive/Desktop/Polymarket/tests/test_learning.py)
- Write tests using `pytest` and `anyio` with fully mocked Supabase and LLM API client interfaces:
  - Test threat sanitization against benign and malicious strings.
  - Test `TradingMemoryStore` OCC retry logic, duplicate rejection, entry addition, removal, surgical replacement, and string formatting.
  - Test agent modules prepending `memory_context` correctly to their prompts.
  - Test curator review prompt generation, LLM response curation, and Supabase upsert logic.

We will run tests via the Windows capture-safe pattern:
```powershell
python -c "
import subprocess, sys
result = subprocess.run(
    [sys.executable, '-m', 'pytest', 'tests/test_learning.py'],
    capture_output=True,
    text=True,
    timeout=60
)
print(result.stdout + result.stderr)
"
```

### Manual Verification
- Execute mock signal processing and mock trade resolution to verify logged sequence in the pipeline logs:
  - `[MEMORY] Loaded memory snapshot for news_analyst: NNN chars`
  - `[CURATOR] Memory updated for trade_decision`
