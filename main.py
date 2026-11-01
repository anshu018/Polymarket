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

        # 0. Liveness endpoint FIRST (crash hardening, 2026-11-01): the Docker
        # HEALTHCHECK curls it. Serving from the very top covers the whole
        # initialization window (spaCy load, migrations, reconciliation) —
        # with the old `import main` probe, 3 slow checks during startup or a
        # signal burst made Docker kill the container (the hard-kill crash
        # signature of 2026-10-30/31). It runs on THIS event loop: a hung
        # loop cannot answer → restart. True liveness, near-zero cost.
        from monitoring.health_server import run_health_server
        asyncio.create_task(run_health_server(), name="health_server")
        logger.info("[HEALTH_SERVER] Liveness endpoint task started (first task).")

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

        # 4b. Singleton instance guard (List-B.md Step 3 — B4). Claims the
        # global __singleton__ lease BEFORE reconciliation or any trading
        # task starts: two overlapping instances must never reconcile or
        # trade concurrently. A second instance waits (retrying every 30s)
        # up to SINGLETON_CLAIM_MAX_WAIT_SECONDS, then alerts and exits(1) —
        # Railway's ALWAYS restart policy makes that a supervised retry
        # cycle. A hard-killed holder cannot release, so a legitimate
        # restart waits out the remaining ≤90s TTL by design (documented in
        # coordinator/lease.py). The renewer task starts immediately after a
        # successful claim and joins the supervised set (E-B13); a definitive
        # lease loss later is split-brain and exits the process.
        from coordinator.lease import acquire_singleton_or_exit, run_singleton_lease_renewer
        await acquire_singleton_or_exit()
        asyncio.create_task(run_singleton_lease_renewer(), name="singleton_lease_renewer")
        logger.info("[SINGLETON] Instance guard active (renewer task started).")

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

        # Start the Step 2 forward-price sampler (List A.md A6). Consumes the
        # instrumentation jobs the pipeline hook enqueues for every signal and
        # records +1m/+5m/+15m/+60m prices into signal_outcomes — the data
        # clock behind the velocity estimator and the drift report.
        from data.forward_sampler import run_forward_sampler_supervisor
        asyncio.create_task(run_forward_sampler_supervisor(), name="forward_price_sampler")
        logger.info("[FORWARD_SAMPLER] Forward-price sampler started (supervisor task).")

        # Start the Step 5 paper maker-fill monitor (List A.md A5). Resolves
        # resting paper maker orders against the live book — fills on a
        # through-trade, falls back to taker if still net-positive after
        # MAKER_FALLBACK_SECONDS, else cancels. State lives in open_positions
        # (restart-safe re-scan), so nothing is lost on restart.
        from execution.paper_fills import run_paper_fill_monitor
        asyncio.create_task(run_paper_fill_monitor(), name="paper_fill_monitor")
        logger.info("[PAPER_FILLS] Paper maker-fill monitor started (supervisor task).")

        # Start the Step 1 equity snapshot supervisor (List-B.md B2). Marks the
        # portfolio to market and upserts daily_performance anchors every
        # EQUITY_SNAPSHOT_INTERVAL_SECONDS; entry paths read only the cached
        # result and fail closed when it is stale or absent.
        from coordinator.equity import run_equity_snapshot_supervisor
        asyncio.create_task(run_equity_snapshot_supervisor(), name="equity_snapshot")
        logger.info("[EQUITY] Equity snapshot supervisor started (supervisor task).")

        # Start the Step 2 health evaluation supervisor (List-B.md B1). Computes
        # the six component scores, persists daily_performance.health_score,
        # caches the enforcement mode, and fires mode-transition / probation /
        # Brier-crossing alerts every HEALTH_CHECK_INTERVAL_SECONDS.
        from monitoring.health_score import run_health_check_supervisor
        asyncio.create_task(run_health_check_supervisor(), name="health_check")
        logger.info("[HEALTH] Health check supervisor started (supervisor task).")

        # Start the Step 4 runtime reconciliation supervisor (List-B.md B3).
        # Every RUNTIME_RECON_INTERVAL_SECONDS: resolved positions close
        # through the ONE shared close path under the Step 3 market lease,
        # stuck pending idempotency entries are swept (D-B14), and in-flight
        # counts are logged + digested daily. Additive and fail-soft — the
        # startup reconciliation still gates all trading (E-B13, unchanged).
        from execution.runtime_reconciliation import run_runtime_reconciliation
        asyncio.create_task(run_runtime_reconciliation(), name="runtime_reconciliation")
        logger.info("[RECON] Runtime reconciliation supervisor started (supervisor task).")

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
