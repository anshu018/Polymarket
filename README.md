# Polymarket

Fully autonomous Polymarket prediction market trading bot.
Runs 24/7 on Oracle Cloud ARM (Ubuntu). Built in async Python.

## Architecture

* **Risk Engine** — pure deterministic Python, <1ms, zero network calls
* **Trade Decision Agent** — Qwen3-235B via OpenRouter, capped at 900 tokens
* **Idempotency Layer** — UUID logged to Supabase before every order
* **Database** — Supabase with 2s timeout + fallback on every query

## Setup

cp .env.example .env

# Fill in .env with real credentials

pip install -r requirements.txt

## Docs

See .planning/ROADMAP.md and .planning/STATE.md for current system state.
