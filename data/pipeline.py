"""
data/pipeline.py — Async signal processing pipeline.

Chains: RSS poller → spaCy pre-filter → News Analyst.
Starts exactly NUM_WORKERS (3) concurrent processing coroutines.

Architecture:
    run_pipeline()     — Public entry point. Creates queue, spawns poller +
                         workers via _run_workers(). Never raises to caller.
    _run_workers()     — Spawns exactly NUM_WORKERS _process_loop tasks.
    _process_loop()    — Per-worker loop: dequeue → filter → classify.
                         Always calls queue.task_done() in finally block.

Invariants (per GEMINI.md):
    - Queue created with asyncio.Queue(maxsize=PIPELINE_QUEUE_MAXSIZE)
    - Workers read via: await queue.get() — never put_nowait anywhere
    - queue.task_done() called unconditionally in each worker's finally block
    - alert_pipeline_component_crash() fired on any top-level unhandled exception
    - Never raises to caller
"""

import asyncio
import logging

import config
from data.rss_poller import start_poller
from data.spacy_filter import filter_signal
from coordinator.pipeline import run_pipeline as run_trading_pipeline
from monitoring.telegram_alerts import alert_pipeline_component_crash

logger = logging.getLogger(__name__)

# Exactly 3 workers per specification
NUM_WORKERS: int = 3


async def _process_loop(queue: asyncio.Queue) -> None:
    """
    Worker coroutine: pulls articles from the shared queue and runs each
    through the spaCy pre-filter then the News Analyst.

    Runs indefinitely until cancelled.  Handles ALL internal exceptions so
    the worker never terminates due to a single bad article.  Always calls
    queue.task_done() in the finally block, even on exception or early exit.

    Args:
        queue: Shared queue populated by the RSS poller.
    """
    while True:
        article: dict = await queue.get()
        headline: str = article.get("headline", "")
        source: str = article.get("source_name", "")

        try:
            # --- Stage 1: spaCy pre-filter -----------------------------------
            passed: bool = await filter_signal(headline, source)
            if not passed:
                logger.debug(
                    "[PIPELINE] spaCy blocked: %.60s",
                    headline,
                )
            else:
                # --- Stage 2: Coordinated Integration Pipeline ----------------
                snippet = headline[:60]
                logger.info("[PIPELINE] Routing signal to coordinator | Headline: '%s'", snippet)
                
                # Run the trading pipeline
                result = await run_trading_pipeline(headline=headline, source=source)
                
                # Check outcome and log/update Supabase
                outcome = "skipped"
                detail = None
                if result:
                    status = result.get("status")
                    if status == "success":
                        outcome = "traded"
                    elif status == "blocked":
                        outcome = "risk-blocked"
                        detail = f"risk-blocked: {result.get('reason')}"
                    elif status == "exit_triggered":
                        outcome = "exit-triggered"
                        detail = f"exit-triggered: {result.get('reason')}"
                else:
                    # News Analyst failed, timed out, or Coordinator decided ABSTAIN
                    outcome = "abstain"
                    detail = "abstain or news analyst timeout/error"
                
                log_outcome = "skipped"
                if outcome == "traded":
                    log_outcome = "traded"
                elif outcome in ("risk-blocked", "exit-triggered"):
                    log_outcome = "risk-blocked"
                
                logger.info("[PIPELINE] Coordinator execution completed | Outcome: %s | Headline: '%s'", log_outcome, snippet)
                
                # Ensure market_signals is written to / updated with the final outcome
                await log_final_signal_status(headline, source, outcome, detail)


        except Exception as exc:
            # Log but never propagate — a single bad article must not kill
            # the worker and starve the queue.
            logger.error(
                "[PIPELINE] Worker error on '%.40s': %s: %s",
                headline,
                type(exc).__name__,
                exc,
                exc_info=True,
            )
            # Record error in market_signals if it passed spaCy filter
            try:
                await log_final_signal_status(headline, source, "error", f"worker exception: {type(exc).__name__}")
            except Exception:
                pass
        finally:
            # Always signal completion so queue.join() (if used) works
            # correctly and the queue's internal counter never diverges.
            queue.task_done()


async def _run_workers(queue: asyncio.Queue) -> None:
    """
    Spawn exactly NUM_WORKERS concurrent _process_loop coroutines as asyncio
    Tasks and await their collective completion.

    Under normal operation the workers run forever, so this coroutine only
    returns if all workers are cancelled (e.g. pipeline shutdown) or one
    raises an unhandled exception that escapes _process_loop's own guard.

    Args:
        queue: Shared queue to pass to each worker.
    """
    worker_tasks = [
        asyncio.create_task(
            _process_loop(queue),
            name=f"signal_worker_{i}",
        )
        for i in range(NUM_WORKERS)
    ]
    logger.info("[PIPELINE] %d workers started.", NUM_WORKERS)
    await asyncio.gather(*worker_tasks)


async def run_pipeline() -> None:
    """
    Start the full data pipeline and run it indefinitely.

    Flow:
        1. Creates asyncio.Queue(maxsize=PIPELINE_QUEUE_MAXSIZE).
        2. Spawns start_poller (RSS poller) as a background Task.
        3. Spawns _run_workers which creates NUM_WORKERS _process_loop Tasks.
        4. Uses asyncio.wait(FIRST_COMPLETED) to detect unexpected task death.

    On any top-level unhandled exception OR unexpected task completion:
        - Logs a CRITICAL entry with full traceback.
        - Fires alert_pipeline_component_crash() to Telegram.
        - Returns without re-raising (caller is never disturbed).

    This function is the sole public entry point for the pipeline and is
    intended to be called as a top-level asyncio task from main.py.

    Design note on asyncio.gather vs asyncio.wait:
        gather() would hang silently if workers_task completes normally
        (all workers returned) while poller_task keeps running — the queue
        would fill forever with no alert fired.  wait(FIRST_COMPLETED) detects
        any unexpected task exit immediately.
    """
    queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=config.PIPELINE_QUEUE_MAXSIZE)

    try:
        logger.info(
            "[PIPELINE] Starting. workers=%d queue_maxsize=%d",
            NUM_WORKERS,
            config.PIPELINE_QUEUE_MAXSIZE,
        )

        poller_task = asyncio.create_task(
            start_poller(queue),
            name="rss_poller",
        )
        workers_task = asyncio.create_task(
            _run_workers(queue),
            name="signal_workers",
        )

        # Wait until EITHER task finishes.  Under normal operation neither
        # ever does — both run indefinitely.  A task exiting means something
        # crashed or was cancelled, so we treat it as a pipeline failure.
        done, pending = await asyncio.wait(
            {poller_task, workers_task},
            return_when=asyncio.FIRST_COMPLETED,
        )

        # Cancel any tasks still alive to avoid orphaned coroutines.
        for t in pending:
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

        # Surface any exception from the completed task, or synthesise a
        # message for the case where a task exited cleanly (logic bug).
        for t in done:
            exc = t.exception()
            if exc is not None:
                raise exc

        # Reached only if a task exited without raising — still a pipeline
        # failure because both tasks are designed to run forever.
        task_names = ", ".join(t.get_name() for t in done)
        raise RuntimeError(
            f"Pipeline task(s) exited cleanly without error: [{task_names}]. "
            "This is a logic error — pipeline workers must never return normally."
        )

    except asyncio.CancelledError:
        # Intentional shutdown from the event loop — do not alert, just exit.
        logger.info("[PIPELINE] Cancelled. Shutting down cleanly.")
        raise  # Re-raise so the event loop knows we respected cancellation.

    except Exception as exc:
        logger.critical(
            "[PIPELINE] Top-level unhandled exception: %s: %s",
            type(exc).__name__,
            exc,
            exc_info=True,
        )
        try:
            await alert_pipeline_component_crash(
                component="pipeline.run_pipeline",
                error_type=type(exc).__name__,
                error_detail=str(exc)[:200],
            )
        except Exception as alert_exc:
            # Alert failure must never mask the original crash log.
            logger.error(
                "[PIPELINE] Failed to send crash alert to Telegram: %s",
                alert_exc,
            )
        # Intentionally do NOT re-raise — caller (main.py) is never disturbed.


async def log_final_signal_status(headline: str, source: str, outcome: str, detail: str = None) -> None:
    """
    Ensure market_signals is written/updated with the final pipeline outcome.
    Per Rule 5: 2-second timeout wrapper.
    """
    from memory.supabase_client import get_client
    import config
    from datetime import datetime, timezone

    async def _db_write():
        client = await get_client()
        # Find if a record already exists for this headline and source (inserted by classify_signal)
        # We query the latest matching row
        res = client.table("market_signals")\
            .select("id,action_taken")\
            .eq("raw_headline", headline)\
            .eq("source_name", source)\
            .order("detected_at", desc=True)\
            .limit(1)\
            .execute()
            
        if res.data:
            row_id = res.data[0]["id"]
            update_data = {
                "processed_at": datetime.now(timezone.utc).isoformat()
            }
            if outcome == "traded":
                update_data["action_taken"] = "traded"
                update_data["discard_reason"] = None
            elif outcome == "risk-blocked":
                update_data["action_taken"] = "discarded"
                update_data["discard_reason"] = detail or "risk-blocked"
            elif outcome == "abstain":
                update_data["action_taken"] = "discarded"
                update_data["discard_reason"] = "abstain"
            elif outcome == "error":
                # Only overwrite if it was pending
                if res.data[0]["action_taken"] == "pending":
                    update_data["action_taken"] = "discarded"
                    update_data["discard_reason"] = detail or "pipeline error"
                    
            client.table("market_signals").update(update_data).eq("id", row_id).execute()
        else:
            # No existing row (e.g., News Analyst failed/timed out before logging)
            action_taken = "traded" if outcome == "traded" else "discarded"
            insert_data = {
                "raw_headline": headline,
                "source_name": source,
                "source_url": source,
                "action_taken": action_taken,
                "discard_reason": detail if outcome != "traded" else None,
                "detected_at": datetime.now(timezone.utc).isoformat(),
                "processed_at": datetime.now(timezone.utc).isoformat()
            }
            client.table("market_signals").insert(insert_data).execute()

    try:
        db_timeout = getattr(config, "SUPABASE_TIMEOUT_SECONDS", 2)
        await asyncio.wait_for(_db_write(), timeout=db_timeout)
    except Exception as e:
        logger.error(f"[PIPELINE] Failed to update final signal status in Supabase: {e}")

