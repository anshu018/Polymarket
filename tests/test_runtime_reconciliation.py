"""
tests/test_runtime_reconciliation.py — Step 4 (B3) runtime loop tests.

Exercises execution/runtime_reconciliation.py against a mock Supabase client
(patched into BOTH the runtime module and coordinator.lease, so the lease
claim/release flows run for real against the shared tests.lease_mock
semantics) and the REAL shared close path (execution/position_close.py —
the DoD requirement: closes flow through it, not a parallel one):

  resolved position closes WITHOUT a restart via the shared path (ledger row
  + open row gone + copy-trade trust updated); void markets skipped + warned
  (D-B15); resting makers on resolved markets cancelled, never closed;
  unresolved/unavailable untouched; Gamma cap with rotating offset; lease
  conflict → skip + retry next tick; stuck-pending classification (D-B14:
  resting skip / filled confirm / no-match fail) with the fresh-row guard;
  per-row fail-softness; the daily digest fires only on a UTC-date change.
"""

import asyncio
import sys
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import config
from coordinator import lease
from execution import runtime_reconciliation as rr
from execution.resolution import MarketResolution, ResolutionStatus
from tests.lease_mock import MockRpcBuilder


def _iso(dt: datetime) -> str:
    return dt.isoformat()


class MockTableBuilder:
    """Generic eq/order builder: select / insert / update / delete, with
    DELETE returning the removed rows (postgrest-py representation semantics,
    verified in the installed client)."""

    def __init__(self, db_state: dict[str, list[dict[str, Any]]], table_name: str) -> None:
        self.db_state = db_state
        self.table_name = table_name
        self.filters: list[tuple[str, Any]] = []
        self._order: Optional[tuple[str, bool]] = None
        self._payload: dict[str, Any] = {}
        self._mode = "select"

    def select(self, cols: str) -> "MockTableBuilder":
        self._mode = "select"
        return self

    def insert(self, data: dict[str, Any]) -> "MockTableBuilder":
        self._mode = "insert"
        self._payload = data
        return self

    def update(self, data: dict[str, Any]) -> "MockTableBuilder":
        self._mode = "update"
        self._payload = data
        return self

    def delete(self) -> "MockTableBuilder":
        self._mode = "delete"
        return self

    def eq(self, col: str, val: Any) -> "MockTableBuilder":
        self.filters.append((col, val))
        return self

    def order(self, col: str, desc: bool = False) -> "MockTableBuilder":
        self._order = (col, desc)
        return self

    def limit(self, val: int) -> "MockTableBuilder":
        return self

    def execute(self) -> Any:
        class Result:
            def __init__(self, data: list[dict[str, Any]]) -> None:
                self.data = data

        rows = self.db_state.get(self.table_name, [])
        matched = [r for r in rows if all(r.get(c) == v for c, v in self.filters)]
        if self._mode == "insert":
            self.db_state.setdefault(self.table_name, []).append(dict(self._payload))
            return Result([dict(self._payload)])
        if self._mode == "update":
            for r in rows:
                if all(r.get(c) == v for c, v in self.filters):
                    r.update(self._payload)
            return Result([r.copy() for r in matched])
        if self._mode == "delete":
            if matched:
                self.db_state[self.table_name] = [r for r in rows if r not in matched]
            return Result([r.copy() for r in matched])
        ordered = sorted(
            matched,
            key=lambda x: str(x.get(self._order[0], "")) if self._order else "",
            reverse=bool(self._order and self._order[1]),
        )
        return Result([r.copy() for r in ordered])


class MockSupabaseClient:
    """Unified mock: tables + the claim RPC, patched into both the runtime
    module and coordinator.lease so leases run for real against db_state."""

    def __init__(self, db_state: dict[str, list[dict[str, Any]]]) -> None:
        self.db_state = db_state
        self.rpc_call_count = 0

    def table(self, name: str) -> MockTableBuilder:
        return MockTableBuilder(self.db_state, name)

    def rpc(self, fn_name: str, params: dict[str, Any]) -> MockRpcBuilder:
        self.rpc_call_count += 1
        return MockRpcBuilder(self.db_state, fn_name, params)


def _position_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "id": str(uuid.uuid4()),
        "market_id": f"mkt-{uuid.uuid4().hex[:8]}",
        "market_question": "Question?",
        "direction": "YES",
        "entry_price": 0.60,
        "position_size_usdc": 10.0,
        "strategy": "velocity",
        "agent_estimate": 0.65,
        "confidence_at_entry": 0.80,
        "category": "politics",
        "idempotency_uuid": str(uuid.uuid4()),
        "opened_at": "2026-10-01T00:00:00+00:00",
        "thesis_id": None,
        "p_model_at_entry": None,
        "market_price_at_entry": None,
        "net_edge_at_entry": None,
        "costs_paid_usdc": None,
        "order_type": "taker",
        "fill_price": 0.61,
        "fill_type": "taker",
    }
    row.update(overrides)
    return row


def _pending_log(**overrides: Any) -> dict[str, Any]:
    row = {
        "id": str(uuid.uuid4()),
        "market_id": f"mkt-{uuid.uuid4().hex[:8]}",
        "direction": "YES",
        "status": "pending",
        "polymarket_order_id": None,
        "created_at": _iso(datetime.now(timezone.utc) - timedelta(minutes=30)),
    }
    row.update(overrides)
    return row


class _ResolutionScript:
    """Per-market fake resolution results (fetches counted)."""

    def __init__(self) -> None:
        self.results: dict[str, MarketResolution] = {}
        self.fetches: list[str] = []

    async def __call__(self, market_id: str, direction: str) -> MarketResolution:
        self.fetches.append(market_id)
        res = self.results.get(
            market_id, MarketResolution(ResolutionStatus.NOT_RESOLVED, None, None)
        )
        return res


@pytest.fixture
def recon_db():
    """Fresh mock DB patched into the runtime module, coordinator.lease, and
    the paper_fills cancel path."""
    db_state: dict[str, list[dict[str, Any]]] = {
        "open_positions": [],
        "closed_trades": [],
        "idempotency_log": [],
        "market_leases": [],
    }
    client = MockSupabaseClient(db_state)

    async def fake_get_client():
        return client

    with patch("execution.runtime_reconciliation.get_client", fake_get_client), \
         patch("coordinator.lease.get_client", fake_get_client), \
         patch("execution.paper_fills.get_client", fake_get_client):
        yield client, db_state


@pytest.fixture(autouse=True)
def clean_recon_state():
    """Isolate module state and the lease identity between tests."""
    rr.reset_state_for_tests()
    lease.reset_instance_id_for_tests()
    yield
    rr.reset_state_for_tests()
    lease.reset_instance_id_for_tests()


@pytest.fixture(autouse=True)
def silent_alerts():
    """Alerts are AsyncMocks by default; individual tests re-patch to assert."""
    with patch("execution.runtime_reconciliation.alert_recon_warning", AsyncMock()) as warn, \
         patch("execution.runtime_reconciliation.alert_inflight_digest", AsyncMock()) as digest:
        yield {"warning": warn, "digest": digest}


NOW = datetime(2026, 11, 2, 12, 0, 0, tzinfo=timezone.utc)


# ── Resolution sweep ─────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_resolved_position_closes_without_restart_via_shared_path(recon_db, silent_alerts):
    """DoD: a resolved position closes end-to-end in a paper tick — through
    the SHARED close path (ledger row + open row gone) — and a copy-trade
    row also gets its trust update."""
    client, db = recon_db
    script = _ResolutionScript()
    script.results["mkt-copy"] = MarketResolution(ResolutionStatus.RESOLVED, 1.0, 1.0)
    row = _position_row(market_id="mkt-copy", strategy="copy_edge_class_a")
    db["open_positions"].append(row)

    with patch("execution.runtime_reconciliation.fetch_market_resolution", script), \
         patch("copytrade.performance_tracker.resolve_copy_trade", AsyncMock()) as mock_resolve:
        await rr.run_reconciliation_once(now=NOW)

    assert len(db["open_positions"]) == 0
    recorded = db["closed_trades"]
    assert len(recorded) == 1
    assert recorded[0]["outcome"] == "win"
    assert recorded[0]["exit_reason"] == "resolved"
    assert recorded[0]["pnl_usdc"] == pytest.approx(10.0 * (1.0 / 0.60) - 10.0)
    assert "runtime reconciliation sweep" in (recorded[0]["notes"] or "")
    mock_resolve.assert_awaited_once()
    assert mock_resolve.await_args.kwargs["idempotency_uuid"] == row["idempotency_uuid"]
    assert rr.get_recon_counters()["recon:resolved_closed"] == 1
    # The lease was claimed and released — no row survives for this market.
    assert db["market_leases"] == []


@pytest.mark.anyio
async def test_void_resolution_is_skipped_and_warned_not_closed(recon_db, silent_alerts):
    client, db = recon_db
    script = _ResolutionScript()
    script.results["mkt-void"] = MarketResolution(ResolutionStatus.VOID, 0.5, 0.5)
    row = _position_row(market_id="mkt-void")
    db["open_positions"].append(row)

    with patch("execution.runtime_reconciliation.fetch_market_resolution", script):
        await rr.run_reconciliation_once(now=NOW)

    assert len(db["open_positions"]) == 1  # left open — a human decides (D-B15)
    assert db["closed_trades"] == []
    assert rr.get_recon_counters()["recon:void_skipped"] == 1
    # Fire-and-forget dispatch (codebase alert pattern): assert on the
    # dispatch, not the await (sys-scheduled task may not run before assert).
    assert silent_alerts["warning"].call_count == 1
    assert "RECON_VOID_MARKET" in silent_alerts["warning"].call_args.args[0]


@pytest.mark.anyio
async def test_resting_maker_on_resolved_market_cancelled_never_closed(recon_db, silent_alerts):
    """D-B15: a never-filled order owns no shares — cancelled (row deleted +
    idempotency 'cancelled' via the paper_fills cancel flow), never closed
    into the ledger."""
    client, db = recon_db
    script = _ResolutionScript()
    script.results["mkt-rest"] = MarketResolution(ResolutionStatus.RESOLVED, 1.0, 1.0)
    row = _position_row(
        market_id="mkt-rest", order_type="maker", fill_price=None, resting_price=0.55
    )
    db["open_positions"].append(row)
    db["idempotency_log"].append({
        "id": row["idempotency_uuid"], "market_id": "mkt-rest", "direction": "YES",
        "status": "confirmed", "polymarket_order_id": None, "created_at": _iso(NOW),
    })

    with patch("execution.runtime_reconciliation.fetch_market_resolution", script):
        await rr.run_reconciliation_once(now=NOW)

    assert len(db["open_positions"]) == 0  # the resting row is deleted
    assert db["closed_trades"] == []       # never closed as a trade
    logs = {r["id"]: r for r in db["idempotency_log"]}
    assert logs[row["idempotency_uuid"]]["status"] == "cancelled"
    assert rr.get_recon_counters()["recon:resting_cancelled"] == 1


@pytest.mark.anyio
async def test_unresolved_and_unavailable_leave_positions_untouched(recon_db, silent_alerts):
    client, db = recon_db
    script = _ResolutionScript()
    script.results["mkt-live"] = MarketResolution(ResolutionStatus.NOT_RESOLVED, None, None)
    script.results["mkt-dead"] = MarketResolution(ResolutionStatus.UNAVAILABLE, None, None)
    live = _position_row(market_id="mkt-live")
    dead = _position_row(market_id="mkt-dead")
    db["open_positions"].extend([live, dead])

    with patch("execution.runtime_reconciliation.fetch_market_resolution", script):
        await rr.run_reconciliation_once(now=NOW)

    assert len(db["open_positions"]) == 2
    assert db["closed_trades"] == []
    assert rr.get_recon_counters()["recon:fetch_failed"] == 1


@pytest.mark.anyio
async def test_gamma_cap_rotates_so_newest_positions_are_not_starved(recon_db, silent_alerts):
    client, db = recon_db
    script = _ResolutionScript()
    rows = [_position_row(market_id=f"mkt-{i}") for i in range(2)]
    db["open_positions"].extend(rows)

    with patch("execution.runtime_reconciliation.fetch_market_resolution", script), \
         patch.object(config, "RECON_MAX_RESOLUTION_FETCHES", 1):
        await rr.run_reconciliation_once(now=NOW)   # fetches the OLDEST row
        await rr.run_reconciliation_once(now=NOW)   # rotation fetches the OTHER row

    assert len(script.fetches) == 2
    assert script.fetches[0] != script.fetches[1], "a full cap must not starve the newest rows"


@pytest.mark.anyio
async def test_lease_conflict_skips_the_close_and_retries_next_tick(recon_db, silent_alerts):
    client, db = recon_db
    script = _ResolutionScript()
    script.results["mkt-held"] = MarketResolution(ResolutionStatus.RESOLVED, 1.0, 1.0)
    row = _position_row(market_id="mkt-held")
    db["open_positions"].append(row)
    # Another live process holds the market lease.
    db["market_leases"].append({
        "market_id": "mkt-held", "owner": "other-process",
        "lease_until": _iso(NOW + timedelta(hours=1)),
    })

    with patch("execution.runtime_reconciliation.fetch_market_resolution", script):
        await rr.run_reconciliation_once(now=NOW)

    assert len(db["open_positions"]) == 1  # untouched — retried next tick
    assert db["closed_trades"] == []
    assert rr.get_recon_counters()["recon:lease_skipped"] == 1
    # The foreign lease is untouched (never steal another holder's row).
    assert db["market_leases"][0]["owner"] == "other-process"


@pytest.mark.anyio
async def test_one_poisoned_resolution_check_does_not_kill_the_sweep(recon_db, silent_alerts):
    client, db = recon_db
    script = _ResolutionScript()
    script.results["mkt-boom"] = MarketResolution(ResolutionStatus.RESOLVED, 1.0, 1.0)
    boom = _position_row(market_id="mkt-boom")
    good = _position_row(market_id="mkt-good")
    db["open_positions"].extend([boom, good])

    async def boom_then_ok(market_id: str, direction: str) -> MarketResolution:
        if market_id == "mkt-boom":
            raise RuntimeError("gamma exploded")
        return MarketResolution(ResolutionStatus.RESOLVED, 1.0, 1.0)

    with patch("execution.runtime_reconciliation.fetch_market_resolution", boom_then_ok):
        await rr.run_reconciliation_once(now=NOW)

    assert len(db["closed_trades"]) == 1       # the healthy row still closed
    assert db["closed_trades"][0]["market_id"] == good["market_id"]
    assert len(db["open_positions"]) == 1      # the poisoned row remains


# ── Stuck-pending sweep (D-B14) ──────────────────────────────────────────────

@pytest.mark.anyio
async def test_stuck_pending_matching_resting_maker_is_skipped(recon_db, silent_alerts):
    client, db = recon_db
    row = _position_row(order_type="maker", fill_price=None, resting_price=0.55)
    db["open_positions"].append(row)
    log = _pending_log(id=row["idempotency_uuid"])  # 30 min old — stuck
    db["idempotency_log"].append(log)

    await rr.run_reconciliation_once(now=NOW)

    assert db["idempotency_log"][0]["status"] == "pending"  # legitimate rest
    assert rr.get_recon_counters()["recon:pending_resting_skipped"] == 1


@pytest.mark.anyio
async def test_stuck_pending_matching_filled_position_is_confirmed(recon_db, silent_alerts):
    client, db = recon_db
    row = _position_row()  # filled taker
    db["open_positions"].append(row)
    log = _pending_log(id=row["idempotency_uuid"])
    db["idempotency_log"].append(log)

    await rr.run_reconciliation_once(now=NOW)

    assert db["idempotency_log"][0]["status"] == "confirmed"  # the entry happened (D-B14)
    assert rr.get_recon_counters()["recon:pending_confirmed"] == 1


@pytest.mark.anyio
async def test_stuck_pending_without_match_marked_failed_with_warning(recon_db, silent_alerts):
    client, db = recon_db
    stuck = _pending_log()                                  # 30 min old, no position
    fresh = _pending_log(created_at=_iso(NOW - timedelta(minutes=1)))  # too young
    db["idempotency_log"].extend([stuck, fresh])

    await rr.run_reconciliation_once(now=NOW)

    logs = {r["id"]: r for r in db["idempotency_log"]}
    assert logs[stuck["id"]]["status"] == "failed"
    assert "no order ID or position" in (logs[stuck["id"]].get("failure_reason") or "")
    assert logs[fresh["id"]]["status"] == "pending"         # freshness guard
    assert rr.get_recon_counters()["recon:pending_failed"] == 1
    assert silent_alerts["warning"].call_count == 1
    assert "RECON_STUCK_PENDING" in silent_alerts["warning"].call_args.args[0]


# ── Fail-softness + visibility ───────────────────────────────────────────────

@pytest.mark.anyio
async def test_open_positions_read_failure_raises_for_the_supervisor(recon_db, silent_alerts):
    """The tick's structural read failing propagates — the supervisor logs it
    and retries next interval (the loop itself is what must survive)."""
    client, db = recon_db

    def broken_table(name: str) -> Any:
        raise ConnectionError("supabase down")

    with patch.object(client, "table", broken_table):
        with pytest.raises(ConnectionError):
            await rr.run_reconciliation_once(now=NOW)


@pytest.mark.anyio
async def test_supervisor_survives_a_failed_tick(recon_db, silent_alerts):
    client, db = recon_db

    def broken_table(name: str) -> Any:
        raise ConnectionError("supabase down")

    with patch.object(client, "table", broken_table), \
         patch.object(config, "RUNTIME_RECON_INTERVAL_SECONDS", 0.01):
        task = asyncio.create_task(rr.run_runtime_reconciliation())
        await asyncio.sleep(0.05)
        assert not task.done(), "supervisor died on a failed tick"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.anyio
async def test_daily_digest_fires_on_utc_date_change_only(recon_db, silent_alerts):
    client, db = recon_db
    later = NOW + timedelta(days=1, minutes=5)

    await rr.run_reconciliation_once(now=NOW)     # seeds the day anchor silently
    silent_alerts["digest"].assert_not_awaited()
    await rr.run_reconciliation_once(now=NOW + timedelta(minutes=5))  # same UTC day
    silent_alerts["digest"].assert_not_awaited()
    await rr.run_reconciliation_once(now=later)   # new UTC day → digest
    assert silent_alerts["digest"].call_count == 1
    kwargs = silent_alerts["digest"].call_args.kwargs
    assert kwargs["open_positions"] == 0
    assert kwargs["closed_today"] == 0


@pytest.mark.anyio
async def test_supervisor_re_raises_cancellation(recon_db, silent_alerts):
    with patch.object(config, "RUNTIME_RECON_INTERVAL_SECONDS", 30):
        task = asyncio.create_task(rr.run_runtime_reconciliation())
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.anyio
async def test_poisoned_close_does_not_wedge_the_tick(recon_db, silent_alerts):
    """Self-review regression: a close that CRASHES (malformed row — NULL
    entry_price) must not abort the tick; the healthy rows keep closing and
    the poisoned row retries next tick."""
    client, db = recon_db
    poisoned = _position_row(market_id="mkt-poison", entry_price=None)  # float(None) → TypeError
    good = _position_row(market_id="mkt-good")
    db["open_positions"].extend([poisoned, good])
    script = _ResolutionScript()
    script.results["mkt-poison"] = MarketResolution(ResolutionStatus.RESOLVED, 1.0, 1.0)
    script.results["mkt-good"] = MarketResolution(ResolutionStatus.RESOLVED, 1.0, 1.0)

    with patch("execution.runtime_reconciliation.fetch_market_resolution", script):
        await rr.run_reconciliation_once(now=NOW)

    assert len(db["closed_trades"]) == 1                       # the healthy row closed
    assert db["closed_trades"][0]["market_id"] == "mkt-good"
    assert len(db["open_positions"]) == 1                      # the poisoned row retries later
    assert db["open_positions"][0]["market_id"] == "mkt-poison"
