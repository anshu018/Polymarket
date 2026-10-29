"""
tests/test_paper_fills.py — Step 5 (A5) paper maker-fill monitor tests.

The open_positions row IS the monitor's state, so every test seeds resting rows
and asserts on the post-pass DB state (fill columns / deletion / idempotency
cancel-mark). Module-local mocks, no conftest (repo rule).
"""

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import patch

import pytest

from execution.paper_fills import check_pending_maker_orders_once
from risk.cost_model import BookSnapshot


class Result:
    def __init__(self, data: Any):
        self.data = data


class MockTableBuilder:
    """Chainable query builder over one table's rows (subset the monitor uses)."""

    def __init__(self, rows: list[dict[str, Any]]):
        self._rows = rows
        self._eq: dict[str, Any] = {}
        self._is_null: list[str] = []
        self._update_payload: dict[str, Any] | None = None

    def select(self, _cols: str) -> "MockTableBuilder":
        return self

    def update(self, payload: dict[str, Any]) -> "MockTableBuilder":
        self._update_payload = payload
        return self

    def delete(self) -> "MockTableBuilder":
        self._delete = True
        return self

    def eq(self, column: str, value: Any) -> "MockTableBuilder":
        self._eq[column] = value
        return self

    def is_(self, column: str, value: str) -> "MockTableBuilder":
        if value == "null":
            self._is_null.append(column)
        return self

    def order(self, _col: str) -> "MockTableBuilder":
        return self

    def limit(self, _n: int) -> "MockTableBuilder":
        return self

    def execute(self) -> Result:
        matched = [
            r for r in self._rows
            if all(r.get(c) == v for c, v in self._eq.items())
            and all(r.get(c) is None for c in self._is_null)
        ]
        if self._update_payload is not None:
            for r in matched:
                r.update(self._update_payload)
            return Result([])
        if getattr(self, "_delete", False):
            for r in matched:
                self._rows.remove(r)
            return Result([])
        return Result([dict(r) for r in matched])


class MockSupabaseClient:
    def __init__(self, state: dict[str, list[dict[str, Any]]]):
        self._state = state

    def table(self, name: str) -> MockTableBuilder:
        return MockTableBuilder(self._state[name])


def make_resting_row(
    row_id: str = "row-1",
    market_id: str = "mkt-1",
    direction: str = "YES",
    resting_price: float = 0.48,
    age_seconds: float = 0.0,
    p_model: float | None = 0.65,
) -> dict[str, Any]:
    opened_at = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    return {
        "id": row_id,
        "market_id": market_id,
        "direction": direction,
        "category": "politics",
        "position_size_usdc": 100.0,
        "p_model_at_entry": p_model,
        "order_type": "maker",
        "fill_price": None,
        "fill_type": None,
        "resting_price": resting_price,
        "opened_at": opened_at.isoformat(),
        "idempotency_uuid": str(uuid.uuid4()),
    }


@pytest.fixture
def monitor_db():
    """State dict + patched get_client / get_market_book for the monitor."""
    state: dict[str, list[dict[str, Any]]] = {
        "open_positions": [],
        "idempotency_log": [],
    }
    client = MockSupabaseClient(state)
    book_holder: dict[str, Any] = {"book": BookSnapshot(0.48, 0.52, 5000.0)}

    async def fake_get_client():
        return client

    async def fake_book(market_id: str):
        return book_holder["book"]

    with patch("execution.paper_fills.get_client", fake_get_client), \
         patch("execution.paper_fills.get_market_book", fake_book):
        yield state, book_holder


# ── Inside the wait window ────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_maker_fills_when_book_trades_through_the_level(monitor_db):
    """Rested at 0.48; the ask traded down to 0.47 → filled at the resting level."""
    state, book_holder = monitor_db
    row = make_resting_row()
    state["open_positions"].append(row)
    book_holder["book"] = BookSnapshot(best_bid=0.44, best_ask=0.47, depth_usd=5000.0)

    examined = await check_pending_maker_orders_once()

    assert examined == 1
    assert row["fill_price"] == 0.48
    assert row["fill_type"] == "maker_fill"


@pytest.mark.anyio
async def test_maker_stays_resting_while_ask_above_level(monitor_db):
    """Unchanged book: the ask (0.52) never reached our 0.48 bid — no fill."""
    state, _ = monitor_db
    row = make_resting_row()
    state["open_positions"].append(row)

    await check_pending_maker_orders_once()

    assert row["fill_price"] is None
    assert row["fill_type"] is None


# ── Past the wait window: fallback / cancel ───────────────────────────────────

@pytest.mark.anyio
async def test_expired_maker_falls_back_to_taker_when_still_net_positive(monitor_db):
    """Step 1 fallback rule: expired + current-book taker net edge > 0 → take."""
    state, book_holder = monitor_db
    row = make_resting_row(age_seconds=60.0)  # window is 20s → expired
    state["open_positions"].append(row)
    # Market drifted UP (mid 0.55 < p_model 0.65): taker still net-positive.
    book_holder["book"] = BookSnapshot(best_bid=0.53, best_ask=0.57, depth_usd=5000.0)

    await check_pending_maker_orders_once()

    assert row["fill_price"] == 0.57  # the taker price paid
    assert row["fill_type"] == "taker_fallback"


@pytest.mark.anyio
async def test_expired_maker_cancels_when_no_longer_net_positive(monitor_db):
    """The market ran past the estimate: taking would be negative-edge → cancel
    (position deleted, idempotency marked cancelled)."""
    state, book_holder = monitor_db
    row = make_resting_row(age_seconds=60.0)
    state["open_positions"].append(row)
    idem_id = row["idempotency_uuid"]
    state["idempotency_log"].append({"id": idem_id, "status": "pending"})
    # Market ran to mid 0.75 ≫ p_model 0.65: a YES taker pays 0.78 for 0.65.
    book_holder["book"] = BookSnapshot(best_bid=0.72, best_ask=0.78, depth_usd=5000.0)

    await check_pending_maker_orders_once()

    assert state["open_positions"] == []
    assert state["idempotency_log"][0]["status"] == "cancelled"


@pytest.mark.anyio
async def test_expired_maker_cancels_fail_closed_without_p_model(monitor_db):
    """Without a model probability the fallback's net-positivity cannot be
    evaluated — fail closed to cancel, never take blind."""
    state, book_holder = monitor_db
    row = make_resting_row(age_seconds=60.0, p_model=None)
    state["open_positions"].append(row)
    book_holder["book"] = BookSnapshot(best_bid=0.44, best_ask=0.46, depth_usd=5000.0)

    await check_pending_maker_orders_once()

    assert state["open_positions"] == []


@pytest.mark.anyio
async def test_expired_maker_cancels_when_depth_cannot_carry_the_fallback(monitor_db):
    """Net-positive but the book cannot carry the taker size → cancel (an
    unfillable fallback is not a fallback)."""
    state, book_holder = monitor_db
    row = make_resting_row(age_seconds=60.0)
    state["open_positions"].append(row)
    book_holder["book"] = BookSnapshot(best_bid=0.53, best_ask=0.57, depth_usd=10.0)

    await check_pending_maker_orders_once()

    assert state["open_positions"] == []


# ── Robustness: isolation, corrupt rows, DB/book failures, restart re-arm ─────

@pytest.mark.anyio
async def test_row_failure_is_isolated_and_later_rows_still_resolve(monitor_db):
    """One row's book read exploding must not stall the others (per-row isolation)."""
    state, book_holder = monitor_db
    bad_row = make_resting_row(row_id="row-bad", market_id="mkt-bad")
    good_row = make_resting_row(row_id="row-good", market_id="mkt-good")
    state["open_positions"].extend([bad_row, good_row])

    calls: list[str] = []

    async def flaky_book(market_id: str):
        calls.append(market_id)
        if market_id == "mkt-bad":
            raise RuntimeError("book exploded")
        return book_holder["book"]

    with patch("execution.paper_fills.get_market_book", flaky_book):
        examined = await check_pending_maker_orders_once()

    assert examined == 2
    # The good row got a real verdict (unchanged book → still resting, but READ):
    assert calls == ["mkt-bad", "mkt-good"]
    assert good_row["fill_price"] is None
    assert good_row["fill_type"] is None
    assert "row-bad" in [r["id"] for r in state["open_positions"]]


@pytest.mark.anyio
async def test_corrupt_resting_row_is_cancelled_not_looped_forever(monitor_db):
    """A resting row without a usable level/direction can never resolve — it is
    removed (loudly logged) instead of sitting in the scan forever."""
    state, _ = monitor_db
    row = make_resting_row(row_id="row-corrupt", direction="SIDEWAYS")
    state["open_positions"].append(row)

    await check_pending_maker_orders_once()

    assert state["open_positions"] == []


@pytest.mark.anyio
async def test_scan_db_failure_is_fail_soft(monitor_db):
    """A failed scan never raises out of the monitor — the next tick retries."""
    async def dead_client():
        raise RuntimeError("supabase down")

    with patch("execution.paper_fills.get_client", dead_client):
        examined = await check_pending_maker_orders_once()

    assert examined == 0


@pytest.mark.anyio
async def test_unavailable_book_leaves_the_order_resting(monitor_db):
    """No honest fill verdict without a book — the row stays resting for the
    next tick (fail-soft, never silently filled or cancelled)."""
    state, book_holder = monitor_db
    row = make_resting_row()
    state["open_positions"].append(row)
    book_holder["book"] = None

    await check_pending_maker_orders_once()

    assert row["fill_price"] is None
    assert row["fill_type"] is None
    assert state["open_positions"] == [row]


@pytest.mark.anyio
async def test_restart_rearm_resolves_rows_left_from_before(monitor_db):
    """Restart-safety: state lives in the DB, so rows created by a previous
    process are picked up by the very first scan — here an order that expired
    while the process was down falls back exactly like a live one."""
    state, book_holder = monitor_db
    # opened_at 2 hours ago: created long before "this" process started.
    row = make_resting_row(age_seconds=2 * 3600.0)
    state["open_positions"].append(row)
    book_holder["book"] = BookSnapshot(best_bid=0.53, best_ask=0.57, depth_usd=5000.0)

    await check_pending_maker_orders_once()

    assert row["fill_type"] == "taker_fallback"
    assert row["fill_price"] == 0.57
