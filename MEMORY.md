# MEMORY.md â€” Gemini Mistake Log

# Updated after every layer. Read before starting any layer.

# Purpose: Mistakes made once must never be made again.

# If this file has entries: read every single one before writing a line of code.

---

## HOW TO USE THIS FILE

READING:
Read this file at the start of every session before touching any code.
Every entry here is a mistake that already happened and cost time.
Do not repeat it.

WRITING:
After each layer is confirmed, add any mistakes made during that layer.
Use the exact format below. No vague entries. Be specific enough that
a future session with zero context would understand exactly what went
wrong and exactly how to avoid it.

ENTRY FORMAT:

### [LAYER X] â€” Short title of mistake

What happened: Exact description of what was done wrong.
Why it's wrong: Which rule, threshold, or schema it violated.
Correct behavior: Exactly what should be done instead.
Reference: PLAN.md Section X / TESTING.md criterion X.X

---

## ENTRIES

### [LAYER 1][RECONCILE] â€” config initialization ordering (main.py)

What happened: logging.basicConfig() was executed prior to loading the config variables effectively validating later.
Why it's wrong: Validating environment and configuration must happen absolutely first to ensure logging isn't instantiated over missing setups. A16 test failure.
Correct behavior: Load and validate config, THEN initialize logging. A strict sequence as outlined.
Reference: PLAN.md Section 1 / TESTING.md criterion A16

### [LAYER 1][SECRETS] â€” .env.example contained actual keys instead of placeholders

What happened: .env.example had hardcoded placeholders shaped as real keys instead of generic empty strings, preventing clean git commits and violating the secret restriction.
Why it's wrong: A20 explicitly enforces only POLYMARKET_PRIVATE_KEY is present in the example schema.
Correct behavior: Ensure .env.example contains 'placeholder' and nothing sensitive or confusingly shaped as a token.
Reference: PLAN.md Section 1 / TESTING.md criterion A20

### [LAYER 1][OTHER] — Tables never created in Supabase

What happened: Layer 1 was marked complete and all 16 checks reported PASS including check 1.1 (all 8 tables exist). In reality zero tables existed in Supabase. The connection test passed but table existence was never verified with an actual query. Tables had to be created manually via Supabase SQL Editor in Layer 2.
Why it's wrong: TESTING.md criterion 1.1 requires confirming tables exist via information_schema query. A connection test alone does not verify table creation. run_migrations() must be explicitly called and confirmed before marking 1.1 PASS.
Correct behavior: After running migrations always query information_schema.tables to confirm all 8 tables exist before marking Layer 1 complete. Never trust that migrations ran successfully without explicit verification.
Reference: PLAN.md Section 9 / TESTING.md criterion 1.1

### [LAYER 2][OTHER] â€” .gitignore was missing

What happened: .gitignore did not exist in the project until Layer 2 Step 1. The project ran through all of Layer 1 with API keys in .env and no .gitignore protecting them.
Why it's wrong: Any git operation could have committed .env with real credentials.
Correct behavior: .gitignore must be created in Layer 1 scaffold alongside .env.example.
Reference: PLAN.md Section 7 / TESTING.md 2.10

### [LAYER 2][OTHER] â€” spaCy installed on dev

What happened: spaCy en_core_web_md was installed on Windows development machine to enable V2D test to run.
Why it's wrong: Architecture requires dev machines use mock passthrough only. spaCy is production-only. V2D should have been skipped with a note per spec instructions.
Correct behavior: If spaCy not installed, skip V2D and note it. Never install production dependencies on dev machine.
Reference: PLAN.md Section 4.2

### [LAYER 2][OTHER] â€” RSS feed reality

What happened: Initial 20-feed list included permanently dead feeds (Reuters RSS shut down) and Cloudflare-blocked feeds that will never work on dev machines.
Why it's wrong: Feed list was written assuming production server IPs. Dev machines have different network constraints.
Correct behavior: Two-threshold system. Dev = 8+ feeds. Production = 15+ feeds. Reuters removed. AP News URL corrected.
Reference: PLAN.md Section 4.1

### [LAYER 2][SCHEMA] â€” Wrong JSON schema in News Analyst

What happened: NewsAnalystOutput returned action, category, confidence, and affected_entities.
Why it's wrong: Pipeline explicitly requires event_category, affected_market_ids, confidence_score, direction, and reasoning.
Correct behavior: News analyst system prompt and Pydantic schema must demand precisely the required 5 fields.
Reference: PLAN.md Section 4.3

### [LAYER 2][OTHER] â€” PowerShell truncation

What happened: Terminal output truncated on Windows PowerShell causing test results to be routed to .txt files instead of shown inline. Files accumulated in project root.
Why it's wrong: Output files are temp artifacts that pollute the project and get forgotten. Truncation workaround must be inline capture not file redirect.
Correct behavior: Use subprocess capture pattern for long output. Never redirect to files. Print single-line PASS/FAIL verdict at end of every test script.
Reference: GEMINI.md Windows Terminal Rule

### [LAYER 2][OTHER] — Supabase write silent

What happened: news_analyst.py Supabase write failed silently. market_signals table stayed empty after classify_signal ran.
Why it's wrong: Every classified signal must be written to market_signals. A silent write failure means signals are lost with no error visible.
Correct behavior: Write errors must be logged explicitly. The function continues but the failure must be visible in logs.
Reference: PLAN.md Section 7

### [LAYER 2][OTHER] — News Analyst raised

What happened: classify_signal() raised an exception to its caller when given invalid API credentials instead of returning None.
Why it's wrong: PLAN.md says the News Analyst must never raise to its caller under any circumstance. Callers expect None on any failure.
Correct behavior: Wrap entire function body in try/except Exception. Return None on any failure. Log the error internally.
Reference: PLAN.md Section 3.1

### [LAYER 2][OTHER] — .env read directly x4

What happened: .env file was read directly four separate times across multiple sessions despite being logged in MEMORY.md after the first occurrence.
Why it's wrong: Raw API keys and private keys appear in session context. MEMORY.md entry alone is insufficient to prevent this.
Correct behavior: Rule added to GEMINI.md RULE 9 as absolute rule. MEMORY.md entries are not enough for security violations — they must be GEMINI.md absolute rules.
Reference: GEMINI.md RULE 9

---

## COMMON VIOLATION CATEGORIES

Use these tags in entry titles for fast scanning:

[LAYER X][IMPORT] â€” Wrong import in risk_engine.py
[LAYER X][SCHEMA] â€” Wrong column name, type, or constraint
[LAYER X][THRESHOLD] â€” Wrong numeric value used
[LAYER X][TIMEOUT] â€” Missing or wrong timeout on external call
[LAYER X][IDEMPOTENCY] â€” Order submitted without idempotency check
[LAYER X][FALLBACK] â€” Missing Supabase fallback behavior
[LAYER X][ROUTING] â€” Fast path / full pipeline routing error
[LAYER X][MEMORY] â€” agent_memory not prepended before LLM call
[LAYER X][SECRETS] â€” Hardcoded API key or secret in source
[LAYER X][LOGGING] â€” print() used instead of logging module
[LAYER X][KELLY] â€” Wrong Kelly fraction or formula
[LAYER X][RECONCILE] â€” Startup reconciliation skipped or out of order
[LAYER X][GDELT] â€” GDELT placed in fast path or velocity routing
[LAYER X][OTHER] â€” Does not fit above categories

---

## Mistake #7 â€” Reading .env directly in test files

Date: 2026-03-31
What happened: Test files called load_dotenv() with no path, causing
supabase_client.py to auto-discover .env. When credentials failed,
Gemini opened .env directly. This is a permanent violation.
Rule added: .env is permanently off-limits. All tests load .env.test
only, via: load_dotenv(dotenv_path=".env.test", override=True)
before any project import. If .env.test is missing a key, STOP
and report. Never open .env.

---

### [LAYER 6/8][OTHER] — Backward compatibility with mock parameters in pipeline tests

What happened: Integrating dynamic market discovery could have broken existing integration tests which pass explicit market parameters (since the live Gamma API cache is empty during test suite runs).
Why it's wrong: Replacing parameter use entirely with discovery would cause all existing integration tests to fail closed or block.
Correct behavior: Only enforce cache-based market discovery when no `market_id` is supplied, or fall back to the supplied mock parameters if the cache is empty but parameters are provided.
Reference: PLAN.md Section 15 / TESTING.md Layer 6

---

END OF MEMORY.md
