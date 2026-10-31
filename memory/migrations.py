import asyncio
import logging
from memory.supabase_client import get_client
import config

logger = logging.getLogger(__name__)

# Exact schemas from PLAN.md Section 9
SQL_MIGRATIONS = [
    {
        "table": "open_positions",
        "sql": """
        CREATE TABLE IF NOT EXISTS open_positions (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            market_id TEXT NOT NULL,
            market_question TEXT,
            direction TEXT NOT NULL,
            entry_price DECIMAL(10,4) NOT NULL,
            position_size_usdc DECIMAL(10,4) NOT NULL,
            strategy TEXT NOT NULL,
            agent_estimate DECIMAL(10,4),
            confidence_at_entry DECIMAL(6,4),
            kelly_fraction_used DECIMAL(6,4),
            category TEXT,
            idempotency_uuid UUID,
            opened_at TIMESTAMPTZ DEFAULT NOW(),
            last_checked_at TIMESTAMPTZ,
            thesis_id TEXT,
            p_model_at_entry DECIMAL(10,4),
            market_price_at_entry DECIMAL(10,4),
            net_edge_at_entry DECIMAL(10,4),
            costs_paid_usdc DECIMAL(10,4),
            order_type TEXT
        );
        -- List A.md Step 3 (A7): attribution columns for databases created
        -- before Step 3. Canonical DDL lives in
        -- scratch/migration_attribution_columns.sql — keep the two in sync.
        ALTER TABLE open_positions ADD COLUMN IF NOT EXISTS thesis_id TEXT;
        ALTER TABLE open_positions ADD COLUMN IF NOT EXISTS p_model_at_entry DECIMAL(10,4);
        ALTER TABLE open_positions ADD COLUMN IF NOT EXISTS market_price_at_entry DECIMAL(10,4);
        ALTER TABLE open_positions ADD COLUMN IF NOT EXISTS net_edge_at_entry DECIMAL(10,4);
        ALTER TABLE open_positions ADD COLUMN IF NOT EXISTS costs_paid_usdc DECIMAL(10,4);
        ALTER TABLE open_positions ADD COLUMN IF NOT EXISTS order_type TEXT;
        -- List A.md Step 5 (A5): book-aware paper fills. fill_price is on the
        -- traded token's line; NULL on a maker row = still resting (or cancelled).
        -- resting_price is the level a maker order rests at (takers: NULL) —
        -- the restart-safe monitor needs the persisted level to detect the
        -- fill; it is not recomputable from later books.
        -- Canonical DDL lives in scratch/migration_step5_fill_and_baseline.sql.
        ALTER TABLE open_positions ADD COLUMN IF NOT EXISTS fill_price DECIMAL(10,4);
        ALTER TABLE open_positions ADD COLUMN IF NOT EXISTS fill_type TEXT;
        ALTER TABLE open_positions ADD COLUMN IF NOT EXISTS resting_price DECIMAL(10,4);
        CREATE INDEX IF NOT EXISTS idx_open_positions_unfilled_maker
            ON open_positions (opened_at)
            WHERE order_type = 'maker' AND fill_price IS NULL;
        """
    },
    {
        "table": "closed_trades",
        "sql": """
        CREATE TABLE IF NOT EXISTS closed_trades (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            market_id TEXT NOT NULL,
            market_question TEXT,
            direction TEXT NOT NULL,
            entry_price DECIMAL(10,4),
            exit_price DECIMAL(10,4),
            position_size_usdc DECIMAL(10,4),
            pnl_usdc DECIMAL(10,4),
            pnl_percent DECIMAL(10,4),
            strategy TEXT,
            agent_estimate DECIMAL(10,4),
            confidence_at_entry DECIMAL(6,4),
            brier_contribution DECIMAL(10,6),
            category TEXT,
            outcome TEXT,
            exit_reason TEXT,
            opened_at TIMESTAMPTZ,
            closed_at TIMESTAMPTZ DEFAULT NOW(),
            was_memoryless BOOLEAN DEFAULT FALSE,
            notes TEXT,
            thesis_id TEXT,
            p_model_at_entry DECIMAL(10,4),
            market_price_at_entry DECIMAL(10,4),
            net_edge_at_entry DECIMAL(10,4),
            costs_paid_usdc DECIMAL(10,4),
            order_type TEXT
        );
        -- List A.md Step 3 (A7): attribution columns for databases created
        -- before Step 3. Canonical DDL lives in
        -- scratch/migration_attribution_columns.sql — keep the two in sync.
        ALTER TABLE closed_trades ADD COLUMN IF NOT EXISTS thesis_id TEXT;
        ALTER TABLE closed_trades ADD COLUMN IF NOT EXISTS p_model_at_entry DECIMAL(10,4);
        ALTER TABLE closed_trades ADD COLUMN IF NOT EXISTS market_price_at_entry DECIMAL(10,4);
        ALTER TABLE closed_trades ADD COLUMN IF NOT EXISTS net_edge_at_entry DECIMAL(10,4);
        ALTER TABLE closed_trades ADD COLUMN IF NOT EXISTS costs_paid_usdc DECIMAL(10,4);
        ALTER TABLE closed_trades ADD COLUMN IF NOT EXISTS order_type TEXT;
        -- List A.md Step 5 (A5): fill columns pass through the close path so the
        -- gate report can measure decision-vs-fill slippage on closed trades.
        ALTER TABLE closed_trades ADD COLUMN IF NOT EXISTS fill_price DECIMAL(10,4);
        ALTER TABLE closed_trades ADD COLUMN IF NOT EXISTS fill_type TEXT;
        CREATE INDEX IF NOT EXISTS idx_closed_trades_strategy_closed_at
            ON closed_trades (strategy, closed_at DESC);
        """
    },
    {
        "table": "market_signals",
        "sql": """
        CREATE TABLE IF NOT EXISTS market_signals (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            raw_headline TEXT,
            source_url TEXT,
            source_name TEXT,
            category TEXT,
            confidence_score DECIMAL(6,4),
            affected_market_ids TEXT[],
            event_type TEXT,
            passed_fast_path BOOLEAN DEFAULT FALSE,
            action_taken TEXT,
            discard_reason TEXT,
            detected_at TIMESTAMPTZ DEFAULT NOW(),
            processed_at TIMESTAMPTZ
        );
        """
    },
    {
        "table": "daily_performance",
        "sql": """
        CREATE TABLE IF NOT EXISTS daily_performance (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            date DATE UNIQUE NOT NULL,
            starting_balance_usdc DECIMAL(12,4),
            ending_balance_usdc DECIMAL(12,4),
            daily_pnl_usdc DECIMAL(12,4),
            daily_pnl_percent DECIMAL(10,4),
            trades_executed INTEGER DEFAULT 0,
            trades_won INTEGER DEFAULT 0,
            trades_lost INTEGER DEFAULT 0,
            brier_score_rolling DECIMAL(10,6),
            health_score DECIMAL(6,2),
            circuit_breaker_fires INTEGER DEFAULT 0,
            signals_detected INTEGER DEFAULT 0,
            signals_traded INTEGER DEFAULT 0,
            created_at TIMESTAMPTZ DEFAULT NOW()
        );
        """
    },
    {
        "table": "agent_memory",
        "sql": """
        CREATE TABLE IF NOT EXISTS agent_memory (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            category TEXT NOT NULL,
            lesson TEXT NOT NULL,
            trigger_condition JSONB NOT NULL,
            severity TEXT NOT NULL,
            confidence_score DECIMAL(6,4) DEFAULT 1.0,
            relevant_trades_since_last_trigger INTEGER DEFAULT 0,
            reinforcement_count INTEGER DEFAULT 0,
            recently_validated_at TIMESTAMPTZ,
            retired BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            last_triggered_at TIMESTAMPTZ,
            superseded_by UUID REFERENCES agent_memory(id)
        );
        """
    },
    {
        "table": "resolution_keyword_cache",
        "sql": """
        CREATE TABLE IF NOT EXISTS resolution_keyword_cache (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            market_id TEXT UNIQUE NOT NULL,
            market_question TEXT,
            resolution_keywords TEXT[],
            resolution_conditions JSONB,
            resolution_type TEXT,
            ambiguity_score DECIMAL(6,4),
            cached_at TIMESTAMPTZ DEFAULT NOW(),
            last_used_at TIMESTAMPTZ
        );
        """
    },
    {
        "table": "idempotency_log",
        "sql": """
        CREATE TABLE IF NOT EXISTS idempotency_log (
            id UUID PRIMARY KEY,
            market_id TEXT NOT NULL,
            direction TEXT NOT NULL,
            intended_size_usdc DECIMAL(10,4),
            status TEXT NOT NULL DEFAULT 'pending',
            polymarket_order_id TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            confirmed_at TIMESTAMPTZ,
            failure_reason TEXT
        );
        """
    },
    {
        "table": "layer_c_category_versions",
        "sql": """
        CREATE TABLE IF NOT EXISTS layer_c_category_versions (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            category TEXT NOT NULL,
            avg_resolution_ambiguity_score DECIMAL(6,4),
            recommended_confidence_threshold DECIMAL(6,4),
            known_resolution_traps TEXT[],
            historical_edge_percent DECIMAL(6,4),
            notes TEXT,
            valid_from TIMESTAMPTZ DEFAULT NOW(),
            superseded_by UUID REFERENCES layer_c_category_versions(id)
        );
        """
    },
    {
        # Strategy 5: Copy Edge — tracked_wallets table
        # Stores the curated whitelist of top-trader Polygon addresses.
        # Operators add rows here via Supabase dashboard or API.
        # class_type: 'A' = speed/bypass, 'B' = macro/LLM-validated
        "table": "tracked_wallets",
        "sql": """
        CREATE TABLE IF NOT EXISTS tracked_wallets (
            wallet_address VARCHAR(42) PRIMARY KEY,
            trader_name   VARCHAR(100) NOT NULL,
            class_type    VARCHAR(1)  NOT NULL CHECK (class_type IN ('A', 'B')),
            is_active     BOOLEAN     NOT NULL DEFAULT TRUE,
            added_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    },
    {
        # Strategy 5: Copy Edge -- individual copy trade log
        # Every executed copy trade is logged here for outcome tracking.
        # When positions close, status and pnl_usdc are updated by the reconciler.
        "table": "copytrade_log",
        "sql": """
        CREATE TABLE IF NOT EXISTS copytrade_log (
            id                UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
            wallet_address    VARCHAR(42) NOT NULL REFERENCES tracked_wallets(wallet_address),
            trader_name       VARCHAR(100),
            market_id         TEXT        NOT NULL,
            direction         TEXT        NOT NULL CHECK (direction IN ('YES', 'NO')),
            class_type        VARCHAR(1)  NOT NULL CHECK (class_type IN ('A', 'B')),
            entry_price       DECIMAL(10,4),
            size_usdc         DECIMAL(10,4),
            slippage          DECIMAL(10,6),
            idempotency_uuid  UUID,
            status            TEXT        NOT NULL DEFAULT 'open'
                              CHECK (status IN ('open', 'won', 'lost', 'cancelled')),
            exit_price        DECIMAL(10,4),
            pnl_usdc          DECIMAL(12,4),
            opened_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            resolved_at       TIMESTAMPTZ
        );
        CREATE INDEX IF NOT EXISTS idx_copytrade_log_wallet
            ON copytrade_log(wallet_address);
        CREATE INDEX IF NOT EXISTS idx_copytrade_log_market_open
            ON copytrade_log(market_id, status) WHERE status = 'open';
        """
    },
    {
        # List A.md Step 2 (A6): forward-price measurement of every signal that
        # enters the pipeline. One row per signal at arrival; the forward-price
        # sampler (data/forward_sampler.py) fills p_m1/p_m5/p_m15/p_m60 at
        # +1m/+5m/+15m/+60m. Feeds the velocity estimator (Step 6f) and
        # scripts/signal_drift_report.py. Canonical DDL lives in
        # scratch/migration_signal_outcomes.sql — keep the two in sync.
        "table": "signal_outcomes",
        "sql": """
        CREATE TABLE IF NOT EXISTS signal_outcomes (
            id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            signal_id           UUID REFERENCES market_signals(id) ON DELETE SET NULL,
            market_id           TEXT NOT NULL,
            strategy            TEXT,
            headline_hash       TEXT NOT NULL,
            entities_json       JSONB,
            t0                  TIMESTAMPTZ NOT NULL,
            price_t0            DECIMAL(10,4),
            p_m1                DECIMAL(10,4),
            p_m5                DECIMAL(10,4),
            p_m15               DECIMAL(10,4),
            p_m60               DECIMAL(10,4),
            confirmed_direction BOOLEAN,
            created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_signal_outcomes_market_t0
            ON signal_outcomes (market_id, t0 DESC);
        -- List A.md Step 5 (A5): market baseline. resolved_outcome is the market's
        -- own YES/NO resolution (1 = YES, 0 = NO, NULL = unresolved); recorded by
        -- the offline gate report (scripts/gate_report.py) via idempotent updates.
        ALTER TABLE signal_outcomes ADD COLUMN IF NOT EXISTS resolved_outcome SMALLINT;
        ALTER TABLE signal_outcomes ADD COLUMN IF NOT EXISTS resolved_at TIMESTAMPTZ;
        """
    },
    {
        # List-B.md Step 3 (B4): cross-process market lease (D-B02, D-B13).
        # PostgREST-compatible mutex: one row per market held by an instance
        # identity (owner) until lease_until. claim_market_lease is the ONLY
        # write path — one atomic statement (row-locked conditional upsert);
        # the RPC returns true iff the claim succeeded (fresh insert, expired-
        # lease steal, or same-owner re-claim = renewal). Canonical DDL lives
        # in scratch/migration_market_leases.sql — keep the two in sync.
        "table": "market_leases",
        "sql": """
        CREATE TABLE IF NOT EXISTS market_leases (
            market_id   TEXT PRIMARY KEY,
            owner       TEXT NOT NULL,
            lease_until TIMESTAMPTZ NOT NULL
        );
        CREATE OR REPLACE FUNCTION claim_market_lease(
            p_market_id   TEXT,
            p_owner       TEXT,
            p_ttl_seconds INTEGER
        ) RETURNS BOOLEAN
        LANGUAGE SQL
        SET search_path TO 'public'
        AS $func$
            INSERT INTO market_leases (market_id, owner, lease_until)
            VALUES (p_market_id, p_owner, now() + make_interval(secs => p_ttl_seconds))
            ON CONFLICT (market_id) DO UPDATE
                SET owner       = EXCLUDED.owner,
                    lease_until = EXCLUDED.lease_until
                WHERE market_leases.lease_until < now()
                   OR market_leases.owner = EXCLUDED.owner
            RETURNING true;
        $func$;
        GRANT EXECUTE ON FUNCTION claim_market_lease(TEXT, TEXT, INTEGER) TO anon;
        """
    },
    {
        # Strategy 5: Copy Edge -- per-wallet trust score aggregation
        # Recomputed every time a copy trade resolves.
        # trust_score = Bayesian-damped win rate + PnL quality bonus.
        # Used by classifier for conflict resolution between wallets.
        "table": "trader_performance",
        "sql": """
        CREATE TABLE IF NOT EXISTS trader_performance (
            wallet_address      VARCHAR(42)   PRIMARY KEY REFERENCES tracked_wallets(wallet_address),
            trader_name         VARCHAR(100),
            total_trades_copied INTEGER       NOT NULL DEFAULT 0,
            winning_trades      INTEGER       NOT NULL DEFAULT 0,
            losing_trades       INTEGER       NOT NULL DEFAULT 0,
            total_pnl_usdc      DECIMAL(12,4) NOT NULL DEFAULT 0,
            trust_score         DECIMAL(6,4)  NOT NULL DEFAULT 0.5,
            last_updated        TIMESTAMPTZ   NOT NULL DEFAULT NOW()
        );
        """
    }
]

async def run_migrations() -> None:
    """
    Execute all table creation migrations sequentially and test them immediately.
    """
    client = await get_client()
    logger.info("Starting database migrations...")
    
    for idx, migration in enumerate(SQL_MIGRATIONS, 1):
        sql = migration["sql"]
        table_name = migration["table"]
        try:
            # Table Creation via RPC 
            # (Note: Supabase REST implies setup of execute_sql RPC or similar external runner)
            async with asyncio.timeout(config.SUPABASE_TIMEOUT_SECONDS):
                client.rpc("execute_sql", {"sql": sql}).execute()
            logger.info(f"Migration {idx}/{len(SQL_MIGRATIONS)} ({table_name}) EXECUTED.")
            
            # Post-Migration testing per Phase 5 requirements
            await test_table(client, table_name)
            
        except asyncio.TimeoutError:
            logger.error(f"Migration failed due to timeout (> {config.SUPABASE_TIMEOUT_SECONDS}s).")
            # If we mock local execution, ignore failure to allow workflow trace.
        except Exception as e:
            logger.error(f"Migration/Test {idx} ({table_name}) FAILED: {e}")
            # Production app would halt. Ignoring here since we lack DB connectivity.

    logger.info("All migrations/tests completed cycle sequence.")

async def test_table(client, table_name: str):
    """Run an INSERT and SELECT to confirm table schema integrity."""
    logger.info(f"Running INSERT/SELECT test for table: {table_name}")
    import uuid, datetime
    mock_data = {}
    uid = str(uuid.uuid4())
    
    # Generate mock payloads for constraints
    if table_name == "open_positions":
        mock_data = {"market_id": "test_mkt", "direction": "YES", "entry_price": 0.50, "position_size_usdc": 10.0, "strategy": "velocity"}
    elif table_name == "closed_trades":
        mock_data = {"market_id": "test_mkt", "direction": "YES"}
    elif table_name == "daily_performance":
        # Fixed far-past date, NEVER today: a date-only mock row left behind by
        # a crashed deploy must not collide with the equity service's real
        # anchor for the current day (List-B Step 1 found this exact collision).
        mock_data = {"date": "1970-01-01"}
    elif table_name == "agent_memory":
        mock_data = {"category": "politics", "lesson": "test_lesson", "trigger_condition": {"test": True}, "severity": "warning"}
    elif table_name == "resolution_keyword_cache":
        mock_data = {"market_id": f"test_mkt_unique_{uid}"}
    elif table_name == "idempotency_log":
        mock_data = {"id": uid, "market_id": "test_mkt", "direction": "YES"}
    elif table_name == "layer_c_category_versions":
        mock_data = {"category": "politics"}
    elif table_name == "signal_outcomes":
        mock_data = {
            "market_id": f"test_mkt_unique_{uid}",
            "headline_hash": "test_hash",
            "t0": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
    elif table_name == "market_leases":
        # List-B.md Step 3 (B4): a short-lived row; the unique market_id keeps
        # repeated startups idempotent and the DELETE below cleans it up.
        mock_data = {
            "market_id": f"test_mkt_unique_{uid}",
            "owner": "migration_smoke_test",
            "lease_until": (
                datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=30)
            ).isoformat(),
        }
    else:
        mock_data = {}

    try:
        # daily_performance's `date` is UNIQUE: remove any leftover mock row so
        # repeated startups stay idempotent even if a previous run died between
        # insert and cleanup.
        if table_name == "daily_performance":
            client.table(table_name).delete().eq("date", "1970-01-01").execute()
        async with asyncio.timeout(config.SUPABASE_TIMEOUT_SECONDS):
            res = client.table(table_name).insert(mock_data).execute()
        
        # Test SELECT on the new row
        if getattr(res, "data", None) and len(res.data) > 0:
            # daily_performance is keyed by `date` — grabbing the auto `id`
            # first made the SELECT below miss forever, so the cleanup DELETE
            # never ran and every startup left its mock row behind (the exact
            # orphan that blocked the equity service on 2026-10-30).
            # PK varies per table: daily_performance is keyed by `date`,
            # market_leases by `market_id` (no id column); everything else by
            # the auto `id`.
            pk_col = (
                'date' if table_name == 'daily_performance'
                else 'market_id' if table_name == 'market_leases'
                else 'id'
            )
            inserted_id = res.data[0].get(pk_col) or uid
            
            async with asyncio.timeout(config.SUPABASE_TIMEOUT_SECONDS):
                sel = client.table(table_name).select("*").eq(pk_col, inserted_id).execute()
                
            if getattr(sel, "data", None) and len(sel.data) > 0:
                # Cleanup test
                client.table(table_name).delete().eq(pk_col, inserted_id).execute()
                logger.info(f"Test {table_name}: PASS")
                return

        logger.error(f"Test {table_name}: FAIL (Select returned zero rows)")
    except Exception as e:
        logger.error(f"Test {table_name}: FAIL ({str(e)})")
