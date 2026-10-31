"""
Cross-process lease service — Part B Step 3 (B4).

Design contract (List-B.md §6 Step 3; decisions D-B02, D-B13):

- The per-market asyncio.Locks (coordinator/market_state.py) guard THIS
  process only. Two processes — overlapping Railway deploys, or Hetzner plus
  a stray container — each run their own lock registry and could trade the
  same market concurrently. The DB lease closes that gap: one row per market
  in market_leases, held by an instance identity until lease_until.
- The claim is ONE atomic SQL statement via the claim_market_lease RPC
  (D-B13): INSERT ... ON CONFLICT (market_id) DO UPDATE ... WHERE
  lease_until < now() OR owner = EXCLUDED.owner RETURNING true. PostgreSQL
  re-evaluates the WHERE clause after the row-lock wait, so two concurrent
  claims produce exactly one winner. Session-level pg_advisory_lock is
  forbidden (D-B02): supabase-py pools connections and each RPC runs in its
  own transaction, so a session lock could never be reliably released.
- The same-owner match branch makes re-claiming an unexpired own lease
  succeed — renew_lease IS a claim under our owner (atomic TTL refresh).
- Owner = instance identity (hostname + boot-unique random suffix),
  computed once per process and logged at first use.
- Entry paths acquire the lease INSIDE the asyncio lock (process-local lock
  first, lease second — defense in depth) and release it in a `finally`.
- RESIDUAL RISK (documented, accepted by the spec): a hung holder's lease
  expires while it still runs, and another process may then enter the same
  critical section. The DB-level re-checks inside the section (tranche
  count via load_market_state, idempotency_log) remain the second line of
  defense — idempotency blocks a duplicate order UUID outright. This is the
  honest trade for PostgREST compatibility (no session state to hang onto).
- Failure semantics: a failed claim BLOCKS the entry — fail-closed, never
  fail-open. CONFLICT (another live owner) and ERROR (RPC failed, claim
  unverifiable) are distinguished for callers and observability, because
  they demand different reactions: an entry path drops either way, but the
  singleton RENEWER must survive transient errors (the TTL self-heals) and
  exit only on a definitive loss. Drop counters live here (the
  equity/health service-module precedent), not in the pipeline registry:
  lock:timeout (bounded asyncio-lock wait expired), lease:conflict (another
  live owner holds the lease), lease:error (RPC failed — cannot verify).
- release_lease is the one fail-soft path: a failed release only means the
  row survives until its TTL self-heals (LEASE_TTL_SECONDS); it must never
  raise through a `finally` and mask an in-flight exception from the
  critical section.

This module performs I/O. It must NEVER be imported by risk/risk_engine.py
(purity rule), and nothing here can place an order or block an exit.
"""

import asyncio
import enum
import logging
import socket
import sys
import time
import uuid
from typing import Any, Callable, Optional

import config
from memory.supabase_client import get_client
from monitoring.telegram_alerts import alert_system_halt

logger = logging.getLogger(__name__)

# Reserved lease key guarding "exactly one trading process" at startup.
SINGLETON_LEASE_KEY = "__singleton__"


class LeaseOutcome(enum.Enum):
    """Result of a claim attempt. Not-CLAIMED is always fail-closed for
    callers that gate capital; the distinction exists because the singleton
    renewer must tolerate ERROR (transient, TTL self-heals) but treat
    CONFLICT (definitive loss to another owner) as split-brain."""

    CLAIMED = "claimed"
    CONFLICT = "conflict"   # another live owner holds the lease
    ERROR = "error"         # claim could not be verified (RPC failed/timeout)


# ── Drop counters (service-module registry — equity/health precedent) ────────

_drop_counters: dict[str, int] = {
    "lock:timeout": 0,
    "lease:conflict": 0,
    "lease:error": 0,
}


def record_drop(reason: str) -> None:
    """Increment a Step 3 drop counter (lock:timeout / lease:conflict / lease:error)."""
    _drop_counters[reason] = _drop_counters.get(reason, 0) + 1


def get_drop_counters() -> dict[str, int]:
    """Copy of the Step 3 drop counters."""
    return _drop_counters.copy()


# ── Instance identity ────────────────────────────────────────────────────────

_instance_id: Optional[str] = None


def get_instance_id() -> str:
    """Boot-unique instance identity: hostname + random suffix, computed once.

    A fresh suffix per process boot (not the PID) keeps identities distinct
    even across a fast restart that reuses a PID; it is logged once so lease
    rows in the DB remain attributable to a boot."""
    global _instance_id
    if _instance_id is None:
        _instance_id = f"{socket.gethostname()}-{uuid.uuid4().hex[:12]}"
        logger.info("[LEASE] Instance identity: %s", _instance_id)
    return _instance_id


def reset_instance_id_for_tests() -> None:
    """Clear the cached identity (test isolation only)."""
    global _instance_id
    _instance_id = None


# ── RPC result coercion ──────────────────────────────────────────────────────

def _coerce_claim_result(raw: Any) -> bool:
    """PostgREST returns a bare JSON scalar for a scalar-returning RPC; older
    client versions may wrap it in a one-element list. Coerce defensively —
    anything not recognizably true is NOT a claim (fail-closed)."""
    if isinstance(raw, list):
        raw = raw[0] if raw else False
    return raw is True


def _validate_ttl(ttl_seconds: int) -> None:
    """A non-positive TTL would expire instantly (fail-open) — refuse loudly."""
    if ttl_seconds <= 0:
        raise ValueError(f"lease TTL must be positive, got {ttl_seconds}")


# ── Lease primitives ─────────────────────────────────────────────────────────

async def acquire_lease(market_id: str, owner: str, ttl_seconds: int) -> LeaseOutcome:
    """Atomically claim the lease for (market_id, owner) for ttl_seconds.

    CLAIMED  — fresh claim, expired-lease steal, or same-owner renewal.
    CONFLICT — a conflicting live lease exists (lease:conflict counter).
    ERROR    — the claim could not be verified: RPC failed or timed out
               (lease:error counter). Callers gate capital, so anything but
               CLAIMED is fail-closed: an unguardable entry never proceeds.

    The RPC execute runs via asyncio.to_thread: supabase-py's execute() is a
    blocking HTTP call, and asyncio.timeout can only cancel at await points —
    to_thread makes the SUPABASE_TIMEOUT_SECONDS bound real (the stricter
    pattern of market_state.load_market_state, our neighbor inside the same
    critical section) instead of the loose direct-execute precedent. If the
    timeout fires while the orphaned thread completes server-side, the row
    is leaked under our owner — it self-heals via the TTL and reads as a
    same-owner renewal on our next claim: fail-closed direction."""
    _validate_ttl(ttl_seconds)
    try:
        async with asyncio.timeout(config.SUPABASE_TIMEOUT_SECONDS):
            client = await get_client()
            builder = client.rpc("claim_market_lease", {
                "p_market_id": market_id,
                "p_owner": owner,
                "p_ttl_seconds": ttl_seconds,
            })
            res = await asyncio.to_thread(builder.execute)
    except asyncio.TimeoutError:
        record_drop("lease:error")
        logger.warning(
            "[LEASE][DROP:lease:error] claim timed out after %ss on %s — fail-closed",
            config.SUPABASE_TIMEOUT_SECONDS, market_id,
        )
        return LeaseOutcome.ERROR
    except Exception as e:
        record_drop("lease:error")
        logger.warning(
            "[LEASE][DROP:lease:error] claim failed on %s: %s: %s — fail-closed",
            market_id, type(e).__name__, e,
        )
        return LeaseOutcome.ERROR
    if not _coerce_claim_result(getattr(res, "data", None)):
        record_drop("lease:conflict")
        logger.warning(
            "[LEASE][DROP:lease:conflict] %s is held by another live process — fail-closed",
            market_id,
        )
        return LeaseOutcome.CONFLICT
    return LeaseOutcome.CLAIMED


async def renew_lease(market_id: str, owner: str, ttl_seconds: int) -> LeaseOutcome:
    """Refresh OUR unexpired lease (atomic same-owner re-claim).

    CONFLICT means the lease is definitively gone: another owner claimed it
    after our expiry (the split-brain signal for the singleton guard).
    ERROR is transient — the TTL self-heals while the next renewal ticks."""
    return await acquire_lease(market_id, owner, ttl_seconds)


async def release_lease(market_id: str, owner: str) -> bool:
    """Delete OUR lease row only (owner filter — never another holder's).

    The one fail-soft path in this module: a failed release leaves the row
    to expire via its TTL, which self-heals in LEASE_TTL_SECONDS; it must
    never raise through a caller's `finally` and mask an in-flight
    exception from the critical section. Same to_thread bound as
    acquire_lease; a timed-out orphaned DELETE is harmless — the row merely
    lives until its TTL."""
    try:
        async with asyncio.timeout(config.SUPABASE_TIMEOUT_SECONDS):
            client = await get_client()
            builder = (
                client.table("market_leases")
                .delete()
                .eq("market_id", market_id)
                .eq("owner", owner)
            )
            res = await asyncio.to_thread(builder.execute)
            released = bool(getattr(res, "data", None))
            if not released:
                logger.info(
                    "[LEASE] release matched no rows for %s (already expired or re-keyed)",
                    market_id,
                )
            return released
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.warning(
            "[LEASE] release failed on %s (TTL self-heals): %s: %s",
            market_id, type(e).__name__, e,
        )
        return False


# ── Singleton instance guard ─────────────────────────────────────────────────

def _dispatch_singleton_halt(reason: str, detail: str) -> None:
    """alert_system_halt with the Step 2 precedent: a non-drawdown halt keeps
    the field meanings and carries the story in `detail` (drawdown_pct 0.0)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        logger.warning("[SINGLETON] halt alert suppressed (no running loop): %s", reason)
        return
    asyncio.create_task(
        alert_system_halt(reason, 0.0, 0.0, detail=detail)
    )


async def acquire_singleton_or_exit(
    monotonic: Optional[Callable[[], float]] = None,
) -> None:
    """Claim the global __singleton__ lease or exit(1) after the max wait.

    Startup gate against overlapping deploys: while another instance holds
    the lease, this process retries every LEASE_RENEW_INTERVAL_SECONDS up to
    SINGLETON_CLAIM_MAX_WAIT_SECONDS, then alerts and exits — Railway's
    ALWAYS restart policy turns that into a supervised retry cycle. A claim
    that cannot be verified (ERROR) counts as not-claimed: trading without
    knowing we are alone is exactly the hazard this guard kills.

    Deploy-overlap math: the dying old instance renews every 30s on a 90s
    TTL, so a new instance claims within ~90-120s of the old process dying;
    a hard-killed holder cannot release, so the successor waits out the
    remaining TTL (≤90s) by design."""
    owner = get_instance_id()
    max_wait = config.SINGLETON_CLAIM_MAX_WAIT_SECONDS
    interval = config.LEASE_RENEW_INTERVAL_SECONDS
    clock = monotonic or time.monotonic
    deadline = clock() + max_wait

    while True:
        outcome = await acquire_lease(
            SINGLETON_LEASE_KEY, owner, config.SINGLETON_LEASE_TTL_SECONDS
        )
        if outcome is LeaseOutcome.CLAIMED:
            logger.info(
                "[SINGLETON] Global lease claimed by %s — sole trading process "
                "(ttl=%ds, renew=%ds)",
                owner, config.SINGLETON_LEASE_TTL_SECONDS, interval,
            )
            return
        if clock() >= deadline:
            detail = (
                f"Instance {owner} could not claim the __singleton__ lease for "
                f"{max_wait}s — another trading instance is alive or the DB "
                "refuses claims. Exiting so two agents never trade in parallel."
            )
            logger.critical(
                "[SINGLETON] Claim unsuccessful after %ds — alerting and exiting", max_wait
            )
            _dispatch_singleton_halt("singleton_claim_timeout", detail)
            sys.exit(1)
        logger.info(
            "[SINGLETON] Singleton lease not available (%s) — retrying in %ds",
            outcome.value, interval,
        )
        await asyncio.sleep(interval)


async def run_singleton_lease_renewer() -> None:
    """Keep the __singleton__ lease alive every LEASE_RENEW_INTERVAL_SECONDS.

    paper_fills supervisor pattern (E-B12): CancelledError re-raises. ERROR
    only logs — the 90s TTL tolerates two missed 30s ticks while the DB
    recovers. A DEFINITIVE loss (CONFLICT — another owner now holds the
    lease) is the split-brain this guard exists to kill: alert, then
    exit(1) (SystemExit propagates through the event loop and terminates
    the process, matching main.py's crash semantics)."""
    owner = get_instance_id()
    logger.info(
        "[SINGLETON] Lease renewer started (interval=%ds, ttl=%ds)",
        config.LEASE_RENEW_INTERVAL_SECONDS, config.SINGLETON_LEASE_TTL_SECONDS,
    )
    while True:
        await asyncio.sleep(config.LEASE_RENEW_INTERVAL_SECONDS)
        try:
            outcome = await renew_lease(
                SINGLETON_LEASE_KEY, owner, config.SINGLETON_LEASE_TTL_SECONDS
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # Defensive: renew_lease maps RPC failures to ERROR internally;
            # reaching here means something unexpected — do not kill trading
            # on it, the TTL grants grace until the next tick.
            logger.warning(
                "[SINGLETON] Renew errored unexpectedly (TTL tolerates missed "
                "ticks): %s: %s", type(e).__name__, e,
            )
            continue
        if outcome is LeaseOutcome.CLAIMED:
            continue
        if outcome is LeaseOutcome.ERROR:
            logger.warning(
                "[SINGLETON] Renew unverifiable this tick (lease:error) — TTL "
                "tolerates missed ticks; retrying"
            )
            continue
        detail = (
            f"Instance {owner} lost the __singleton__ lease to another "
            "process — two trading instances would now run in parallel. "
            "Exiting; the survivor remains the singleton."
        )
        logger.critical(
            "[SINGLETON] Lease lost to another instance — alerting and exiting"
        )
        _dispatch_singleton_halt("singleton_lease_lost", detail)
        sys.exit(1)
