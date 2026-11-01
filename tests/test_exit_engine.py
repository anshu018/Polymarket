"""
tests/test_exit_engine.py — List-B Step 5 (B5) exit engine tests.

Exercises execution/exit_engine.py against a mock Supabase client (patched
into BOTH the engine and coordinator.lease, so lease claim/release runs for
real against the shared tests.lease_mock semantics), the REAL shared close
path (execution/position_close.py — every close flows through it, D-B08),
and scripted books/metadata:

  pure rule evaluator boundaries (R1 floor, R2 move/hold/final-24h guard,
  R3 both branches, R4 hours/band), priority order (first hit wins),
  the end-to-end paper exit (rule -> simulated sell -> shared close ->
  ledger rows) WITHOUT a restart, the R4 once-per-position guard (D-B19),
  resting makers untouched, book-unavailable fail-soft + book-dead alert,
  lease conflict skip, per-position isolation, depth-rejected retry,
  copy-trade early exits recording the log and NEVER the wallet (D-B06),
  supervisor survival, main.py wiring, and the recon digest's pending-exits
  slot.
"""

import asyncio
import sys
import os
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import config
from coordinator import lease
from execution import exit_engine as ee
from execution import runtime_reconciliation as rr
from risk.cost_model import BookSnapshot
from tests.lease_mock import MockRpcBuilder

NOW = datetime(2026, 11, 2, 12, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


class MockTableBuilder:
    """Generic eq/order builder: select / insert / update / delete, with
    DELETE/UPDATE returning the affected rows (postgrest-py representation
    semantics, verified in the installed client)."""

    def __init__(self, db_state: dict[str, list[dict[str, Any]]], table_name: str) -> None:
        self.db_state = db_state
        self.table_name = table_name
        self.filters: list[tuple[str, Any]] = []
        self._order: tuple[str, bool] | None = None
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
    """Tables + the claim RPC, so leases run for real against db_state."""

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
        "opened_at": _iso(NOW - timedelta(hours=48)),
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


def _book(bid: float = 0.48, ask: float = 0.52, depth: float = 20000.0) -> BookSnapshot:
    return BookSnapshot(best_bid=bid, best_ask=ask, depth_usd=depth)


@pytest.fixture
def exit_env():
    """Mock DB + scripted books/metadata + mocked alerts. The books/metadata
    dicts are mutated per test; unlisted markets get no book (unavailable)."""
    db_state: dict[str, list[dict[str, Any]]] = {
        "open_positions": [], "closed_trades": [], "market_leases": [], "idempotency_log": [],
    }
    client = MockSupabaseClient(db_state)

    async def fake_get_client():
        return client

    books: dict[str, BookSnapshot | None] = {}
    metadata: dict[str, dict] = {}

    async def fake_book(market_id: str):
        return books.get(market_id)

    async def fake_metadata(market_id: str):
        return metadata.get(market_id, {})

    with patch("execution.exit_engine.get_client", fake_get_client), \
         patch("coordinator.lease.get_client", fake_get_client), \
         patch("execution.exit_engine.get_market_book", fake_book), \
         patch("execution.exit_engine.get_market_metadata", fake_metadata), \
         patch("execution.exit_engine.alert_exit_warning", AsyncMock()) as warn:
        yield SimpleNamespace(
            client=client, db=db_state, books=books, metadata=metadata, warn=warn,
        )


@pytest.fixture(autouse=True)
def clean_exit_state():
    ee.reset_state_for_tests()
    lease.reset_instance_id_for_tests()
    yield
    ee.reset_state_for_tests()
    lease.reset_instance_id_for_tests()


# ── Pure rule evaluator ───────────────────────────────────────────────────────

def _evaluate(**overrides):
    """Call evaluate_exit_rules with the standard healthy shape; overrides
    replace kwargs (entry/mid book/end/...)."""
    kwargs: dict[str, Any] = {
        "direction": "YES",
        "entry_price": 0.60,
        "opened_at": NOW - timedelta(hours=48),
        "book": _book(),
        "end_dt": None,
        "now": NOW,
    }
    kwargs.update(overrides)
    return ee.evaluate_exit_rules(**kwargs)


class TestEvaluateExitRules:
    def test_healthy_position_triggers_no_rule(self):
        # Mid 0.50 vs entry 0.60: adverse 0.10 < 0.15, deep book, no end date
        # (calendar rules skip), gain negative.
        assert _evaluate() is None

    def test_r1_fires_below_floor_without_end_date_or_hold(self):
        # No exceptions: an emergency fires on a fresh position with no
        # calendar data (D-B17: R1 alone has no days guard).
        d = _evaluate(book=_book(depth=2500.0), opened_at=NOW)
        assert d is not None and d.rule == "exit_liquidity" and not d.partial
        assert d.mark_price == pytest.approx(0.50)

    def test_r1_boundary_at_floor_does_not_fire(self):
        assert _evaluate(book=_book(depth=3000.0)) is None

    def test_r1_beats_the_profit_rules(self):
        # Thin book + a big gain: first hit wins — the emergency rule.
        d = _evaluate(book=_book(depth=2500.0, bid=0.90, ask=0.94), entry_price=0.60)
        assert d.rule == "exit_liquidity"

    def test_r2_fires_beyond_stop_move_after_min_hold(self):
        d = _evaluate(book=_book(bid=0.42, ask=0.46), end_dt=NOW + timedelta(days=5))
        # Own mid 0.44 vs entry 0.60: adverse 0.16 > 0.15, held 48h >= 24h.
        assert d is not None and d.rule == "exit_stop"
        assert "adverse" in d.reason

    def test_r2_boundary_at_exact_move_does_not_fire(self):
        # Own mid 0.45: adverse exactly 0.15 — "beyond" is strict.
        assert _evaluate(book=_book(bid=0.43, ask=0.47)) is None

    def test_r2_held_too_short_does_not_fire(self):
        d = _evaluate(
            book=_book(bid=0.42, ask=0.46),
            opened_at=NOW - timedelta(hours=23),
            end_dt=NOW + timedelta(days=5),
        )
        assert d is None

    def test_r2_held_in_the_final_24h_does_not_fire(self):
        # D-B17: never stop out into the final-24h illiquid window — a deep
        # adverse move rides to resolution instead.
        d = _evaluate(
            book=_book(bid=0.42, ask=0.46), end_dt=NOW + timedelta(hours=12),
        )
        assert d is None

    def test_r2_unknown_end_date_holds(self):
        d = _evaluate(book=_book(bid=0.42, ask=0.46), end_dt=None)
        assert d is None

    def test_r2_no_side_adverse_move(self):
        # NO entry at 0.55 (YES mid 0.45); YES mid moved UP to 0.62 → own mid
        # 0.38 → adverse 0.17 > 0.15. The own-line mirroring is load-bearing.
        d = _evaluate(
            direction="NO", entry_price=0.55,
            book=_book(bid=0.60, ask=0.64), end_dt=NOW + timedelta(days=5),
        )
        assert d is not None and d.rule == "exit_stop"

    def test_r3_branch1_gain_fires_regardless_of_days(self):
        # Banking a real gain needs no calendar (D-B17): end date unknown.
        d = _evaluate(book=_book(bid=0.85, ask=0.89), entry_price=0.60, end_dt=None)
        assert d is not None and d.rule == "exit_profit" and not d.partial
        assert "gain" in d.reason

    def test_r3_branch1_boundary_at_exact_gain_fires(self):
        # Own mid 0.85: gain exactly 0.25 — ">=" fires.
        d = _evaluate(book=_book(bid=0.83, ask=0.87), entry_price=0.60)
        assert d is not None and d.rule == "exit_profit"

    def test_r3_branch2_price_with_days_fires(self):
        # Entry 0.68 → own mid 0.92: gain 0.24 < 0.25 (branch 1 misses), but
        # price >= 0.90 with 3 days to resolution → branch 2 fires.
        d = _evaluate(
            book=_book(bid=0.90, ask=0.94), entry_price=0.68,
            end_dt=NOW + timedelta(days=3),
        )
        assert d is not None and d.rule == "exit_profit"
        assert "0.92" in d.reason

    def test_r3_branch2_refuses_the_final_24h(self):
        d = _evaluate(
            book=_book(bid=0.90, ask=0.94), entry_price=0.68,
            end_dt=NOW + timedelta(hours=12),
        )
        assert d is None

    def test_r3_branch2_unknown_end_date_holds(self):
        d = _evaluate(book=_book(bid=0.90, ask=0.94), entry_price=0.68, end_dt=None)
        assert d is None

    def test_r4_fires_near_resolution_in_band(self):
        d = _evaluate(
            entry_price=0.50, book=_book(bid=0.50, ask=0.54),
            opened_at=NOW - timedelta(hours=48),
            end_dt=NOW + timedelta(hours=48),
        )
        # Own mid 0.52: in-band (0.02 <= 0.10), 48h < 72h → PARTIAL reduction.
        assert d is not None and d.rule == "exit_time_decay" and d.partial

    def test_r4_hours_boundary(self):
        # 73h remaining: no. 71h: fires.
        in_band_book = _book(bid=0.50, ask=0.54)
        assert _evaluate(entry_price=0.50, book=in_band_book,
                         end_dt=NOW + timedelta(hours=73)) is None
        d = _evaluate(entry_price=0.50, book=in_band_book,
                      end_dt=NOW + timedelta(hours=71))
        assert d is not None and d.partial

    def test_r4_band_boundary(self):
        end = NOW + timedelta(hours=48)
        # |delta| exactly 0.10 → in band ("<="); 0.12 → thesis confirmed, no.
        assert _evaluate(entry_price=0.50,
                         book=_book(bid=0.58, ask=0.62), end_dt=end) is not None
        assert _evaluate(entry_price=0.50,
                         book=_book(bid=0.60, ask=0.64), end_dt=end) is None

    def test_r4_unknown_end_date_does_not_fire(self):
        assert _evaluate(entry_price=0.50, book=_book(bid=0.50, ask=0.54)) is None

    def test_unknown_direction_fails_closed(self):
        assert _evaluate(direction="LONG") is None


# ── The monitor loop ──────────────────────────────────────────────────────────

class TestRunExitEngineOnce:
    @pytest.mark.anyio
    async def test_liquidity_exit_closes_end_to_end_via_shared_path(self, exit_env):
        """The DoD headline: rule → simulated sell → SHARED close → ledger
        rows, without a restart."""
        row = _position_row()
        exit_env.db["open_positions"].append(row)
        exit_env.books[row["market_id"]] = _book(depth=2500.0)  # R1, bid 0.48

        await ee.run_exit_engine_once(now=NOW)

        assert exit_env.db["open_positions"] == []
        ledger = exit_env.db["closed_trades"]
        assert len(ledger) == 1
        assert ledger[0]["exit_type"] == "exit_liquidity"
        assert ledger[0]["exit_price"] == 0.48  # the bid — a real sell price
        assert ledger[0]["pnl_usdc"] == pytest.approx((10.0 / 0.60) * 0.48 - 10.0)
        assert ledger[0]["outcome"] == "loss"
        # The lease was released (nothing left held).
        assert exit_env.db["market_leases"] == []
        assert ee.get_exit_counters()["exit:closed"] == 1
        assert ee.get_pending_exit_count() == 0

    @pytest.mark.anyio
    async def test_resting_maker_is_never_touched(self, exit_env):
        row = _position_row(order_type="maker", fill_price=None)
        exit_env.db["open_positions"].append(row)
        exit_env.books[row["market_id"]] = _book(depth=2500.0)
        await ee.run_exit_engine_once(now=NOW)
        assert len(exit_env.db["open_positions"]) == 1
        assert exit_env.db["closed_trades"] == []
        assert ee.get_exit_counters()["exit:resting_skipped"] == 1

    @pytest.mark.anyio
    async def test_book_unavailable_is_fail_soft_never_a_blind_exit(self, exit_env):
        row = _position_row()
        exit_env.db["open_positions"].append(row)
        # No book scripted → get_market_book returns None.
        await ee.run_exit_engine_once(now=NOW)
        assert len(exit_env.db["open_positions"]) == 1
        assert exit_env.db["closed_trades"] == []
        assert ee.get_exit_counters()["exit:book_unavailable"] == 1
        exit_env.warn.assert_not_awaited()

    @pytest.mark.anyio
    async def test_book_dead_beyond_6h_alerts_then_cooldown(self, exit_env):
        row = _position_row()
        exit_env.db["open_positions"].append(row)
        await ee.run_exit_engine_once(now=NOW)  # book missing — seeds the clock
        exit_env.warn.assert_not_awaited()
        later = NOW + timedelta(hours=7)
        await ee.run_exit_engine_once(now=later)  # 7h dead >= 6h → WARNING
        await asyncio.sleep(0)  # let the fire-and-forget alert task run
        exit_env.warn.assert_awaited_once()
        assert exit_env.warn.await_args.args[0] == "EXIT_BOOK_DEAD"
        # Cooldown: an immediately-following tick does not re-alert.
        await ee.run_exit_engine_once(now=later + timedelta(minutes=1))
        exit_env.warn.assert_awaited_once()
        # And still no blind exit at any point.
        assert len(exit_env.db["open_positions"]) == 1

    @pytest.mark.anyio
    async def test_time_decay_partial_reduces_exactly_once(self, exit_env):
        row = _position_row(entry_price=0.50)
        exit_env.db["open_positions"].append(row)
        # Own mid 0.49 (in band 0.01 <= 0.10); the SELL realizes the bid 0.46.
        exit_env.books[row["market_id"]] = _book(bid=0.46, ask=0.52)
        exit_env.metadata[row["market_id"]] = {
            "end_date_iso": _iso(NOW + timedelta(hours=48))
        }

        await ee.run_exit_engine_once(now=NOW)

        assert len(exit_env.db["open_positions"]) == 1
        assert exit_env.db["open_positions"][0]["position_size_usdc"] == pytest.approx(5.0)
        ledger = exit_env.db["closed_trades"]
        assert len(ledger) == 1
        assert ledger[0]["exit_type"] == "exit_time_decay"
        assert ledger[0]["position_size_usdc"] == pytest.approx(5.0)
        assert ledger[0]["exit_price"] == 0.46  # the bid, not the mid
        assert ledger[0]["pnl_usdc"] == pytest.approx((10.0 / 0.50) * 0.5 * (0.46 - 0.50))
        assert ledger[0]["outcome"] == "loss"
        assert ee.get_exit_counters()["exit:partial_reduced"] == 1

        # Second tick: the ledger IS the once-guard (D-B19) — no second sell.
        await ee.run_exit_engine_once(now=NOW)
        assert exit_env.db["open_positions"][0]["position_size_usdc"] == pytest.approx(5.0)
        assert len(exit_env.db["closed_trades"]) == 1
        assert ee.get_exit_counters()["exit:partial_already_reduced"] == 1

    @pytest.mark.anyio
    async def test_lease_conflict_skips_and_pends(self, exit_env):
        row = _position_row()
        exit_env.db["open_positions"].append(row)
        exit_env.books[row["market_id"]] = _book(depth=2500.0)
        # Another live owner holds the market lease.
        exit_env.db["market_leases"].append({
            "market_id": row["market_id"], "owner": "other-instance",
            "lease_until": _iso(NOW + timedelta(seconds=30)),
        })

        await ee.run_exit_engine_once(now=NOW)

        assert len(exit_env.db["open_positions"]) == 1
        assert exit_env.db["closed_trades"] == []
        assert ee.get_exit_counters()["exit:lease_skipped"] == 1
        assert ee.get_pending_exit_count() == 1  # retries next tick

    @pytest.mark.anyio
    async def test_poisoned_position_isolated_while_other_exits(self, exit_env):
        poisoned = _position_row(entry_price=None)  # float(None) explodes
        healthy = _position_row()
        exit_env.db["open_positions"] = [poisoned, healthy]
        exit_env.books[poisoned["market_id"]] = _book()   # reach the poison
        exit_env.books[healthy["market_id"]] = _book(depth=2500.0)

        await ee.run_exit_engine_once(now=NOW)

        assert ee.get_exit_counters()["exit:failed"] == 1
        assert ee.get_exit_counters()["exit:closed"] == 1
        assert [r["id"] for r in exit_env.db["open_positions"]] == [poisoned["id"]]

    @pytest.mark.anyio
    async def test_depth_rejected_exit_retries_next_tick(self, exit_env):
        # R1 fires (depth 2500 < 3000) but the position's sell notional
        # (10000 USDC ≈ 0.48 × 16666 shares) dwarfs the book — the exit is
        # NOT forced; it retries next tick.
        row = _position_row(position_size_usdc=10000.0)
        exit_env.db["open_positions"].append(row)
        exit_env.books[row["market_id"]] = _book(depth=2500.0)

        await ee.run_exit_engine_once(now=NOW)

        assert len(exit_env.db["open_positions"]) == 1
        assert exit_env.db["closed_trades"] == []
        assert ee.get_exit_counters()["exit:depth_rejected"] == 1
        assert ee.get_pending_exit_count() == 1

    @pytest.mark.anyio
    async def test_copy_trade_early_exit_records_log_never_the_wallet(self, exit_env):
        """D-B06 through the whole engine: the copy position exits through
        the shared path and the wallet machinery is structurally unreachable."""
        row = _position_row(strategy="copy_edge_class_a")
        exit_env.db["open_positions"].append(row)
        exit_env.books[row["market_id"]] = _book(depth=2500.0)
        with patch("copytrade.performance_tracker.resolve_copy_trade", AsyncMock()) as mock_resolve, \
             patch("copytrade.performance_tracker.record_early_exit", AsyncMock()) as mock_early:
            await ee.run_exit_engine_once(now=NOW)
        mock_resolve.assert_not_called()
        mock_early.assert_awaited_once()
        assert mock_early.await_args.kwargs["exit_type"] == "exit_liquidity"
        assert exit_env.db["closed_trades"][0]["strategy"] == "copy_edge_class_a"

    @pytest.mark.anyio
    async def test_no_halt_reference_on_any_exit_path(self):
        """D-B04 guard: the exit engine must never import the health/drawdown
        enforcement machinery — a halt gates ENTRIES only, never exits."""
        import ast
        from pathlib import Path
        source = Path(ee.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        for banned in ("monitoring.health_score", "coordinator.equity", "risk.risk_engine"):
            assert banned not in imported, f"exit engine imports {banned} — halts must never gate exits"


class TestSupervisorAndWiring:
    @pytest.mark.anyio
    async def test_supervisor_survives_a_crashed_tick(self):
        async def boom():
            raise ConnectionError("mocked supabase outage")

        with patch("execution.exit_engine.get_client", boom), \
             patch.object(config, "EXIT_MONITOR_INTERVAL_SECONDS", 0.01):
            task = asyncio.create_task(ee.run_exit_engine())
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task  # alive through the crash, cancellable when asked

    def test_main_registers_exit_engine_task(self):
        """Wiring contract: run_exit_engine joins the supervised task set."""
        import ast
        from pathlib import Path
        main_py = Path(__file__).resolve().parent.parent / "main.py"
        tree = ast.parse(main_py.read_text(encoding="utf-8"))
        main_fn = next(
            n for n in tree.body
            if isinstance(n, ast.AsyncFunctionDef) and n.name == "main"
        )
        called = {
            node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id
            for node in ast.walk(main_fn) if isinstance(node, ast.Call)
        }
        assert "run_exit_engine" in called


class TestReconDigestPendingExits:
    @pytest.mark.anyio
    async def test_recon_digest_reports_pending_exits(self):
        """Step 4's "(after Step 5)" slot: the recon's INFLIGHT log and daily
        digest carry the exit engine's pending-exit count (fail-soft)."""
        db_state: dict[str, list[dict[str, Any]]] = {
            "open_positions": [], "closed_trades": [], "idempotency_log": [], "market_leases": [],
        }
        client = MockSupabaseClient(db_state)

        async def fake_get_client():
            return client

        day1 = datetime(2026, 11, 2, 12, 0, 0, tzinfo=timezone.utc)
        day2 = day1 + timedelta(days=1)
        with patch("execution.runtime_reconciliation.get_client", fake_get_client), \
             patch("coordinator.lease.get_client", fake_get_client), \
             patch("execution.paper_fills.get_client", fake_get_client), \
             patch("execution.runtime_reconciliation.alert_recon_warning", AsyncMock()), \
             patch("execution.runtime_reconciliation.alert_inflight_digest", AsyncMock()) as digest, \
             patch("execution.exit_engine.get_pending_exit_count", return_value=3):
            rr.reset_state_for_tests()
            await rr.run_reconciliation_once(now=day1)   # seeds the day silently
            await rr.run_reconciliation_once(now=day2)   # day boundary → digest
            await asyncio.sleep(0)  # let the fire-and-forget digest task run

        assert digest.await_count == 1
        assert digest.await_args.kwargs["pending_exits"] == 3


# ── R6: re-entry cooldown (enforced on BOTH entry paths) ─────────────────────

class TestReentryCooldown:
    @pytest.mark.anyio
    async def test_no_prior_exit_allows(self, exit_env):
        v = await ee.check_reentry_cooldown("mkt-x", "YES", now=NOW)
        assert v.blocked is False

    @pytest.mark.anyio
    async def test_recent_exit_blocks(self, exit_env):
        exit_env.db["closed_trades"].append({
            "market_id": "mkt-x", "direction": "YES",
            "closed_at": _iso(NOW - timedelta(hours=2)), "exit_type": "exit_stop",
        })
        v = await ee.check_reentry_cooldown("mkt-x", "YES", now=NOW)
        assert v.blocked is True
        assert "exit_stop" in v.reason

    @pytest.mark.anyio
    async def test_direction_mismatch_does_not_block(self, exit_env):
        """The cooldown is per (market_id, direction) — a NO exit never
        blocks a YES entry."""
        exit_env.db["closed_trades"].append({
            "market_id": "mkt-x", "direction": "NO",
            "closed_at": _iso(NOW - timedelta(hours=2)), "exit_type": "exit_stop",
        })
        v = await ee.check_reentry_cooldown("mkt-x", "YES", now=NOW)
        assert v.blocked is False

    @pytest.mark.anyio
    async def test_boundary_exactly_24h_allows(self, exit_env):
        exit_env.db["closed_trades"].append({
            "market_id": "mkt-x", "direction": "YES",
            "closed_at": _iso(NOW - timedelta(hours=config.REENTRY_COOLDOWN_HOURS)),
            "exit_type": "exit_profit",
        })
        v = await ee.check_reentry_cooldown("mkt-x", "YES", now=NOW)
        assert v.blocked is False  # age < 24h blocks; equality has served its time

    @pytest.mark.anyio
    async def test_most_recent_row_governs(self, exit_env):
        db = exit_env.db["closed_trades"]
        db.append({"market_id": "mkt-x", "direction": "YES",
                   "closed_at": _iso(NOW - timedelta(hours=30)), "exit_type": "resolved"})
        db.append({"market_id": "mkt-x", "direction": "YES",
                   "closed_at": _iso(NOW - timedelta(hours=2)), "exit_type": "exit_swap"})
        v = await ee.check_reentry_cooldown("mkt-x", "YES", now=NOW)
        assert v.blocked is True  # the LAST exit is 2h old

    @pytest.mark.anyio
    async def test_read_failure_fails_closed(self):
        class ExplodingClient:
            def table(self, name: str) -> Any:
                raise ConnectionError("mocked supabase outage")

        async def fake_get_client():
            return ExplodingClient()

        with patch("execution.exit_engine.get_client", fake_get_client):
            v = await ee.check_reentry_cooldown("mkt-x", "YES", now=NOW)
        assert v.blocked is True  # an unreadable guard is treated as armed

    @pytest.mark.anyio
    async def test_unparseable_closed_at_fails_closed(self, exit_env):
        exit_env.db["closed_trades"].append({
            "market_id": "mkt-x", "direction": "YES",
            "closed_at": "not-a-timestamp", "exit_type": "exit_stop",
        })
        v = await ee.check_reentry_cooldown("mkt-x", "YES", now=NOW)
        assert v.blocked is True

    def test_both_entry_paths_enforce_the_cooldown(self):
        """Wiring contract: check_reentry_cooldown is called in BOTH critical
        sections (pipeline + Class A executor)."""
        import ast
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent
        for rel in ("coordinator/pipeline.py", "copytrade/executor.py"):
            tree = ast.parse((root / rel).read_text(encoding="utf-8"))
            called = {
                node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id
                for node in ast.walk(tree) if isinstance(node, ast.Call)
            }
            assert "check_reentry_cooldown" in called, f"{rel} no longer enforces R6"



# ── R5: exit-weakest-for-stronger (pipeline-only swap) ────────────────────────

def _swap_victim(**overrides: Any) -> dict[str, Any]:
    """A same-category position the engine would independently exit (R1: a
    thin book), with a recorded edge the proxy math can move."""
    overrides.setdefault("category", "politics")
    overrides.setdefault("net_edge_at_entry", 0.02)
    return _position_row(**overrides)


def _swap_call(**overrides: Any):
    kwargs: dict[str, Any] = {
        "category": "politics",
        "portfolio_value": 10000.0,
        "proposed_pct": 0.04,
        "category_exposure_pct": 0.10,
        "incoming_net_edge": 0.05,
        "incoming_market_id": "mkt-incoming",
        "incoming_direction": "YES",
        "now": NOW,
    }
    kwargs.update(overrides)
    return ee.evaluate_swap_for_entry(**kwargs)


class TestR5Swap:
    @pytest.mark.anyio
    async def test_swap_exits_the_victim_through_the_shared_path(self, exit_env):
        victim = _swap_victim()
        exit_env.db["open_positions"].append(victim)
        exit_env.books[victim["market_id"]] = _book(depth=2500.0)  # R1 fires

        out = await _swap_call()

        assert out.performed is True
        assert out.victim_market_id == victim["market_id"]
        ledger = exit_env.db["closed_trades"]
        assert len(ledger) == 1
        assert ledger[0]["exit_type"] == "exit_swap"
        assert "R5 swap" in ledger[0]["exit_reason"]
        assert exit_env.db["open_positions"] == []
        assert exit_env.db["market_leases"] == []  # lease released

    @pytest.mark.anyio
    async def test_no_incoming_edge_never_swaps(self, exit_env):
        """Class A has no measured net edge — R5 never fires there (D-B09)."""
        victim = _swap_victim()
        exit_env.db["open_positions"].append(victim)
        exit_env.books[victim["market_id"]] = _book(depth=2500.0)
        out = await _swap_call(incoming_net_edge=None)
        assert out.performed is False
        assert len(exit_env.db["open_positions"]) == 1

    @pytest.mark.anyio
    async def test_no_victims_no_swap(self, exit_env):
        out = await _swap_call()
        assert out.performed is False

    @pytest.mark.anyio
    async def test_victim_must_be_independently_exit_worthy(self, exit_env):
        victim = _swap_victim()
        exit_env.db["open_positions"].append(victim)
        exit_env.books[victim["market_id"]] = _book()  # healthy book — no rule
        out = await _swap_call()
        assert out.performed is False
        assert "not independently exit-worthy" in out.reason
        assert len(exit_env.db["open_positions"]) == 1

    @pytest.mark.anyio
    async def test_r4_partial_victim_frees_no_seat(self, exit_env):
        victim = _swap_victim(entry_price=0.50)
        exit_env.db["open_positions"].append(victim)
        exit_env.books[victim["market_id"]] = _book(bid=0.50, ask=0.54)
        exit_env.metadata[victim["market_id"]] = {
            "end_date_iso": _iso(NOW + timedelta(hours=48))
        }
        out = await _swap_call()
        assert out.performed is False
        assert len(exit_env.db["open_positions"]) == 1  # not even reduced

    @pytest.mark.anyio
    async def test_margin_gates_the_swap(self, exit_env):
        victim = _swap_victim()
        exit_env.db["open_positions"].append(victim)
        exit_env.books[victim["market_id"]] = _book(depth=2500.0)
        # proxy = 0.02 + (0.50 − 0.60) = −0.08; incoming 0.05 → margin 0.13.
        out = await _swap_call(incoming_net_edge=0.05)
        assert out.performed is True

    @pytest.mark.anyio
    async def test_margin_not_met_no_swap(self, exit_env):
        victim = _swap_victim(net_edge_at_entry=0.20)
        exit_env.db["open_positions"].append(victim)
        exit_env.books[victim["market_id"]] = _book(depth=2500.0)
        # proxy = 0.20 + (0.50 − 0.60) = 0.10 > incoming 0.05 → no swap.
        out = await _swap_call(incoming_net_edge=0.05)
        assert out.performed is False
        assert "margin not met" in out.reason

    @pytest.mark.anyio
    async def test_margin_boundary_exactly_at_threshold_no_swap(self, exit_env):
        # proxy = 0.02 + (0.61 − 0.60) = 0.03; incoming 0.05 → margin exactly
        # 0.02 — "more than SWAP_MARGIN_CENTS" is strict → no swap.
        victim = _swap_victim()
        exit_env.db["open_positions"].append(victim)
        exit_env.books[victim["market_id"]] = _book(bid=0.59, ask=0.63, depth=2500.0)
        out = await _swap_call(incoming_net_edge=0.05)
        assert out.performed is False

    @pytest.mark.anyio
    async def test_weakest_victim_is_selected_and_only_one_swap(self, exit_env):
        weak = _swap_victim(net_edge_at_entry=0.02)
        strong = _swap_victim(net_edge_at_entry=0.05)
        exit_env.db["open_positions"] = [weak, strong]
        exit_env.books[weak["market_id"]] = _book(depth=2500.0)  # weak: R1 fires
        exit_env.books[strong["market_id"]] = _book()            # strong: healthy

        out = await _swap_call()

        assert out.performed is True
        assert out.victim_market_id == weak["market_id"]
        assert [r["market_id"] for r in exit_env.db["open_positions"]] == [strong["market_id"]]
        assert len(exit_env.db["closed_trades"]) == 1  # one swap, never multi

    @pytest.mark.anyio
    async def test_null_edge_sorts_weakest(self, exit_env):
        """Class A rows record net_edge_at_entry=NULL — the weakest rank."""
        class_a = _swap_victim(strategy="copy_edge_class_a", net_edge_at_entry=None)
        pipeline_pos = _swap_victim(net_edge_at_entry=0.05)
        exit_env.db["open_positions"] = [class_a, pipeline_pos]
        exit_env.books[class_a["market_id"]] = _book(depth=2500.0)
        exit_env.books[pipeline_pos["market_id"]] = _book()

        out = await _swap_call()

        assert out.performed is True
        assert out.victim_market_id == class_a["market_id"]
        # The proxy for a NULL-edge victim is the pure price move (0.0 base).
        assert "proxy" in exit_env.db["closed_trades"][0]["exit_reason"]

    @pytest.mark.anyio
    async def test_swap_must_free_room_under_the_cap(self, exit_env):
        victim = _swap_victim(position_size_usdc=10.0)  # victim_pct 0.001
        exit_env.db["open_positions"].append(victim)
        exit_env.books[victim["market_id"]] = _book(depth=2500.0)
        # 0.29 − 0.001 + 0.04 = 0.329 > 0.30 → the swap buys nothing.
        out = await _swap_call(category_exposure_pct=0.29)
        assert out.performed is False
        assert "not free room" in out.reason

        # A larger victim frees real room: 0.29 − 0.04 + 0.04 = 0.29 <= 0.30.
        big = _swap_victim(position_size_usdc=400.0)
        exit_env.db["open_positions"] = [big]
        exit_env.books[big["market_id"]] = _book(depth=2500.0)
        out = await _swap_call(category_exposure_pct=0.29)
        assert out.performed is True

    @pytest.mark.anyio
    async def test_category_read_failure_fails_closed(self):
        class ExplodingClient:
            def table(self, name: str) -> Any:
                raise ConnectionError("mocked supabase outage")

        async def fake_get_client():
            return ExplodingClient()

        with patch("execution.exit_engine.get_client", fake_get_client):
            out = await _swap_call()
        assert out.performed is False  # the signal stays dropped, as before

    def test_r5_is_pipeline_only(self):
        """Wiring contract: evaluate_swap_for_entry is called in the pipeline
        and NOWHERE on the Class A executor path (estimate-free — no margin
        comparison is possible honestly)."""
        import ast
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent
        pipeline_tree = ast.parse((root / "coordinator/pipeline.py").read_text(encoding="utf-8"))
        pipeline_calls = {
            node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id
            for node in ast.walk(pipeline_tree) if isinstance(node, ast.Call)
        }
        assert "evaluate_swap_for_entry" in pipeline_calls
        executor_tree = ast.parse((root / "copytrade/executor.py").read_text(encoding="utf-8"))
        executor_calls = {
            node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id
            for node in ast.walk(executor_tree) if isinstance(node, ast.Call)
        }
        assert "evaluate_swap_for_entry" not in executor_calls


class TestR5SameKeyGuard:
    @pytest.mark.anyio
    async def test_victim_on_the_incoming_key_is_never_selected(self, exit_env):
        """Self-review catch: a victim sharing the incoming signal's
        (market_id, direction) must never be swapped — the swap's own ledger
        row would trip the R6 cooldown and block the entry it was meant to
        admit. Same-key adds are the tranche/market gates' business."""
        same_key = _swap_victim(market_id="mkt-incoming", direction="YES")
        other = _swap_victim(market_id="mkt-other")
        exit_env.db["open_positions"] = [same_key, other]
        exit_env.books["mkt-incoming"] = _book(depth=2500.0)  # R1-worthy
        exit_env.books["mkt-other"] = _book(depth=2500.0)

        out = await _swap_call()

        assert out.performed is True
        assert out.victim_market_id == "mkt-other"  # the same-key row survives
        assert [r["market_id"] for r in exit_env.db["open_positions"]] == ["mkt-incoming"]

    @pytest.mark.anyio
    async def test_only_same_key_candidates_means_no_swap(self, exit_env):
        same_key = _swap_victim(market_id="mkt-incoming", direction="YES")
        exit_env.db["open_positions"] = [same_key]
        exit_env.books["mkt-incoming"] = _book(depth=2500.0)
        out = await _swap_call()
        assert out.performed is False
        assert len(exit_env.db["open_positions"]) == 1
