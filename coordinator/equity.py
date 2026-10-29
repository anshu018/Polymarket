"""
Mark-to-market equity and drawdown anchors — Part B Step 1 (B2).

Design contract (List-B.md §6 Step 1; decisions D-B03, D-B12):

- Equity = cash + Σ over open positions of shares × current_price(direction),
  where shares = position_size_usdc / entry_price and a NO-side position is
  worth shares × (1 − price_yes). `market_id` holds the YES token's book id
  (the established paper_fills / fill_simulator convention), so
  get_market_price(market_id) IS price_yes.
- Paper cash is HONEST cash (D-B12), not the bare starting constant:
  PAPER_TRADING_PORTFOLIO_USDC + Σ closed_trades.pnl_usdc (realized)
  − Σ cost committed to open filled positions. The literal constant would
  make every position resolution look like an equity drop (a win counted as
  drawdown) and realized P&L would never count. Live cash is the CLOB
  collateral balance, which already nets everything.
- A resting maker order (order_type='maker' AND fill_price IS NULL — the
  idx_open_positions_unfilled_maker predicate) owns no shares and has
  committed no cash: it contributes zero to equity and is counted in the
  snapshot log.
- Per-position price fetch failure → the position is valued at its entry
  price (conservative: never fabricate gains); the fallback is counted and
  shown in the snapshot log line. Book fetches are capped at
  EQUITY_MAX_MARKED_POSITIONS per pass (risk register: cap fetches);
  positions beyond the cap use the same entry-price fallback.
- Anchors live in the existing daily_performance table (one row per UTC day,
  upsert on `date` — no schema change). Daily start = today's row's
  starting_balance_usdc; weekly start = starting_balance_usdc of the earliest
  row in the current ISO week; monthly = earliest row of the current calendar
  month. When the period's first day is missing (bot was down) or the
  bootstrap row becomes the period anchor mid-period, the earliest available
  row is used AND a WARNING alert (EQUITY_ANCHOR_BOOTSTRAP) fires — a silent
  bootstrap is forbidden (D-B03).
- A supervisor task refreshes an in-process cache every
  EQUITY_SNAPSHOT_INTERVAL_SECONDS. Entry paths read ONLY the cache (zero DB
  reads on the trade path): cache absent → `equity:unavailable`, older than
  EQUITY_STALE_HALT_MINUTES → `equity:stale`. Both BLOCK the entry
  (fail-closed) — an equity system that silently returns zeros is exactly the
  bug this module kills, so a blind breaker is worse than no trading. ANY
  failure inside a tick (DB read or anchor write) leaves the cache untouched
  so it ages toward fail-closed: the anchor write is capital-safety-adjacent
  (a silently stale anchor masks drawdown), so it does NOT get the telemetry
  fail-soft treatment.

This module performs I/O. It must NEVER be imported by risk/risk_engine.py
(purity rule), and nothing here can place an order or block an exit.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

import config
from data.market_discovery import get_market_price
from memory.supabase_client import get_client
from monitoring.telegram_alerts import alert_equity_warning

logger = logging.getLogger(__name__)

_PERIODS = ("daily", "weekly", "monthly")


@dataclass(frozen=True)
class MarkedEquity:
    """One mark-to-market computation plus its diagnostics."""

    value: float                    # cash + marked positions
    cash: float                     # paper: starting constant + realized − committed; live: CLOB collateral
    marked_positions_value: float   # Σ shares × current_price(direction)
    positions_marked: int           # filled positions valued this pass
    fallback_positions: int         # valued at entry price (fetch failed, capped, or unusable row)
    skipped_resting: int            # resting makers: no shares, no committed cash


@dataclass(frozen=True)
class EquityGate:
    """Fresh-checked equity snapshot handed to the entry paths."""

    as_of: datetime
    current_equity: float
    starting_balances: dict[str, float]   # period-start equity per daily/weekly/monthly
    current_balances: dict[str, float]    # current equity per period (always the live marked value)


# ── Snapshot cache & gate state (in-process; single event loop, no lock needed) ──

_snapshot_cache: Optional[EquityGate] = None
_last_gate_failure: Optional[str] = None
_gate_drops: dict[str, int] = {"equity:stale": 0, "equity:unavailable": 0}
_last_alert_at: dict[str, datetime] = {}


def get_drop_counters() -> dict[str, int]:
    """Copy of the equity gate drop counters (equity:stale / equity:unavailable)."""
    return _gate_drops.copy()


def get_last_gate_failure() -> str:
    """Reason of the most recent entry-gate failure ('equity:stale' | 'equity:unavailable')."""
    return _last_gate_failure or "equity:unavailable"


# ── Helpers ──────────────────────────────────────────────────────────────────

def _to_float(raw: Any, default: float = 0.0) -> float:
    """Supabase NUMERIC columns arrive as float or string — coerce defensively."""
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _is_resting_maker(row: dict[str, Any]) -> bool:
    """Resting maker = the unfilled-maker predicate of idx_open_positions_unfilled_maker."""
    return row.get("order_type") == "maker" and row.get("fill_price") is None


def _entry_price(row: dict[str, Any]) -> Optional[float]:
    """Cost-basis entry price: fill_price → market_price_at_entry → entry_price.

    fill_price is the truth for filled rows; market_price_at_entry covers
    legacy rows written before the Step 5 fill columns; entry_price is NOT NULL.
    Returns None only when every price column is unusable (row then contributes
    zero and counts as a fallback — never fabricate a gain)."""
    for key in ("fill_price", "market_price_at_entry", "entry_price"):
        value = _to_float(row.get(key), 0.0)
        if value > 0.0:
            return value
    return None


def _dispatch_equity_alert(event: str, trigger: str, state: str, action: str, now: datetime) -> None:
    """Send an equity WARNING with a per-event cooldown (global DO-NOT list:
    cooldowns required on repeating alerts). Fire-and-forget; skipped when no
    event loop is running (unit-test context without a loop)."""
    last = _last_alert_at.get(event)
    if last is not None and (now - last).total_seconds() < config.EQUITY_ALERT_COOLDOWN_SECONDS:
        return
    _last_alert_at[event] = now
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        logger.warning("[EQUITY] %s alert suppressed (no running event loop): %s", event, trigger)
        return
    asyncio.create_task(alert_equity_warning(event, trigger, state, action))


# ── Mark-to-market ───────────────────────────────────────────────────────────

async def _fetch_open_positions() -> list[dict[str, Any]]:
    """All open positions (oldest first — the book-fetch cap marks the oldest)."""
    async with asyncio.timeout(config.SUPABASE_TIMEOUT_SECONDS):
        client = await get_client()
        res = (
            client.table("open_positions")
            .select(
                "market_id,direction,entry_price,position_size_usdc,"
                "fill_price,market_price_at_entry,order_type,opened_at"
            )
            .order("opened_at")
            .execute()
        )
        return res.data or []


async def _fetch_realized_pnl() -> float:
    """Σ closed_trades.pnl_usdc — realized P&L to date (paper cash component)."""
    async with asyncio.timeout(config.SUPABASE_TIMEOUT_SECONDS):
        client = await get_client()
        res = client.table("closed_trades").select("pnl_usdc").execute()
        return sum(_to_float(r.get("pnl_usdc"), 0.0) for r in res.data or [])


async def _get_live_collateral() -> float:
    """CLOB USDC collateral (live mode only). Raises on failure — unknown cash
    means unknown equity, and the tick must not refresh the cache."""
    from execution.polymarket_auth import get_polymarket_client
    from py_clob_client.clob_types import BalanceAllowanceParams, AssetType

    client = get_polymarket_client()
    usdc_res = client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
    return float(usdc_res.get("balance", "0")) / 1000000.0


async def _mark_position_row(row: dict[str, Any], fetch_live: bool) -> tuple[float, bool]:
    """Value one filled position: shares × current_price(direction).

    Returns (value, used_entry_fallback). Price fetch failure → entry price
    (conservative, counted); an unusable row contributes 0.0 and counts as a
    fallback so it can never fabricate a gain. NO-side mirrors the YES price
    (1 − price_yes) — the fill_simulator NO convention."""
    entry = _entry_price(row)
    size = _to_float(row.get("position_size_usdc"), 0.0)
    direction = str(row.get("direction") or "").upper()
    if entry is None or entry <= 0.0 or size <= 0.0 or direction not in ("YES", "NO"):
        logger.error(
            "[EQUITY] Unusable position row (market=%s direction=%s entry=%s size=%s) — valued at 0 (counted as fallback)",
            str(row.get("market_id"))[:16], row.get("direction"), entry, size,
        )
        return 0.0, True

    shares = size / entry
    price = entry
    used_fallback = True
    if fetch_live:
        market_id = str(row.get("market_id") or "")
        try:
            price = await get_market_price(market_id)
            used_fallback = False
        except Exception as e:
            logger.warning(
                "[EQUITY] Price fetch failed for %s — valuing at entry %.4f: %s",
                market_id[:16], entry, e,
            )
    if direction == "NO":
        return shares * (1.0 - price), used_fallback
    return shares * price, used_fallback


async def mark_to_market_value(now: Optional[datetime] = None) -> MarkedEquity:
    """Compute portfolio equity: cash + Σ shares × current_price(direction).

    Paper cash = PAPER_TRADING_PORTFOLIO_USDC + realized P&L − committed open
    cost (D-B12); live cash = CLOB collateral. Resting makers contribute zero.
    Per-position failures fall back to entry price (counted); structural
    failures (DB reads, live balance) raise so the caller leaves the cache
    untouched (fail-closed aging)."""
    rows = await _fetch_open_positions()
    filled = [r for r in rows if not _is_resting_maker(r)]
    skipped_resting = len(rows) - len(filled)

    if config.PAPER_TRADING:
        committed = sum(_to_float(r.get("position_size_usdc"), 0.0) for r in filled)
        cash = config.PAPER_TRADING_PORTFOLIO_USDC + await _fetch_realized_pnl() - committed
    else:
        cash = await _get_live_collateral()

    marked_value = 0.0
    fallbacks = 0
    for index, row in enumerate(filled):
        fetch_live = index < config.EQUITY_MAX_MARKED_POSITIONS
        value, used_fallback = await _mark_position_row(row, fetch_live=fetch_live)
        marked_value += value
        if used_fallback:
            fallbacks += 1

    return MarkedEquity(
        value=cash + marked_value,
        cash=cash,
        marked_positions_value=marked_value,
        positions_marked=len(filled),
        fallback_positions=fallbacks,
        skipped_resting=skipped_resting,
    )


# ── Period anchors (daily_performance) ───────────────────────────────────────

async def _upsert_daily_anchor(client: Any, today: date, current_equity: float) -> float:
    """Create or extend today's anchor row; return the day's starting balance.

    Missing row → bootstrap (anchor = current equity). Existing row → only the
    ending balance and daily P&L advance; the starting anchor NEVER moves
    (a moving anchor would mask drawdown — the bug this step kills)."""
    async with asyncio.timeout(config.SUPABASE_TIMEOUT_SECONDS):
        res = (
            client.table("daily_performance")
            .select("starting_balance_usdc")
            .eq("date", today.isoformat())
            .limit(1)
            .execute()
        )
        rows = res.data or []

    if rows:
        starting = _to_float(rows[0].get("starting_balance_usdc"), 0.0)
        if starting <= 0.0:
            # A NULL/zero anchor is corrupted equity truth: feeding it to the
            # breaker would either divide by zero or report infinite drawdown.
            # Fail closed — the cache is not refreshed and entries block until
            # the row is fixed.
            raise RuntimeError(
                f"[EQUITY] daily_performance row for {today.isoformat()} has a "
                f"non-positive starting anchor ({starting}) — refusing to derive balances"
            )
        async with asyncio.timeout(config.SUPABASE_TIMEOUT_SECONDS):
            client.table("daily_performance").update({
                "ending_balance_usdc": current_equity,
                "daily_pnl_usdc": current_equity - starting,
            }).eq("date", today.isoformat()).execute()
        return starting

    async with asyncio.timeout(config.SUPABASE_TIMEOUT_SECONDS):
        client.table("daily_performance").upsert({
            "date": today.isoformat(),
            "starting_balance_usdc": current_equity,
            "ending_balance_usdc": current_equity,
            "daily_pnl_usdc": 0.0,
        }, on_conflict="date").execute()
    logger.info(
        "[EQUITY] Daily anchor bootstrapped for %s (equity=%.2f)",
        today.isoformat(), current_equity,
    )
    return current_equity


async def _earliest_anchor(
    client: Any, period_start: date, period: str, now: datetime
) -> float:
    """starting_balance_usdc of the earliest anchor row in the period.

    When the period's first day is missing (bot was down) or the bootstrapped
    row became the period anchor mid-period, the earliest available row is the
    documented approximation and a WARNING fires (D-B03: a silent bootstrap is
    forbidden). Called after today's row exists, so a missing row means the
    equity truth itself is broken → raise (fail-closed)."""
    async with asyncio.timeout(config.SUPABASE_TIMEOUT_SECONDS):
        res = (
            client.table("daily_performance")
            .select("date,starting_balance_usdc")
            .gte("date", period_start.isoformat())
            .order("date")
            .limit(1)
            .execute()
        )
        rows = res.data or []
    if not rows:
        raise RuntimeError(
            f"[EQUITY] No {period} anchor row on/after {period_start.isoformat()} "
            "despite today's row existing — daily_performance read is inconsistent"
        )

    row_date = str(rows[0].get("date"))[:10]
    starting = _to_float(rows[0].get("starting_balance_usdc"), 0.0)
    if starting <= 0.0:
        raise RuntimeError(
            f"[EQUITY] {period} anchor row {row_date} has a non-positive "
            f"starting balance ({starting}) — refusing to derive balances"
        )
    if row_date != period_start.isoformat():
        _dispatch_equity_alert(
            "EQUITY_ANCHOR_BOOTSTRAP",
            f"{period} anchor begins {row_date} but the period began "
            f"{period_start.isoformat()} (first day missing or anchor bootstrapped mid-period)",
            f"{period} drawdown measured from {starting:.2f}",
            "Documented approximation: the first-day anchor is absent, so "
            f"{period} drawdown is a lower bound until a full period completes.",
            now,
        )
    return starting


async def get_period_balances(
    now: datetime, current_equity: float
) -> dict[str, tuple[float, float]]:
    """Upsert today's anchor row, then derive (start, current) per period.

    Weekly anchor = earliest row of the current ISO week (Monday-based);
    monthly = earliest row of the current calendar month. `current` is the
    live marked equity for every period."""
    today = now.date()
    week_start = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)

    client = await get_client()
    daily_start = await _upsert_daily_anchor(client, today, current_equity)
    weekly_start = await _earliest_anchor(client, week_start, "weekly", now)
    monthly_start = await _earliest_anchor(client, month_start, "monthly", now)

    return {
        "daily": (daily_start, current_equity),
        "weekly": (weekly_start, current_equity),
        "monthly": (monthly_start, current_equity),
    }


# ── Snapshot cache & entry gate ──────────────────────────────────────────────

async def take_equity_snapshot_once(now: Optional[datetime] = None) -> bool:
    """One snapshot pass: mark to market, upsert the daily anchor, refresh the
    cache. Returns True when the cache was refreshed.

    On failure the exception propagates (the supervisor logs and continues) and
    the cache is left untouched so the entry gate ages toward fail-closed."""
    checked_at = now or datetime.now(timezone.utc)
    marked = await mark_to_market_value(checked_at)
    if marked.value <= 0.0:
        # A portfolio at or below zero cannot anchor a drawdown calculation
        # (check_drawdown divides by the starting balance) and there is nothing
        # left to trade — fail closed: the cache stays cold, entries block.
        raise RuntimeError(
            f"[EQUITY] Marked equity {marked.value:.2f} <= 0 — refusing to anchor"
        )
    period_balances = await get_period_balances(checked_at, marked.value)

    global _snapshot_cache
    _snapshot_cache = EquityGate(
        as_of=checked_at,
        current_equity=marked.value,
        starting_balances={p: start for p, (start, _c) in period_balances.items()},
        current_balances={p: current for p, (_s, current) in period_balances.items()},
    )
    logger.info(
        "[EQUITY] Snapshot: equity=%.2f cash=%.2f marked=%.2f positions=%d "
        "fallbacks=%d skipped_resting=%d anchors=%s",
        marked.value, marked.cash, marked.marked_positions_value,
        marked.positions_marked, marked.fallback_positions, marked.skipped_resting,
        {p: round(s, 2) for p, (s, _c) in period_balances.items()},
    )
    return True


async def run_equity_snapshot_supervisor() -> None:
    """Supervisor entry: refresh the equity snapshot forever.

    paper_fills pattern (E-B12): fail-soft tick — a failed tick logs and
    retries next interval while entries fail closed on the aging cache;
    CancelledError is always re-raised."""
    logger.info(
        "[EQUITY] Equity snapshot supervisor started (interval=%ds, stale_halt=%dmin)",
        config.EQUITY_SNAPSHOT_INTERVAL_SECONDS, config.EQUITY_STALE_HALT_MINUTES,
    )
    while True:
        try:
            await take_equity_snapshot_once()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(
                "[EQUITY] Snapshot tick failed (continues; entries fail closed "
                "while the cache ages): %s", e,
            )
        await asyncio.sleep(config.EQUITY_SNAPSHOT_INTERVAL_SECONDS)


def get_entry_gate(now: Optional[datetime] = None) -> Optional[EquityGate]:
    """Entry-side equity gate (fail-closed, cache-only — zero DB reads on the
    trade path, so Supabase call-order tests never shift because of it).

    Returns the cached snapshot, or None when equity was never computed
    (`equity:unavailable`) or is older than EQUITY_STALE_HALT_MINUTES
    (`equity:stale`). A None return means the caller MUST block the entry and
    log the reason from get_last_gate_failure()."""
    global _last_gate_failure
    checked_at = now or datetime.now(timezone.utc)
    gate = _snapshot_cache

    if gate is None:
        reason = "equity:unavailable"
    elif (checked_at - gate.as_of).total_seconds() > config.EQUITY_STALE_HALT_MINUTES * 60.0:
        reason = "equity:stale"
    else:
        return gate

    _last_gate_failure = reason
    _gate_drops[reason] += 1
    if gate is None:
        age_desc = "never computed (supervisor not started or dead)"
    else:
        age_desc = f"{(checked_at - gate.as_of).total_seconds():.0f}s old (threshold {config.EQUITY_STALE_HALT_MINUTES * 60:.0f}s)"
    logger.warning("[EQUITY][DROP:%s] entry blocked fail-closed (cache %s)", reason, age_desc)
    _dispatch_equity_alert(
        "EQUITY_STALE" if reason == "equity:stale" else "EQUITY_UNAVAILABLE",
        f"Equity snapshot cache {age_desc}",
        "Portfolio equity unknown — drawdown breakers would be blind",
        "New entries blocked fail-closed. Exits unaffected. Check the equity_snapshot task.",
        checked_at,
    )
    return None


async def get_current_portfolio_value() -> float:
    """Current marked equity for sizing/exposure reads.

    Cache-backed (no per-call book fetches — the snapshot task owns I/O).
    A cold cache computes once (boot-race self-heal, before the first
    supervisor tick lands). Raises on failure — callers fail closed; never a
    fabricated constant."""
    if _snapshot_cache is not None:
        return _snapshot_cache.current_equity
    marked = await mark_to_market_value()
    return marked.value
