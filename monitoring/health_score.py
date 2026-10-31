"""
monitoring/health_score.py — B1: wire the health score (List-B.md Step 2).

This module performs I/O. It must NEVER be imported by risk/risk_engine.py
(purity rule): the pure math — compute_health_score / interpret_health_score —
lives in risk_engine and only ever consumes numbers produced here.

Design contract (List-B.md §6 Step 2; decisions D-B04, D-B11):

- SIX PRODUCERS, one per health component, each returning 0-100. Each producer
  FAILS SOFT to a NEUTRAL 50 when its data is absent (a missing input degrades
  the signal; it must not crash the loop and must not fake a healthy score) —
  every fallback is logged. A producer EXCEPTION is also neutral-50 (telemetry
  path, explicitly documented); it never blocks anything by itself.
- The EVALUATION TASK (supervisor pattern from E-B12, interval
  HEALTH_CHECK_INTERVAL_SECONDS) computes the composite score through the pure
  risk_engine.compute_health_score, maps it through
  risk_engine.interpret_health_score, persists the score into TODAY's
  daily_performance row, and caches the resulting mode in-process.
- PERSISTENCE is UPDATE-only: the daily row is owned by the Step 1 equity
  anchor upsert (coordinator/equity.py). If today's row does not exist yet
  (equity task has not ticked), the score is logged and NOT written — never
  insert a date-only row (the orphaned-mock-row incident of the 2026-10-31
  deploy made NULL-anchor rows fail the equity service fail-closed).
- MODE CACHE: starts at NORMAL and is seeded by the first evaluation tick.
  Enforcement reads ONLY this cache — zero DB reads on the trade path (the
  equity gate precedent; Supabase call-order tests must not shift). A cold
  cache reads as NORMAL by design: the score's own producers define absent
  data as neutral-50 (empty-data score 66.7 → NORMAL), and the independent
  fail-closed capital gates (equity staleness, drawdown breakers, idempotency,
  net-edge, exposure) own startup protection. Once evaluated, FULL_HALT is an
  unconditional entry drop on BOTH entry paths (D-B04) — EXITS ARE NEVER
  GATED BY HEALTH, anywhere.
- TRANSITION ALERTS fire only on mode changes (NORMAL→DEFENSIVE→FULL_HALT and
  recoveries), cooldown-gated by HEALTH_ALERT_COOLDOWN_SECONDS. The first
  evaluation seeds silently when NORMAL; booting straight into a degraded mode
  alerts. Entering FULL_HALT additionally fires the existing
  alert_system_halt with real values (reason carries the score; the drawdown
  percentage is the measured worst-period drawdown).
- STRATEGY PROBATION/RETIREMENT: each tick diffs the current
  risk/edge_monitor.derive_allocations states against the last-known states
  and fires alert_strategy_probation on a False→True transition (first tick
  seeds silently — no restart alert storms).
- BRIER ALERT: the long-orphaned alert_brier_score_exceeded
  (telegram_alerts.py) fires when the rolling 30-day agent Brier crosses
  BRIER_SCORE_THRESHOLD (0.23), same cooldown mechanism.
- FEED LATENCY: the pipeline timing wrapper in coordinator/pipeline.py calls
  record_signal_latency for every signal that reached the News Analyst;
  this module keeps a bounded deque and scores each observation linearly —
  at target → 100, at 2× target → 0 (fast path 5s, full path 22s targets).
  SPEC CORRECTION (recorded in the List-B.md session log): the Step 2 spec
  said to "reuse the existing stats-reporter data", but the stats reporter
  holds only counters — no latency exists anywhere, so the pipeline is
  instrumented to measure the real thing.
- CORRELATION COMPONENT is the D-B11 INTERIM definition: category
  concentration, 100 − (max category exposure / MAX_CATEGORY_EXPOSURE_PCT)×100.
  It is replaced by event-cluster exposure when List-B Step 6 lands — recorded
  so it is revisited, not forgotten.

Nothing in this module can place an order, block an exit, or touch a position.
"""

import asyncio
import logging
import math
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Coroutine, Optional

import config
from memory.supabase_client import get_client
from monitoring.telegram_alerts import (
    alert_brier_score_exceeded,
    alert_health_score,
    alert_strategy_probation,
    alert_system_halt,
)
from risk import fill_simulator
from risk import risk_engine
from risk.edge_monitor import StrategyAllocation, derive_allocations

logger = logging.getLogger(__name__)

# Spec-fixed design constant (List-B.md §6 Step 2): every producer fails soft
# to a neutral 50 when its data is absent — logged, never faked healthy.
NEUTRAL_SCORE = 50.0

# Mode → Telegram severity for transition alerts (CLAUDE.md alert format).
_MODE_SEVERITY = {"NORMAL": "INFO", "DEFENSIVE_MODE": "WARNING", "FULL_HALT": "CRITICAL"}


@dataclass(frozen=True)
class BrierSample:
    """Rolling agent-Brier observation: component score plus the raw value the
    Brier alert needs (raw_brier is None when the sample was too small)."""

    component: float
    raw_brier: Optional[float]
    n_resolved: int


@dataclass(frozen=True)
class DrawdownSample:
    """Worst-period drawdown observation: component score plus the measured
    worst drawdown (None when the equity snapshot cache is absent)."""

    component: float
    worst_drawdown_pct: Optional[float]


@dataclass(frozen=True)
class HealthEvaluation:
    """One evaluation tick's outcome (returned for tests and observability)."""

    score: float
    mode: str
    components: dict[str, float]
    raw_brier: Optional[float]
    persisted: bool


# ── Enforcement state (in-process; single event loop, no lock needed) ────────

_current_mode: str = "NORMAL"
_last_announced_mode: Optional[str] = None
_gate_drops: dict[str, int] = {"health:full_halt": 0}
_last_alert_at: dict[str, datetime] = {}
_last_allocations: Optional[dict[str, StrategyAllocation]] = None
_last_raw_brier: Optional[float] = None

# Feed-latency observations (duration_s, fast_path) — bounded ring buffer.
_latency_observations: deque[tuple[float, bool]] = deque(
    maxlen=config.HEALTH_FEED_LATENCY_SAMPLES
)


def get_current_mode() -> str:
    """Cached health mode for the entry gates (NORMAL before the first tick).

    Cache-only by contract: the entry paths must never wait on I/O for this —
    see the module docstring for the cold-cache semantics."""
    return _current_mode


def set_current_mode(mode: str) -> None:
    """Set the cached health mode. Used by the evaluation task; exposed for
    tests and future manual overrides. Unknown modes are rejected loudly —
    a typo must never silently disable enforcement."""
    if mode not in _MODE_SEVERITY:
        raise ValueError(f"Unknown health mode: {mode!r}")
    global _current_mode
    _current_mode = mode


def increment_gate_drop(reason: str) -> None:
    """Count an enforcement drop (authoritative counters live here — the
    coordinator.equity precedent; the pipeline registry is not shared)."""
    _gate_drops[reason] = _gate_drops.get(reason, 0) + 1


def get_gate_drop_counters() -> dict[str, int]:
    """Copy of the health enforcement drop counters (health:full_halt)."""
    return _gate_drops.copy()


def record_signal_latency(duration_s: float, fast_path: bool) -> None:
    """Record one end-to-end pipeline signal latency (coordinator/pipeline.py
    timing wrapper). Non-finite or negative durations are ignored loudly —
    a clock glitch must not poison the component."""
    if not math.isfinite(duration_s) or duration_s < 0.0:
        logger.warning(
            "[HEALTH] Ignoring invalid signal latency observation: %r", duration_s
        )
        return
    _latency_observations.append((duration_s, bool(fast_path)))


# ── Helpers ──────────────────────────────────────────────────────────────────

def _clamp_score(value: float) -> float:
    """Clamp a component score into [0, 100]."""
    return min(100.0, max(0.0, value))


def _optional_float(raw: Any) -> Optional[float]:
    """Coerce a DB numeric to float; None on NULL/unparseable (a missing
    measurement is skipped by every producer, never coerced to 0)."""
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _dispatch_health_alert(event: str, alert: Coroutine[Any, Any, None], now: datetime) -> None:
    """Send a health alert with a per-event cooldown (global DO-NOT list:
    cooldowns required on repeating alerts — the caller owns the cooldown,
    the telegram_alerts functions stay cooldown-free, equity precedent).
    Fire-and-forget; suppressed when no event loop is running (unit tests)."""
    last = _last_alert_at.get(event)
    if last is not None and (now - last).total_seconds() < config.HEALTH_ALERT_COOLDOWN_SECONDS:
        return
    _last_alert_at[event] = now
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        logger.warning("[HEALTH] %s alert suppressed (no running event loop)", event)
        return
    asyncio.create_task(alert)


# ── Producers (each 0-100; fail-soft to NEUTRAL_SCORE on absent data) ────────

async def _win_rate_component(client: Any) -> float:
    """Win rate of the last HEALTH_WIN_RATE_SAMPLE_SIZE closed trades × 100.

    Fewer than HEALTH_WIN_RATE_MIN_RESOLVED resolved trades → NEUTRAL_SCORE:
    a tiny sample says nothing about health (the live bot starts with an
    EMPTY closed_trades — the designed neutral state, never faked)."""
    async with asyncio.timeout(config.SUPABASE_TIMEOUT_SECONDS):
        res = (
            client.table("closed_trades")
            .select("outcome")
            .order("closed_at", desc=True)
            .limit(config.HEALTH_WIN_RATE_SAMPLE_SIZE)
            .execute()
        )
        rows = res.data or []

    outcomes = [str(r.get("outcome") or "").strip().lower() for r in rows]
    resolved = [o for o in outcomes if o in ("win", "loss")]
    if len(resolved) < config.HEALTH_WIN_RATE_MIN_RESOLVED:
        logger.info(
            "[HEALTH] win_rate: %d resolved trades < %d — NEUTRAL %.0f",
            len(resolved), config.HEALTH_WIN_RATE_MIN_RESOLVED, NEUTRAL_SCORE,
        )
        return NEUTRAL_SCORE
    wins = sum(1 for o in resolved if o == "win")
    return 100.0 * wins / len(resolved)


def _yes_line_outcome(direction: str, outcome: str) -> Optional[float]:
    """Map a closed trade's (direction, outcome) to the YES-line resolution
    (1.0 YES won / 0.0 NO) — the scripts/gate_report.py convention. Class A
    and the close path store `outcome` on the TRADED side ("win"/"loss");
    None on anything unresolvable (row excluded whole — never guessed)."""
    direction = (direction or "").strip().upper()
    outcome = (outcome or "").strip().lower()
    if direction not in ("YES", "NO") or outcome not in ("win", "loss"):
        return None
    yes_won = (direction == "YES" and outcome == "win") or (
        direction == "NO" and outcome == "loss"
    )
    return 1.0 if yes_won else 0.0


async def _brier_component(client: Any, now: datetime) -> BrierSample:
    """Rolling HEALTH_BRIER_WINDOW_DAYS-day AGENT Brier from closed_trades.

    Forecast = p_model_at_entry (YES line); outcome = the YES-line resolution.
    Rows without a model probability (estimate-free Class A copies) are
    excluded WHOLE — a fill price or entry price is not a forecast and
    substituting one would fabricate skill (gate-report convention). Fewer
    than HEALTH_BRIER_MIN_RESOLVED observations → NEUTRAL_SCORE with
    raw_brier=None (no alert can fire on an empty sample). Mapping:
    0.00 → 100, BRIER_SCORE_THRESHOLD (0.23) → 0, linear between."""
    cutoff = (now - timedelta(days=config.HEALTH_BRIER_WINDOW_DAYS)).isoformat()
    async with asyncio.timeout(config.SUPABASE_TIMEOUT_SECONDS):
        res = (
            client.table("closed_trades")
            .select("direction,outcome,p_model_at_entry,closed_at")
            .gte("closed_at", cutoff)
            .order("closed_at", desc=True)
            # Ledger-wide read cap — same convention as the allocation read.
            .limit(config.ALLOCATION_HISTORY_LIMIT)
            .execute()
        )
        rows = res.data or []

    squared_errors: list[float] = []
    for row in rows:
        p_model = _optional_float(row.get("p_model_at_entry"))
        if p_model is None or not 0.0 <= p_model <= 1.0:
            continue
        yes_outcome = _yes_line_outcome(row.get("direction"), row.get("outcome"))
        if yes_outcome is None:
            continue
        squared_errors.append((p_model - yes_outcome) ** 2)

    n = len(squared_errors)
    if n < config.HEALTH_BRIER_MIN_RESOLVED:
        logger.info(
            "[HEALTH] brier: %d resolved observations < %d — NEUTRAL %.0f",
            n, config.HEALTH_BRIER_MIN_RESOLVED, NEUTRAL_SCORE,
        )
        return BrierSample(component=NEUTRAL_SCORE, raw_brier=None, n_resolved=n)

    raw_brier = sum(squared_errors) / n
    component = _clamp_score(
        100.0 * (1.0 - raw_brier / config.BRIER_SCORE_THRESHOLD)
    )
    return BrierSample(component=component, raw_brier=raw_brier, n_resolved=n)


async def _slippage_component(client: Any) -> float:
    """Mean signed slippage of recent FILLS vs their decision reference.

    Observations come from List A's fill data on both tables:
    closed_trades and open_positions rows with a recorded fill_price,
    scored through the pure fill_simulator.signed_slippage_units
    (fill on the traded token's line vs the YES-line decision mid,
    positive = adverse). 0 → 100, HEALTH_SLIPPAGE_MAX_UNITS → 0, linear;
    a negative mean (price improvement) clamps at 100; no fills →
    NEUTRAL_SCORE. Rows are fetched most-recent-first and NULL fills are
    skipped in Python (paper scale — the LIMIT is the query safety valve)."""
    rows: list[dict[str, Any]] = []
    async with asyncio.timeout(config.SUPABASE_TIMEOUT_SECONDS):
        closed = (
            client.table("closed_trades")
            .select("direction,fill_price,market_price_at_entry")
            .order("closed_at", desc=True)
            .limit(config.HEALTH_SLIPPAGE_SAMPLE_LIMIT)
            .execute()
        )
        rows.extend(closed.data or [])
        open_pos = (
            client.table("open_positions")
            .select("direction,fill_price,market_price_at_entry")
            .order("opened_at", desc=True)
            .limit(config.HEALTH_SLIPPAGE_SAMPLE_LIMIT)
            .execute()
        )
        rows.extend(open_pos.data or [])

    observations: list[float] = []
    for row in rows:
        fill = _optional_float(row.get("fill_price"))
        reference = _optional_float(row.get("market_price_at_entry"))
        direction = str(row.get("direction") or "").strip().upper()
        slippage = fill_simulator.signed_slippage_units(fill, reference, direction)
        if slippage is not None:
            observations.append(slippage)

    if not observations:
        logger.info("[HEALTH] slippage: no fill observations — NEUTRAL %.0f", NEUTRAL_SCORE)
        return NEUTRAL_SCORE

    mean_slippage = sum(observations) / len(observations)
    component = _clamp_score(
        100.0 * (1.0 - mean_slippage / config.HEALTH_SLIPPAGE_MAX_UNITS)
    )
    return component


def _feed_latency_component() -> float:
    """Pipeline latency vs its targets, from the recorded signal latencies.

    Each observation scores linearly against its path target (fast 5s /
    full 22s): at target → 100, at 2× target → 0. The component is the mean
    over the bounded recent window; no observations → NEUTRAL_SCORE."""
    if not _latency_observations:
        logger.info("[HEALTH] feed_latency: no latency observations — NEUTRAL %.0f", NEUTRAL_SCORE)
        return NEUTRAL_SCORE

    scores: list[float] = []
    for duration_s, fast_path in _latency_observations:
        target = (
            config.HEALTH_FEED_FAST_TARGET_SECONDS
            if fast_path
            else config.HEALTH_FEED_FULL_TARGET_SECONDS
        )
        ratio = duration_s / target
        scores.append(_clamp_score(100.0 * (2.0 - ratio)))
    return sum(scores) / len(scores)


def _drawdown_component() -> DrawdownSample:
    """Worst-period drawdown (Step 1 equity truth) vs each period's own halt
    threshold: 0% → 100, at-threshold → 0, linear; the component is the worst
    (lowest) period score. No equity snapshot → NEUTRAL_SCORE (data absent —
    the equity gate independently fail-closes entries when equity is stale,
    so a blind health number here would change nothing)."""
    from coordinator import equity  # local import: keeps module import order stable

    snapshot = equity.get_cached_snapshot()
    if snapshot is None:
        logger.info("[HEALTH] drawdown: no equity snapshot — NEUTRAL %.0f", NEUTRAL_SCORE)
        return DrawdownSample(component=NEUTRAL_SCORE, worst_drawdown_pct=None)

    thresholds = {
        "daily": config.DAILY_DRAWDOWN_HALT_PCT,
        "weekly": config.WEEKLY_DRAWDOWN_HALT_PCT,
        "monthly": config.MONTHLY_DRAWDOWN_SHUTDOWN_PCT,
    }
    worst_component = 100.0
    worst_drawdown = 0.0
    for period, threshold in thresholds.items():
        starting = snapshot.starting_balances.get(period, 0.0)
        current = snapshot.current_balances.get(period, 0.0)
        if starting <= 0.0:
            # A non-positive anchor cannot divide; skip the period loudly
            # (the equity service refuses to anchor on <= 0 itself).
            logger.warning("[HEALTH] drawdown: %s anchor %.2f unusable — period skipped", period, starting)
            continue
        drawdown = max(0.0, (starting - current) / starting)
        worst_drawdown = max(worst_drawdown, drawdown)
        worst_component = min(
            worst_component, _clamp_score(100.0 * (1.0 - drawdown / threshold))
        )
    return DrawdownSample(component=worst_component, worst_drawdown_pct=worst_drawdown)


async def _correlation_component(client: Any) -> float:
    """INTERIM correlation component (D-B11): category concentration.

    100 − (max category exposure / MAX_CATEGORY_EXPOSURE_PCT) × 100 — at the
    30% cap → 0. No open positions → 100 (no exposure). Exposure percentages
    need the equity truth: no snapshot (or a non-positive value) →
    NEUTRAL_SCORE. Rows are counted like the pipeline's exposure gate (all
    open rows, resting makers included) so this component can never disagree
    with the gate it mirrors. Upgraded to event-cluster exposure in Step 6."""
    from coordinator import equity  # local import: keeps module import order stable

    async with asyncio.timeout(config.SUPABASE_TIMEOUT_SECONDS):
        res = (
            client.table("open_positions")
            .select("position_size_usdc,category")
            .execute()
        )
        rows = res.data or []

    if not rows:
        return 100.0

    snapshot = equity.get_cached_snapshot()
    if snapshot is None or snapshot.current_equity <= 0.0:
        logger.info(
            "[HEALTH] correlation: equity snapshot unavailable — NEUTRAL %.0f", NEUTRAL_SCORE
        )
        return NEUTRAL_SCORE

    category_sums: dict[str, float] = {}
    for row in rows:
        size = _optional_float(row.get("position_size_usdc")) or 0.0
        label = str(row.get("category") or "uncategorized")
        category_sums[label] = category_sums.get(label, 0.0) + size

    max_category_pct = max(category_sums.values()) / snapshot.current_equity
    return _clamp_score(
        100.0 - (max_category_pct / config.MAX_CATEGORY_EXPOSURE_PCT) * 100.0
    )


# ── Persistence ──────────────────────────────────────────────────────────────

async def _persist_health_score(client: Any, now: datetime, score: float) -> bool:
    """Write the score into TODAY's daily_performance row (UPDATE-only).

    The row belongs to the Step 1 equity anchor upsert; if it does not exist
    yet the score is logged and skipped (the next tick retries) — inserting a
    date-only row would recreate the NULL-anchor orphan that fail-closed the
    equity service on the 2026-10-31 deploy. Returns True when persisted."""
    today = now.date().isoformat()
    async with asyncio.timeout(config.SUPABASE_TIMEOUT_SECONDS):
        res = (
            client.table("daily_performance")
            .select("date")
            .eq("date", today)
            .limit(1)
            .execute()
        )
        if not res.data:
            logger.warning(
                "[HEALTH] daily_performance has no row for %s — health_score %.2f not "
                "persisted this tick (the equity anchor task owns row creation; "
                "inserting a date-only row is forbidden)",
                today, score,
            )
            return False
        client.table("daily_performance").update(
            {"health_score": round(score, 2)}
        ).eq("date", today).execute()
    return True


# ── Alerts: transitions, probation diff, Brier crossing ──────────────────────

def _announce_mode(
    mode: str,
    score: float,
    components: dict[str, float],
    worst_drawdown_pct: Optional[float],
    now: datetime,
    first_evaluation: bool,
) -> None:
    """Fire the transition alert when the health mode changed (and the system
    halt alert when FULL_HALT was entered). First evaluation seeds silently
    when NORMAL; booting straight into a degraded mode alerts. The cooldown
    guards flapping; the announcement state still advances when suppressed."""
    global _last_announced_mode
    previous = _last_announced_mode
    if not first_evaluation and previous == mode:
        return
    if first_evaluation and mode == "NORMAL":
        _last_announced_mode = mode
        return

    _dispatch_health_alert(
        "HEALTH_TRANSITION",
        alert_health_score(_MODE_SEVERITY[mode], score, mode, components),
        now,
    )
    if mode == "FULL_HALT":
        # Real values only: the measured worst-period drawdown (0.0 when the
        # equity snapshot is absent — the detail line says so) and the cached
        # marked equity. The reason carries the score so the alert is
        # self-describing.
        from coordinator import equity  # local import: keeps module import order stable

        snapshot = equity.get_cached_snapshot()
        equity_value = snapshot.current_equity if snapshot is not None else 0.0
        _dispatch_health_alert(
            "HEALTH_HALT",
            alert_system_halt(
                reason=f"health_full_halt (score={score:.1f})",
                drawdown_pct=worst_drawdown_pct if worst_drawdown_pct is not None else 0.0,
                portfolio_value=equity_value,
                detail=(
                    "Health score entered FULL_HALT: new entries are blocked on "
                    "both paths; exits are unaffected."
                    if snapshot is not None
                    else "Health score entered FULL_HALT and the equity snapshot is "
                    "unavailable (equity gate independently blocks entries); exits "
                    "are unaffected."
                ),
            ),
            now,
        )
    _last_announced_mode = mode


async def _check_probation_transitions(client: Any, now: datetime) -> None:
    """Diff derive_allocations states against last-known; alert on a
    probation/retirement ENTRY (False→True). Same ledger read conventions as
    the pipeline's allocation fetch (2s timeout, ALLOCATION_HISTORY_LIMIT,
    oldest first, unknown-strategy bucket). First tick seeds silently."""
    async with asyncio.timeout(config.SUPABASE_TIMEOUT_SECONDS):
        res = (
            client.table("closed_trades")
            .select("strategy,net_edge_at_entry,closed_at")
            .order("closed_at")
            .limit(config.ALLOCATION_HISTORY_LIMIT)
            .execute()
        )
        rows = res.data or []

    trades_by_strategy: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        trades_by_strategy.setdefault(str(row.get("strategy") or "unknown"), []).append(row)
    current = derive_allocations(trades_by_strategy)

    global _last_allocations
    previous = _last_allocations
    _last_allocations = current
    if previous is None:
        return

    for strategy, alloc in current.items():
        prev = previous.get(strategy)
        if prev is None:
            continue
        if not (alloc.probation and not prev.probation) and not (
            alloc.retired and not prev.retired
        ):
            continue
        window_stats: dict[str, Any] = {
            "n": alloc.n,
            "rolling_net_edge": alloc.rolling_net_edge,
            "rolling_sharpe": alloc.rolling_sharpe,
            "weight": alloc.weight,
            "probation": alloc.probation,
            "retired": alloc.retired,
        }
        event = f"retired:{strategy}" if alloc.retired else f"probation:{strategy}"
        _dispatch_health_alert(
            event, alert_strategy_probation(strategy, window_stats), now
        )


# ── Evaluation task ──────────────────────────────────────────────────────────

async def _produce_neutral_on_error(
    name: str, producer: Callable[[], Awaitable[Any]], neutral: Any
) -> Any:
    """Run one producer; ANY failure (except cancellation) yields the
    neutral value with a loud error — fail-soft telemetry, never faked
    healthy, never crash-the-loop."""
    try:
        return await producer()
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.error(
            "[HEALTH] Component '%s' failed — NEUTRAL %.0f (fail-soft telemetry): %s: %s",
            name, NEUTRAL_SCORE, type(e).__name__, e,
        )
        return neutral


async def evaluate_health_once(now: Optional[datetime] = None) -> HealthEvaluation:
    """One evaluation tick: compute the six components, score, interpret,
    persist, announce transitions, diff allocations, check the Brier crossing,
    and cache the mode. Raises only on cancellation — the supervisor owns
    fail-softness at the tick level (per-component failures are neutral-50)."""
    checked_at = now or datetime.now(timezone.utc)
    client = await get_client()
    first_evaluation = _last_announced_mode is None

    win_rate = await _produce_neutral_on_error(
        "win_rate", lambda: _win_rate_component(client), NEUTRAL_SCORE
    )
    brier_sample: BrierSample = await _produce_neutral_on_error(
        "brier",
        lambda: _brier_component(client, checked_at),
        BrierSample(component=NEUTRAL_SCORE, raw_brier=None, n_resolved=0),
    )
    slippage = await _produce_neutral_on_error(
        "slippage", lambda: _slippage_component(client), NEUTRAL_SCORE
    )
    feed_latency = await _produce_neutral_on_error(
        "feed_latency", lambda: _feed_latency_component(), NEUTRAL_SCORE
    )
    drawdown_sample: DrawdownSample = await _produce_neutral_on_error(
        "drawdown",
        lambda: _drawdown_component(),
        DrawdownSample(component=NEUTRAL_SCORE, worst_drawdown_pct=None),
    )
    correlation = await _produce_neutral_on_error(
        "correlation", lambda: _correlation_component(client), NEUTRAL_SCORE
    )

    components = {
        "win_rate_score": win_rate,
        "brier_score_score": brier_sample.component,
        "slippage_score": slippage,
        "feed_latency_score": feed_latency,
        "drawdown_score": drawdown_sample.component,
        "correlation_score": correlation,
    }
    score = risk_engine.compute_health_score(**components)
    mode = risk_engine.interpret_health_score(score)

    persisted = False
    try:
        persisted = await _persist_health_score(client, checked_at, score)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        # Persistence failure must not stop the mode from being enforced.
        logger.error("[HEALTH] health_score persistence failed: %s: %s", type(e).__name__, e)

    _announce_mode(
        mode, score, components, drawdown_sample.worst_drawdown_pct,
        checked_at, first_evaluation,
    )
    set_current_mode(mode)

    try:
        await _check_probation_transitions(client, checked_at)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        # Probation alerting is telemetry — a failed diff retries next tick.
        logger.error("[HEALTH] Probation diff failed: %s: %s", type(e).__name__, e)

    global _last_raw_brier
    if brier_sample.raw_brier is not None:
        crossed = brier_sample.raw_brier > config.BRIER_SCORE_THRESHOLD and (
            _last_raw_brier is None or _last_raw_brier <= config.BRIER_SCORE_THRESHOLD
        )
        if crossed:
            _dispatch_health_alert(
                "BRIER_EXCEEDED",
                alert_brier_score_exceeded(
                    brier_sample.raw_brier,
                    config.BRIER_SCORE_THRESHOLD,
                    config.HEALTH_BRIER_WINDOW_DAYS,
                ),
                checked_at,
            )
        _last_raw_brier = brier_sample.raw_brier

    logger.info(
        "[HEALTH] score=%.1f mode=%s components=%s brier_raw=%s persisted=%s",
        score, mode,
        {k: round(v, 1) for k, v in components.items()},
        (
            round(brier_sample.raw_brier, 4)
            if brier_sample.raw_brier is not None
            else None
        ),
        persisted,
    )
    return HealthEvaluation(
        score=score,
        mode=mode,
        components=components,
        raw_brier=brier_sample.raw_brier,
        persisted=persisted,
    )


async def run_health_check_supervisor() -> None:
    """Supervisor entry: evaluate the health score forever.

    paper_fills/equity pattern (E-B12): a failed tick logs and retries next
    interval (the mode cache keeps its last-known value meanwhile);
    CancelledError is always re-raised."""
    logger.info(
        "[HEALTH] Health check supervisor started (interval=%ds, defensive<%d halt<%d)",
        config.HEALTH_CHECK_INTERVAL_SECONDS,
        config.HEALTH_SCORE_DEFENSIVE_THRESHOLD,
        config.HEALTH_SCORE_HALT_THRESHOLD,
    )
    while True:
        try:
            await evaluate_health_once()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(
                "[HEALTH] Evaluation tick failed (continues; the mode cache keeps "
                "its last-known value): %s: %s", type(e).__name__, e,
            )
        await asyncio.sleep(config.HEALTH_CHECK_INTERVAL_SECONDS)
