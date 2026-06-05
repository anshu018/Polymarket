import asyncio
import logging
import sys

# 1. Load and validate config (fails fast if missing env variables)
try:
    import config
    from memory.supabase_client import get_client
    from memory.migrations import run_migrations
    from monitoring.telegram_alerts import alert_startup
    from execution.polymarket_auth import initialize_polymarket_client
except Exception as e:
    import sys
    sys.stderr.write(f"Failed to load system config or startup libraries: {e}\n")
    sys.exit(1)

# 2. Initialize structured logging
import logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(name)s | %(levelname)s | %(message)s',
    datefmt='%Y-%m-%dT%H:%M:%S%z'
)
logger = logging.getLogger("main")


async def _market_cache_loop():
    """Background loop to periodically refresh the market discovery cache."""
    from data.market_discovery import refresh_market_cache
    import config
    logger.info("[MARKET_CACHE] Starting background cache loop...")
    while True:
        try:
            await asyncio.sleep(config.MARKET_CACHE_REFRESH_INTERVAL_SECONDS)
            logger.info("[MARKET_CACHE] Refreshing cache in background...")
            await refresh_market_cache()
        except asyncio.CancelledError:
            logger.info("[MARKET_CACHE] Background cache loop cancelled.")
            raise
        except Exception as e:
            logger.error(f"[MARKET_CACHE] Error in background cache loop: {e}")


async def main():
    try:
        logger.info("Starting up Polymarket Agent (Layer 1 Foundation)...")
        
        # 3. Derive Polymarket credentials
        try:
            await initialize_polymarket_client()
        except Exception:
            logger.critical("CRITICAL: Startup halted at credential derivation")
            sys.exit(1)

        # 4. Run Supabase migrations (IF NOT EXISTS — safe) and test tables
        await run_migrations()
        
        # 4. Verify Supabase connection (SELECT 1 fallback logic)
        client = await get_client()
        try:
            async with asyncio.timeout(config.SUPABASE_TIMEOUT_SECONDS):
                client.table('open_positions').select('*').limit(1).execute()
            logger.info("Supabase connection verified.")
        except asyncio.TimeoutError:
            logger.error("Supabase connection timed out during startup verification.")
            # In a true deployment, this halts execution.
        except Exception as e:
            logger.error(f"Supabase connection verification failed: {e}")

        # 5. Layer 7: startup reconciliation runs here
        from execution.reconciliation import reconcile_on_startup
        try:
            await reconcile_on_startup()
        except Exception as reconciliation_error:
            logger.critical(f"Startup reconciliation failed or halted: {reconciliation_error}")
            sys.exit(1)

        # 6. Send startup Telegram alert
        await alert_startup(
            environment=config.ENVIRONMENT,
            paper_trading=config.PAPER_TRADING
        )
        logger.info("Startup alert dispatched to Telegram.")

        # Populate market cache once on startup
        from data.market_discovery import refresh_market_cache
        logger.info("Performing initial market cache refresh...")
        await refresh_market_cache()

        # Start background market cache loop
        cache_loop_task = asyncio.create_task(_market_cache_loop(), name="market_cache_loop")

        # 7. Expected log sequence: "[AGENT] Beginning signal processing."
        logger.info("[AGENT] Beginning signal processing.")

        # 8. Start signal pipeline task
        from data.pipeline import run_pipeline
        pipeline_task = asyncio.create_task(run_pipeline(), name="signal_pipeline")

        # Keep main running continuously, catch components crashes
        try:
            await pipeline_task
        except asyncio.CancelledError:
            logger.info("Signal pipeline task cancelled. Shutting down...")
            cache_loop_task.cancel()
            try:
                await cache_loop_task
            except asyncio.CancelledError:
                pass
            raise
        except Exception as pipeline_exc:
            logger.critical(f"Continuous signal pipeline crashed: {pipeline_exc}", exc_info=True)
            cache_loop_task.cancel()
            from monitoring.telegram_alerts import alert_pipeline_component_crash
            await alert_pipeline_component_crash(
                component="main.continuous_pipeline",
                error_type=type(pipeline_exc).__name__,
                error_detail=str(pipeline_exc)[:200]
            )
            sys.exit(1)

    except Exception as e:
        logger.critical(f"Startup completely failed: {e}")
        from monitoring.telegram_alerts import alert_pipeline_component_crash
        try:
            await alert_pipeline_component_crash(
                component="main.startup",
                error_type=type(e).__name__,
                error_detail=str(e)[:200]
            )
        except Exception as alert_err:
            logger.error(f"Failed to send startup crash alert: {alert_err}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
