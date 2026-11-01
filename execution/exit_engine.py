"""
execution/exit_engine.py — Part B Step 5 (B5): the exit engine.

The exit machinery E-B9 said did not exist. Paper sells priced by the SAME
fill_simulator conventions as entries, six rules in priority order (first
hit wins), every close through the ONE shared close path
(execution/position_close.py, D-B08) under the Step 3 market lease.

Rules — the monitor loop evaluates R1–R4 per FILLED open position; R5 runs
on the ENTRY paths only (evaluate_swap_for_entry); R6 is enforced by the
entry paths via check_reentry_cooldown:

  R1 exit_liquidity  — book depth_usd < AUTO_EXIT_LIQUIDITY_FLOOR_USDC.
                       CLAUDE.md: "exit at market, no exceptions" — no
                       days-to-resolution guard (D-B17). Finally makes
                       EXIT_NOW mean exit.
  R2 exit_stop       — INTERIM price-move proxy (D-B09): adverse own-side
                       move > PROB_STOP_MOVE_CENTS AND held >= MIN_HOLD_HOURS
                       AND days-to-resolution >= 1 (D-B17: never stop out
                       into the final-24h illiquid window). UPGRADE HOOK: when
                       List A Step 6 provides estimators, replace the
                       price-move proxy with a real shrunk-p_model stop —
                       Class A positions have no p_model and one is never
                       fabricated.
  R3 exit_profit     — own-side gain >= TAKE_PROFIT_GAIN_CENTS (any day
                       count — banking real profit), OR own-side price >=
                       TAKE_PROFIT_MIN_PRICE with days-to-resolution >= 1
                       (D-B17: never sell near-certain value into the
                       final-24h illiquid window).
  R4 exit_time_decay — hours-to-resolution < TIME_DECAY_EXIT_HOURS_REMAINING
                       AND thesis unconfirmed (own-side price within
                       TIME_DECAY_BAND of entry) → PARTIAL reduction by
                       TIME_DECAY_POSITION_REDUCTION_PCT through
                       partial_close_position, at most once per position
                       (D-B19 — the closed_trades ledger is the guard).

Semantics locked by decisions:
  - Triggers evaluate on the position's OWN-side mid; execution prices via
    fill_simulator.taker_exit_price (sell YES → best_bid; sell NO →
    1 − best_ask). R1's liquidity measure is the same snapshot's depth_usd
    (D-B18).
  - No halt (health, drawdown, kill switch) ever gates an exit (D-B04) —
    this module NEVER imports monitoring.health_score or the equity gate;
    a guard test locks that.
  - Book unavailable → fail-soft retry, never a blind exit; dead beyond
    EXIT_BOOK_DEAD_HOURS → cooldown-gated WARNING, still no exit.
  - Unknown end date → the affected rule is skipped (D-B17).
  - Resting makers (order_type='maker' AND fill_price IS NULL) are NOT
    exits — the paper fill monitor owns them (D-13/D-B15).
  - Copy-trade early exits never touch wallet W/L/trust (D-B06 — decided
    inside the shared close path).

This module performs I/O. It must NEVER be imported by risk/risk_engine.py
(purity rule).
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import config
from coordinator import lease
from data.market_discovery import get_market_book, get_market_metadata
from execution.position_close import close_position, partial_close_position
from memory.supabase_client import get_client
from monitoring.telegram_alerts import alert_exit_warning
from risk.cost_model import BookSnapshot, fee_config_for, taker_fee_units
from risk.fill_simulator import (
    signed_exit_slippage,
    taker_exit_depth_ok,
    taker_exit_price,
)

logger = logging.getLogger(__name__)

# Full-exit rule tags (the exit engine's own exit_type vocabulary — the
# shared close path persists them into closed_trades.exit_type).
RULE_LIQUIDITY = "exit_liquidity"
RULE_STOP = "exit_stop"
RULE_PROFIT = "exit_profit"
RULE_TIME_DECAY = "exit_time_decay"
RULE_SWAP = "exit_swap"

# ── Counters (service-module registry — equity/health/recon precedent) ───────

_tick_counters: dict[str, int] = {
    "exit:closed": 0,
    "exit:partial_reduced": 0,
    "exit:partial_already_reduced": 0,
    "exit:book_unavailable": 0,
    "exit:end_date_unavailable": 0,
    "exit:lease_skipped": 0,
    "exit:depth_rejected": 0,
    "exit:unpriceable": 0,
    "exit:resting_skipped": 0,
    "exit:already_closed": 0,
    "exit:failed": 0,
}


def _record(reason: str) -> None:
    _tick_counters[reason] = _tick_counters.get(reason, 0) + 1


def get_exit_counters() -> dict[str, int]:
    """Copy of the exit-engine counters."""
    return _tick_counters.copy()


# Exit attempts that could not complete this tick (lease skip, thin book,
# I/O failure) — retried next tick; the runtime recon's daily digest reports
# the count as "pending exits" (Step 4's "(after Step 5)" slot).
_pending_exits: dict[str, str] = {}  # position id -> rule tag

# Per-market first-unseen timestamp for books (EXIT_BOOK_DEAD_HOURS alert).
_book_missing_since: dict[str, datetime] = {}

# In-process end-date cache: market_id -> (end_dt | None, fetched_at).
_end_date_cache: dict[str, tuple[Optional[datetime], datetime]] = {}

_last_alert_at: dict[str, datetime] = {}


def reset_state_for_tests() -> None:
    """Reset module state (counters, pending exits, book-dead tracking,
    end-date cache, alert cooldowns) for test isolation."""
    for key in _tick_counters:
        _tick_counters[key] = 0
    _pending_exits.clear()
    _book_missing_since.clear()
    _end_date_cache.clear()
    _last_alert_at.clear()


def get_pending_exit_count() -> int:
    """Exit attempts awaiting completion — the recon digest's pending-exits slot."""
    return len(_pending_exits)


def _dispatch_cooldown_warning(trigger: str, detail: str, action: str, now: datetime) -> None:
    """Fire an exit WARNING under the per-trigger cooldown (global DO-NOT:
    cooldowns required on repeating alerts). Fire-and-forget; skipped when no
    event loop is running (unit-test context without a loop)."""
    last = _last_alert_at.get(trigger)
    if last is not None and (now - last).total_seconds() < config.EXIT_ALERT_COOLDOWN_SECONDS:
        return
    _last_alert_at[trigger] = now
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        logger.warning("[EXIT] %s alert suppressed (no running loop): %s", trigger, detail)
        return
    asyncio.create_task(alert_exit_warning(trigger, detail, action))


# ── Pure rule evaluation ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class ExitDecision:
    """The first rule hit for one position (first hit wins)."""

    rule: str            # exit_type tag: RULE_LIQUIDITY / STOP / PROFIT / TIME_DECAY
    reason: str          # human-readable detail for closed_trades.exit_reason
    partial: bool        # True only for R4 — reduce the row, don't close it
    mark_price: float    # own-side mid the rule triggered on (slippage reference)


def _own_side_mid(book: BookSnapshot, direction: str) -> float:
    """The position's own-line mid: the YES mid for YES, 1 − the YES mid for
    NO (the established mirroring convention — open_positions.market_id is
    the YES token's book id)."""
    return book.mid if direction == "YES" else 1.0 - book.mid


def evaluate_exit_rules(
    *,
    direction: str,
    entry_price: float,
    opened_at: datetime,
    book: BookSnapshot,
    end_dt: Optional[datetime],
    now: datetime,
) -> Optional[ExitDecision]:
    """Evaluate R1–R4 in priority order for one FILLED position; first hit
    wins. PURE: no I/O, deterministic, thresholds from config only.

    days-to-resolution uses datetime floor semantics (delta.days — matches
    the pipeline's deadline math); hours-to-resolution for R4's 72h key is
    the exact remaining time. end_dt None (unavailable) skips every rule
    that needs the calendar (D-B17: fail-soft-to-hold, never a blind exit).
    """
    if direction not in ("YES", "NO"):
        return None
    own_mid = _own_side_mid(book, direction)

    # Days/hours remaining (None when the end date is unavailable).
    days_to_res: Optional[int] = None
    hours_to_res: Optional[float] = None
    if end_dt is not None:
        delta = end_dt - now
        days_to_res = max(0, delta.days)
        hours_to_res = delta.total_seconds() / 3600.0

    # R1 — liquidity auto-exit. depth_usd is the measure (D-B18); the floor
    # has NO days guard (CLAUDE.md: "exit at market, no exceptions").
    if book.depth_usd < config.AUTO_EXIT_LIQUIDITY_FLOOR_USDC:
        return ExitDecision(
            rule=RULE_LIQUIDITY,
            reason=(
                f"liquidity {book.depth_usd:.0f} < floor "
                f"{config.AUTO_EXIT_LIQUIDITY_FLOOR_USDC}"
            ),
            partial=False,
            mark_price=own_mid,
        )

    # R2 — probability/edge-decay stop, price-move proxy interim (D-B09).
    adverse_move = entry_price - own_mid  # positive = the market moved against us
    held_hours = (now - opened_at).total_seconds() / 3600.0
    if (
        adverse_move > config.PROB_STOP_MOVE_CENTS
        and held_hours >= config.MIN_HOLD_HOURS
        and days_to_res is not None
        and days_to_res >= 1  # D-B17: never stop out into the final-24h window
    ):
        return ExitDecision(
            rule=RULE_STOP,
            reason=(
                f"adverse move {adverse_move:.4f} > {config.PROB_STOP_MOVE_CENTS} "
                f"(proxy), held {held_hours:.1f}h >= {config.MIN_HOLD_HOURS}h"
            ),
            partial=False,
            mark_price=own_mid,
        )

    # R3 — profit-taking. Branch 1 banks a real gain at any day count;
    # branch 2 (deep in-the-money) refuses the final-24h illiquid window.
    gain = own_mid - entry_price
    if gain >= config.TAKE_PROFIT_GAIN_CENTS:
        return ExitDecision(
            rule=RULE_PROFIT,
            reason=f"gain {gain:.4f} >= {config.TAKE_PROFIT_GAIN_CENTS}",
            partial=False,
            mark_price=own_mid,
        )
    if (
        own_mid >= config.TAKE_PROFIT_MIN_PRICE
        and days_to_res is not None
        and days_to_res >= 1  # D-B17
    ):
        return ExitDecision(
            rule=RULE_PROFIT,
            reason=(
                f"price {own_mid:.4f} >= {config.TAKE_PROFIT_MIN_PRICE} "
                f"with {days_to_res}d to resolution"
            ),
            partial=False,
            mark_price=own_mid,
        )

    # R4 — time decay: thesis unconfirmed (price still in the entry band)
    # near resolution → partial reduction, once per position (D-B19).
    if (
        hours_to_res is not None
        and hours_to_res < config.TIME_DECAY_EXIT_HOURS_REMAINING
        and abs(own_mid - entry_price) <= config.TIME_DECAY_BAND
    ):
        return ExitDecision(
            rule=RULE_TIME_DECAY,
            reason=(
                f"{hours_to_res:.1f}h to resolution < "
                f"{config.TIME_DECAY_EXIT_HOURS_REMAINING}h, in-band "
                f"|delta|={abs(own_mid - entry_price):.4f} <= {config.TIME_DECAY_BAND}"
            ),
            partial=True,
            mark_price=own_mid,
        )

    return None


# ── Reads ────────────────────────────────────────────────────────────────────

def _is_resting_maker(row: dict[str, Any]) -> bool:
    """Resting maker = the unfilled-maker predicate (D-13) — not an exit."""
    return row.get("order_type") == "maker" and row.get("fill_price") is None


def _parse_ts(raw: Any) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


async def _fetch_open_positions(client: Any) -> list[dict[str, Any]]:
    """All open positions, oldest first."""
    async def _read() -> list[dict[str, Any]]:
        builder = client.table("open_positions").select("*").order("opened_at")
        res = await asyncio.to_thread(builder.execute)
        return list(res.data or [])

    async with asyncio.timeout(config.SUPABASE_TIMEOUT_SECONDS):
        return await _read()


async def _fetch_row_by_id(client: Any, row_id: str) -> Optional[dict[str, Any]]:
    """Fresh copy of one open row (re-check inside the lease — paper_fills
    may have filled or cancelled it since the tick's scan and does not take
    the lease; the fresh read is the consistency point)."""
    async def _read() -> list[dict[str, Any]]:
        builder = client.table("open_positions").select("*").eq("id", row_id).limit(1)
        res = await asyncio.to_thread(builder.execute)
        return list(res.data or [])

    async with asyncio.timeout(config.SUPABASE_TIMEOUT_SECONDS):
        rows = await _read()
    return rows[0] if rows else None


async def _time_decay_already_applied(client: Any, market_id: str, direction: str) -> bool:
    """R4's once-per-position guard (D-B19): True when the ledger already
    carries an exit_time_decay row for this (market_id, direction). The
    ledger is the truth — no new open_positions column. FAIL-CLOSED for the
    reduction: a read failure reads as already-reduced (the guard must never
    license a second sell), which merely skips this tick's reduction."""
    async def _read() -> list[dict[str, Any]]:
        builder = (
            client.table("closed_trades")
            .select("id")
            .eq("market_id", market_id)
            .eq("direction", direction)
            .eq("exit_type", RULE_TIME_DECAY)
            .limit(1)
        )
        res = await asyncio.to_thread(builder.execute)
        return list(res.data or [])

    try:
        async with asyncio.timeout(config.SUPABASE_TIMEOUT_SECONDS):
            rows = await _read()
        return bool(rows)
    except Exception as e:
        logger.warning(
            "[EXIT] Time-decay guard read failed for %s — treating as already "
            "reduced (never re-reduce): %s: %s",
            market_id[:16], type(e).__name__, e,
        )
        return True


async def _get_end_dt(market_id: str, now: datetime) -> Optional[datetime]:
    """The market's end date (TTL-cached; Gamma on miss). None = unavailable
    → the calendar-dependent rules are skipped this tick (D-B17)."""
    cached = _end_date_cache.get(market_id)
    if cached is not None and (now - cached[1]).total_seconds() < config.EXIT_END_DATE_TTL_SECONDS:
        return cached[0]
    try:
        metadata = await get_market_metadata(market_id)
        end_dt = _parse_ts(metadata.get("end_date_iso"))
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.warning(
            "[EXIT] End-date fetch failed for %s (calendar rules skip): %s: %s",
            market_id[:16], type(e).__name__, e,
        )
        end_dt = None
    _end_date_cache[market_id] = (end_dt, now)
    if end_dt is None:
        _record("exit:end_date_unavailable")
    return end_dt


# ── Exit execution (under the market lease, through the shared close path) ──

def _book_missing_alert(market_id: str, now: datetime) -> None:
    """Track first-unseen per market; a book dead beyond EXIT_BOOK_DEAD_HOURS
    fires a cooldown-gated WARNING — and STILL no exit (spec)."""
    first = _book_missing_since.setdefault(market_id, now)
    dead_hours = (now - first).total_seconds() / 3600.0
    if dead_hours >= config.EXIT_BOOK_DEAD_HOURS:
        _dispatch_cooldown_warning(
            "EXIT_BOOK_DEAD",
            f"Market {market_id[:16]} book unavailable for {dead_hours:.1f}h "
            f"(>= {config.EXIT_BOOK_DEAD_HOURS}h) — the exit engine cannot "
            "price an exit",
            "No blind exit was taken (fail-soft retry continues). Check the "
            "market on Polymarket; close manually if it is dead.",
            now,
        )


async def _exit_under_lease(
    client: Any,
    row: dict[str, Any],
    decision: ExitDecision,
    book: BookSnapshot,
    now: datetime,
) -> bool:
    """Execute one exit decision under the market lease, through the shared
    close path. Returns True when the close/reduction ran.

    Lease CONFLICT/ERROR → skip this tick (the entry path holds the lease
    for ~seconds; the next tick retries). The row is re-read INSIDE the
    lease; a vanished row skips cleanly (close_position's claim guard stays
    the second line). Depth is re-checked for the actual sell size — a book
    that cannot carry the exit is retried next tick, never forced."""
    market_id = str(row.get("market_id") or "")
    direction = str(row.get("direction") or "")
    position_id = str(row.get("id"))

    owner = lease.get_instance_id()
    outcome = await lease.acquire_lease(market_id, owner, config.LEASE_TTL_SECONDS)
    if outcome is not lease.LeaseOutcome.CLAIMED:
        _record("exit:lease_skipped")
        _pending_exits[position_id] = decision.rule
        return False
    try:
        fresh = await _fetch_row_by_id(client, position_id)
        if fresh is None:
            logger.info(
                "[EXIT] Row %s vanished before the exit (closed elsewhere) — skipping",
                position_id[:8],
            )
            _record("exit:already_closed")
            _pending_exits.pop(position_id, None)
            return False

        entry_price = float(fresh["entry_price"])
        size_usdc = float(fresh["position_size_usdc"])
        shares_total = size_usdc / entry_price

        fill = taker_exit_price(book, direction)
        if fill is None:
            _record("exit:unpriceable")
            _pending_exits[position_id] = decision.rule
            return False

        sell_shares = shares_total * (
            config.TIME_DECAY_POSITION_REDUCTION_PCT if decision.partial else 1.0
        )
        if not taker_exit_depth_ok(book, sell_shares, direction):
            _record("exit:depth_rejected")
            _pending_exits[position_id] = decision.rule
            logger.warning(
                "[EXIT] %s book cannot carry the %s sell (%.0f shares) — retry next tick",
                market_id[:16], decision.rule, sell_shares,
            )
            return False

        fee_cfg = fee_config_for(str(fresh.get("category") or "Other"))
        fee_units = taker_fee_units(fill, fee_cfg)
        slippage = signed_exit_slippage(fill, decision.mark_price, direction)
        logger.info(
            "[OBSERVABILITY][EXIT] market=%s dir=%s rule=%s mark=%.4f fill=%.4f "
            "slippage=%.4f fee_units=%.4f depth=%.0f shares=%.1f",
            market_id[:16], direction, decision.rule, decision.mark_price,
            fill, slippage if slippage is not None else float("nan"),
            fee_units, book.depth_usd, sell_shares,
        )

        if decision.partial:
            # R4: at most once per position (D-B19) — the ledger is the guard.
            if await _time_decay_already_applied(client, market_id, direction):
                _record("exit:partial_already_reduced")
                _pending_exits.pop(position_id, None)
                return False
            res = await partial_close_position(
                client,
                fresh,
                fraction=config.TIME_DECAY_POSITION_REDUCTION_PCT,
                exit_price=fill,
                exit_type=decision.rule,
                exit_reason=decision.reason,
                notes="Time-decay partial reduction by the exit engine (R4).",
            )
        else:
            res = await close_position(
                client,
                fresh,
                exit_price=fill,
                exit_type=decision.rule,
                exit_reason=decision.reason,
                notes=f"Exited by the exit engine ({decision.rule}).",
            )

        if res.closed:
            if decision.partial:
                _record("exit:partial_reduced")
            else:
                _record("exit:closed")
            _pending_exits.pop(position_id, None)
            return True

        _record("exit:already_closed")
        _pending_exits.pop(position_id, None)
        return False
    finally:
        await lease.release_lease(market_id, owner)


async def _process_position(
    client: Any, row: dict[str, Any], now: datetime
) -> None:
    """One position: book → end date → rules → execute. Per-position
    isolation is the caller's concern; book/end-date failures are fail-soft
    (retry, never a blind exit)."""
    market_id = str(row.get("market_id") or "")
    direction = str(row.get("direction") or "")
    position_id = str(row.get("id"))

    book = await get_market_book(market_id)
    if book is None:
        _record("exit:book_unavailable")
        _book_missing_alert(market_id, now)
        return
    _book_missing_since.pop(market_id, None)

    entry_price = float(row["entry_price"])
    opened_at = _parse_ts(row.get("opened_at"))
    if opened_at is None:
        # A corrupt timestamp must never enable the hold-time gate — treating
        # it as "opened now" holds R2 (0h < MIN_HOLD_HOURS) and nothing else.
        logger.warning(
            "[EXIT] Unparseable opened_at on %s — R2 held-time gate held closed",
            position_id[:8],
        )
        opened_at = now

    end_dt = await _get_end_dt(market_id, now)

    decision = evaluate_exit_rules(
        direction=direction,
        entry_price=entry_price,
        opened_at=opened_at,
        book=book,
        end_dt=end_dt,
        now=now,
    )
    if decision is None:
        return

    try:
        ran = await _exit_under_lease(client, row, decision, book, now)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        _record("exit:failed")
        _pending_exits[position_id] = decision.rule
        logger.error(
            "[EXIT] %s failed for %s (retries next tick): %s: %s",
            decision.rule, market_id[:16], type(e).__name__, e,
        )
        return
    if not ran:
        _pending_exits[position_id] = decision.rule


# ── Tick + supervisor ────────────────────────────────────────────────────────

async def run_exit_engine_once(now: Optional[datetime] = None) -> None:
    """One exit tick: scan open positions, evaluate the rules per FILLED
    position, execute first-hit exits under the lease. Fail-soft: a failed
    read raises (the supervisor retries next tick); per-position isolation
    keeps one poisoned row from wedging the scan."""
    checked_at = now or datetime.now(timezone.utc)
    client = await get_client()

    rows = await _fetch_open_positions(client)

    for row in rows:
        if _is_resting_maker(row):
            # A resting order owns no shares — the paper fill monitor owns
            # its lifecycle (D-13/D-B15); the exit engine never touches it.
            _record("exit:resting_skipped")
            continue
        try:
            await _process_position(client, row, checked_at)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # Per-row isolation: one poisoned position never kills the scan —
            # the next tick retries this row while the others keep exiting.
            _record("exit:failed")
            logger.error(
                "[EXIT] Position %s crashed the rule pass (retries next tick): %s: %s",
                str(row.get("id"))[:8], type(e).__name__, e,
            )

    logger.info(
        "[OBSERVABILITY][EXITS] open_positions=%d pending_exits=%d counters=%s",
        len(rows), get_pending_exit_count(),
        {k: v for k, v in _tick_counters.items() if v},
    )


async def run_exit_engine() -> None:
    """Supervisor entry: monitor forever. paper_fills/recon pattern (E-B12):
    fail-soft tick — a failed tick logs and retries next interval;
    CancelledError is always re-raised."""
    logger.info(
        "[EXIT] Exit engine supervisor started (interval=%ds, book_dead=%dh, "
        "liquidity_floor=%d)",
        config.EXIT_MONITOR_INTERVAL_SECONDS, config.EXIT_BOOK_DEAD_HOURS,
        config.AUTO_EXIT_LIQUIDITY_FLOOR_USDC,
    )
    while True:
        try:
            await run_exit_engine_once()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(
                "[EXIT] Tick failed (fail-soft, retries next tick): %s: %s",
                type(e).__name__, e,
            )
        await asyncio.sleep(config.EXIT_MONITOR_INTERVAL_SECONDS)
