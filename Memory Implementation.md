# Implementation Plan: Hermes-Adapted Self-Learning & Memory System

Replicate the NousResearch/hermes-agent self-learning memory system, frozen snapshot logic, and threat sanitization, surgically adapted for the Polymarket trading agent pipeline. This enables the agent to learn from resolved trades, evolving its performance profiles and notes asynchronously without introducing latency on the trading hot-path.

## User Review Required

> [!IMPORTANT]
> - **Supabase Table Setup:** You must run the SQL in [schema.sql](file:///c:/Users/ash74/OneDrive/Desktop/Polymarket/.planning/phases/09-hermes-adapted-self-learning-memory-system/schema.sql) in your Supabase dashboard to create the `agent_memories` table and insert initial seed records before starting the agent.
> - **Curation Model choice:** The Trade Curator reviews outcomes using the cheapest available model. We propose using `Qwen3-32B` (via OpenRouter), matching our News Analyst stack, to keep costs minimal (~$0.02 per million tokens).
> - **No local disk writes:** In alignment with Hetzner/Railway deployment constraints, all persistence is handled purely via Supabase.

## Open Questions

> [!NOTE]
> None. The PRD is extremely detailed and provides clear schemas, thresholds, prompts, security sanitization patterns, and integration hooks.

## Proposed Changes

---

### Foundation and Storage Layer

#### [NEW] [sanitizer.py](file:///c:/Users/ash74/OneDrive/Desktop/Polymarket/learning/sanitizer.py)
- Scan memory strings for potential prompt injection patterns (e.g. "ignore previous instructions", "forget everything") using regular expressions before injecting them into agent system prompts.

#### [NEW] [seeds.py](file:///c:/Users/ash74/OneDrive/Desktop/Polymarket/learning/seeds.py)
- Define the initial cold-start memory notes and category performance profiles for all four agents as specified in the PRD.

#### [NEW] [trading_memory.py](file:///c:/Users/ash74/OneDrive/Desktop/Polymarket/learning/trading_memory.py)
- Port the Hermes `MemoryStore` as `TradingMemoryStore`.
- Implement `load_from_supabase` and `save_to_supabase` using the standard project Supabase client.
- Enforce the 2-second timeout using `asyncio.wait_for` on Supabase operations, degrading to empty strings if database reads time out.
- Enforce strict character limits (2,200 chars for memory notes, 1,375 for market profile).
- Enforce entry additions, removals, and surgical substring replacements (`patch` preferred over full edit).
- Format output snapshot exactly matching the Hermes `═══` layout.

---

### Agent System Prompt and Pipeline Integration

#### [MODIFY] [news_analyst.py](file:///c:/Users/ash74/OneDrive/Desktop/Polymarket/llm/news_analyst.py)
- Add optional `memory_context: str = ""` argument.
- Prepend the context to the system prompt if provided.

#### [MODIFY] [contract_parser.py](file:///c:/Users/ash74/OneDrive/Desktop/Polymarket/llm/contract_parser.py)
- Add optional `memory_context: str = ""` argument.
- Prepend the context to the system prompt if provided.

#### [MODIFY] [trade_decision.py](file:///c:/Users/ash74/OneDrive/Desktop/Polymarket/llm/trade_decision.py)
- Add optional `memory_context: str = ""` argument.
- Prepend the context to the system prompt if provided.

#### [MODIFY] [coordinator.py](file:///c:/Users/ash74/OneDrive/Desktop/Polymarket/llm/coordinator.py)
- Add optional `memory_context: str = ""` argument.
- Prepend the context to the system prompt if provided.

#### [MODIFY] [pipeline.py](file:///c:/Users/ash74/OneDrive/Desktop/Polymarket/coordinator/pipeline.py)
- Load frozen memory snapshots for all agents at the start of `run_pipeline()`.
- Pass corresponding snapshots to each agent function call.
- Degrade gracefully to empty strings on database timeouts, logging the degraded execution.

---

### Asynchronous Post-Resolution Learning Loop

#### [NEW] [trade_curator.py](file:///c:/Users/ash74/OneDrive/Desktop/Polymarket/learning/trade_curator.py)
- Implement `run_post_resolution_curator(resolved_trade)`.
- For each agent, load the current memory state from Supabase.
- Compile the review prompts utilizing the resolved trade outcome and performance statistics.
- Submit the prompt to the cheapest LLM (Qwen3-32B) to generate updates or determine `NO_UPDATE`.
- Perform character limit checks and threat signature checks on returned responses.
- Write updates back to Supabase using `save_to_supabase`.

#### [MODIFY] [pipeline.py](file:///c:/Users/ash74/OneDrive/Desktop/Polymarket/coordinator/pipeline.py)
- Dispatch `run_post_resolution_curator` asynchronously as a background task using `asyncio.create_task()` immediately after trade outcome resolution.

---

### Verification and Test Suite

#### [NEW] [test_learning.py](file:///c:/Users/ash74/OneDrive/Desktop/Polymarket/tests/test_learning.py)
- Write tests using `pytest` and `anyio` with fully mocked Supabase and LLM API calls.
- Verify threat sanitizer detection of benign and injection payloads.
- Verify `TradingMemoryStore` operations: adding, replacing, removing, limit checking, and formatting.
- Verify system prompt prepending for all four agents.
- Verify pipeline loads memory context correctly and falls back to empty context on timeouts.
- Verify `trade_curator` successfully processes resolved trades, builds correct prompts, sanitizes outputs, and writes updates back to Supabase.

## Verification Plan

### Automated Tests
- Run the new test suite to verify the memory system:
  ```powershell
  pytest tests/test_learning.py
  ```
- Use the standard Python subprocess wrapper to run verification on Windows without truncation:
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
- Execute mock signal processing and mock trade resolution to verify logged messages:
  - `[MEMORY] Loaded memory snapshot for news_analyst: NNN chars`
  - `[CURATOR] Spawned background review for news_analyst`
  - `[CURATOR] Memory updated for trade_decision`
