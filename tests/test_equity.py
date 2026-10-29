"""
tests/test_equity.py — coordinator/equity.py (List-B.md Step 1 — B2).

Covers: marked-value math (YES/NO, cash netting, realized P&L), fallback-to-
entry on price failure (never fabricate gains), resting-maker exclusion, the
book-fetch cap, daily anchor bootstrap + mid-period WARNING, weekly/monthly
derivation including a missing first day, the staleness/unavailable entry
gate, alert cooldown, and the supervisor's fail-soft tick.

Module-local mocks, no conftest (repo rule). No live Supabase, no network,
no Telegram: alert_equity_warning and get_market_price are patched; only
.env.test is ever loaded (via config).
"""

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config  # noqa: E402 — must come after sys.path setup
from coordinator import equity  # noqa: E402

# 2026-10-14 is a Wednesday; its ISO week starts Monday 2026-10-12 and the
# calendar month starts 2026-10-01 — a fixed mid-week, mid-month "now" makes
# the weekly/monthly anchor derivations exact.
NOW = datetime(2026, 10, 14, 12, 0, 0, tzinfo=timezone.utc)
MONDAY = "2026-10-12"
TUESDAY = "2026-10-13"
WEDNESDAY = "2026-10-14"
PREV_MONTH = "2026-09-20"


# ── Mocks (module-local, per repo rule) ──────────────────────────────────────

class MockResult:
    def __init__(self, data: list[dict[str, Any]]) -> None:
        self.data = data


class MockTableBuilder:
    """Chainable stand-in for supabase-py over one table (subset equity uses)."""

    def __init__(self, db_state: dict[str, list[dict[str, Any]]], table_name: str) -> None:
        self._state = db_state
        self.table_name = table_name
        self._eq: dict[str, Any] = {}
        self._gte: dict[str, str] = {}
        self._order_col: Optional[str] = None
        self._limit: Optional[int] = None
        self._mutation: Optional[tuple[str, dict[str, Any]]] = None
        self._upsert_conflict: Optional[str] = None

    def select(self, *_cols: str) -> "MockTableBuilder":
        return self

    def eq(self, col: str, val: Any) -> "MockTableBuilder":
        self._eq[col] = val
        return self

    def gte(self, col: str, val: Any) -> "MockTableBuilder":
        self._gte[col] = str(val)
        return self

    def order(self, col: str) -> "MockTableBuilder":
        self._order_col = col
        return self

    def limit(self, n: int) -> "MockTableBuilder":
        self._limit = n
        return self

    def insert(self, payload: dict[str, Any]) -> "MockTableBuilder":
        self._mutation = ("insert", payload)
        return self

    def upsert(self, payload: dict[str, Any], on_conflict: Optional[str] = None) -> "MockTableBuilder":
        self._mutation = ("upsert", payload)
        self._upsert_conflict = on_conflict
        return self

    def update(self, payload: dict[str, Any]) -> "MockTableBuilder":
        self._mutation = ("update", payload)
        return self

    def execute(self) -> MockResult:
        rows = self._state.setdefault(self.table_name, [])
        if self._mutation is None:
            out = [dict(r) for r in rows]
            for col, val in self._eq.items():
                out = [r for r in out if r.get(col) == val]
            for col, val in self._gte.items():
                out = [r for r in out if str(r.get(col)) >= val]
            if self._order_col is not None:
                out = sorted(out, key=lambda r: str(r.get(self._order_col) or ""))
            if self._limit is not None:
                out = out[: self._limit]
            return MockResult(out)

        op, payload = self._mutation
        if op == "insert":
            rows.append(dict(payload))
            return MockResult([dict(payload)])
        if op == "upsert":
            conflict = self._upsert_conflict
            match = None
            if conflict:
                match = next(
                    (r for r in rows if r.get(conflict) == payload.get(conflict)), None
                )
            if match is not None:
                match.update(payload)
                return MockResult([dict(match)])
            rows.append(dict(payload))
            return MockResult([dict(payload)])
        if op == "update":
            matched = [r for r in rows if all(r.get(c) == v for c, v in self._eq.items())]
            for r in matched:
                r.update(payload)
            return MockResult([dict(r) for r in matched])
        raise AssertionError(f"Unexpected mutation: {op}")


class MockSupabaseClient:
    def __init__(self, db_state: dict[str, list[dict[str, Any]]]) -> None:
        self.db_state = db_state

    def table(self, name: str) -> MockTableBuilder:
        return MockTableBuilder(self.db_state, name)


def make_position(
    market_id: str = "mkt1",
    direction: str = "YES",
    entry: float = 0.50,
    size: float = 100.0,
    fill: Optional[float] = 0.50,
    market_price_at_entry: Optional[float] = None,
    order_type: str = "taker",
) -> dict[str, Any]:
    return {
        "market_id": market_id,
        "direction": direction,
        "entry_price": entry,
        "position_size_usdc": size,
        "fill_price": fill,
        "market_price_at_entry": market_price_at_entry,
        "order_type": order_type,
        "opened_at": "2026-10-13T09:00:00+00:00",
    }


async def _drain_tasks() -> None:
    """Let fire-and-forget alert tasks run."""
    for _ in range(3):
        await asyncio.sleep(0)


@pytest.fixture
def equity_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deterministic paper-mode config."""
    monkeypatch.setattr(config, "PAPER_TRADING", True)
    monkeypatch.setattr(config, "PAPER_TRADING_PORTFOLIO_USDC", 10000.0)
    monkeypatch.setattr(config, "EQUITY_MAX_MARKED_POSITIONS", 50)


@pytest.fixture(autouse=True)
def reset_equity_state():
    """The equity module holds process-wide cache/counters — reset around each test."""
    equity._snapshot_cache = None
    equity._last_gate_failure = None
    equity._gate_drops.update({"equity:stale": 0, "equity:unavailable": 0})
    equity._last_alert_at.clear()
    yield
    equity._snapshot_cache = None
    equity._last_gate_failure = None


@pytest.fixture
def equity_db(monkeypatch: pytest.MonkeyPatch, equity_env):
    """Mock DB + book price + captured alerts. Returns (state, price_holder, alerts)."""
    state: dict[str, list[dict[str, Any]]] = {
        "open_positions": [],
        "closed_trades": [],
        "daily_performance": [],
    }
    client = MockSupabaseClient(state)

    async def fake_get_client() -> MockSupabaseClient:
        return client

    price_holder: dict[str, Any] = {"price": 0.50, "fail": False}

    async def fake_get_market_price(token_id: str) -> float:
        if price_holder["fail"]:
            raise RuntimeError("book unavailable")
        return price_holder["price"]

    alerts: list[tuple[str, str, str, str]] = []

    async def fake_alert(event: str, trigger: str, alert_state: str, action: str) -> None:
        alerts.append((event, trigger, alert_state, action))

    monkeypatch.setattr(equity, "get_client", fake_get_client)
    monkeypatch.setattr(equity, "get_market_price", fake_get_market_price)
    monkeypatch.setattr(equity, "alert_equity_warning", fake_alert)
    return state, price_holder, alerts


# ── Mark-to-market math ──────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_mark_to_market_yes_and_no_positions(equity_db) -> None:
    """YES = shares × price_yes; NO = shares × (1 − price_yes); paper cash nets
    the committed open cost (D-B12)."""
    state, price_holder, _ = equity_db
    # YES: $100 @ 0.50 → 200 shares → 200 × 0.60 = 120
    state["open_positions"].append(make_position(market_id="mkt_yes", direction="YES"))
    # NO: $50 @ 0.20 → 250 shares → 250 × (1 − 0.60) = 100
    state["open_positions"].append(make_position(market_id="mkt_no", direction="NO", entry=0.20, size=50.0, fill=0.20))
    price_holder["price"] = 0.60

    marked = await equity.mark_to_market_value(NOW)

    assert marked.cash == pytest.approx(10000.0 - 150.0)  # 10000 − committed 150
    assert marked.marked_positions_value == pytest.approx(220.0)
    assert marked.value == pytest.approx(10070.0)
    assert marked.positions_marked == 2
    assert marked.fallback_positions == 0
    assert marked.skipped_resting == 0


@pytest.mark.anyio
async def test_paper_cash_includes_realized_pnl(equity_db) -> None:
    """Realized P&L from closed_trades counts toward paper equity (D-B12) —
    otherwise every resolution would look like an equity drop."""
    state, _, _ = equity_db
    state["closed_trades"].append({"pnl_usdc": -900.0})
    state["closed_trades"].append({"pnl_usdc": 250.0})

    marked = await equity.mark_to_market_value(NOW)

    assert marked.cash == pytest.approx(10000.0 - 900.0 + 250.0)
    assert marked.value == pytest.approx(9350.0)


@pytest.mark.anyio
async def test_resting_maker_contributes_zero(equity_db) -> None:
    """A resting maker owns no shares and committed no cash — excluded from
    both sides of the equation, counted in the diagnostics."""
    state, _, _ = equity_db
    state["open_positions"].append(
        make_position(order_type="maker", fill=None, size=100.0)
    )

    marked = await equity.mark_to_market_value(NOW)

    assert marked.value == pytest.approx(10000.0)
    assert marked.skipped_resting == 1
    assert marked.positions_marked == 0


@pytest.mark.anyio
async def test_price_fetch_failure_falls_back_to_entry_and_counts(equity_db) -> None:
    """Unfetchable price → entry price (never fabricate a gain), counted."""
    state, price_holder, _ = equity_db
    state["open_positions"].append(make_position(entry=0.50, fill=0.50))
    price_holder["fail"] = True

    marked = await equity.mark_to_market_value(NOW)

    # 200 shares × 0.50 entry — no gain fabricated despite the real price.
    assert marked.marked_positions_value == pytest.approx(100.0)
    assert marked.fallback_positions == 1


@pytest.mark.anyio
async def test_unusable_row_valued_at_zero_and_counted(equity_db) -> None:
    """A row with no usable price/direction contributes 0 and is counted."""
    state, _, _ = equity_db
    state["open_positions"].append(
        {"market_id": "bad", "direction": "SIDEWAYS", "entry_price": 0.0,
         "position_size_usdc": 100.0, "fill_price": None,
         "market_price_at_entry": None, "order_type": "taker", "opened_at": None}
    )

    marked = await equity.mark_to_market_value(NOW)

    # The unusable row contributed 0 to marked value, but its cost was
    # committed (taker row) — equity is understated, the conservative direction.
    assert marked.value == pytest.approx(9900.0)
    assert marked.fallback_positions == 1


@pytest.mark.anyio
async def test_book_fetch_cap_marks_only_oldest(equity_db, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fetches are capped (risk register); positions beyond the cap fall back
    to entry price and are counted."""
    state, price_holder, _ = equity_db
    monkeypatch.setattr(config, "EQUITY_MAX_MARKED_POSITIONS", 1)
    state["open_positions"].append(make_position(market_id="old", entry=0.50, fill=0.50))
    state["open_positions"].append(
        make_position(market_id="new", entry=0.40, fill=0.40, size=100.0)
    )
    price_holder["price"] = 0.80

    marked = await equity.mark_to_market_value(NOW)

    # oldest: live price 0.80 → 200 × 0.80 = 160; newest: capped → 250 × 0.40 = 100
    assert marked.marked_positions_value == pytest.approx(260.0)
    assert marked.fallback_positions == 1


@pytest.mark.anyio
async def test_live_mode_uses_clob_collateral(monkeypatch: pytest.MonkeyPatch) -> None:
    """Live cash = CLOB collateral (already nets realized P&L and open cost)."""
    monkeypatch.setattr(config, "PAPER_TRADING", False)

    class FakeClobClient:
        def get_balance_allowance(self, _params: Any) -> dict[str, str]:
            return {"balance": "5000000"}  # micro-USDC → 5.0

    monkeypatch.setattr(
        "execution.polymarket_auth.get_polymarket_client", lambda: FakeClobClient()
    )

    state: dict[str, list[dict[str, Any]]] = {"open_positions": [], "closed_trades": []}

    class OneTableClient:
        def table(self, name: str) -> MockTableBuilder:
            return MockTableBuilder({"open_positions": state["open_positions"]}, name)

    async def fake_get_client() -> OneTableClient:
        return OneTableClient()

    async def fake_price(token_id: str) -> float:
        return 0.50

    monkeypatch.setattr(equity, "get_client", fake_get_client)
    monkeypatch.setattr(equity, "get_market_price", fake_price)
    state["open_positions"].append(make_position(entry=0.50, fill=0.50, size=100.0))

    marked = await equity.mark_to_market_value(NOW)

    assert marked.cash == pytest.approx(5.0)
    assert marked.value == pytest.approx(5.0 + 200.0 * 0.50)


# ── Daily anchors & period derivation ────────────────────────────────────────

@pytest.mark.anyio
async def test_first_snapshot_bootstraps_daily_anchor(equity_db) -> None:
    """No row for today → created with anchor = current equity."""
    state, _, _ = equity_db
    state["open_positions"].append(make_position(entry=0.50, fill=0.50))

    ok = await equity.take_equity_snapshot_once(NOW)

    assert ok is True
    rows = state["daily_performance"]
    assert len(rows) == 1
    assert rows[0]["date"] == WEDNESDAY
    assert rows[0]["starting_balance_usdc"] == pytest.approx(10000.0 - 100.0 + 200.0 * 0.50)
    assert rows[0]["ending_balance_usdc"] == pytest.approx(rows[0]["starting_balance_usdc"])


@pytest.mark.anyio
async def test_second_snapshot_updates_ending_never_starting(equity_db) -> None:
    """The daily starting anchor NEVER moves — a moving anchor would mask the
    very drawdown the breaker is supposed to catch."""
    state, price_holder, _ = equity_db
    state["daily_performance"].append({
        "date": WEDNESDAY, "starting_balance_usdc": 10000.0,
        "ending_balance_usdc": 10000.0, "daily_pnl_usdc": 0.0,
    })
    state["open_positions"].append(make_position(entry=0.50, fill=0.50))
    price_holder["price"] = 0.45  # 200 shares → 90; cash 9900 → equity 9990

    await equity.take_equity_snapshot_once(NOW)

    row = state["daily_performance"][0]
    assert row["starting_balance_usdc"] == pytest.approx(10000.0)
    assert row["ending_balance_usdc"] == pytest.approx(9990.0)
    assert row["daily_pnl_usdc"] == pytest.approx(-10.0)


def _anchor_row(date: str, starting: float) -> dict[str, Any]:
    return {
        "date": date, "starting_balance_usdc": starting,
        "ending_balance_usdc": starting, "daily_pnl_usdc": 0.0,
    }


@pytest.mark.anyio
async def test_weekly_anchor_is_earliest_row_of_iso_week(equity_db) -> None:
    """Weekly start = starting_balance_usdc of the earliest row in the ISO week."""
    state, _, _ = equity_db
    state["daily_performance"].extend([
        _anchor_row(MONDAY, 11000.0),
        _anchor_row(TUESDAY, 10500.0),
    ])

    balances = await equity.get_period_balances(NOW, 9800.0)

    assert balances["weekly"] == (pytest.approx(11000.0), pytest.approx(9800.0))
    assert balances["daily"] == (pytest.approx(9800.0), pytest.approx(9800.0))


@pytest.mark.anyio
async def test_monthly_anchor_is_earliest_row_of_month(equity_db) -> None:
    """Monthly start = earliest row of the calendar month; last month's rows
    never leak into it."""
    state, _, _ = equity_db
    state["daily_performance"].extend([
        _anchor_row(PREV_MONTH, 9000.0),
        _anchor_row("2026-10-01", 12000.0),
        _anchor_row(MONDAY, 11000.0),
    ])

    balances = await equity.get_period_balances(NOW, 9800.0)

    assert balances["monthly"] == (pytest.approx(12000.0), pytest.approx(9800.0))
    assert balances["weekly"] == (pytest.approx(11000.0), pytest.approx(9800.0))


@pytest.mark.anyio
async def test_missing_period_first_day_uses_earliest_and_warns(equity_db) -> None:
    """Bot was down Monday → weekly anchor = earliest available row (Tuesday)
    and a WARNING fires — the documented approximation (D-B03)."""
    state, _, alerts = equity_db
    state["daily_performance"].extend([
        _anchor_row(TUESDAY, 10500.0),
    ])

    balances = await equity.get_period_balances(NOW, 9800.0)

    assert balances["weekly"] == (pytest.approx(10500.0), pytest.approx(9800.0))
    await _drain_tasks()
    assert any(a[0] == "EQUITY_ANCHOR_BOOTSTRAP" and "weekly" in a[1] for a in alerts)


@pytest.mark.anyio
async def test_mid_period_bootstrap_of_anchor_warns(equity_db) -> None:
    """No rows at all mid-week/mid-month → today's bootstrap row becomes BOTH
    period anchors → WARNING (a silent bootstrap is forbidden)."""
    state, _, alerts = equity_db

    balances = await equity.get_period_balances(NOW, 9800.0)

    # Today (Wednesday) is neither Monday nor the 1st → both anchors bootstrap.
    assert balances["weekly"] == (pytest.approx(9800.0), pytest.approx(9800.0))
    assert balances["monthly"] == (pytest.approx(9800.0), pytest.approx(9800.0))
    await _drain_tasks()
    # One shared EQUITY_ANCHOR_BOOTSTRAP event tag → the alert cooldown
    # deliberately sends a single WARNING for both periods (anti-spam).
    events = [a for a in alerts if a[0] == "EQUITY_ANCHOR_BOOTSTRAP"]
    assert len(events) == 1
    assert "weekly" in events[0][1]


@pytest.mark.anyio
async def test_period_first_day_bootstrap_does_not_warn(equity_db) -> None:
    """Bootstrapping ON the period's first day is legitimate — no WARNING.
    2026-06-01 is both a Monday (ISO week start) and the 1st of the month."""
    state, _, alerts = equity_db
    first_day = datetime(2026, 6, 1, 8, 0, 0, tzinfo=timezone.utc)

    await equity.get_period_balances(first_day, 10000.0)

    await _drain_tasks()
    assert not [a for a in alerts if a[0] == "EQUITY_ANCHOR_BOOTSTRAP"]


# ── Entry gate: staleness / unavailability (fail-closed) ─────────────────────

@pytest.mark.anyio
async def test_gate_unavailable_when_never_computed(equity_db) -> None:
    state, _, alerts = equity_db

    gate = equity.get_entry_gate(NOW)

    assert gate is None
    assert equity.get_last_gate_failure() == "equity:unavailable"
    assert equity.get_drop_counters()["equity:unavailable"] == 1
    await _drain_tasks()
    assert any(a[0] == "EQUITY_UNAVAILABLE" for a in alerts)


@pytest.mark.anyio
async def test_gate_stale_after_threshold_and_fresh_within_it(equity_db) -> None:
    state, _, alerts = equity_db
    await equity.take_equity_snapshot_once(NOW)

    fresh = equity.get_entry_gate(NOW)
    assert fresh is not None
    assert fresh.current_equity == pytest.approx(10000.0)

    # Exactly at the threshold (60min) is still fresh (strictly-greater check);
    # one minute beyond is stale.
    boundary = equity.get_entry_gate(NOW.replace(hour=13))  # +60min
    assert boundary is not None

    stale = equity.get_entry_gate(NOW.replace(hour=13, minute=1))  # +61min
    assert stale is None
    assert equity.get_last_gate_failure() == "equity:stale"
    assert equity.get_drop_counters()["equity:stale"] == 1
    await _drain_tasks()
    assert any(a[0] == "EQUITY_STALE" for a in alerts)


@pytest.mark.anyio
async def test_gate_carries_real_period_balances(equity_db) -> None:
    """The gate hands the entry paths per-period (start, current) values."""
    state, _, _ = equity_db
    state["daily_performance"].extend([_anchor_row(MONDAY, 11000.0)])
    await equity.take_equity_snapshot_once(NOW)

    gate = equity.get_entry_gate(NOW)

    assert gate is not None
    assert set(gate.starting_balances) == {"daily", "weekly", "monthly"}
    assert gate.starting_balances["weekly"] == pytest.approx(11000.0)
    assert gate.current_balances == {p: pytest.approx(gate.current_equity) for p in ("daily", "weekly", "monthly")}


@pytest.mark.anyio
async def test_alert_cooldown_suppresses_repeats(equity_db) -> None:
    """Repeating gate failures inside the cooldown send ONE alert (global
    DO-NOT: cooldowns required on repeating alerts)."""
    state, _, alerts = equity_db

    equity.get_entry_gate(NOW)
    equity.get_entry_gate(NOW.replace(minute=1))
    await _drain_tasks()
    assert len([a for a in alerts if a[0] == "EQUITY_UNAVAILABLE"]) == 1

    # Counters keep counting even while alerts are suppressed.
    assert equity.get_drop_counters()["equity:unavailable"] == 2


# ── Supervisor & cache-backed portfolio value ────────────────────────────────

@pytest.mark.anyio
async def test_supervisor_tick_failure_leaves_cache_cold(equity_db, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed snapshot tick must NOT refresh the cache — entries then fail
    closed on the aging/absent cache (D-B03)."""
    state, _, _ = equity_db

    async def broken_get_client() -> Any:
        raise RuntimeError("supabase down")

    monkeypatch.setattr(equity, "get_client", broken_get_client)

    with pytest.raises(RuntimeError):
        await equity.take_equity_snapshot_once(NOW)

    assert equity._snapshot_cache is None
    with pytest.raises(RuntimeError):
        await equity.get_current_portfolio_value()


@pytest.mark.anyio
async def test_get_current_portfolio_value_uses_cache(equity_db) -> None:
    """Sizing reads are cache-backed: no book fetches per call after a tick."""
    state, price_holder, _ = equity_db
    state["open_positions"].append(make_position(entry=0.50, fill=0.50))
    await equity.take_equity_snapshot_once(NOW)

    price_holder["price"] = 0.99  # would change a fresh computation

    value = await equity.get_current_portfolio_value()

    assert value == pytest.approx(equity._snapshot_cache.current_equity)


@pytest.mark.anyio
async def test_supervisor_reraises_cancellation(equity_db, monkeypatch: pytest.MonkeyPatch) -> None:
    """The supervisor must die on cancel (supervisor pattern, E-B12)."""
    ticks = {"n": 0}

    async def quick_tick(now: Optional[datetime] = None) -> bool:
        ticks["n"] += 1
        return True

    sleep_count = {"n": 0}

    async def fake_sleep(_seconds: float) -> None:
        sleep_count["n"] += 1
        if sleep_count["n"] >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(equity, "take_equity_snapshot_once", quick_tick)
    monkeypatch.setattr(equity.asyncio, "sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await equity.run_equity_snapshot_supervisor()

    assert ticks["n"] == 2
