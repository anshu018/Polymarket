import os
import sys
import asyncio
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env.test")
load_dotenv(dotenv_path=env_path, override=True)

import config
from memory.supabase_client import get_client

async def test_layer1():
    print("--- LAYER 1 TESTS ---")
    
    # 1.10 - Env vars loaded correctly
    req_vars = [
        "SUPABASE_URL", "SUPABASE_KEY", "OPENROUTER_API_KEY", 
        "NVIDIA_API_KEY", "DEEPSEEK_API_KEY", "POLYMARKET_PRIVATE_KEY", 
        "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"
    ]
    missing = [v for v in req_vars if not os.environ.get(v) or os.environ.get(v) == "placeholder" or os.environ.get(v) == "test_placeholder"]
    if missing:
        print(f"[FAIL] 1.10 - Missing or placeholder vars: {missing}")
        return False
    print("[PASS] 1.10 - All env vars strictly present and non-placeholder")

    try:
        client = await get_client()
        # 1.9 - Connection success (if we reached here & got results, we connected)
        print("[PASS] 1.9 - Supabase connection succeeds from Python.")

        tables_to_check = {
            "open_positions": "id, market_id, market_question, direction, entry_price, position_size_usdc, strategy, agent_estimate, confidence_at_entry, kelly_fraction_used, category, idempotency_uuid, opened_at, last_checked_at",
            "closed_trades": "id, market_id, market_question, direction, entry_price, exit_price, position_size_usdc, pnl_usdc, pnl_percent, strategy, agent_estimate, confidence_at_entry, brier_contribution, category, outcome, exit_reason, opened_at, closed_at, was_memoryless, notes",
            "agent_memory": "id, category, lesson, trigger_condition, severity, confidence_score, relevant_trades_since_last_trigger, reinforcement_count, recently_validated_at, retired, created_at, last_triggered_at, superseded_by",
            "resolution_keyword_cache": "id, market_id, market_question, resolution_keywords, resolution_conditions, resolution_type, ambiguity_score, cached_at, last_used_at",
            "idempotency_log": "id, market_id, direction, intended_size_usdc, status, polymarket_order_id, created_at, confirmed_at, failure_reason",
            "layer_c_category_versions": "id, category, avg_resolution_ambiguity_score, recommended_confidence_threshold, known_resolution_traps, historical_edge_percent, notes, valid_from, superseded_by",
            "market_signals": "id, raw_headline, source_url, source_name, category, confidence_score, affected_market_ids, event_type, passed_fast_path, action_taken, discard_reason, detected_at, processed_at",
            "daily_performance": "id, date, starting_balance_usdc, ending_balance_usdc, daily_pnl_usdc, daily_pnl_percent, trades_executed, trades_won, trades_lost, brier_score_rolling, health_score, circuit_breaker_fires, signals_detected, signals_traded, created_at"
        }

        all_tables_exist = True
        for t_name, cols in tables_to_check.items():
            try:
                res = client.table(t_name).select(cols).limit(0).execute()
                print(f"[PASS] 1.1 through 1.8 - {t_name} schema verified.")
            except Exception as e:
                print(f"[FAIL] 1.1 through 1.8 - {t_name} schema verification failed: {str(e)[:100]}...")
                all_tables_exist = False

    except Exception as e:
        print(f"[FAIL] 1.9 - Supabase connection failed: {e}")
        return False

    return True

if __name__ == "__main__":
    asyncio.run(test_layer1())
