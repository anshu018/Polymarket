# risk/GEMINI.md — Absolute Rules for This Folder
# READ BEFORE WRITING A SINGLE LINE IN /risk/

## THE ONE RULE THAT GOVERNS THIS ENTIRE FOLDER

This folder is pure Python. Always. No exceptions. Ever.

ZERO LLM calls.
ZERO imports from /llm/.
ZERO external API calls.
ZERO network calls of any kind.
ZERO imports from: requests, httpx, aiohttp, openai,
anthropic, openrouter, siliconflow, or any HTTP library.

Permitted imports (and ONLY these):
  import math
  import decimal
  from decimal import Decimal
  import datetime
  from datetime import datetime, timezone
  import logging
  import config

Every function in this folder must be:
  - Deterministic: same input always produces same output
  - Fast: executes in under 1ms without exception
  - Isolated: no dependency on any external service
  - Inviolable: cannot be overridden by any other
    component's output or reasoning

If you are about to write an import not on the list above:
STOP. You are about to make a critical violation.
Remove it. Find a pure Python alternative.

This folder's rules exist because the risk engine must
enforce position limits, drawdown halts, and circuit
breakers even when every external API is down.
It cannot have external dependencies. Ever.
