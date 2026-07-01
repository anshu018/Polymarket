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


async def _market_cache_loop() -> None:
    """Refresh market discovery cache every 5 minutes indefinitely."""
    from data.market_discovery import refresh_market_cache, _MARKET_CACHE
    # Wait 300 seconds first, because we already ran an initial refresh on startup
    await asyncio.sleep(300)
    while True:
        try:
            await refresh_market_cache()
            cache_size = len(_MARKET_CACHE)
            logger.info(
                f"[MARKET_DISCOVERY] Background cache refreshed: {cache_size} markets"
            )
        except Exception as e:
            logger.error(f"[MAIN] Market cache refresh failed: {e}")
        await asyncio.sleep(300)


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
        from data.market_discovery import refresh_market_cache, _MARKET_CACHE
        logger.info("Performing initial market cache refresh...")
        await refresh_market_cache()
        cache_size = len(_MARKET_CACHE)
        if cache_size < 10:
            logger.critical(
                f"[MARKET_CACHE] STARTUP: Cache has {cache_size} markets "
                f"(under 10). This is a primary trade blocker."
            )
        else:
            logger.info(
                f"[MARKET_DISCOVERY] Startup cache loaded: {cache_size} markets"
            )

        # Start background market cache loop
        asyncio.create_task(_market_cache_loop(), name="market_cache_refresher")

        # Start Strategy 5: Copy Edge (CopyTrade) engine
        from copytrade.poller import run_copy_poller
        from copytrade.classifier import run_classifier
        from copytrade.executor import run_class_a_executor, run_class_b_executor
        _copy_signal_queue = asyncio.Queue(maxsize=config.COPY_SIGNAL_QUEUE_MAXSIZE)
        _copy_queue_a = asyncio.Queue(maxsize=config.COPY_EXECUTION_QUEUE_MAXSIZE)
        _copy_queue_b = asyncio.Queue(maxsize=config.COPY_EXECUTION_QUEUE_MAXSIZE)
        asyncio.create_task(run_copy_poller(_copy_signal_queue), name="copy_poller")
        asyncio.create_task(run_classifier(_copy_signal_queue, _copy_queue_a, _copy_queue_b), name="copy_classifier")
        asyncio.create_task(run_class_a_executor(_copy_queue_a), name="copy_executor_a")
        asyncio.create_task(run_class_b_executor(_copy_queue_b), name="copy_executor_b")
        logger.info("[COPY_EDGE] Strategy 5 CopyTrade engine started (poller + classifier + 2 executors).")

        # Validate News Analyst models before starting signal processing
        from llm.news_analyst import validate_models
        logger.info("Validating news analyst models...")
        await validate_models()
        logger.info("News analyst models validated successfully.")

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
            raise
        except Exception as pipeline_exc:
            logger.critical(f"Continuous signal pipeline crashed: {pipeline_exc}", exc_info=True)
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
