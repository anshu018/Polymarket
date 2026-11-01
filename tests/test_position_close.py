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
from execution.position_close import close_position, partial_close_position


class MockTableBuilder:
    """eq-filtered table builder; DELETE returns the removed rows and UPDATE
    returns the updated rows (both mirror postgrest-py's default
    return=representation — verified in the installed client), so the
    claim-result guards are exercised honestly."""

    def __init__(self, db_state: dict[str, list[dict[str, Any]]], table_name: str) -> None:
        self.db_state = db_state
        self.table_name = table_name
        self.filters: list[tuple[str, Any]] = []
        self._mode = "delete"
        self._update_data: dict[str, Any] = {}

    def delete(self) -> "MockTableBuilder":
        self._mode = "delete"
        return self

    def update(self, data: dict[str, Any]) -> "MockTableBuilder":
        self._mode = "update"
        self._update_data = data
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
            if self._mode == "delete":
                self.db_state[self.table_name] = [
                    r for r in rows if r not in matched
                ]
            else:
                for r in matched:
                    r.update(self._update_data)
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


# ── Step 5 (B5): exit_type persistence + early-exit semantics (D-B05/D-B06) ──

@pytest.mark.anyio
@pytest.mark.parametrize("exit_type", ["resolved", "exit_stop", "exit_profit", "exit_liquidity"])
async def test_exit_type_persisted_in_the_ledger_row(close_db, exit_type):
    """The Step 5 exit_type column rides every close, resolution or early."""
    client, db = close_db
    row = _position_row()
    db["open_positions"].append(row)
    await close_position(client, row, exit_price=0.55, exit_type=exit_type, exit_reason="detail")
    assert db["closed_trades"][0]["exit_type"] == exit_type


@pytest.mark.anyio
async def test_early_exit_outcome_is_the_pnl_sign_not_the_resolution_rule(close_db):
    """An early exit has no resolution: exit_price 0.70 on a 0.60 entry is a
    realized GAIN and must read 'win' — the verbatim exit_price == 1.0 rule
    applies to resolutions only."""
    client, db = close_db
    row = _position_row()
    db["open_positions"].append(row)
    await close_position(client, row, exit_price=0.70, exit_type="exit_profit", exit_reason="profit")
    recorded = db["closed_trades"][0]
    assert recorded["outcome"] == "win"
    assert recorded["pnl_usdc"] == pytest.approx((10.0 / 0.60) * (0.70 - 0.60))

    row2 = _position_row()
    db["open_positions"].append(row2)
    await close_position(client, row2, exit_price=0.40, exit_type="exit_stop", exit_reason="stop")
    assert db["closed_trades"][1]["outcome"] == "loss"


@pytest.mark.anyio
async def test_early_exit_records_copytrade_log_never_the_wallet(close_db):
    """D-B06/D-B20 regression: an early exit of a copy-trade position calls
    record_early_exit and NEVER resolve_copy_trade — wallet W/L/trust is
    structurally unreachable on early exits."""
    client, db = close_db
    row = _position_row(strategy="copy_edge_class_a")
    db["open_positions"].append(row)
    with patch("copytrade.performance_tracker.resolve_copy_trade", AsyncMock()) as mock_resolve, \
         patch("copytrade.performance_tracker.record_early_exit", AsyncMock()) as mock_early:
        res = await close_position(client, row, exit_price=0.55, exit_type="exit_stop", exit_reason="stop")
    mock_resolve.assert_not_called()
    mock_early.assert_awaited_once()
    kwargs = mock_early.await_args.kwargs
    assert kwargs["exit_type"] == "exit_stop"
    assert kwargs["idempotency_uuid"] == row["idempotency_uuid"]
    assert res.copy_trade is True  # a copy-trade side-effect ran (the recording)


@pytest.mark.anyio
async def test_resolution_still_updates_wallet_never_records_early(close_db):
    """The mirror regression: resolutions keep the verbatim trust update."""
    client, db = close_db
    row = _position_row(strategy="copy_edge_class_a")
    db["open_positions"].append(row)
    with patch("copytrade.performance_tracker.resolve_copy_trade", AsyncMock()) as mock_resolve, \
         patch("copytrade.performance_tracker.record_early_exit", AsyncMock()) as mock_early:
        await close_position(client, row, exit_price=1.0, exit_type="resolved", exit_reason="resolved")
    mock_resolve.assert_awaited_once()
    mock_early.assert_not_called()


@pytest.mark.anyio
async def test_early_exit_sideeffect_failure_never_fails_the_close(close_db):
    client, db = close_db
    row = _position_row(strategy="copy_edge_class_a")
    db["open_positions"].append(row)
    with patch(
        "copytrade.performance_tracker.record_early_exit",
        AsyncMock(side_effect=RuntimeError("log boom")),
    ):
        res = await close_position(client, row, exit_price=0.55, exit_type="exit_stop", exit_reason="stop")
    assert res.closed and res.pnl_usdc is not None
    assert res.copy_trade is False
    assert len(db["closed_trades"]) == 1


# ── Step 5 (B5, D-B19): partial_close_position — R4's shared-path mechanics ──

@pytest.mark.anyio
async def test_partial_exit_shrinks_the_row_and_records_the_sold_portion(close_db):
    client, db = close_db
    row = _position_row()  # entry 0.60, size 10.0 → 16.6667 shares
    db["open_positions"].append(row)
    res = await partial_close_position(
        client, row, fraction=0.5, exit_price=0.70,
        exit_type="exit_time_decay", exit_reason="72h left, thesis unconfirmed",
    )
    assert res.closed and not res.already_closed
    assert res.copy_trade is False  # partial exits never touch the copy log (D-B20)
    # The open row is REDUCED, not closed.
    assert len(db["open_positions"]) == 1
    assert db["open_positions"][0]["position_size_usdc"] == pytest.approx(5.0)
    # The ledger row carries the SOLD portion only.
    sold = db["closed_trades"][0]
    assert sold["position_size_usdc"] == pytest.approx(5.0)
    assert sold["exit_price"] == 0.70
    assert sold["pnl_usdc"] == pytest.approx((10.0 / 0.60) * 0.5 * (0.70 - 0.60))
    assert sold["exit_type"] == "exit_time_decay"
    assert sold["outcome"] == "win"  # pnl > 0 — early-exit semantics
    assert res.pnl_usdc == pytest.approx(sold["pnl_usdc"])
    # Attribution passthrough rides along on partials too.
    assert sold["thesis_id"] == row["thesis_id"]
    assert sold["fill_type"] == row["fill_type"]


@pytest.mark.anyio
async def test_partial_exit_performs_no_copytrade_sideeffect(close_db):
    client, db = close_db
    row = _position_row(strategy="copy_edge_class_a")
    db["open_positions"].append(row)
    with patch("copytrade.performance_tracker.resolve_copy_trade", AsyncMock()) as mock_resolve, \
         patch("copytrade.performance_tracker.record_early_exit", AsyncMock()) as mock_early:
        await partial_close_position(
            client, row, fraction=0.5, exit_price=0.70,
            exit_type="exit_time_decay", exit_reason="partial",
        )
    mock_resolve.assert_not_called()
    mock_early.assert_not_called()


@pytest.mark.anyio
async def test_partial_exit_on_vanished_row_writes_nothing(close_db):
    client, db = close_db
    row = _position_row()  # NOT seeded — another closer won
    res = await partial_close_position(
        client, row, fraction=0.5, exit_price=0.70,
        exit_type="exit_time_decay", exit_reason="partial",
    )
    assert res.already_closed and not res.closed
    assert db["closed_trades"] == []


@pytest.mark.anyio
async def test_partial_then_full_close_ledger_is_consistent(close_db):
    """R4 (partial) followed by a resolution (full): the two ledger rows carry
    disjoint portions whose sizes sum to the original and whose identity
    columns agree — the equity identity stays continuous."""
    client, db = close_db
    row = _position_row()  # entry 0.60, size 10.0
    db["open_positions"].append(row)
    await partial_close_position(
        client, row, fraction=0.5, exit_price=0.70,
        exit_type="exit_time_decay", exit_reason="partial",
    )
    reduced = db["open_positions"][0]
    assert reduced["position_size_usdc"] == pytest.approx(5.0)
    res = await close_position(client, reduced, exit_price=1.0, exit_type="resolved", exit_reason="resolved")
    assert res.closed
    assert len(db["open_positions"]) == 0
    assert len(db["closed_trades"]) == 2
    assert sum(r["position_size_usdc"] for r in db["closed_trades"]) == pytest.approx(10.0)
    # Remaining shares (8.3333) resolved at 1.0: pnl = 8.3333 × (1 − 0.60).
    assert db["closed_trades"][1]["pnl_usdc"] == pytest.approx((10.0 / 0.60) * 0.5 * (1.0 - 0.60))
    assert db["closed_trades"][1]["outcome"] == "win"


@pytest.mark.anyio
async def test_partial_ledger_failure_after_claim_is_loud(close_db, caplog):
    client, db = close_db
    row = _position_row()
    db["open_positions"].append(row)
    client.fail_insert = True
    with caplog.at_level("CRITICAL", logger="execution.position_close"):
        with pytest.raises(ConnectionError):
            await partial_close_position(
                client, row, fraction=0.5, exit_price=0.70,
                exit_type="exit_time_decay", exit_reason="partial",
            )
    assert "UNRECORDED" in caplog.text
    # The claim (size reduction) succeeded — no silent duplicate on retry.
    assert db["open_positions"][0]["position_size_usdc"] == pytest.approx(5.0)


@pytest.mark.anyio
@pytest.mark.parametrize("fraction", [0.0, 1.0, 1.5, -0.5])
async def test_partial_fraction_validation_raises(close_db, fraction):
    client, db = close_db
    row = _position_row()
    with pytest.raises(ValueError):
        await partial_close_position(
            client, row, fraction=fraction, exit_price=0.70,
            exit_type="exit_time_decay", exit_reason="partial",
        )
    assert db["closed_trades"] == []


@pytest.mark.anyio
async def test_partial_rejects_non_positive_size_or_entry(close_db):
    client, db = close_db
    for bad in ({"entry_price": 0.0}, {"position_size_usdc": 0.0}):
        with pytest.raises(ValueError):
            await partial_close_position(
                client, _position_row(**bad), fraction=0.5, exit_price=0.70,
                exit_type="exit_time_decay", exit_reason="partial",
            )
    assert db["closed_trades"] == []

