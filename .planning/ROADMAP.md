# Roadmap: Polymarket Autonomous Trading Agent

## Overview

A phased build of an autonomous prediction market trading agent, taking it from database setup and live data feeds through calibration, pure-Python risk controls, structured contract parsing, full-pipeline integration, Hetzner deployment, and paper trading gates to production launch.

## Phases

- [x] **Phase 1: Foundation** - All 8 Supabase tables created and connection verified.
- [x] **Phase 2: Data Pipeline** - RSS feed poller, spaCy pre-filter, and News Analyst live signals.
- [x] **Phase 3: Calibration Engine** - Statistical recalibration curves and rolling Brier score.
- [x] **Phase 4: Risk Engine** - Pure Python Kelly sizing, drawdowns, and position limits.
- [x] **Phase 5: Contract Parser** - DeepSeek V3 resolution criteria extraction cached for 24 hours.
- [x] **Phase 6: Integration** - End-to-end routing, fast path (<5s), full pipeline (<22s), and idempotency checks.
- [x] **Phase 7: Deployment** - Hetzner continuous hosting, startup reconciliation, and Telegram alerts.
- [x] **Phase 8: Market Discovery Module** - Cache-based matching of news signals to Polymarket markets.

---

## Phase Details

### Phase 1: Foundation
**Goal**: Core database schemas and credentials connection established.
**Depends on**: Nothing
**Requirements**: REQ-01
**Success Criteria**:
  1. All 8 tables exist in Supabase and schemas match PLAN.md exactly.
  2. Supabase connection succeeds from Python.
  3. All 7 environment variables loaded from .env.test.
**Plans**: 1 plan
- [x] 01-01: Create tables and verify schemas

### Phase 2: Data Pipeline
**Goal**: Live event signals flowing into the database through NLP filter.
**Depends on**: Phase 1
**Requirements**: REQ-02, REQ-03
**Success Criteria**:
  1. RSS poller connects to 20+ feeds with <= 10s polling interval.
  2. spaCy pre-filter loads and allowlist bypasses classification.
  3. News Analyst returns structured JSON event categories with 10s timeout fail-safe.
**Plans**: 2 plans
- [x] 02-01: Build RSS poller and spaCy prefilter
- [x] 02-02: Implement News Analyst and pipeline queue workers

### Phase 3: Calibration Engine
**Goal**: Probability calibration curves computed withrolling Brier score tracking.
**Depends on**: Phase 2
**Requirements**: REQ-04
**Success Criteria**:
  1. Brier score computation mathematically correct (returns 0.025 on benchmark).
  2. Model flags markets with edge >= 7 cents on paper data.
  3. Separate calibration curves exist for all 6 target categories.
**Plans**: 1 plan
- [x] 03-01: Build statistical calibration curves and edge logic

### Phase 4: Risk Engine
**Goal**: Deterministic pure-Python risk controls protecting capital.
**Depends on**: Phase 3
**Requirements**: REQ-05
**Success Criteria**:
  1. Zero LLM, HTTP, or network imports in risk_engine.py.
  2. Sizing correct (fractional Kelly for velocity/recal/correlation/resolution).
  3. Hard limits (5% single cap, 30% category cap, 20% correlation cap, drawdown circuit breakers).
  4. All functions execute in under 1ms with 100% test coverage.
**Plans**: 1 plan
- [x] 04-01: Build risk_engine.py and write pytest unit tests

### Phase 5: Contract Parser
**Goal**: DeepSeek V3 parsing contract resolution details into cached keyword rules.
**Depends on**: Phase 4
**Requirements**: REQ-06
**Success Criteria**:
  1. Parser returns valid structured JSON on all 10 real test contracts.
  2. Ambiguity score calibrated reasonably; cached for 24 hours.
  3. Entries older than 24 hours trigger automated refresh.
**Plans**: 1 plan
- [x] 05-01: DeepSeek V3 Contract Parser & Caching

### Phase 6: Integration
**Goal**: Integrated execution pipeline running both paths with robust fail-safes.
**Depends on**: Phase 5
**Requirements**: REQ-07, REQ-08
**Success Criteria**:
  1. Fast path completes in < 5 seconds; full pipeline in < 22 seconds.
  2. agent_memory warnings prepended on all Trade Decision calls.
  3. Idempotency UUID written to log before order submission.
  4. Supabase read timeouts handle degradation elegantly.
**Plans**: 1 plan
- [x] 06-01: End-to-End Integration & Routing

### Phase 7: Deployment
**Goal**: 24/7 continuous operation on Hetzner with startup reconciliation and active alerts.
**Depends on**: Phase 6
**Requirements**: REQ-09, REQ-10, REQ-11
**Success Criteria**:
  1. Process runs continuously under systemd; restarts automatically.
  2. Startup reconciliation diffs actual assets vs Supabase before signal processing.
  3. Seven Telegram alerts wire to correct triggers and fire within 30s.
**Plans**: 1 plan
- [x] 07-01: Continuous Deployment & Operations

### Phase 8: Market Discovery Module
**Goal**: Match classified news signals with live Polymarket markets using a background cache.
**Depends on**: Phase 7
**Requirements**: REQ-12
**Success Criteria**:
  1. Background cache loop fetches and parses live Gamma API markets under 6 seconds timeout.
  2. Signal matching matches entities from headlines to markets in-memory with scoring overlap.
  3. Real-time pricing fetches and validates CLOB midpoint spread under 4 seconds.
  4. Integration connects discovered markets dynamically in the execution pipeline.
  5. 8/8 unit tests pass successfully.
**Plans**: 1 plan
- [x] 08-01: Build market discovery cache, matching, and pipeline integration

### Phase 9: Hermes-Adapted Self-Learning & Memory System

**Goal:** [To be planned]
**Requirements**: TBD
**Depends on:** Phase 8
**Plans:** 0 plans

Plans:
- [ ] TBD (run /gsd:plan-phase 9 to break down)

---

## Progress

| Phase | Milestone | Plans Complete | Status | Completed |
|-------|-----------|----------------|--------|-----------|
| 1. Foundation | v1.0 MVP | 1/1 | Complete | 2026-04-03 |
| 2. Data Pipeline | v1.0 MVP | 2/2 | Complete | 2026-04-03 |
| 3. Calibration Engine | v1.0 MVP | 1/1 | Complete | 2026-04-03 |
| 4. Risk Engine | v1.0 MVP | 1/1 | Complete | 2026-04-04 |
| 5. Contract Parser | v1.0 MVP | 1/1 | Complete | 2026-05-27 |
| 6. Integration | v1.0 MVP | 1/1 | Complete | 2026-05-27 |
| 7. Deployment | v1.0 MVP | 1/1 | Complete | 2026-06-04 |
| 8. Market Discovery | v1.0 MVP | 1/1 | Complete | 2026-06-06 |
