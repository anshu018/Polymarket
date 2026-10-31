"""
tests/test_position_close.py — Step 4 (B3) shared close path tests.

Locks the contract of execution/position_close.close_position — the ONE
writer of closed_trades (D-B08, used by startup reconciliation, the runtime
resolution sweep, and from Step 5 the exit engine):

  PnL math (direction-correct exit price, both sides × win/loss),
  attribution + fill passthrough columns, outcome derived from exit_price,
  claim-then-record ordering (delete result = double-close guard),
  copy-trade trust updates only for copy_edge_class_* rows (idempotency_uuid
  keying), trust failures never fail the close, and the two failure modes
  (claim failure → clean raise; ledger failure after claim → CRITICAL).
"""

import sys
import os
import asyncio
import uuid
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from execution.position_close import close_position


class MockTableBuilder:
    """eq-filtered table builder; DELETE returns the removed rows (mirrors
    postgrest-py's default return=representation — verified in the installed
    client), so the claim-result guard is exercised honestly."""

    def __init__(self, db_state: dict[str, list[dict[str, Any]]], table_name: str) -> None:
        self.db_state = db_state
        self.table_name = table_name
        self.filters: list[tuple[str, Any]] = []

    def delete(self) -> "MockTableBuilder":
        return self

    def eq(self, col: str, val: Any) -> "MockTableBuilder":
        self.filters.append((col, val))
        return self

    def execute(self) -> Any:
        class Result:
            def __init__(self, data: list[dict[str, Any]]) -> None:
                self.data = data

        rows = self.db_state.get(self.table_name, [])
        matched = [r for r in rows if all(r.get(c) == v for c, v in self.filters)]
        if matched:
            self.db_state[self.table_name] = [
                r for r in rows if r not in matched
            ]
        return Result([r.copy() for r in matched])


class MockClosedTradesBuilder:
    """Insert-capturing builder for closed_trades."""

    def __init__(self, db_state: dict[str, list[dict[str, Any]]], fail_insert: bool) -> None:
        self.db_state = db_state
        self.fail_insert = fail_insert
        self._row: dict[str, Any] = {}

    def insert(self, data: dict[str, Any]) -> "MockClosedTradesBuilder":
        self._row = data
        return self

    def execute(self) -> Any:
        class Result:
            data = [self._row]

        if self.fail_insert:
            raise ConnectionError("mocked insert outage")
        self.db_state.setdefault("closed_trades", []).append(dict(self._row))
        return Result()


class MockSupabaseClient:
    """Dispatches table() to the right builder: open_positions gets the
    delete/claim builder (returns the removed rows, mirroring postgrest-py's
    default return=representation — verified in the installed client);
    closed_trades gets the insert-capturing builder."""

    def __init__(self, db_state: dict[str, list[dict[str, Any]]]) -> None:
        self.db_state = db_state
        self.fail_delete = False
        self.fail_insert = False

    def table(self, name: str) -> Any:
        if name == "open_positions":
            if self.fail_delete:
                raise ConnectionError("mocked delete outage")
            return MockTableBuilder(self.db_state, name)
        if name == "closed_trades":
            return MockClosedTradesBuilder(self.db_state, self.fail_insert)
        raise AssertionError(f"unexpected table {name}")


def _position_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "id": str(uuid.uuid4()),
        "market_id": "mkt-close-1",
        "market_question": "Question?",
        "direction": "YES",
        "entry_price": 0.60,
        "position_size_usdc": 10.0,
        "strategy": "velocity",
        "agent_estimate": 0.65,
        "confidence_at_entry": 0.80,
        "category": "politics",
        "idempotency_uuid": str(uuid.uuid4()),
        "opened_at": datetime.now(timezone.utc).isoformat(),
        "thesis_id": "mkt-close-1:velocity:evk",
        "p_model_at_entry": 0.65,
        "market_price_at_entry": 0.59,
        "net_edge_at_entry": 0.03,
        "costs_paid_usdc": 0.05,
        "order_type": "taker",
        "fill_price": 0.61,
        "fill_type": "taker",
    }
    row.update(overrides)
    return row


@pytest.fixture
def close_db() -> tuple[MockSupabaseClient, dict[str, list[dict[str, Any]]]]:
    db_state: dict[str, list[dict[str, Any]]] = {"open_positions": [], "closed_trades": []}
    return MockSupabaseClient(db_state), db_state


# ── PnL math (direction-correct exit price supplied by the caller) ───────────

@pytest.mark.anyio
@pytest.mark.parametrize("exit_price,expected_pnl,outcome", [
    (1.0, 10.0 * (1.0 / 0.60) - 10.0, "win"),   # YES win: qty × 1.0 − size
    (0.0, -10.0, "loss"),                        # YES loss: worthless
])
async def test_pnl_math_yes_side(close_db, exit_price, expected_pnl, outcome):
    client, db = close_db
    row = _position_row()
    db["open_positions"].append(row)
    res = await close_position(client, row, exit_price=exit_price, exit_type="resolved", exit_reason="resolved")
    assert res.closed and not res.already_closed
    assert res.pnl_usdc == pytest.approx(expected_pnl)
    assert db["closed_trades"][0]["pnl_usdc"] == pytest.approx(expected_pnl)
    assert db["closed_trades"][0]["outcome"] == outcome


@pytest.mark.anyio
async def test_pnl_math_no_side_uses_side_price(close_db):
    """The caller hands the position's OWN side price: a NO position resolved
    against it at 1.0 is a win, at 0.0 a loss — the math itself is
    direction-agnostic by convention (the resolution predicate extracts the
    side price)."""
    client, db = close_db
    row = _position_row(direction="NO")
    db["open_positions"].append(row)
    res = await close_position(client, row, exit_price=1.0, exit_type="resolved", exit_reason="resolved")
    assert res.pnl_usdc == pytest.approx(10.0 * (1.0 / 0.60) - 10.0)
    assert db["closed_trades"][0]["outcome"] == "win"
    assert db["closed_trades"][0]["direction"] == "NO"


@pytest.mark.anyio
async def test_outcome_derived_from_exit_price_and_passthrough_columns(close_db):
    """outcome = win iff exit_price == 1.0 (verbatim legacy derivation); every
    attribution (Step 3) and fill (Step 5) column rides through to the row."""
    client, db = close_db
    row = _position_row(exit_price=0.35)
    db["open_positions"].append(row)
    await close_position(client, row, exit_price=0.35, exit_type="resolved", exit_reason="resolved")
    recorded = db["closed_trades"][0]
    assert recorded["outcome"] == "loss"
    assert recorded["exit_reason"] == "resolved"
    for col in ("thesis_id", "p_model_at_entry", "market_price_at_entry",
                "net_edge_at_entry", "costs_paid_usdc", "order_type",
                "fill_price", "fill_type", "strategy", "category",
                "confidence_at_entry", "market_id", "direction",
                "position_size_usdc", "opened_at"):
        assert recorded[col] == row[col], col
    # The legacy brier_contribution column keeps its semantics: the entry
    # estimate against the win/loss indicator (health producers use the
    # p_model + YES-line mapping instead — recorded noticed issue).
    assert recorded["brier_contribution"] == pytest.approx((0.65 - 0.0) ** 2)


# ── Claim-then-record ordering ───────────────────────────────────────────────

@pytest.mark.anyio
async def test_already_closed_row_writes_nothing(close_db):
    """A vanished open row (another closer won the race) → nothing inserted,
    no trust update, already_closed=True — the double-close guard."""
    client, db = close_db
    row = _position_row(strategy="copy_edge_class_a")
    # Row NOT seeded into open_positions → the delete claims nothing.
    with patch("copytrade.performance_tracker.resolve_copy_trade", AsyncMock()) as mock_resolve:
        res = await close_position(client, row, exit_price=1.0, exit_type="resolved", exit_reason="resolved")
    assert res.already_closed and not res.closed
    assert db["closed_trades"] == []
    mock_resolve.assert_not_called()


@pytest.mark.anyio
async def test_claim_failure_raises_and_writes_nothing(close_db):
    client, db = close_db
    row = _position_row()
    db["open_positions"].append(row)
    client.fail_delete = True
    with pytest.raises(ConnectionError):
        await close_position(client, row, exit_price=1.0, exit_type="resolved", exit_reason="resolved")
    assert db["closed_trades"] == []
    assert len(db["open_positions"]) == 1  # untouched — a later pass retries cleanly


@pytest.mark.anyio
async def test_ledger_failure_after_claim_is_loud(close_db, caplog):
    """The documented failure mode: claim succeeded (row gone), ledger write
    failed → raise + CRITICAL (the position is closed but unrecorded)."""
    client, db = close_db
    row = _position_row()
    db["open_positions"].append(row)
    client.fail_insert = True
    with caplog.at_level("CRITICAL", logger="execution.position_close"):
        with pytest.raises(ConnectionError):
            await close_position(client, row, exit_price=1.0, exit_type="resolved", exit_reason="resolved")
    assert "UNRECORDED" in caplog.text
    assert len(db["open_positions"]) == 0  # claimed — no silent duplicate on retry


# ── Copy-trade trust updates ─────────────────────────────────────────────────

@pytest.mark.parametrize("strategy,called", [
    ("copy_edge_class_a", True),
    ("copy_edge_class_b", True),
    ("velocity", False),
    ("recalibration", False),
])
@pytest.mark.anyio
async def test_copy_trade_trust_called_only_for_copy_strategies(close_db, strategy, called):
    client, db = close_db
    row = _position_row(strategy=strategy)
    db["open_positions"].append(row)
    with patch("copytrade.performance_tracker.resolve_copy_trade", AsyncMock()) as mock_resolve:
        res = await close_position(client, row, exit_price=0.0, exit_type="resolved", exit_reason="resolved")
    assert res.copy_trade is called
    if called:
        mock_resolve.assert_awaited_once()
        kwargs = mock_resolve.await_args.kwargs
        assert kwargs["idempotency_uuid"] == row["idempotency_uuid"]  # uuid keying, never market_id
        assert kwargs["market_id"] == row["market_id"]
        assert kwargs["exit_price"] == 0.0


@pytest.mark.anyio
async def test_trust_failure_never_fails_the_close(close_db):
    """Verbatim legacy behavior: a resolve_copy_trade error is logged loudly
    and the close (already claimed and recorded) still succeeds."""
    client, db = close_db
    row = _position_row(strategy="copy_edge_class_a")
    db["open_positions"].append(row)
    with patch("copytrade.performance_tracker.resolve_copy_trade", AsyncMock(side_effect=RuntimeError("trust boom"))):
        res = await close_position(client, row, exit_price=1.0, exit_type="resolved", exit_reason="resolved")
    assert res.closed and res.pnl_usdc is not None
    assert len(db["closed_trades"]) == 1
