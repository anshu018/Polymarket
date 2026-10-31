"""
tests/test_health_score.py — monitoring/health_score.py (List-B.md Step 2 — B1).

Covers: the six component mappings (boundaries) and their neutral-50 fallbacks,
score persistence into today's daily_performance row (UPDATE-only), mode
transition alerts + cooldown, the strategy probation/retirement diff alert, the
Brier-crossing alert wiring, the supervisor's fail-soft tick and cancellation,
and the exits-NOT-affected guard (no exit-path module may import the health
gate — written now so Step 5's exit engine inherits the assertion).

Entry-path enforcement (FULL_HALT drops, DEFENSIVE sizing, latency wrapper)
is covered where the harnesses live: tests/test_net_edge_gate.py (pipeline)
and tests/test_dedupe_gate.py (Class A executor).

Module-local mocks, no conftest (repo rule). No live Supabase, no network, no
Telegram: get_client and the alert functions are patched; only .env.test is
ever loaded (via config).
"""

import asyncio
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config  # noqa: E402 — must come after sys.path setup
from monitoring import health_score  # noqa: E402

# 2026-10-14 is a Wednesday — a fixed "now" keeps the 30-day Brier window and
# the daily_performance date key exact.
NOW = datetime(2026, 10, 14, 12, 0, 0, tzinfo=timezone.utc)
TODAY = "2026-10-14"


# ── Mocks (module-local, per repo rule) ──────────────────────────────────────

class MockResult:
    def __init__(self, data: list[dict[str, Any]]) -> None:
        self.data = data


class MockTableBuilder:
    """Chainable stand-in for supabase-py over one table (subset health_score
    uses): select/eq/gte/order(desc)/limit + update-by-eq."""

    def __init__(self, db_state: dict[str, list[dict[str, Any]]], table_name: str) -> None:
        self._state = db_state
        self.table_name = table_name
        self._eq: dict[str, Any] = {}
        self._gte: dict[str, str] = {}
        self._order: Optional[tuple[str, bool]] = None
        self._limit: Optional[int] = None
        self._update_payload: Optional[dict[str, Any]] = None

    def select(self, *_cols: str) -> "MockTableBuilder":
        return self

    def eq(self, col: str, val: Any) -> "MockTableBuilder":
        self._eq[col] = val
        return self

    def gte(self, col: str, val: Any) -> "MockTableBuilder":
        self._gte[col] = str(val)
        return self

    def order(self, col: str, desc: bool = False) -> "MockTableBuilder":
        self._order = (col, desc)
        return self

    def limit(self, n: int) -> "MockTableBuilder":
        self._limit = n
        return self

    def update(self, payload: dict[str, Any]) -> "MockTableBuilder":
        self._update_payload = payload
        return self

    def execute(self) -> MockResult:
        rows = self._state.setdefault(self.table_name, [])
        if self._update_payload is None:
            out = [dict(r) for r in rows]
            for col, val in self._eq.items():
                out = [r for r in out if r.get(col) == val]
            for col, val in self._gte.items():
                out = [r for r in out if str(r.get(col)) >= val]
            if self._order is not None:
                col, desc = self._order
                out = sorted(out, key=lambda r: str(r.get(col) or ""), reverse=desc)
            if self._limit is not None:
                out = out[: self._limit]
            return MockResult(out)

        matched = [r for r in rows if all(r.get(c) == v for c, v in self._eq.items())]
        for r in matched:
            r.update(self._update_payload)
        return MockResult([dict(r) for r in matched])


class MockSupabaseClient:
    def __init__(self, db_state: dict[str, list[dict[str, Any]]]) -> None:
        self.db_state = db_state

    def table(self, name: str) -> MockTableBuilder:
        return MockTableBuilder(self.db_state, name)


def make_equity_snapshot(
    current: float = 10000.0,
    starting: dict[str, float] | None = None,
) -> Any:
    """A flat (or shaped) EquityGate for the drawdown/correlation producers."""
    from coordinator import equity

    starts = starting or {"daily": current, "weekly": current, "monthly": current}
    return equity.EquityGate(
        as_of=NOW,
        current_equity=current,
        starting_balances=starts,
        current_balances={p: current for p in ("daily", "weekly", "monthly")},
    )


async def _drain_tasks() -> None:
    """Let fire-and-forget alert tasks run."""
    for _ in range(3):
        await asyncio.sleep(0)


@pytest.fixture(autouse=True)
def reset_health_state():
    """The health module holds process-wide enforcement/alert state — reset
    around every test so nothing leaks into the wider suite."""
    health_score._current_mode = "NORMAL"
    health_score._last_announced_mode = None
    health_score._gate_drops.update({"health:full_halt": 0})
    health_score._last_alert_at.clear()
    health_score._last_allocations = None
    health_score._last_raw_brier = None
    health_score._latency_observations.clear()
    yield
    health_score._current_mode = "NORMAL"
    health_score._last_announced_mode = None


@pytest.fixture
def health_db(monkeypatch: pytest.MonkeyPatch):
    """Mock DB + captured alerts + injectable equity snapshot.
    Returns (state, snapshot_holder, alerts)."""
    state: dict[str, list[dict[str, Any]]] = {
        "open_positions": [],
        "closed_trades": [],
        "daily_performance": [],
    }
    client = MockSupabaseClient(state)

    async def fake_get_client() -> MockSupabaseClient:
        return client

    snapshot_holder: dict[str, Any] = {"snapshot": None}

    def fake_get_cached_snapshot() -> Any:
        return snapshot_holder["snapshot"]

    alerts: list[tuple[str, tuple[Any, ...]]] = []

    async def fake_alert_health(level, score, mode, components) -> None:
        alerts.append(("HEALTH_SCORE", (level, score, mode, dict(components))))

    async def fake_alert_probation(strategy, window_stats) -> None:
        alerts.append(("STRATEGY_PROBATION", (strategy, dict(window_stats))))

    async def fake_alert_brier(current_score, threshold, period_days) -> None:
        alerts.append(("BRIER_EXCEEDED", (current_score, threshold, period_days)))

    async def fake_alert_halt(reason, drawdown_pct, portfolio_value, detail=None) -> None:
        alerts.append(("SYSTEM_HALT", (reason, drawdown_pct, portfolio_value, detail)))

    monkeypatch.setattr(health_score, "get_client", fake_get_client)
    monkeypatch.setattr(health_score, "alert_health_score", fake_alert_health)
    monkeypatch.setattr(health_score, "alert_strategy_probation", fake_alert_probation)
    monkeypatch.setattr(health_score, "alert_brier_score_exceeded", fake_alert_brier)
    monkeypatch.setattr(health_score, "alert_system_halt", fake_alert_halt)
    monkeypatch.setattr("coordinator.equity.get_cached_snapshot", fake_get_cached_snapshot)
    return state, snapshot_holder, alerts


def _patch_producers(
    monkeypatch: pytest.MonkeyPatch,
    value: float,
    worst_drawdown_pct: Optional[float] = None,
    raw_brier: Optional[float] = None,
    brier_n: int = 50,
) -> None:
    """Pin all six producers to fixed component values (evaluate-level tests)."""

    async def win_rate(client: Any) -> float:
        return value

    async def brier(client: Any, now: datetime) -> health_score.BrierSample:
        return health_score.BrierSample(
            component=value, raw_brier=raw_brier, n_resolved=brier_n
        )

    async def slippage(client: Any) -> float:
        return value

    def feed_latency() -> float:
        return value

    def drawdown() -> health_score.DrawdownSample:
        return health_score.DrawdownSample(component=value, worst_drawdown_pct=worst_drawdown_pct)

    async def correlation(client: Any) -> float:
        return value

    monkeypatch.setattr(health_score, "_win_rate_component", win_rate)
    monkeypatch.setattr(health_score, "_brier_component", brier)
    monkeypatch.setattr(health_score, "_slippage_component", slippage)
    monkeypatch.setattr(health_score, "_feed_latency_component", feed_latency)
    monkeypatch.setattr(health_score, "_drawdown_component", drawdown)
    monkeypatch.setattr(health_score, "_correlation_component", correlation)


# ── Win-rate producer ────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_win_rate_scores_last_20(health_db) -> None:
    """15 wins of 20 resolved → 75."""
    state, _, _ = health_db
    outcomes = ["win"] * 15 + ["loss"] * 5
    state["closed_trades"] = [{"outcome": o, "closed_at": NOW.isoformat()} for o in outcomes]

    score = await health_score._win_rate_component(MockSupabaseClient(state))

    assert score == pytest.approx(75.0)


@pytest.mark.anyio
async def test_win_rate_unresolved_rows_are_skipped(health_db) -> None:
    """Rows without a win/loss outcome (legacy/odd rows) are not trades."""
    state, _, _ = health_db
    rows = [{"outcome": "win", "closed_at": NOW.isoformat()}] * 3 + [
        {"outcome": None, "closed_at": NOW.isoformat()},
        {"outcome": "pending", "closed_at": NOW.isoformat()},
    ]
    state["closed_trades"] = rows

    score = await health_score._win_rate_component(MockSupabaseClient(state))

    assert score == pytest.approx(100.0)


@pytest.mark.anyio
async def test_win_rate_neutral_below_min_resolved(health_db) -> None:
    """<2 resolved trades says nothing about health → neutral 50 (the live bot
    starts with an EMPTY closed_trades — designed neutral state, never faked)."""
    state, _, _ = health_db
    state["closed_trades"] = [{"outcome": "win", "closed_at": NOW.isoformat()}]

    score = await health_score._win_rate_component(MockSupabaseClient(state))

    assert score == health_score.NEUTRAL_SCORE


@pytest.mark.anyio
async def test_win_rate_uses_only_sample_size(health_db) -> None:
    """The LIMIT bounds the window: 30 rows (25 wins) sample only the latest 20."""
    state, _, _ = health_db
    rows = []
    for i in range(30):
        # Oldest 10 rows are losses; the sampled latest 20 are all wins.
        rows.append({
            "outcome": "loss" if i < 10 else "win",
            "closed_at": (NOW - timedelta(days=30 - i)).isoformat(),
        })
    state["closed_trades"] = rows

    score = await health_score._win_rate_component(MockSupabaseClient(state))

    assert score == pytest.approx(100.0)


# ── Brier producer ───────────────────────────────────────────────────────────

def _brier_row(
    p_model: Optional[float], direction: str, outcome: str, age_days: float = 1.0
) -> dict[str, Any]:
    return {
        "p_model_at_entry": p_model,
        "direction": direction,
        "outcome": outcome,
        "closed_at": (NOW - timedelta(days=age_days)).isoformat(),
    }


@pytest.mark.anyio
async def test_brier_perfect_forecasts_score_100(health_db) -> None:
    """Raw Brier 0.00 → component 100."""
    state, _, _ = health_db
    state["closed_trades"] = [_brier_row(1.0, "YES", "win", age_days=i) for i in range(10)]

    sample = await health_score._brier_component(MockSupabaseClient(state), NOW)

    assert sample.n_resolved == 10
    assert sample.raw_brier == pytest.approx(0.0)
    assert sample.component == pytest.approx(100.0)


@pytest.mark.anyio
async def test_brier_mapping_is_linear_to_threshold(health_db) -> None:
    """(p−o)² = 0.16 → 100×(1 − 0.16/0.23) ≈ 30.43; raw 0.25 (> 0.23) → 0."""
    state, _, _ = health_db
    state["closed_trades"] = [_brier_row(0.6, "YES", "win", age_days=i) for i in range(10)]

    sample = await health_score._brier_component(MockSupabaseClient(state), NOW)

    assert sample.raw_brier == pytest.approx(0.16)  # (0.6 − 1)²
    assert sample.component == pytest.approx(100.0 * (1.0 - 0.16 / config.BRIER_SCORE_THRESHOLD))

    state["closed_trades"] = [_brier_row(0.5, "YES", "loss", age_days=i) for i in range(10)]

    sample = await health_score._brier_component(MockSupabaseClient(state), NOW)

    assert sample.raw_brier == pytest.approx(0.25)
    assert sample.component == pytest.approx(0.0)  # clamped at the threshold


@pytest.mark.anyio
async def test_brier_yes_line_mapping_for_no_rows(health_db) -> None:
    """direction NO + outcome 'win' means YES lost → yes_outcome 0. A NO-side
    forecast of 0.4 against a YES resolution of 0: (0.4 − 0)² = 0.16."""
    state, _, _ = health_db
    state["closed_trades"] = [_brier_row(0.4, "NO", "win", age_days=i) for i in range(10)]

    sample = await health_score._brier_component(MockSupabaseClient(state), NOW)

    assert sample.raw_brier == pytest.approx(0.16)


@pytest.mark.anyio
async def test_brier_excludes_null_model_rows_and_unresolvable(health_db) -> None:
    """Estimate-free rows (Class A: p_model NULL) are excluded WHOLE — a fill
    price is not a forecast; never fabricate skill. Unresolvable outcomes too."""
    state, _, _ = health_db
    state["closed_trades"] = (
        [_brier_row(None, "YES", "win") for _ in range(5)]
        + [_brier_row(0.5, "SIDEWAYS", "win") for _ in range(5)]
    )

    sample = await health_score._brier_component(MockSupabaseClient(state), NOW)

    assert sample.n_resolved == 0
    assert sample.raw_brier is None
    assert sample.component == health_score.NEUTRAL_SCORE


@pytest.mark.anyio
async def test_brier_window_excludes_rows_older_than_30_days(health_db) -> None:
    """Only rows closed inside HEALTH_BRIER_WINDOW_DAYS count."""
    state, _, _ = health_db
    state["closed_trades"] = (
        [_brier_row(1.0, "YES", "win", age_days=1.0) for _ in range(10)]
        + [_brier_row(0.0, "YES", "win", age_days=45.0) for _ in range(10)]
    )

    sample = await health_score._brier_component(MockSupabaseClient(state), NOW)

    assert sample.n_resolved == 10
    assert sample.raw_brier == pytest.approx(0.0)


@pytest.mark.anyio
async def test_brier_neutral_below_min_resolved(health_db) -> None:
    state, _, _ = health_db
    state["closed_trades"] = [_brier_row(0.0, "YES", "win") for _ in range(9)]

    sample = await health_score._brier_component(MockSupabaseClient(state), NOW)

    assert sample.component == health_score.NEUTRAL_SCORE
    assert sample.raw_brier is None  # no alert can fire on an empty sample


# ── Slippage producer ────────────────────────────────────────────────────────

def _fill_row(
    fill: Optional[float], reference: Optional[float], direction: str = "YES"
) -> dict[str, Any]:
    return {
        "fill_price": fill,
        "market_price_at_entry": reference,
        "direction": direction,
    }


@pytest.mark.anyio
async def test_slippage_mean_maps_linearly(health_db) -> None:
    """Mean adverse slippage 0.01 (half of HEALTH_SLIPPAGE_MAX_UNITS) → 50."""
    state, _, _ = health_db
    state["closed_trades"] = [_fill_row(0.51, 0.50) for _ in range(5)]

    score = await health_score._slippage_component(MockSupabaseClient(state))

    assert score == pytest.approx(50.0)


@pytest.mark.anyio
async def test_slippage_at_max_maps_0_and_improvement_clamps_100(health_db) -> None:
    state, _, _ = health_db
    state["closed_trades"] = [_fill_row(0.52, 0.50) for _ in range(5)]

    score = await health_score._slippage_component(MockSupabaseClient(state))

    assert score == pytest.approx(0.0)

    state["closed_trades"] = [_fill_row(0.49, 0.50) for _ in range(5)]  # price improved

    score = await health_score._slippage_component(MockSupabaseClient(state))

    assert score == pytest.approx(100.0)


@pytest.mark.anyio
async def test_slippage_no_side_uses_mirror_reference(health_db) -> None:
    """A NO fill lives on the NO line: reference = 1 − YES mid. Fill 0.52 vs
    NO reference 0.50 → +0.02 adverse → 0."""
    state, _, _ = health_db
    state["closed_trades"] = [_fill_row(0.52, 0.50, direction="NO") for _ in range(5)]

    score = await health_score._slippage_component(MockSupabaseClient(state))

    assert score == pytest.approx(0.0)


@pytest.mark.anyio
async def test_slippage_includes_open_positions_and_skips_null_fills(health_db) -> None:
    """Filled open positions are observations too; NULL-fill rows (resting
    makers) are skipped, never coerced to zero slippage."""
    state, _, _ = health_db
    state["closed_trades"] = [_fill_row(None, 0.50) for _ in range(3)]
    state["open_positions"] = [_fill_row(0.51, 0.50) for _ in range(2)]

    score = await health_score._slippage_component(MockSupabaseClient(state))

    assert score == pytest.approx(50.0)


@pytest.mark.anyio
async def test_slippage_no_fills_neutral(health_db) -> None:
    state, _, _ = health_db

    score = await health_score._slippage_component(MockSupabaseClient(state))

    assert score == health_score.NEUTRAL_SCORE


# ── Feed-latency producer & recorder ─────────────────────────────────────────

def test_feed_latency_at_target_100_at_double_0() -> None:
    health_score.record_signal_latency(config.HEALTH_FEED_FULL_TARGET_SECONDS, False)
    health_score.record_signal_latency(2 * config.HEALTH_FEED_FULL_TARGET_SECONDS, False)

    score = health_score._feed_latency_component()

    assert score == pytest.approx(50.0)  # mean of 100 and 0


def test_feed_latency_halfway_target_maps_50() -> None:
    health_score._latency_observations.clear()
    health_score.record_signal_latency(1.5 * config.HEALTH_FEED_FULL_TARGET_SECONDS, False)

    assert health_score._feed_latency_component() == pytest.approx(50.0)


def test_feed_latency_empty_is_neutral() -> None:
    assert health_score._feed_latency_component() == health_score.NEUTRAL_SCORE


def test_feed_latency_fast_path_uses_fast_target() -> None:
    health_score._latency_observations.clear()
    # 10s is 2× the FAST target (→ 0) but under half the FULL target (→ 100+).
    health_score.record_signal_latency(2 * config.HEALTH_FEED_FAST_TARGET_SECONDS, True)
    health_score.record_signal_latency(0.5 * config.HEALTH_FEED_FULL_TARGET_SECONDS, False)

    score = health_score._feed_latency_component()

    assert score == pytest.approx(50.0)  # mean of 0 and 100


def test_record_signal_latency_ignores_invalid() -> None:
    health_score._latency_observations.clear()
    for bad in (-1.0, float("nan"), float("inf")):
        health_score.record_signal_latency(bad, False)

    assert len(health_score._latency_observations) == 0


# ── Drawdown producer ────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_drawdown_zero_drawdown_scores_100(health_db) -> None:
    _, snapshot_holder, _ = health_db
    snapshot_holder["snapshot"] = make_equity_snapshot(current=10000.0)

    sample = health_score._drawdown_component()

    assert sample.component == pytest.approx(100.0)
    assert sample.worst_drawdown_pct == pytest.approx(0.0)


@pytest.mark.anyio
async def test_drawdown_at_daily_threshold_scores_0(health_db) -> None:
    _, snapshot_holder, _ = health_db
    dd = config.DAILY_DRAWDOWN_HALT_PCT
    snapshot_holder["snapshot"] = make_equity_snapshot(
        current=10000.0 * (1.0 - dd),
        starting={"daily": 10000.0, "weekly": 10000.0, "monthly": 10000.0},
    )

    sample = health_score._drawdown_component()

    assert sample.component == pytest.approx(0.0)
    assert sample.worst_drawdown_pct == pytest.approx(dd)


@pytest.mark.anyio
async def test_drawdown_takes_worst_period(health_db) -> None:
    """Daily 4% (score 50) is worse than weekly/monthly 0% (score 100) → 50."""
    _, snapshot_holder, _ = health_db
    snapshot_holder["snapshot"] = make_equity_snapshot(
        current=9600.0,
        starting={"daily": 10000.0, "weekly": 9600.0, "monthly": 9600.0},
    )

    sample = health_score._drawdown_component()

    assert sample.component == pytest.approx(50.0)


@pytest.mark.anyio
async def test_drawdown_no_snapshot_is_neutral(health_db) -> None:
    _, snapshot_holder, _ = health_db  # snapshot stays None

    sample = health_score._drawdown_component()

    assert sample.component == health_score.NEUTRAL_SCORE
    assert sample.worst_drawdown_pct is None


@pytest.mark.anyio
async def test_drawdown_gain_clamps_at_100(health_db) -> None:
    """A period in GAIN (current > start) is not negative drawdown."""
    _, snapshot_holder, _ = health_db
    snapshot_holder["snapshot"] = make_equity_snapshot(
        current=11000.0,
        starting={"daily": 10000.0, "weekly": 10000.0, "monthly": 10000.0},
    )

    sample = health_score._drawdown_component()

    assert sample.component == pytest.approx(100.0)


# ── Correlation producer (D-B11 interim) ─────────────────────────────────────

@pytest.mark.anyio
async def test_correlation_no_positions_scores_100(health_db) -> None:
    state, snapshot_holder, _ = health_db
    snapshot_holder["snapshot"] = make_equity_snapshot()

    score = await health_score._correlation_component(MockSupabaseClient(state))

    assert score == pytest.approx(100.0)


@pytest.mark.anyio
async def test_correlation_at_cap_scores_0(health_db) -> None:
    state, snapshot_holder, _ = health_db
    snapshot_holder["snapshot"] = make_equity_snapshot()
    cap = config.MAX_CATEGORY_EXPOSURE_PCT
    state["open_positions"] = [
        {"position_size_usdc": 10000.0 * cap, "category": "politics"}
    ]

    score = await health_score._correlation_component(MockSupabaseClient(state))

    assert score == pytest.approx(0.0)


@pytest.mark.anyio
async def test_correlation_halfway_to_cap_maps_50(health_db) -> None:
    state, snapshot_holder, _ = health_db
    snapshot_holder["snapshot"] = make_equity_snapshot()
    state["open_positions"] = [
        {"position_size_usdc": 1000.0, "category": "politics"},
        {"position_size_usdc": 500.0, "category": "politics"},   # same bucket: 1500
        {"position_size_usdc": 500.0, "category": "crypto"},     # other bucket: 500
    ]

    # max category 1500/10000 = 15% = half of the 30% cap → 50.
    score = await health_score._correlation_component(MockSupabaseClient(state))

    assert score == pytest.approx(50.0)


@pytest.mark.anyio
async def test_correlation_null_category_is_its_own_bucket(health_db) -> None:
    state, snapshot_holder, _ = health_db
    snapshot_holder["snapshot"] = make_equity_snapshot()
    state["open_positions"] = [
        {"position_size_usdc": 3000.0, "category": None},
    ]

    # 30% in the uncategorized bucket → at the cap → 0.
    score = await health_score._correlation_component(MockSupabaseClient(state))

    assert score == pytest.approx(0.0)


@pytest.mark.anyio
async def test_correlation_no_snapshot_is_neutral(health_db) -> None:
    state, _, _ = health_db  # no snapshot → exposure percentages unknowable
    state["open_positions"] = [{"position_size_usdc": 100.0, "category": "politics"}]

    score = await health_score._correlation_component(MockSupabaseClient(state))

    assert score == health_score.NEUTRAL_SCORE


@pytest.mark.anyio
async def test_persists_into_todays_row(health_db, monkeypatch: pytest.MonkeyPatch) -> None:
    """The score lands in TODAY'S daily_performance row (the Step 1 anchor row
    — extended, never double-written)."""
    state, _, _ = health_db
    state["daily_performance"].append({
        "date": TODAY, "starting_balance_usdc": 10000.0,
        "ending_balance_usdc": 10000.0, "daily_pnl_usdc": 0.0,
    })
    _patch_producers(monkeypatch, 80.0)  # all-80 → score 80 → NORMAL

    evaluation = await health_score.evaluate_health_once(NOW)

    assert evaluation.persisted is True
    assert evaluation.score == pytest.approx(80.0)
    row = state["daily_performance"][0]
    assert row["health_score"] == pytest.approx(round(80.0, 2))
    # The anchor fields are the equity task's — a health write must not touch them.
    assert row["starting_balance_usdc"] == pytest.approx(10000.0)
    assert health_score.get_current_mode() == "NORMAL"


@pytest.mark.anyio
async def test_missing_daily_row_skips_persistence_without_insert(
    health_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No row for today (equity task has not ticked yet) → the score is logged
    and skipped. Inserting a date-only row is FORBIDDEN (the orphaned-mock-row
    incident fail-closed the equity service on the 2026-10-31 deploy)."""
    state, _, _ = health_db
    _patch_producers(monkeypatch, 80.0)

    evaluation = await health_score.evaluate_health_once(NOW)

    assert evaluation.persisted is False
    assert state["daily_performance"] == []  # no insert happened
    # The mode is still cached — enforcement does not depend on persistence.
    assert health_score.get_current_mode() == "NORMAL"


@pytest.mark.anyio
async def test_producer_exception_is_neutral_not_fatal(
    health_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One broken producer degrades to neutral 50 (logged) — the evaluation
    completes and the other components still count."""
    state, _, _ = health_db
    state["daily_performance"].append({"date": TODAY})
    _patch_producers(monkeypatch, 80.0)

    async def broken_win_rate(client: Any) -> float:
        raise RuntimeError("db down")

    monkeypatch.setattr(health_score, "_win_rate_component", broken_win_rate)

    evaluation = await health_score.evaluate_health_once(NOW)

    assert evaluation.components["win_rate_score"] == health_score.NEUTRAL_SCORE
    assert evaluation.score == pytest.approx((50.0 + 80.0 * 5) / 6.0)


# ── Mode-transition alerts & cooldown ────────────────────────────────────────

@pytest.mark.anyio
async def test_first_evaluation_normal_seeds_silently(
    health_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Booting healthy is not an event — no transition alert on the first tick."""
    _, _, alerts = health_db
    _patch_producers(monkeypatch, 80.0)

    await health_score.evaluate_health_once(NOW)

    await _drain_tasks()
    assert alerts == []
    assert health_score._last_announced_mode == "NORMAL"


@pytest.mark.anyio
async def test_first_evaluation_degraded_alerts(
    health_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Booting straight into a degraded mode IS alert-worthy."""
    _, _, alerts = health_db
    _patch_producers(monkeypatch, 60.0)  # 60 → DEFENSIVE_MODE

    await health_score.evaluate_health_once(NOW)

    await _drain_tasks()
    health_alerts = [a for a in alerts if a[0] == "HEALTH_SCORE"]
    assert len(health_alerts) == 1
    level, score, mode, components = health_alerts[0][1]
    assert level == "WARNING"
    assert mode == "DEFENSIVE_MODE"
    assert score == pytest.approx(60.0)
    assert set(components) == {
        "win_rate_score", "brier_score_score", "slippage_score",
        "feed_latency_score", "drawdown_score", "correlation_score",
    }


@pytest.mark.anyio
async def test_transition_to_defensive_then_full_halt_alerts(
    health_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, alerts = health_db
    _patch_producers(monkeypatch, 80.0)
    await health_score.evaluate_health_once(NOW)  # seeds NORMAL silently

    _patch_producers(monkeypatch, 60.0)
    await health_score.evaluate_health_once(NOW)  # NORMAL → DEFENSIVE_MODE

    _patch_producers(monkeypatch, 30.0)
    await health_score.evaluate_health_once(NOW)  # DEFENSIVE_MODE → FULL_HALT

    await _drain_tasks()
    health_alerts = [a for a in alerts if a[0] == "HEALTH_SCORE"]
    assert [(a[1][0], a[1][2]) for a in health_alerts] == [
        ("WARNING", "DEFENSIVE_MODE"),
        ("CRITICAL", "FULL_HALT"),
    ]
    # Entering FULL_HALT also fires the existing system-halt alert with real
    # values: the reason carries the score, the drawdown is the measured one.
    halts = [a for a in alerts if a[0] == "SYSTEM_HALT"]
    assert len(halts) == 1
    reason, drawdown_pct, portfolio_value, detail = halts[0][1]
    assert "health_full_halt" in reason and "30.0" in reason
    assert drawdown_pct == pytest.approx(0.0)  # no equity snapshot → 0.0 + detail
    assert "equity snapshot is unavailable" in detail
    assert portfolio_value == pytest.approx(0.0)


@pytest.mark.anyio
async def test_full_halt_alert_carries_real_equity_and_drawdown(
    health_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the equity snapshot exists, the halt alert reports the real marked
    equity and the real worst-period drawdown — never a fabricated number."""
    _, snapshot_holder, alerts = health_db
    snapshot_holder["snapshot"] = make_equity_snapshot(
        current=9000.0,
        starting={"daily": 10000.0, "weekly": 10000.0, "monthly": 10000.0},
    )
    _patch_producers(monkeypatch, 80.0)
    await health_score.evaluate_health_once(NOW)

    _patch_producers(monkeypatch, 30.0, worst_drawdown_pct=0.10)
    await health_score.evaluate_health_once(NOW)

    await _drain_tasks()
    halts = [a for a in alerts if a[0] == "SYSTEM_HALT"]
    assert len(halts) == 1
    _, drawdown_pct, portfolio_value, _ = halts[0][1]
    assert drawdown_pct == pytest.approx(0.10)
    assert portfolio_value == pytest.approx(9000.0)


@pytest.mark.anyio
async def test_recovery_alerts_info(
    health_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, alerts = health_db
    _patch_producers(monkeypatch, 80.0)
    await health_score.evaluate_health_once(NOW)
    _patch_producers(monkeypatch, 30.0)
    await health_score.evaluate_health_once(NOW)
    _patch_producers(monkeypatch, 80.0)
    await health_score.evaluate_health_once(NOW)  # FULL_HALT → NORMAL (recovery)

    await _drain_tasks()
    health_alerts = [a for a in alerts if a[0] == "HEALTH_SCORE"]
    assert [(a[1][0], a[1][2]) for a in health_alerts] == [
        ("CRITICAL", "FULL_HALT"),
        ("INFO", "NORMAL"),
    ]


@pytest.mark.anyio
async def test_transition_cooldown_suppresses_flapping(
    health_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Transition cooldowns are PER TARGET MODE: each distinct mode's entry
    alerts at most once per HEALTH_ALERT_COOLDOWN_SECONDS — an escalation is
    never suppressed by an earlier, less-severe alert, but flapping BACK to a
    recently-alerted mode is. The announcement state (and the mode cache)
    still tracks every change."""
    _, _, alerts = health_db
    _patch_producers(monkeypatch, 80.0)
    await health_score.evaluate_health_once(NOW)
    _patch_producers(monkeypatch, 30.0)
    await health_score.evaluate_health_once(NOW)   # alert #1: → FULL_HALT
    _patch_producers(monkeypatch, 80.0)
    await health_score.evaluate_health_once(NOW)   # alert #2: → NORMAL (fresh key)
    _patch_producers(monkeypatch, 30.0)
    await health_score.evaluate_health_once(NOW)   # suppressed: FULL_HALT alerted < 1h ago

    await _drain_tasks()
    health_alerts = [a for a in alerts if a[0] == "HEALTH_SCORE"]
    assert [(a[1][0], a[1][2]) for a in health_alerts] == [
        ("CRITICAL", "FULL_HALT"),
        ("INFO", "NORMAL"),
    ]
    # But the mode cache always reflects the LATEST evaluation — enforcement
    # is never left behind by alert suppression.
    assert health_score.get_current_mode() == "FULL_HALT"


# ── Strategy probation / retirement diff ─────────────────────────────────────

def _ledger_rows(strategy: str, n: int, edge: float) -> list[dict[str, Any]]:
    return [
        {
            "strategy": strategy,
            "net_edge_at_entry": edge,
            "closed_at": (NOW - timedelta(minutes=i)).isoformat(),
        }
        for i in range(n)
    ]


@pytest.mark.anyio
async def test_probation_transition_alerts(
    health_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """First tick seeds silently; a strategy entering probation (mean net edge
    of the last 20 trades < 4¢) alerts on the diff."""
    state, _, alerts = health_db
    _patch_producers(monkeypatch, 80.0)
    await health_score.evaluate_health_once(NOW)  # seeds (empty ledger)
    assert alerts == []

    state["closed_trades"] = _ledger_rows("velocity", 20, edge=0.0)
    await health_score.evaluate_health_once(NOW)

    await _drain_tasks()
    probation = [a for a in alerts if a[0] == "STRATEGY_PROBATION"]
    assert len(probation) == 1
    strategy, stats = probation[0][1]
    assert strategy == "velocity"
    assert stats["probation"] is True
    assert stats["retired"] is False
    assert stats["n"] == 20


@pytest.mark.anyio
async def test_no_realert_without_new_transition(
    health_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, _, alerts = health_db
    _patch_producers(monkeypatch, 80.0)
    await health_score.evaluate_health_once(NOW)
    state["closed_trades"] = _ledger_rows("velocity", 20, edge=0.0)
    await health_score.evaluate_health_once(NOW)  # probation alert
    await health_score.evaluate_health_once(NOW)  # still probation — not a new transition

    await _drain_tasks()
    assert len([a for a in alerts if a[0] == "STRATEGY_PROBATION"]) == 1


@pytest.mark.anyio
async def test_retirement_transition_alerts(
    health_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two consecutive complete failing windows → retired → alert with
    retired=True (flag, never a deletion)."""
    state, _, alerts = health_db
    _patch_producers(monkeypatch, 80.0)
    await health_score.evaluate_health_once(NOW)

    state["closed_trades"] = _ledger_rows("velocity", 40, edge=0.0)
    await health_score.evaluate_health_once(NOW)

    await _drain_tasks()
    retirement = [a for a in alerts if a[0] == "STRATEGY_PROBATION"]
    assert len(retirement) == 1
    strategy, stats = retirement[0][1]
    assert strategy == "velocity"
    assert stats["retired"] is True


# ── Brier crossing alert (orphaned alert wired) ──────────────────────────────

@pytest.mark.anyio
async def test_brier_crossing_fires_orphaned_alert(
    health_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rolling Brier crossing above BRIER_SCORE_THRESHOLD fires the long-
    orphaned alert_brier_score_exceeded with (score, threshold, window)."""
    _, _, alerts = health_db
    _patch_producers(monkeypatch, 80.0, raw_brier=0.25)

    await health_score.evaluate_health_once(NOW)

    await _drain_tasks()
    brier_alerts = [a for a in alerts if a[0] == "BRIER_EXCEEDED"]
    assert len(brier_alerts) == 1
    current, threshold, period_days = brier_alerts[0][1]
    assert current == pytest.approx(0.25)
    assert threshold == pytest.approx(config.BRIER_SCORE_THRESHOLD)
    assert period_days == config.HEALTH_BRIER_WINDOW_DAYS


@pytest.mark.anyio
async def test_brier_above_threshold_does_not_realert(
    health_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, alerts = health_db
    _patch_producers(monkeypatch, 80.0, raw_brier=0.25)
    await health_score.evaluate_health_once(NOW)
    await health_score.evaluate_health_once(NOW)  # still above — not a crossing

    await _drain_tasks()
    assert len([a for a in alerts if a[0] == "BRIER_EXCEEDED"]) == 1


@pytest.mark.anyio
async def test_brier_below_threshold_never_alerts(
    health_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, alerts = health_db
    _patch_producers(monkeypatch, 80.0, raw_brier=0.10)

    await health_score.evaluate_health_once(NOW)

    await _drain_tasks()
    assert [a for a in alerts if a[0] == "BRIER_EXCEEDED"] == []


@pytest.mark.anyio
async def test_brier_neutral_sample_never_alerts(
    health_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty sample (raw_brier None) must not alert — absent data is not a
    threshold crossing."""
    _, _, alerts = health_db
    _patch_producers(monkeypatch, 80.0, raw_brier=None)

    await health_score.evaluate_health_once(NOW)

    await _drain_tasks()
    assert [a for a in alerts if a[0] == "BRIER_EXCEEDED"] == []


# ── Supervisor ───────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_supervisor_reraises_cancellation(
    health_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The supervisor must die on cancel (supervisor pattern, E-B12)."""
    ticks = {"n": 0}

    async def quick_tick(now: Optional[datetime] = None) -> health_score.HealthEvaluation:
        ticks["n"] += 1
        raise asyncio.CancelledError

    async def fake_sleep(_seconds: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(health_score, "evaluate_health_once", quick_tick)
    monkeypatch.setattr(health_score.asyncio, "sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await health_score.run_health_check_supervisor()

    assert ticks["n"] == 1


@pytest.mark.anyio
async def test_supervisor_tick_failure_is_fail_soft(
    health_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed tick logs and retries next interval — the supervisor survives;
    the mode cache keeps its last-known value meanwhile."""
    calls = {"n": 0}
    sleeps = {"n": 0}

    async def flaky_tick(now: Optional[datetime] = None) -> health_score.HealthEvaluation:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("supabase down")
        raise asyncio.CancelledError  # end the loop on the second (good) pass

    async def fake_sleep(_seconds: float) -> None:
        sleeps["n"] += 1
        if sleeps["n"] >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(health_score, "evaluate_health_once", flaky_tick)
    monkeypatch.setattr(health_score.asyncio, "sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await health_score.run_health_check_supervisor()

    assert calls["n"] == 2  # the first failure did not kill the supervisor


# ── Mode helpers & the exits-NOT-affected guard ──────────────────────────────

def test_set_current_mode_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        health_score.set_current_mode("SORT_OF_FINE")


def test_exit_modules_never_import_health() -> None:
    """GUARD TEST for Step 5 (List-B.md Step 2 DoD): a health halt gates ENTRIES
    ONLY — no exit-path module may import or consult the health gate. Today's
    exit machinery is the startup reconciliation close path and the paper
    maker-fill monitor (a fill completes an already-admitted entry, so the
    monitor keeps running under FULL_HALT); the equity snapshot task is the
    telemetry a halt must not kill. When Step 5 adds execution/exit_engine.py,
    add it to this list."""
    repo_root = Path(__file__).resolve().parents[1]
    exit_modules = [
        repo_root / "execution" / "reconciliation.py",
        repo_root / "execution" / "paper_fills.py",
        repo_root / "coordinator" / "equity.py",
    ]
    for module_path in exit_modules:
        source = module_path.read_text(encoding="utf-8")
        assert "health_score" not in source, (
            f"{module_path.name} references the health module — a health halt "
            "must NEVER gate exits or kill the telemetry a halt needs"
        )
