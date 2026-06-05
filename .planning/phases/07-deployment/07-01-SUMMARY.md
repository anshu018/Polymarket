---
phase: 07-deployment
plan: 01
subsystem: deployment
tags: [deployment, systemd, reconciliation, telegram, alerts]
provides:
  - systemd configuration polymarket-agent.service for automatic restart.
  - Startup reconciliation in execution/reconciliation.py to diff API and database positions.
  - 7 Telegram alerts in monitoring/telegram_alerts.py for drawdowns, health score drops, reconciliation halts, and LLM failovers.
  - Correct logging format and continuous main run wrapper.
affects: []
tech-stack:
  added: []
  patterns: [startup reconciliation, systemd daemon, webhook/Telegram alert monitoring]
key-files:
  created: []
  modified:
    - main.py
    - execution/reconciliation.py
    - monitoring/telegram_alerts.py
    - polymarket-agent.service
    - tests/test_reconciliation.py
    - tests/test_alerts.py
key-decisions: []
duration: 45 min
completed: 2026-05-27
---

# Phase 7 Plan 01: Continuous Deployment & Operations Summary

**Continuously running main loop configured on Hetzner with automated startup reconciliation, Telegram alerts, and auto-restart policies, fully verified by unit tests.**

## Accomplishments
- Configured a systemd unit service `polymarket-agent.service` for continuous execution and auto-restart on Hetzner CX22.
- Built startup reconciliation logic in `execution/reconciliation.py` which diffs actual wallet assets and Polymarket positions with Supabase state on start, halting on inconsistency or retrying on API downtime.
- Implemented 7 Telegram alert triggers in `monitoring/telegram_alerts.py` mapped to drawdowns, health score alerts, reconciliation halts, and API failovers.
- Set up main continuous process queue in `main.py` ensuring paper trading mode is active by default.
- Verified functionality through pytest suites in `tests/test_reconciliation.py` and `tests/test_alerts.py` passing successfully.

## Next Phase Readiness
Milestone v1.0 Complete. Ready to complete the milestone and proceed to the Paper Trading Gate.
