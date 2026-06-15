# STATE.md — Agent State and Validation Report

## 1. RSS Feeds Configuration
- **Total Feeds**: 23 (target: 15–25)
- **Status**: Checked and verified active (all return 200 OK and have entries).
- **Cleanup Details**:
  - Removed low-signal/noisy feeds (Federal Register, PACER dockets, environmental/fisheries notices, Coast Guard zone alerts).
  - Retained high-signal feeds: AP News (via Google), Federal Reserve (FOMC announcements), sports (ESPN), crypto (CoinDesk updated to feedburner link, CoinTelegraph), and platform search feeds (Polymarket, Kalshi, Metaculus via Google).
  - Added new high-signal feeds: BBC News World, Politico, NYT World, WSJ World, The Hill.

## 2. Test Verification
- **Total Tests Run**: 104
- **Passed**: 104
- **Failed**: 0
- **Verification Result**: PASS

## 3. Fallback Model Update
- **Observation**: The original fallback model `qwen/qwen3-next-80b-a3b-instruct` on NVIDIA NIM was unresponsive (timed out at 25s/35s), causing the Railway container startup validation probe to fail.
- **Action**: Updated `MODEL_NEWS_ANALYST_FALLBACK` to `meta/llama-3.3-70b-instruct` in `config.py` and `llm/news_analyst.py`. Verified that its latency is exceptionally fast (~1.74s) and it successfully runs model validation and signal processing on Railway.
- **Unit Test Coverage**: Adjusted mock model matchers in `tests/test_integration.py` to match the new Llama model name, ensuring all 104 tests pass successfully.

## 4. Confidence Score Distribution of Last 50 Signals
- **Count with conf = None (Error/Timeout)**: 2
- **Count with conf = 0.0**: 4
- **Count with conf 0.01-0.74**: 41
- **Count with conf >= 0.75**: 3 (6.0%)
- **Top Recent High-Confidence Signals**:
  - `[2026-06-15T22:07:48] conf=0.80 | headline: U.S. Open: Ranking favorites, contenders, more`
  - `[2026-06-15T22:05:54] conf=0.85 | headline: CFTC sues New Mexico over prediction market jurisdiction`
  - `[2026-06-15T22:04:32] conf=0.80 | headline: Bitcoin shoots higher on Iran peace deal, with Strait of Hormuz set to open`
- **Result Details**: High-signal feeds are successfully filtering out low-relevance noise, resulting in significantly fewer `0.0` confidence signals than before (which were previously 99%+ of all signals).
