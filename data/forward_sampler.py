"""
data/forward_sampler.py — A6: forward-price sampler (List A.md Step 2).

For every signal entering the pipeline, records one `signal_outcomes` row at
arrival (t0, price_t0, headline_hash, entities) and samples the CLOB midpoint
at +1m/+5m/+15m/+60m into p_m1/p_m5/p_m15/p_m60. This measures whether a
headline still predicts a tradable move AFTER our pipeline latency — including
signals the fail-closed dormant pipeline drops — and is the raw data behind
the velocity estimator (Step 6f) and scripts/signal_drift_report.py.

Design (List A.md Step 2 spec):
    - The pipeline hook only does `SAMPLER_QUEUE.put_nowait(job)` — it never
      blocks and never crashes the pipeline. A full queue drops the job with a
      logged stat (instrumentation is best-effort, the trade decision is not).
    - One supervisor task (`run_forward_sampler_supervisor`, started in
      main.py) consumes the queue: inserts the row (2s timeout, best-effort)
      and spawns one sampling task per job, tracked via strong references so
      pending timers cannot be garbage-collected.
    - Each price read is wrapped in its own timeout; a miss leaves the column
      NULL (explicit NULLs are valid data, not errors). Nothing in this module
      may raise into its caller: every failure path is logged and counted.
    - Pending reads live in memory; a process restart loses them (recorded as
      NULLs). Backfilling them is deliberately out of scope.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Coroutine, Optional

import config

logger = logging.getLogger(__name__)

# Shared job queue between the pipeline hook and the supervisor task. Created
# lazily (get_sampler_queue) so maxsize reflects config at creation time.
_SAMPLER_QUEUE: Optional[asyncio.Queue] = None
_SAMPLER_QUEUE_LOOP: Optional[asyncio.AbstractEventLoop] = None

# p_mX columns paired positionally with config.FORWARD_SAMPLER_HORIZONS_SECONDS.
# The spec freezes four horizons; extra configured horizons beyond this list
# would be unmeasured (zip truncates), so keep them aligned.
_HORIZON_COLUMNS: tuple[str, ...] = ("p_m1", "p_m5", "p_m15", "p_m60")

# ── Sampler statistics (observability — surfaced by the 5-minute stats reporter)
_sampler_stats: dict[str, int] = {
    "jobs_enqueued": 0,
    "jobs_dropped": 0,
    "rows_inserted": 0,
    "rows_failed": 0,
    "reads_succeeded": 0,
    "reads_missed": 0,
    "direction_updates_ok": 0,
    "direction_updates_failed": 0,
}


def get_sampler_stats() -> dict[str, int]:
    """Return a copy of the sampler's lifetime counters."""
    return _sampler_stats.copy()


def get_sampler_queue() -> asyncio.Queue:
    """
    The shared job queue between the pipeline hook and the supervisor task.

    Created lazily and bound to the running loop: production runs a single
    loop for the process lifetime, while each test drives its own loop (an
    asyncio.Queue binds to the loop that first uses it and raises
    "bound to a different event loop" if reused) — so a new loop gets a
    fresh queue instead of a poisoned singleton.
    """
    global _SAMPLER_QUEUE, _SAMPLER_QUEUE_LOOP
    loop = asyncio.get_running_loop()
    if _SAMPLER_QUEUE is None or _SAMPLER_QUEUE_LOOP is not loop:
        _SAMPLER_QUEUE = asyncio.Queue(maxsize=config.FORWARD_SAMPLER_QUEUE_MAXSIZE)
        _SAMPLER_QUEUE_LOOP = loop
    return _SAMPLER_QUEUE

# Strong references to spawned background tasks — a task only referenced by the
# event loop can be garbage-collected mid-flight (asyncio docs).
_background_tasks: set[asyncio.Task] = set()


# ── Job contract ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SamplerJob:
    """
    One signal's forward-sampling assignment.

    Attributes:
        outcome_id:   Client-generated UUID — the signal_outcomes row's primary
                      key, known to the hook before any DB round-trip.
        market_id:    Signal's market identifier (data-api condition id).
        token_id:     CLOB key the reads use — the same proven key the pipeline
                      uses for its book fetch (cache token for news-path signals,
                      signal market_id for non-cache/copy signals).
        t0_iso:       Signal arrival timestamp (aware UTC ISO 8601). Horizons are
                      scheduled relative to THIS, not to dequeue time.
        price_t0:     Arrival price (the pipeline's fetched midpoint).
        headline_hash: Normalized headline hash (data/novelty.py).
        entities:     Extracted entities (stored as JSONB).
        strategy:     strategy_override when present, else the signal_source.
        headline:     Raw headline — used to backfill signal_id from
                      market_signals (same lookup pattern as
                      data.pipeline.log_final_signal_status).
        source:       Source name for the signal_id backfill lookup.
    """
    outcome_id: str
    market_id: str
    token_id: str
    t0_iso: str
    price_t0: float
    headline_hash: str
    entities: list[str]
    strategy: str
    headline: str
    source: str


def schedule_forward_sampling(job: SamplerJob) -> None:
    """
    Enqueue one sampling job from the pipeline hook. Never blocks, never raises.

    Args:
        job: Fully-populated job for a signal that has a market and an
             arrival price.
    """
    queue = get_sampler_queue()
    try:
        queue.put_nowait(job)
        _sampler_stats["jobs_enqueued"] += 1
        logger.debug(
            "[FORWARD_SAMPLER] Job queued: outcome_id=%s market=%s price_t0=%.4f",
            job.outcome_id, job.market_id[:16], job.price_t0,
        )
    except asyncio.QueueFull:
        _sampler_stats["jobs_dropped"] += 1
        logger.warning(
            "[FORWARD_SAMPLER][DROP:queue_full] Sampling queue full "
            "(maxsize=%d) — job dropped (outcome_id=%s market=%s). "
            "Signal proceeds unmeasured.",
            config.FORWARD_SAMPLER_QUEUE_MAXSIZE, job.outcome_id, job.market_id[:16],
        )


def spawn_background(coro: Coroutine[Any, Any, None], name: str) -> None:
    """
    Spawn a fire-and-forget task with a strong reference (GC-safe).

    Args:
        coro: Coroutine to run.
        name: Task name for log correlation.
    """
    task = asyncio.create_task(coro, name=name)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


# ── Supervisor ────────────────────────────────────────────────────────────────

async def run_forward_sampler_supervisor() -> None:
    """
    Supervisor loop started once in main.py: consumes the job queue forever.

    Per job: insert the signal_outcomes row (best-effort) and spawn one
    sampling task. A failed insert skips sampling — without a row the reads
    could not be recorded anywhere. Every failure is logged and counted; the
    loop itself never exits on error.
    """
    queue = get_sampler_queue()
    logger.info(
        "[FORWARD_SAMPLER] Supervisor started. horizons=%ss read_timeout=%ss "
        "queue_maxsize=%d",
        list(config.FORWARD_SAMPLER_HORIZONS_SECONDS),
        config.FORWARD_SAMPLER_READ_TIMEOUT_SECONDS,
        config.FORWARD_SAMPLER_QUEUE_MAXSIZE,
    )
    while True:
        job: SamplerJob = await queue.get()
        try:
            inserted = await _insert_outcome_row(job)
            if inserted:
                _sampler_stats["rows_inserted"] += 1
                spawn_background(
                    _sample_one_job(job),
                    name=f"forward_sample_{job.outcome_id[:8]}",
                )
            else:
                _sampler_stats["rows_failed"] += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _sampler_stats["rows_failed"] += 1
            logger.error(
                "[FORWARD_SAMPLER] Supervisor error on job %s: %s: %s",
                job.outcome_id, type(exc).__name__, exc,
            )


async def _insert_outcome_row(job: SamplerJob) -> bool:
    """
    Insert the signal_outcomes row for a job (2-second timeout, RULE 5).

    The hook already knows the row id (client-generated UUID), so no
    round-trip is needed before scheduling.

    Returns:
        True when the row is in the table; False on any failure (already
        logged — the caller skips sampling).
    """
    from memory.supabase_client import get_client  # call-time import: test patches apply

    async def _insert() -> None:
        client = await get_client()
        client.table("signal_outcomes").insert({
            "id": job.outcome_id,
            "signal_id": None,  # backfilled post-analyst by apply_signal_direction
            "market_id": job.market_id,
            "strategy": job.strategy,
            "headline_hash": job.headline_hash,
            "entities_json": job.entities,
            "t0": job.t0_iso,
            "price_t0": job.price_t0,
            "confirmed_direction": None,
        }).execute()

    # asyncio.timeout, NOT wait_for: on 3.11, wait_for swallows an external
    # cancellation that arrives after its inner future already completed (the
    # cancelled task then survives and loops). asyncio.timeout propagates
    # external CancelledError untouched — the supervisor must always die on
    # cancel — while still capping the DB write at RULE 5's 2 seconds.
    try:
        async with asyncio.timeout(config.SUPABASE_TIMEOUT_SECONDS):
            await _insert()
        logger.info(
            "[FORWARD_SAMPLER] Outcome row recorded: outcome_id=%s market=%s "
            "strategy=%s price_t0=%.4f",
            job.outcome_id, job.market_id[:16], job.strategy, job.price_t0,
        )
        return True
    except Exception as exc:
        logger.warning(
            "[FORWARD_SAMPLER][DROP:row_insert_failed] outcome_id=%s market=%s: "
            "%s: %s — forward reads skipped (no row to record into)",
            job.outcome_id, job.market_id[:16], type(exc).__name__, exc,
        )
        return False


# ── Delayed reads ─────────────────────────────────────────────────────────────

def _horizon_pairs() -> list[tuple[float, str]]:
    """
    Pair each configured horizon with its signal_outcomes column, positionally.

    Read at call time so tests can shorten the horizons via config.
    """
    return [
        (float(seconds), column)
        for seconds, column in zip(config.FORWARD_SAMPLER_HORIZONS_SECONDS, _HORIZON_COLUMNS)
    ]


async def _read_price(token_id: str) -> Optional[float]:
    """
    One CLOB midpoint read under its own timeout. Never raises.

    Args:
        token_id: CLOB key for the market's YES token.

    Returns:
        Midpoint price in [0, 1], or None on timeout/error (a miss — the
        column stays NULL).
    """
    from data.market_discovery import get_market_price

    try:
        async with asyncio.timeout(config.FORWARD_SAMPLER_READ_TIMEOUT_SECONDS):
            return await get_market_price(token_id)
    except Exception as exc:
        logger.warning(
            "[FORWARD_SAMPLER] Price read miss for token %s: %s: %s",
            str(token_id)[:16], type(exc).__name__, exc,
        )
        return None


async def _update_price_column(outcome_id: str, column: str, price: float) -> bool:
    """
    Write one horizon's price into the outcome row (2-second timeout, RULE 5).

    Returns:
        True when the update ran; False on failure (column stays NULL).
    """
    from memory.supabase_client import get_client  # call-time import: test patches apply

    async def _update() -> None:
        client = await get_client()
        client.table("signal_outcomes").update({column: price}).eq("id", outcome_id).execute()

    try:
        async with asyncio.timeout(config.SUPABASE_TIMEOUT_SECONDS):
            await _update()
        return True
    except Exception as exc:
        logger.warning(
            "[FORWARD_SAMPLER] Failed to record %s for outcome %s: %s: %s — "
            "column stays NULL",
            column, outcome_id, type(exc).__name__, exc,
        )
        return False


async def _sample_one_job(job: SamplerJob) -> None:
    """
    Perform a job's full horizon schedule. Runs as its own task.

    Sleeps until each horizon (relative to the signal's arrival time), reads
    the CLOB midpoint, and records it. Misses leave NULLs. Any unexpected
    error is logged — this task must never crash the supervisor or the loop.
    """
    try:
        t0 = datetime.fromisoformat(job.t0_iso)
        if t0.tzinfo is None:
            t0 = t0.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        for seconds, column in _horizon_pairs():
            due = t0 + timedelta(seconds=seconds)
            wait_seconds = (due - now).total_seconds()
            if wait_seconds > 0:
                await asyncio.sleep(wait_seconds)
                now = datetime.now(timezone.utc)

            price = await _read_price(job.token_id)
            if price is None:
                _sampler_stats["reads_missed"] += 1
                continue  # explicit NULL — a valid, recorded miss
            if await _update_price_column(job.outcome_id, column, price):
                _sampler_stats["reads_succeeded"] += 1
                logger.info(
                    "[FORWARD_SAMPLER] %s recorded for outcome %s: price=%.4f "
                    "(t0=%.4f, drift=%+.4f)",
                    column, job.outcome_id, price, job.price_t0, price - job.price_t0,
                )
    except asyncio.CancelledError:
        raise  # shutdown — the row keeps whatever was recorded so far
    except Exception:
        logger.exception(
            "[FORWARD_SAMPLER] Sampling task crashed for outcome %s "
            "(market=%s) — remaining horizons stay NULL",
            job.outcome_id, job.market_id[:16],
        )


# ── Direction + signal_id backfill ────────────────────────────────────────────

async def apply_signal_direction(
    outcome_id: str,
    headline: str,
    source: str,
    confirmed_yes: bool,
) -> None:
    """
    Record the signal's asserted direction and backfill signal_id (best-effort).

    Called as a background task right after the News Analyst returns: the
    market_signals row now exists (classify_signal awaits its insert), so the
    FK can be resolved with the same latest-row lookup
    log_final_signal_status uses. Never raises.

    Args:
        outcome_id:    signal_outcomes row id (the SamplerJob's outcome_id).
        headline:      Raw headline for the market_signals lookup.
        source:        Source name for the market_signals lookup.
        confirmed_yes: True = signal asserted YES, False = NO.
    """
    from memory.supabase_client import get_client  # call-time import: test patches apply

    async def _apply() -> None:
        client = await get_client()
        res = (
            client.table("market_signals")
            .select("id")
            .eq("raw_headline", headline)
            .eq("source_name", source)
            .order("detected_at", desc=True)
            .limit(1)
            .execute()
        )
        signal_id = res.data[0]["id"] if res.data else None
        client.table("signal_outcomes").update({
            "confirmed_direction": confirmed_yes,
            "signal_id": signal_id,
        }).eq("id", outcome_id).execute()

    try:
        async with asyncio.timeout(config.SUPABASE_TIMEOUT_SECONDS):
            await _apply()
        _sampler_stats["direction_updates_ok"] += 1
    except Exception as exc:
        _sampler_stats["direction_updates_failed"] += 1
        logger.warning(
            "[FORWARD_SAMPLER] Direction update failed for outcome %s: %s: %s "
            "— confirmed_direction stays NULL",
            outcome_id, type(exc).__name__, exc,
        )
