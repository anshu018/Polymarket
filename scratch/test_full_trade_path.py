import os
import sys
import asyncio
import logging
from datetime import datetime, timezone
from unittest.mock import patch

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv(".env")

import config
import coordinator.pipeline
import data.market_discovery
from coordinator.pipeline import run_pipeline
from llm.trade_decision import TradeDecisionOutput

# Initialize basic logging to see pipeline stdout
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s"
)
logger = logging.getLogger("test_full_trade_path")

# ─────────────────────────────────────────────
# MOCK SUPABASE CLIENT FOR OFFLINE SUCCESS
# ─────────────────────────────────────────────

class MockSupabaseTable:
    def __init__(self, table_name, db_state):
        self.table_name = table_name
        self.db_state = db_state

    def select(self, cols="*"):
        return self

    def eq(self, col, val):
        return self

    def is_(self, col, val):
        return self

    def order(self, col, desc=True):
        return self

    def limit(self, val):
        return self

    def execute(self):
        class Result:
            def __init__(self, data):
                self.data = data
        
        # Return mock rows
        if self.table_name == "layer_c_category_versions":
            return Result([{
                "avg_resolution_ambiguity_score": 0.15,
                "recommended_confidence_threshold": 0.75,
                "historical_edge_percent": 0.08
            }])
        elif self.table_name == "agent_memory":
            return Result([])
        elif self.table_name == "open_positions":
            return Result([])
        elif self.table_name == "idempotency_log":
            return Result([])
        return Result([])

    def insert(self, data):
        logger.info(f"[MOCK Supabase] Inserting into {self.table_name}: {data}")
        self.db_state[self.table_name].append(data)
        return self

    def update(self, data):
        logger.info(f"[MOCK Supabase] Updating {self.table_name}: {data}")
        self.db_state[self.table_name].append({"action": "update", "data": data})
        return self

class MockSupabaseClient:
    def __init__(self):
        self.db_state = {
            "idempotency_log": [],
            "open_positions": [],
            "market_signals": []
        }

    def table(self, table_name):
        return MockSupabaseTable(table_name, self.db_state)

async def mock_get_client():
    return MockSupabaseClient()

# ─────────────────────────────────────────────
# MOCK MARKET PRICE / METADATA
# ─────────────────────────────────────────────

async def mock_get_market_price(token_id):
    logger.info(f"[MOCK] Returning mock price 0.55 for token {token_id}")
    return 0.55

async def mock_get_market_metadata(market_id):
    logger.info(f"[MOCK] Returning mock metadata for market {market_id}")
    return {
        "question": "Will Donald Trump be impeached in 2026?",
        "description": "Resolves YES if house votes to impeach.",
        "resolution_criteria": "Resolves YES if house votes to impeach."
    }

# ─────────────────────────────────────────────
# MOCK DECIDE TRADE TO YES
# ─────────────────────────────────────────────

async def mock_decide_trade(*args, **kwargs):
    logger.info("[MOCK] Forcing Trade Decision to YES")
    return TradeDecisionOutput(
        direction="YES",
        confidence_score=0.88,
        reasoning="Mocked trade decision forcing YES for testing execution path"
    ), False

# ─────────────────────────────────────────────
# TEST EXECUTION
# ─────────────────────────────────────────────

async def run_test():
    logger.info("Starting E2E Local Execution Test...")

    # Seed the local market discovery cache
    data.market_discovery._MARKET_CACHE = [
        {
            "market_id": "Politics-Trump-Impeach-001",
            "question": "Will Donald Trump be impeached in 2026?",
            "token_id": "mock-token-politics-impeach",
            "end_date_iso": "2026-12-31T23:59:59Z",
            "volume_usd": 15000.0
        }
    ]
    data.market_discovery._CACHE_UPDATED_AT = datetime.now(timezone.utc)

    # Headline that will match the politics question with high confidence
    headline = "BREAKING: US House of Representatives holds official vote and impeaches Donald Trump"
    source = "AP News"

    # Patch Supabase client & Polymarket pricing & Trade Decision
    with patch("coordinator.pipeline.get_client", mock_get_client), \
         patch("llm.trade_decision.get_client", mock_get_client), \
         patch("memory.supabase_client.get_client", mock_get_client), \
         patch("coordinator.pipeline.get_market_price", mock_get_market_price), \
         patch("coordinator.pipeline.get_market_metadata", mock_get_market_metadata), \
         patch("coordinator.pipeline.decide_trade", mock_decide_trade):
         
        logger.info(f"Running pipeline on headline: '{headline}'")
        res = await run_pipeline(
            headline=headline,
            source=source,
            portfolio_value=10000.0
        )
        
        logger.info("Pipeline execution completed.")
        print("\n--- PIPELINE RESULT ---")
        import pprint
        pprint.pprint(res)
        print("-----------------------\n")
        
        # Verify the trade outcome
        if res and res.get("status") == "success":
            print("VERDICT: PASS")
        else:
            print(f"VERDICT: FAIL - Pipeline status is {res.get('status') if res else 'None'}")

if __name__ == "__main__":
    asyncio.run(run_test())
