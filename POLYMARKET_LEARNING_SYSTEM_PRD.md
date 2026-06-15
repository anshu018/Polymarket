# PRD: Hermes-Adapted Self-Learning & Memory System for Polymarket Bot
**Version:** 2.0 — Direct Code Port (Based on Actual Hermes Source)
**Status:** Ready for Implementation
**Source:** NousResearch/hermes-agent (MIT License)

---

## 0. WHAT WE ARE DOING

We are directly porting three files from the Hermes Agent codebase and adapting them for the Polymarket trading pipeline. This is NOT a reimplementation — it is a surgical adaptation of working, production-tested code.

| Hermes Source File | Our Adapted File | What Changes |
|---|---|---|
| `tools/memory_tool.py` | `learning/trading_memory.py` | Storage backend: disk → Supabase |
| `agent/background_review.py` | `learning/trade_curator.py` | Trigger: turn-count → trade resolution. Review prompts: user preferences → trade outcomes |
| `tools/skill_manager_tool.py` | `learning/pattern_manager.py` | Domain: task procedures → market patterns. Storage: disk → Supabase |
| `agent/conversation_loop.py` (nudge section) | `coordinator/pipeline.py` (resolution hook) | Trigger point only |

All Hermes architectural decisions are preserved: frozen snapshot pattern, § entry delimiter, char limits, surgical patch preference, atomic writes, security scanning, background execution.

---

## 1. CORE ARCHITECTURE (UNCHANGED FROM HERMES)

### Frozen Snapshot Pattern
Memory is fetched from Supabase **once** at pipeline start, formatted, and injected frozen into each agent's system prompt. It never changes mid-run. This is identical to Hermes's prefix-cache preservation design.

### Entry Delimiter
`\n§\n` (section sign) separates memory entries. Identical to Hermes.

### Character Limits (Hard — Enforced in Code)
- Per-agent `memory_content`: **2,200 chars**
- Per-agent `market_profile`: **1,375 chars**
- Pattern files: **100,000 chars** (Hermes MAX_SKILL_CONTENT_CHARS)
- Similar trade context injection: **500 chars**

### Patch Preferred Over Edit
When updating memory or patterns, use substring find-and-replace (`old_text` → `new_content`), never full rewrites. Identical to Hermes's `patch` > `edit` rule.

---

## 2. FILE 1 — `learning/trading_memory.py`
**Source:** Port of `tools/memory_tool.py` `MemoryStore` class

### What to Keep Identical
- `MemoryStore` class structure
- `add()`, `replace()`, `remove()` method signatures and logic
- `format_for_system_prompt()` — frozen snapshot return
- `_render_block()` — the ═══ header format with usage percentage
- `_sanitize_entries_for_snapshot()` — security scan before injection
- `ENTRY_DELIMITER = "\n§\n"`
- All char limit enforcement logic
- Duplicate rejection in `add()`
- Substring matching in `replace()` and `remove()`

### What Changes

**Replace `load_from_disk()` and `save_to_disk()` with Supabase equivalents:**

```python
async def load_from_supabase(self, agent_name: str):
    """
    Replaces load_from_disk(). Fetches memory entries from Supabase
    agent_memories table. Builds frozen system prompt snapshot.
    Called once at pipeline start per agent.
    """
    from supabase import create_client
    # fetch row where agent_name = agent_name
    # parse memory_content by splitting on ENTRY_DELIMITER
    # parse market_profile by splitting on ENTRY_DELIMITER
    # deduplicate (same as Hermes)
    # run _sanitize_entries_for_snapshot() on both (same as Hermes)
    # build _system_prompt_snapshot (same as Hermes)

async def save_to_supabase(self, agent_name: str, target: str):
    """
    Replaces save_to_disk(). Upserts updated entries to Supabase.
    Updates version counter and last_updated timestamp.
    """
```

**Remove file-locking entirely** (`_file_lock`, `fcntl`, `msvcrt`). Supabase handles concurrency.

**Remove drift detection** (`_detect_external_drift`). Not applicable to Supabase.

**Remove `_read_file()` and `_write_file()`** — replaced by Supabase calls above.

**Remove `get_memory_dir()`** — not needed.

**Add agent_name parameter** to all public methods since we have 4 agents sharing one class.

### Supabase Table: `agent_memories`

```sql
CREATE TABLE agent_memories (
  id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  agent_name TEXT NOT NULL UNIQUE,
  memory_content TEXT NOT NULL DEFAULT '',
  market_profile TEXT NOT NULL DEFAULT '',
  version INTEGER NOT NULL DEFAULT 1,
  last_updated TIMESTAMPTZ DEFAULT NOW(),
  total_updates INTEGER NOT NULL DEFAULT 0
);

-- Seed rows on first deploy
INSERT INTO agent_memories (agent_name) VALUES
  ('news_analyst'),
  ('contract_parser'),
  ('trade_decision'),
  ('coordinator')
ON CONFLICT (agent_name) DO NOTHING;
```

### `format_for_system_prompt()` Output Format
Keep identical to Hermes. Example output for news_analyst:

```
══════════════════════════════════════════════
MEMORY (your personal notes) [34% — 748/2,200 chars]
══════════════════════════════════════════════
FDA approval headlines: 40% false positive rate historically. Reduce confidence by 0.2 for this category.
§
Kalshi feed: reliable for crypto/election markets. Less reliable for regulatory outcomes.
§
Markets resolving within 48hrs: highest edge window when price is 0.3–0.5.
══════════════════════════════════════════════
MARKET KNOWLEDGE [22% — 302/1,375 chars]
══════════════════════════════════════════════
"Substantially" and "majority of" in resolution criteria = high ambiguity. Flag and reduce size.
§
Crypto markets 00:00–06:00 UTC: historically noisy signals. Skip or minimum size only.
```

This exact formatted string gets prepended to the agent's system prompt.

### Cold-Start Seeds: `learning/seeds.py`

```python
NEWS_ANALYST_MEMORY = """FDA approval headlines: historically 40% false positive rate. Default conservative confidence.
§
AP News feed: reliable for political/economic markets. Less reliable for science/health.
§
Metaculus and Kalshi signals: higher precision than general news. Weight accordingly."""

NEWS_ANALYST_PROFILE = """Signal categories with best historical accuracy: election outcomes, economic indicators.
§
Signal categories with worst historical accuracy: FDA/regulatory, scientific announcements."""

CONTRACT_PARSER_MEMORY = """Resolution criteria with "substantially", "majority of", "significant": high ambiguity. Flag for skip.
§
Polymarket binary markets resolve YES/NO strictly — "close" outcomes default to NO historically.
§
Markets with multiple conditions joined by AND: all conditions must resolve favorably."""

CONTRACT_PARSER_PROFILE = """Most ambiguous categories: health/science, geopolitical.
§
Least ambiguous categories: sports outcomes, exact numeric thresholds."""

TRADE_DECISION_MEMORY = """Price range 0.3–0.5 with <10 days to resolution: historically highest EV range.
§
Election markets: model consistently overestimates certainty. Reduce position by 25%.
§
Brier score gate exists — prioritize calibration over aggressive sizing in paper trading phase."""

TRADE_DECISION_PROFILE = """Profitable categories (paper): economic indicators, election outcomes.
§
Loss-making categories (paper): FDA/health, long-horizon (>30 days) markets."""

COORDINATOR_MEMORY = """News Analyst confidence >0.8 + Trade Decision = BUY: historically reliable combination.
§
Single-agent signal without corroboration: reduce final confidence by 0.15.
§
Disagreement between News Analyst and Trade Decision: default to SKIP unless both >0.7."""

COORDINATOR_PROFILE = """Best performing signal combinations: high news confidence + medium price (0.35–0.55).
§
Worst performing: low news confidence + high price (>0.65) — avoid."""
```

---

## 2. FILE 2 — `learning/trade_curator.py`
**Source:** Port of `agent/background_review.py`

### What to Keep Identical
- The background execution pattern (runs AFTER pipeline, never blocking)
- The `asyncio.create_task()` dispatch pattern  
- The "nothing to save" escape valve
- The surgical patch preference in review prompts
- The action summary logging pattern

### What Changes

**Trigger**: Not turn-count. Fires after every resolved trade (outcome known in Supabase).

**No forked AIAgent**: Hermes forks a full `AIAgent` to run the review. We instead make a direct LLM API call to the cheapest available model using the existing `aiohttp` infrastructure in the project.

**Review scope**: Instead of "what did the user tell me about themselves", we review "what did this resolved trade teach each agent".

**Tool whitelist**: Hermes restricts the fork to memory+skill tools. We don't need this — our curator writes directly to Supabase, no tool dispatch needed.

### The Four Adapted Review Prompts

Port directly from Hermes's `_MEMORY_REVIEW_PROMPT`, `_SKILL_REVIEW_PROMPT`, adapted for trading:

```python
_MEMORY_REVIEW_PROMPT = """
Review the resolved trade below and update agent memory if appropriate.

AGENT: {agent_name}
CURRENT MEMORY:
{current_memory_content}
CHAR USAGE: {char_count}/{char_limit}

RESOLVED TRADE:
- Market: {market_question}
- Category: {category}
- This agent's signal: {agent_signal}
- This agent's confidence: {agent_confidence}
- Final decision: {final_decision}
- Outcome: {outcome}
- PnL: {pnl}
- Price at decision: {price}
- Time to resolution: {time_to_resolution_hours}hrs
- Headline: {headline}
- Source: {signal_source}

RULES (same as Hermes memory system):
1. Save generalizable patterns only — NOT one-off events.
2. Use ENTRY_DELIMITER (§) between entries.
3. When memory is near limit: consolidate or replace older entries, never exceed {char_limit} chars.
4. Prefer replace over add when updating an existing entry (surgical patch).
5. If this agent performed correctly and there is nothing new to learn: output exactly NO_UPDATE.
6. Never save raw prices, specific market IDs, or ephemeral data as permanent memory.

SAVE THESE (proactively):
- Category-level accuracy patterns discovered ("FDA headlines are unreliable")
- Source reliability learnings ("Kalshi signals are high precision")
- Price/time range patterns ("0.3-0.5 range + <10 days = highest EV")
- Calibration corrections ("I consistently overestimate confidence in crypto markets")
- Resolution criteria edge cases discovered

SKIP THESE:
- This specific trade's details (those are in Supabase)
- Temporary market conditions
- One-off events unlikely to repeat

Output format: Either "NO_UPDATE" or the complete updated memory_content string (max {char_limit} chars, entries separated by \\n§\\n).
"""

_MARKET_PROFILE_REVIEW_PROMPT = """
Review the resolved trade and update the market category profile for {agent_name}.

CURRENT MARKET PROFILE:
{current_market_profile}
CHAR USAGE: {profile_char_count}/{profile_char_limit}

RESOLVED TRADE:
{trade_summary}

CATEGORY PERFORMANCE SUMMARY (last 20 trades in this category):
{category_stats}

Update the market profile with category-level accuracy stats only.
Format: one stat per entry, separated by \\n§\\n.
Example entry: "FDA/Health markets: 34% win rate across 12 trades. Reduce confidence by 0.2."
Output: Either "NO_UPDATE" or complete updated market_profile string (max {profile_char_limit} chars).
"""
```

### Main Curator Flow

```python
async def run_post_resolution_curator(resolved_trade: dict):
    """
    Equivalent of Hermes's background_review._run_review_in_thread().
    
    Runs as asyncio.create_task() — never blocks the pipeline.
    Fires once per resolved trade for all 4 agents.
    Every 5 resolved trades: also runs full review (Mode B).
    """
    agents = ['news_analyst', 'contract_parser', 'trade_decision', 'coordinator']
    
    for agent_name in agents:
        # 1. Load current memory from Supabase
        store = TradingMemoryStore()
        await store.load_from_supabase(agent_name)
        
        # 2. Build review prompt (adapted from Hermes _MEMORY_REVIEW_PROMPT)
        prompt = _build_review_prompt(agent_name, store, resolved_trade)
        
        # 3. LLM call — cheapest available model
        response = await _call_curator_llm(prompt)
        
        # 4. Parse response
        if response.strip() == "NO_UPDATE":
            logger.info(f"[CURATOR] NO_UPDATE for {agent_name}")
            continue
        
        # 5. Validate and write back (same char limit enforcement as Hermes)
        if len(response) > store.memory_char_limit:
            logger.warning(f"[CURATOR] Response exceeds char limit for {agent_name}, truncating")
            response = _truncate_to_limit(response, store.memory_char_limit)
        
        # 6. Sanitize before save (port of Hermes _scan_memory_content)
        scan_error = _scan_memory_content(response)
        if scan_error:
            logger.warning(f"[CURATOR] Security scan blocked update for {agent_name}: {scan_error}")
            continue
        
        # 7. Save to Supabase
        await store.save_to_supabase(agent_name, 'memory', response)
        logger.info(f"[CURATOR] Memory updated for {agent_name}: {len(response)} chars")
        
        # Rate limit between agents (same as pipeline rate limiting)
        await asyncio.sleep(2)
```

### Security Scanning

Port the core pattern from Hermes `tools/threat_patterns.py`. Create `learning/sanitizer.py`:

```python
# Adapted from Hermes tools/threat_patterns.py
# Scans memory content before system prompt injection
INJECTION_PATTERNS = [
    r"ignore (previous|above|all) instructions",
    r"you are now",
    r"new (system |)instructions",
    r"disregard (your |previous |)",
    r"pretend (you are|to be)",
    r"act as (a |an |)",
    r"forget (everything|all|your instructions)",
]

def scan_memory_content(content: str) -> Optional[str]:
    """Returns error string if injection pattern found, else None."""
    for pattern in INJECTION_PATTERNS:
        if re.search(pattern, content, re.IGNORECASE):
            return f"Memory content blocked: matched injection pattern '{pattern}'"
    return None
```

---

## 3. FILE 3 — `learning/pattern_manager.py`
**Source:** Port of `tools/skill_manager_tool.py`

### What to Keep Identical
- `create`, `patch`, `edit`, `delete` action structure
- `_validate_frontmatter()` — YAML frontmatter requirement
- `MAX_SKILL_CONTENT_CHARS = 100_000`
- `_patch_skill()` logic — surgical find-and-replace
- The `absorbed_into` parameter on delete (for merging patterns)

### What Changes

**Storage**: `~/.hermes/skills/` → Supabase `trading_patterns` table

**Domain**: Task procedures → Market trading patterns

**Trigger**: Not agent-created from task sessions. Created by Pattern Extractor after N resolved trades show a statistically significant repeatable pattern.

**Pattern SKILL.md Format** (same structure as Hermes):

```markdown
---
name: fda-approval-low-confidence
description: FDA approval headline markets with analyst confidence below 0.6
version: 1.0.0
metadata:
  hermes:
    tags: [fda, health, regulatory]
    category: health-regulatory
---

# FDA Approval — Low Confidence Pattern

## When This Pattern Applies
- Market category: health/regulatory
- Headline contains: "FDA", "approval", "clearance", "authorization"
- News Analyst confidence: < 0.6
- Market price: any

## Historical Performance
- Total trades: 16
- Win rate: 34%
- Average PnL: -$4.20 per trade
- Last updated: 2026-06-07

## Decision Guidance
SKIP this market unless other strong signals corroborate.
If trading: minimum position size only (bottom 10% of normal sizing).

## Pitfalls
- "FDA grants emergency authorization" is different — higher confidence warranted
- Phase 3 trial results headlines are NOT the same as approval decisions
```

### Supabase Table: `trading_patterns`

```sql
CREATE TABLE trading_patterns (
  id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  pattern_name TEXT NOT NULL UNIQUE,
  category TEXT NOT NULL,
  pattern_content TEXT NOT NULL,
  historical_accuracy FLOAT NOT NULL DEFAULT 0.0,
  total_trades INTEGER NOT NULL DEFAULT 0,
  profitable_trades INTEGER NOT NULL DEFAULT 0,
  avg_pnl FLOAT NOT NULL DEFAULT 0.0,
  version INTEGER NOT NULL DEFAULT 1,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  last_updated TIMESTAMPTZ DEFAULT NOW()
);
```

### Pattern Extractor Trigger

Fires every 5 resolved trades in the same category:

```python
async def maybe_extract_pattern(category: str, resolved_trades: list):
    """
    Port of Hermes skill creation trigger.
    Hermes: "5+ tool calls, error recovery, user correction, non-obvious workflow"
    Ours:   "5+ resolved trades in category, >65% or <35% accuracy signal"
    """
    if len(resolved_trades) < 5:
        return
    
    accuracy = sum(1 for t in resolved_trades if t['outcome'] == 'WIN') / len(resolved_trades)
    
    # Strong enough signal to create/update a pattern
    if accuracy > 0.65 or accuracy < 0.35:
        await _create_or_patch_pattern(category, resolved_trades, accuracy)
```

---

## 4. INTEGRATION INTO EXISTING PIPELINE

### Hook 1 — Memory Injection (Before Each Agent Call)

In each agent file (`llm/news_analyst.py`, `llm/contract_parser.py`, etc.), modify the function signature to accept optional memory:

```python
async def classify_signal(
    headline: str, 
    source: str,
    memory_context: str = ""      # NEW — injected from TradingMemoryStore
) -> dict:
    
    system_prompt = memory_context + "\n\n" + EXISTING_SYSTEM_PROMPT
    # rest of function unchanged
```

In `coordinator/pipeline.py`, before each agent call:

```python
# Load all agent memories once at pipeline start (frozen snapshot)
memories = await load_all_agent_memories()  # returns dict[agent_name, formatted_str]

# Inject before each call
news_output = await classify_signal(
    headline, source,
    memory_context=memories.get('news_analyst', '')
)
await asyncio.sleep(2)

parser_output = await parse_contract(
    market_id, market_question, resolution_criteria,
    memory_context=memories.get('contract_parser', '')
)
await asyncio.sleep(2)
# ... etc
```

### Hook 2 — Post-Resolution Trigger (After Trade Resolves)

In `coordinator/pipeline.py`, wherever a trade resolution is processed:

```python
# Fire curator as background task — never blocks pipeline
asyncio.create_task(
    run_post_resolution_curator(resolved_trade),
    name="trade_curator"
)

# Every 5 resolutions — pattern extractor
resolution_count = await get_resolution_count()
if resolution_count % 5 == 0:
    asyncio.create_task(
        maybe_extract_pattern(resolved_trade['category']),
        name="pattern_extractor"
    )
```

---

## 5. FILE STRUCTURE

```
learning/
  __init__.py
  trading_memory.py      # Port of tools/memory_tool.py — MemoryStore → TradingMemoryStore
  trade_curator.py       # Port of agent/background_review.py — review prompts + async execution
  pattern_manager.py     # Port of tools/skill_manager_tool.py — trading patterns
  pattern_extractor.py   # New — statistical trigger for pattern creation
  sanitizer.py           # Port of tools/threat_patterns.py (core only)
  seeds.py               # Cold-start memory content for all 4 agents
```

---

## 6. IMPLEMENTATION ORDER

Build and verify each step before moving to next. Do NOT batch.

**Step 1:** Create `learning/sanitizer.py` (port threat_patterns core)
**Step 2:** Create `learning/seeds.py` (cold-start memory strings)
**Step 3:** Create Supabase table `agent_memories` + seed 4 rows
**Step 4:** Create `learning/trading_memory.py` (port MemoryStore, Supabase backend)
**Step 5:** Test: call `load_from_supabase('news_analyst')` and `format_for_system_prompt()` — verify ═══ header output
**Step 6:** Add `memory_context` param to all 4 agent functions
**Step 7:** Add `load_all_agent_memories()` + injection calls in `coordinator/pipeline.py`
**Step 8:** Verify in Railway logs: `[MEMORY] Injecting news_analyst: NNN chars`
**Step 9:** Create `learning/trade_curator.py` (port background_review, adapted prompts)
**Step 10:** Wire post-resolution hook in `coordinator/pipeline.py`
**Step 11:** Verify in Railway logs: `[CURATOR] Memory updated for trade_decision`

Steps 12+ (Pattern Manager) begin only after 2 weeks of paper trading data.

---

## 7. HARD RULES

1. **No local disk writes.** Railway filesystem is ephemeral. All persistence is Supabase only.
2. **Memory is frozen per pipeline run.** Fetched once at start of `run_pipeline()`, never re-fetched mid-run. Same as Hermes frozen snapshot.
3. **Curator is always background.** `asyncio.create_task()` only. Never `await run_post_resolution_curator()`.
4. **Char limits are enforced in code**, not just documented. Raise error if exceeded (same as Hermes).
5. **Patch over edit.** Substring replace always preferred over full memory rewrite.
6. **Graceful degradation.** If Supabase unavailable: `memory_context = ""`, pipeline continues. Memory is enhancement, not dependency.
7. **Sanitize before inject.** All Supabase content passes through `sanitizer.py` before system prompt injection.
8. **`config.py` only for config.** No new hardcoded values anywhere else.
9. **Do not modify existing agent logic.** Only add the `memory_context` parameter. When empty, behavior is 100% identical to current.

---

## 8. VERIFY SUCCESS

After Steps 1–11, check Railway logs for:
```
[MEMORY] Injecting news_analyst memory: 748 chars
[MEMORY] Injecting contract_parser memory: 312 chars
[MEMORY] Injecting trade_decision memory: 891 chars
[MEMORY] Injecting coordinator memory: 445 chars
[CURATOR] Resolved trade triggered Mode A review
[CURATOR] NO_UPDATE — news_analyst (no new pattern)
[CURATOR] Memory updated — trade_decision: 891 → 1043 chars
```

After 2 weeks: open Supabase dashboard, inspect `agent_memories` table. Memory content should have evolved beyond the cold-start seeds.

---
*Source: NousResearch/hermes-agent (MIT License). Adaptation for autonomous financial trading pipeline.*
*End of PRD v2.0. Implement Steps 1–11 only. Report after each step.*
