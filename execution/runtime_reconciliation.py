"""
Runtime reconciliation + in-flight visibility — Part B Step 4 (B3).

Startup reconciliation still gates ALL trading and is untouched (E-B13);
this loop is additive and runs every RUNTIME_RECON_INTERVAL_SECONDS on the
supervisor set. Three independent, fail-soft checks per tick:

1. RESOLUTION SWEEP — every open position's market is checked against the
   SAME resolved-market predicate the startup reconciliation and gate report
   use (execution/resolution.py; D-11 void rejection). Clean resolutions:
   FILLED positions close through the ONE shared close path
   (execution/position_close.py, D-B08) UNDER the Step 3 market lease;
   NEVER-FILLED makers are CANCELLED, never closed as trades (a never-filled
   order owns no shares — D-B15). Void (0.5) resolutions are SKIPPED +
   warned — a human decides (DO-NOT: no auto-resolve). Gamma fetches are
   capped per tick (RECON_MAX_RESOLUTION_FETCHES, rate-limit aware) with a
   rotating start offset so a full cap cannot starve the newest positions.
2. STUCK-PENDING SWEEP — idempotency_log rows pending longer than
   PENDING_STUCK_MINUTES are classified (D-B14): matching a resting maker →
   skipped (a legitimate rest, D-13); matching a FILLED position → confirmed
   (the entry happened — 'failed' would license a duplicate live order in
   Step 7, since only 'confirmed' blocks resubmission); no match → 'failed' +
   cooldown WARNING (paper). The live branch reuses the startup
   reconciliation's extracted order-status logic behind the PAPER_TRADING
   check. A pending row created moments ago is never touched.
3. IN-FLIGHT VISIBILITY — one [OBSERVABILITY][INFLIGHT] log per tick plus a
   daily Telegram digest (first tick of a new UTC day; restarts do NOT
   re-send — the startup alert covers boot).

Failure semantics: the whole loop is fail-soft telemetry — a failed tick
logs and retries, a poisoned row is isolated (per-row try/except), and
nothing here can block an entry or an exit. Close attempts hold the market
lease and SKIP on lease conflict/error (the entry path's own lease makes
the conflict window ~seconds; the next tick retries). Counters live in
this service module (equity/health precedent) under the `recon:` prefix.

This module performs I/O. It must NEVER be imported by risk/risk_engine.py
(purity rule).
"""

import asyncio
import logging
from datetime import date, datetime, timezone
from typing import Any, Optional

import config
from coordinator import lease
from execution.paper_fills import cancel_unfilled_maker
from execution.position_close import close_position
from execution.reconciliation import fetch_order_status
from execution.resolution import ResolutionStatus, fetch_market_resolution
from memory.supabase_client import get_client
from monitoring.telegram_alerts import alert_inflight_digest, alert_recon_warning

logger = logging.getLogger(__name__)

# ── Counters (service-module registry — equity/health precedent) ─────────────

_tick_counters: dict[str, int] = {
    "recon:resolved_closed": 0,
    "recon:resting_cancelled": 0,
    "recon:void_skipped": 0,
    "recon:fetch_failed": 0,
    "recon:lease_skipped": 0,
    "recon:pending_failed": 0,
    "recon:pending_confirmed": 0,
    "recon:pending_resting_skipped": 0,
}


def _record(reason: str) -> None:
    _tick_counters[reason] = _tick_counters.get(reason, 0) + 1


def get_recon_counters() -> dict[str, int]:
    """Copy of the runtime reconciliation counters."""
    return _tick_counters.copy()


# ── Alert cooldown + daily digest state ──────────────────────────────────────

_last_alert_at: dict[str, datetime] = {}
_last_digest_date: Optional[date] = None
_closed_today = 0


def reset_state_for_tests() -> None:
    """Reset module state (counters, cooldowns, digest clock) for isolation."""
    global _last_digest_date, _closed_today
    for key in _tick_counters:
        _tick_counters[key] = 0
    _last_alert_at.clear()
    _last_digest_date = None
    _closed_today = 0


def _dispatch_cooldown_warning(trigger: str, detail: str, action: str, now: datetime) -> None:
    """Fire a recon WARNING under the per-trigger cooldown (global DO-NOT:
    cooldowns required on repeating alerts). Fire-and-forget; skipped when no
    event loop is running (unit-test context without a loop)."""
    last = _last_alert_at.get(trigger)
    if last is not None and (now - last).total_seconds() < config.RECON_ALERT_COOLDOWN_SECONDS:
        return
    _last_alert_at[trigger] = now
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        logger.warning("[RECON] %s alert suppressed (no running loop): %s", trigger, detail)
        return
    asyncio.create_task(alert_recon_warning(trigger, detail, action))


# ── Reads ────────────────────────────────────────────────────────────────────

def _is_resting_maker(row: dict[str, Any]) -> bool:
    """Resting maker = the unfilled-maker predicate of
    idx_open_positions_unfilled_maker (D-13)."""
    return row.get("order_type") == "maker" and row.get("fill_price") is None


async def _fetch_open_positions(client: Any) -> list[dict[str, Any]]:
    """All open positions, oldest first (the Gamma cap marks the oldest)."""
    async def _read() -> list[dict[str, Any]]:
        builder = client.table("open_positions").select("*").order("opened_at")
        res = await asyncio.to_thread(builder.execute)
        return list(res.data or [])

    async with asyncio.timeout(config.SUPABASE_TIMEOUT_SECONDS):
        return await _read()


async def _fetch_row_by_id(client: Any, row_id: str) -> Optional[dict[str, Any]]:
    """Fresh copy of one open row (re-check inside the lease — the paper fill
    monitor may have filled or cancelled it since the tick's scan; paper_fills
    does not take the lease, so the re-read is the consistency point)."""
    async def _read() -> list[dict[str, Any]]:
        builder = client.table("open_positions").select("*").eq("id", row_id).limit(1)
        res = await asyncio.to_thread(builder.execute)
        return list(res.data or [])

    async with asyncio.timeout(config.SUPABASE_TIMEOUT_SECONDS):
        rows = await _read()
    return rows[0] if rows else None


async def _fetch_pending_idempotency(client: Any) -> Optional[list[dict[str, Any]]]:
    """All pending idempotency rows (age-filtered in Python — the pending set
    is tiny). None = read failed (the sweep skips; visibility shows None)."""
    async def _read() -> list[dict[str, Any]]:
        builder = client.table("idempotency_log").select("*").eq("status", "pending")
        res = await asyncio.to_thread(builder.execute)
        return list(res.data or [])

    try:
        async with asyncio.timeout(config.SUPABASE_TIMEOUT_SECONDS):
            return await _read()
    except Exception as e:
        logger.warning("[RECON] Pending-idempotency read failed (skips this tick): %s: %s",
                       type(e).__name__, e)
        return None


def _parse_ts(raw: Any) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


async def _mark_idempotency_status(
    client: Any, log_id: str, status: str, reason: Optional[str] = None
) -> bool:
    """Update one idempotency row's status (mirrors startup reconciliation's
    update — same fields incl. confirmed_at on confirm; injected client).
    Best-effort: a failed mark leaves the row pending and the next tick
    retries (pending never blocks a future order UUID — D-13)."""
    payload: dict[str, Any] = {
        "status": status,
        "confirmed_at": (
            datetime.now(timezone.utc).isoformat() if status == "confirmed" else None
        ),
    }
    if reason:
        payload["failure_reason"] = reason

    async def _write() -> None:
        builder = client.table("idempotency_log").update(payload).eq("id", log_id)
        await asyncio.to_thread(builder.execute)

    try:
        async with asyncio.timeout(config.SUPABASE_TIMEOUT_SECONDS):
            await _write()
        return True
    except Exception as e:
        logger.warning("[RECON] Idempotency mark failed for %s (retries next tick): %s: %s",
                       log_id, type(e).__name__, e)
        return False


# ── Check 1: resolution sweep ────────────────────────────────────────────────

_fetch_offset = 0  # rotating start offset — a full Gamma cap cannot starve the newest rows


async def _close_under_lease(client: Any, row: dict[str, Any], exit_price: float) -> bool:
    """Close one cleanly-resolved FILLED position under the market lease.

    Returns True when the close ran. Lease CONFLICT/ERROR → skip this tick
    (the entry path holds the lease for ~seconds; the next tick retries).
    The row is re-read INSIDE the lease: paper_fills may have filled or
    cancelled it since the tick's scan (it does not take the lease — the
    fresh read is the consistency point; a vanished row skips cleanly,
    close_position's own claim guard stays the second line)."""
    owner = lease.get_instance_id()
    outcome = await lease.acquire_lease(
        str(row["market_id"]), owner, config.LEASE_TTL_SECONDS
    )
    if outcome is not lease.LeaseOutcome.CLAIMED:
        _record("recon:lease_skipped")
        return False
    try:
        fresh = await _fetch_row_by_id(client, str(row["id"]))
        if fresh is None:
            logger.info(
                "[RECON] Row %s vanished before the close (cancelled/filled elsewhere) — skipping",
                str(row.get("id"))[:8],
            )
            return False
        res = await close_position(
            client,
            fresh,
            exit_price=exit_price,
            exit_type="resolved",
            exit_reason="resolved",
            notes="Resolved by the runtime reconciliation sweep.",
        )
        if res.closed:
            _record("recon:resolved_closed")
            global _closed_today
            _closed_today += 1
            return True
        return False
    finally:
        await lease.release_lease(str(row["market_id"]), owner)


async def _resolution_sweep(client: Any, rows: list[dict[str, Any]], now: datetime) -> None:
    """Check every open position's market (oldest first, capped, rotating):
    filled+resolved → close under lease; resting+resolved → cancel; void →
    skip + cooldown WARNING; unresolved/unavailable → untouched."""
    global _fetch_offset
    if not rows:
        return
    count = len(rows)
    offset = _fetch_offset % count
    ordered = rows[offset:] + rows[:offset]

    fetches = 0
    for row in ordered:
        if fetches >= config.RECON_MAX_RESOLUTION_FETCHES:
            logger.info(
                "[RECON] Gamma fetch cap reached (%d) — %d position(s) roll to the next tick",
                config.RECON_MAX_RESOLUTION_FETCHES, count - fetches,
            )
            break
        fetches += 1
        try:
            resolution = await fetch_market_resolution(
                str(row["market_id"]), str(row["direction"])
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # Per-row isolation: one poisoned check never kills the sweep.
            logger.error("[RECON] Resolution check crashed for %s: %s: %s",
                         str(row.get("market_id"))[:16], type(e).__name__, e)
            _record("recon:fetch_failed")
            continue

        if resolution.status is ResolutionStatus.UNAVAILABLE:
            _record("recon:fetch_failed")
            continue
        if resolution.status is ResolutionStatus.NOT_RESOLVED:
            continue

        market_id = str(row.get("market_id") or "")
        if _is_resting_maker(row):
            # D-B15: the order can never fill on a closed/void market and owns
            # no shares — cancelled (row deleted + idempotency 'cancelled'),
            # never closed into the ledger as a trade.
            await cancel_unfilled_maker(row)
            _record("recon:resting_cancelled")
            logger.info(
                "[RECON] Resting maker cancelled on resolved market %s (status=%s)",
                market_id[:16], resolution.status.value,
            )
            continue

        if resolution.status is ResolutionStatus.VOID:
            # DO-NOT: never auto-resolve void/ambiguous markets — a human decides.
            _record("recon:void_skipped")
            _dispatch_cooldown_warning(
                "RECON_VOID_MARKET",
                f"Market {market_id[:16]} resolved at a non-binary price "
                f"(yes_price={resolution.yes_price}) — position left open",
                "Human review required. The runtime sweep will keep skipping "
                "this position until it is closed manually.",
                now,
            )
            continue

        try:
            await _close_under_lease(client, row, float(resolution.exit_price))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # Per-row isolation: one poisoned close (a malformed row, a DB
            # blip) must never wedge the tick — the next tick retries this
            # row while the others keep closing.
            logger.error(
                "[RECON] Close failed for %s (retries next tick): %s: %s",
                market_id[:16], type(e).__name__, e,
            )

    _fetch_offset = (_fetch_offset + fetches) % count if count else 0


# ── Check 2: stuck-pending sweep ─────────────────────────────────────────────

async def _stuck_pending_sweep(
    client: Any,
    rows: list[dict[str, Any]],
    pending_rows: list[dict[str, Any]],
    now: datetime,
) -> None:
    """Classify stale pending idempotency rows (D-B14) and mark them."""
    by_uuid = {
        str(r.get("idempotency_uuid")): r for r in rows if r.get("idempotency_uuid")
    }
    for log_row in pending_rows:
        try:
            created = _parse_ts(log_row.get("created_at"))
            if created is None:
                continue
            age = (now - created).total_seconds()
            if age < config.PENDING_STUCK_MINUTES * 60:
                continue

            uuid_str = str(log_row["id"])
            match = by_uuid.get(uuid_str)
            if match is not None and _is_resting_maker(match):
                # A live resting order owns this UUID — its pending entry is
                # the entry that is legitimately still resting (D-13/D-B14).
                _record("recon:pending_resting_skipped")
                continue
            if match is not None:
                # The position EXISTS and is filled — the entry happened.
                # Marking it 'failed' would license a duplicate resubmission
                # (only 'confirmed' blocks — D-B14).
                if await _mark_idempotency_status(client, uuid_str, "confirmed"):
                    _record("recon:pending_confirmed")
                    logger.info(
                        "[RECON] Stuck pending log %s matched a filled position — confirmed",
                        uuid_str[:8],
                    )
                continue

            if not config.PAPER_TRADING and log_row.get("polymarket_order_id"):
                # Live branch: resolve via the order itself (startup-verbatim
                # logic, behind the PAPER_TRADING check).
                status = await fetch_order_status(str(log_row["polymarket_order_id"]))
                if status is None:
                    continue  # query failed — leave pending for a later tick
                if status in ("FILLED", "LIVE", "PARTIALLY_FILLED"):
                    if await _mark_idempotency_status(client, uuid_str, "confirmed"):
                        _record("recon:pending_confirmed")
                    continue
                if await _mark_idempotency_status(
                    client, uuid_str, "failed", reason=f"Order status: {status}"
                ):
                    _record("recon:pending_failed")
                    _dispatch_cooldown_warning(
                        "RECON_STUCK_PENDING",
                        f"Pending idempotency log {uuid_str[:8]} marked failed "
                        f"(live order status: {status})",
                        "Verify on Polymarket whether the order executed; the "
                        "UUID is released either way.",
                        now,
                    )
                continue

            # Paper mode (or live without an order id): the entry never
            # completed — fail it and release the UUID.
            if await _mark_idempotency_status(
                client,
                uuid_str,
                "failed",
                reason="Runtime reconciliation: no order ID or position found.",
            ):
                _record("recon:pending_failed")
                _dispatch_cooldown_warning(
                    "RECON_STUCK_PENDING",
                    f"Pending idempotency log {uuid_str[:8]} (market "
                    f"{str(log_row.get('market_id'))[:16]}) had no order and no "
                    "position — marked failed",
                    "The order UUID is released; no duplicate risk (only "
                    "'confirmed' blocks resubmission).",
                    now,
                )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # Per-row isolation.
            logger.error("[RECON] Stuck-pending classification crashed for %s: %s: %s",
                         str(log_row.get("id"))[:8], type(e).__name__, e)


# ── Check 3: in-flight visibility ────────────────────────────────────────────

def _maybe_send_daily_digest(
    now: datetime,
    open_positions: int,
    resting_makers: int,
    pending_idempotency: Optional[int],
) -> None:
    """Send the in-flight digest on the first tick of a new UTC day. A boot
    does NOT send (the startup alert covers it) — the first tick seeds the
    day anchor silently."""
    global _last_digest_date, _closed_today
    today = now.date()
    if _last_digest_date is None:
        _last_digest_date = today
        return
    if today == _last_digest_date:
        return
    _last_digest_date = today
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    asyncio.create_task(alert_inflight_digest(
        open_positions=open_positions,
        resting_makers=resting_makers,
        pending_idempotency=pending_idempotency if pending_idempotency is not None else -1,
        closed_today=_closed_today,
    ))
    _closed_today = 0


# ── Tick + supervisor ────────────────────────────────────────────────────────

async def run_reconciliation_once(now: Optional[datetime] = None) -> None:
    """One reconciliation tick: the three independent checks + visibility.
    Raises on a structural failure (open_positions read) so the supervisor
    logs and retries — the cache-free design has nothing to age, and the
    next tick redoes everything (all checks are idempotent)."""
    checked_at = now or datetime.now(timezone.utc)
    client = await get_client()

    rows = await _fetch_open_positions(client)

    await _resolution_sweep(client, rows, checked_at)

    pending_rows = await _fetch_pending_idempotency(client)
    if pending_rows is not None:
        await _stuck_pending_sweep(client, rows, pending_rows, checked_at)

    resting = sum(1 for r in rows if _is_resting_maker(r))
    logger.info(
        "[OBSERVABILITY][INFLIGHT] open_positions=%d resting_makers=%d "
        "pending_idempotency=%s counters=%s",
        len(rows), resting,
        len(pending_rows) if pending_rows is not None else None,
        {k: v for k, v in _tick_counters.items() if v},
    )
    _maybe_send_daily_digest(
        checked_at, open_positions=len(rows), resting_makers=resting,
        pending_idempotency=len(pending_rows) if pending_rows is not None else None,
    )


async def run_runtime_reconciliation() -> None:
    """Supervisor entry: reconcile forever. paper_fills pattern (E-B12):
    fail-soft tick — a failed tick logs and retries next interval;
    CancelledError is always re-raised."""
    logger.info(
        "[RECON] Runtime reconciliation supervisor started "
        "(interval=%ds, stuck_pending=%dmin, gamma_cap=%d)",
        config.RUNTIME_RECON_INTERVAL_SECONDS, config.PENDING_STUCK_MINUTES,
        config.RECON_MAX_RESOLUTION_FETCHES,
    )
    while True:
        try:
            await run_reconciliation_once()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(
                "[RECON] Tick failed (fail-soft, retries next tick): %s: %s",
                type(e).__name__, e,
            )
        await asyncio.sleep(config.RUNTIME_RECON_INTERVAL_SECONDS)
