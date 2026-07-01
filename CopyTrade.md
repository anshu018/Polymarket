# Product Requirements Document (PRD)
## Strategy 5: Copy Edge (Leaderboard Copy-Trading Feature)

---

### 1. Title & Summary
* **Title**: Strategy 5: Copy Edge (Leaderboard Copy-Trading)
* **Summary**: A hybrid copy-trading mechanism that tracks the top 1% profitable Polymarket wallets, filters their entries through a dual-track velocity/macro engine, and executes risk-managed positions to capture smart-money alpha without suffering from slippage or wash-trading manipulation.

---

### 2. Problem Statement
Prediction markets are highly information-sensitive. While our bot gathers public news via RSS, top human traders and institutional syndicates often possess proprietary alpha, access to private polls, or faster news scraping models. By not utilizing this "smart money" flow, our bot misses highly profitable, low-latency market entries. 

However, blind copy-trading on Polymarket leads to losses due to:
* **Slippage**: Price spikes occurring between the time the leader enters and when the bot copies.
* **Hedges**: Copying orders that were actually placed to offset offline risks.
* **Market Manipulation**: Whales wash-trading illiquid markets to bait copy-bots.

---

### 3. Goals & Objectives
* **Capital Protection**: Ensure 100% of copied trades pass the bot's core risk engine and drawdown gates.
* **Positive Expected Value**: Achieve a Brier score of `< 0.20` on copied trades that settle.
* **Latency Mitigation**: Execute Class A (Fast Copy) trades in `< 500ms` from block confirmation.
* **Evasion of Scams**: Ingest zero trades on markets with less than $25,000 total volume.

---

### 4. Non-Goals
* **Universal Copy-Trading**: The bot will not copy every trade from every whitelisted wallet. It will aggressively filter signals.
* **Leaderboard Scraping**: The bot will not attempt to scrape or parse the live Polymarket Leaderboard API in real-time due to rate limits. Wallets must be maintained via a curated database table.
* **Exchange Arbitration**: The bot will not attempt to arbitrage prices between Polymarket and other platforms (e.g., Kalshi) as part of this feature.

---

### 5. Target Users / Personas
* **Bot Operator (Developer)**: Wants to scale the bot's capital deployment by tapping into external smart-money alpha, requiring minimal maintenance overhead and strict protection of hot wallet funds.

---

### 6. User Stories / Use Cases
* **As a Bot Operator**, I want the bot to automatically monitor a curated list of top wallets so that we can discover highly profitable markets before they hit public RSS news feeds.
* **As a Bot Operator**, I want the bot to reject trades where the price has already slipped by more than 1.5 cents so that we don't buy the top of a whale's pump.
* **As a Bot Operator**, I want whitelisted macro-trader signals to be validated by our LLM News Analyst so that we don't accidentally copy offline hedge positions.

---

### 7. Requirements

#### 7.1 Functional Requirements
* **Curated Whitelist Management**: The bot must load and monitor addresses from a `tracked_wallets` database table.
* **Dual-Track Processing**:
  - **Class A (Speed/Alpha)**: Bypass LLM logic and execute immediately. Hard cap at $10 USDC. Slippage threshold $\le$ 1.0 cent. Market volume $\ge$ $25,000.
  - **Class B (Macro/Deep Value)**: Route through [llm/news_analyst.py](file:///C:/Users/ash74/OneDrive/Desktop/Polymarket/llm/news_analyst.py) and [llm/trade_decision.py](file:///C:/Users/ash74/OneDrive/Desktop/Polymarket/llm/trade_decision.py). Hard cap at $50 USDC using 10% Kelly.
* **Slippage Guard**: Compare live market ask price against the tracked wallet's execution price. Reject if delta exceeds the threshold.
* **ERC-20 Infinite Approval**: Pre-approve the Polymarket Proxy contract at startup to allow instant trading of any new outcomes.
* **Emergency Exit Override**: Listen for exit transactions from the tracked wallet. If they exit at a loss, trigger an immediate emergency market sell.

#### 7.2 Non-Functional Requirements
* **Performance**: Class A execution must take `< 500ms` from signal detection to order placement.
* **Security**: Enforce strict validation that transactions originate from the Polymarket CLOB contract to prevent malicious peer-to-peer tokens from triggering trades.
* **Reliability**: Fail closed if Supabase or the Polymarket API is unresponsive.

---

### 8. User Flow / Journey

```
[Smart Money Trade] ➔ [Gamma API Poll] ➔ [Class Check]
                                              │
                    ┌─────────────────────────┴────────────────────────┐
          [Class A: Speed]                                   [Class B: Macro]
                    │                                                  │
          [Slippage < 1.0c?]                                 [Slippage < 1.5c?]
                    │                                                  │
          [Volume > $25,000?]                                          │
                    │                                        [LLM Decision OK?]
                    │                                                  │
            (Fast Bypass) ───────────────────┬─────────────────────────┘
                                             ▼
                                   [Risk Engine Gate]
                                             │
                                   [Idempotency pre-log]
                                             │
                                   [Order Execution]
```

---

### 9. Technical Approach

#### 9.1 Ingestion Mechanism: Gamma API Polling
Instead of listening to raw Polygon blocks via WebSockets (which introduces CPU load and ABI decoding complexity), the bot will poll Polymarket’s public Gamma API endpoint for whitelisted addresses every **5 seconds**:
`https://gamma-api.polymarket.com/events?user={wallet_address}`
This returns pre-parsed JSON of their latest executions.

#### 9.2 Sizing & Risk Controls
We introduce **Strategy 5 (Copy Edge)**.
* **Kelly Multiplier**: `KELLY_FRACTION_COPY = 0.10` (10% fractional Kelly).
* **Position Limits**:
  - Class A: Fixed $10 USDC.
  - Class B: Dynamic Kelly capped at $50 USDC (2% portfolio max).
* **Drawdown Halts**: Subject to the standard 8% daily and 15% weekly drawdown halts.

---

### 10. Data Model

#### `tracked_wallets`
| Column | Type | Constraints | Description |
| :--- | :--- | :--- | :--- |
| `wallet_address` | `VARCHAR(42)` | `PRIMARY KEY` | Polygon address of the top trader |
| `trader_name` | `VARCHAR(100)` | `NOT NULL` | Human-readable alias (e.g., "dclogger") |
| `class_type` | `VARCHAR(1)` | `CHECK (class_type IN ('A', 'B'))` | Ingestion track (A = Speed, B = Macro) |
| `is_active` | `BOOLEAN` | `DEFAULT TRUE` | Soft-disable switch |
| `added_at` | `TIMESTAMPTZ` | `DEFAULT NOW()` | Record creation timestamp |

---

### 11. API / Integration Points
* **Polymarket Gamma API**: Used for polling latest user trades (`/events`) and fetching market metadata (`/markets`).
* **Polymarket CLOB API**: Used to place buy/sell orders and check live order book spreads.
* **Supabase REST Interface**: Used to read `tracked_wallets` and log `idempotency_log` and `open_positions` entries.

---

### 12. Dependencies & Risks

#### Dependencies
* **Gamma API Uptime**: Polling relies on Polymarket’s web indexing API. If Gamma goes down, the copy-trading pipeline goes dormant.

#### Risks
* **Liquidity Slippage**: Even if our slippage check passes, placing a market order on a thin book can execute at a poor price. *Mitigation*: The bot will use **Limit Orders** priced at `tracker_entry_price + 0.5 cents` instead of Market Orders.
* **Copying Exit Dumps**: Copying an exit order puts us in the queue *after* the whale has already crashed the price. *Mitigation*: We do not copy exits on Class A; we manage them using our independent time-decay and trailing profit rules.

---

### 13. Milestones / Phases

* **Phase 1: DB Schema & Ingest Poller**
  - Create the `tracked_wallets` table in Supabase.
  - Build the Gamma API polling worker in Python.
* **Phase 2: Slippage & Dual-Track Routing**
  - Implement the Class A / Class B routing check.
  - Implement limit order pricing and slippage validation.
* **Phase 3: Integration & Paper Testing**
  - Integrate with [risk_engine.py](file:///C:/Users/ash74/OneDrive/Desktop/Polymarket/risk/risk_engine.py).
  - Run 20+ copy-trades in paper trading mode to verify slippage limits.

---

### 14. Success Metrics
* **Win Rate**: $\ge$ 60% resolved copy-trades ending in profit.
* **Average Slippage**: $\le$ 0.5 cents between the tracker's entry price and our execution price.
* **Zero Circuit Breaker Triggers**: Zero drawdown halt conditions caused by copying erroneous trades.

---

### 15. Open Questions
* **Private Transactions**: If a top trader begins using private RPC routes (like Flashbots) or OTC desks to hide their orders, how will we adjust? (Current assumption: most Polymarket volume still settles on-chain through public CLOB contracts).
