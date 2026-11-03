"""
tests/test_exposure.py — List-B Step 6 (B6) shared exposure module tests.

coordinator/exposure.py replaced two fetchers that agreed on nothing (E-B6):
the pipeline summed every open position and called the total "correlated"
exposure, and Class A divided by the frozen PAPER_TRADING_PORTFOLIO_USDC
constant. These tests lock the new semantics:

  the cluster numerator is the Step 1 snapshot's MARKED per-key sum (one
  lookup, never a per-row re-sum), falling back to committed cost when the
  snapshot cannot answer; NULL keys are singletons by market_id; category
  and open-value sums keep their cost-basis semantics; the read fails
  closed; and ONE Supabase read per call — the entry-path call-order
  ordinals depend on it.
"""

import asyncio
import sys
import os
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import config
from coordinator import equity, exposure
from coordinator.exposure import ExposureSnapshot, fetch_portfolio_exposure

NOW = datetime(2026, 11, 4, 12, 0, 0, tzinfo=timezone.utc)


class MockTableBuilder:
    """Select-only builder — exposure never writes."""

    def __init__(self, db_state: dict[str, list[dict[str, Any]]], table_name: str) -> None:
        self.db_state = db_state
        self.table_name = table_name
        self.selects: list[str] = []

    def select(self, cols: str) -> "MockTableBuilder":
        self.selects.append(cols)
        return self

    def order(self, col: str, desc: bool = False) -> "MockTableBuilder":
        return self

    def limit(self, val: int) -> "MockTableBuilder":
        return self

    def execute(self) -> Any:
        class Result:
            def __init__(self, data: list[dict[str, Any]]) -> None:
                self.data = data

        return Result([dict(r) for r in self.db_state.get(self.table_name, [])])


class MockSupabaseClient:
    def __init__(self, db_state: dict[str, list[dict[str, Any]]]) -> None:
        self.db_state = db_state
        self.get_client_calls = 0

    def table(self, name: str) -> MockTableBuilder:
        return MockTableBuilder(self.db_state, name)


@pytest.fixture
def exposure_env():
    """Mock DB + a warm equity snapshot the gate can read marks from."""
    db_state: dict[str, list[dict[str, Any]]] = {"open_positions": []}
    client = MockSupabaseClient(db_state)

    async def fake_get_client():
        client.get_client_calls += 1
        return client

    equity._snapshot_cache = equity.EquityGate(
        as_of=NOW,
        current_equity=10000.0,
        starting_balances={"daily": 10000.0, "weekly": 10000.0, "monthly": 10000.0},
        current_balances={"daily": 10000.0, "weekly": 10000.0, "monthly": 10000.0},
        marked_by_key={"fed|ecb": 2400.0, "solo-market": 500.0},
    )
    # equity's binding is patched with the SAME fake: the cold-cache path
    # one-shot marks through coordinator.equity, and a real call there would
    # hit the live Supabase (the Step 5 lesson, twice over).
    with patch.object(exposure, "get_client", fake_get_client),          patch.object(equity, "get_client", fake_get_client):
        yield client, db_state
    equity._snapshot_cache = None


def _row(market_id: str, size: float, category: str = "politics", event_key: Any = None) -> dict[str, Any]:
    return {
        "market_id": market_id,
        "position_size_usdc": size,
        "category": category,
        "event_key": event_key,
    }


class TestClusterNumerator:
    @pytest.mark.anyio
    async def test_snapshot_mark_is_authoritative_for_the_whole_cluster(self, exposure_env):
        """The snapshot's per-key sum is ONE lookup — a $2400 marked cluster
        is fed whole, never re-summed per row (which would double-count)."""
        client, db = exposure_env
        # The snapshot says the fed|ecb cluster is worth $2400 marked; the
        # rows' committed cost is deliberately different ($1800 total).
        db["open_positions"].extend([
            _row("mkt-a", 900.0, event_key="fed|ecb"),
            _row("mkt-b", 900.0, event_key="fed|ecb"),
        ])
        snap = await fetch_portfolio_exposure("politics", "fed|ecb")
        assert snap.cluster_pct == pytest.approx(0.24)
        assert snap.category_pct == pytest.approx(0.18)      # cost basis, unchanged semantics
        assert snap.open_value_usdc == pytest.approx(1800.0)

    @pytest.mark.anyio
    async def test_cost_fallback_when_the_snapshot_cannot_answer(self, exposure_env):
        """No snapshot (cold cache) or no mark for the key (rows fresher than
        the last tick) → the cluster costs what its rows committed."""
        client, db = exposure_env
        db["open_positions"].extend([
            _row("mkt-a", 900.0, event_key="fresh|event"),
            _row("mkt-b", 900.0, event_key="fresh|event"),
        ])
        snap = await fetch_portfolio_exposure("politics", "fresh|event")
        assert snap.cluster_pct == pytest.approx(0.18)

        equity._snapshot_cache = None  # cold cache: same fallback...
        snap = await fetch_portfolio_exposure("politics", "fresh|event")
        # ...and the fresh one-shot mark values these unpriceable rows at
        # cost, which nets back out of paper cash — the denominator is still
        # 10000 and the fraction unchanged. The fallback answer is identical
        # with and without a snapshot, which is the point.
        assert snap.cluster_pct == pytest.approx(0.18)

    @pytest.mark.anyio
    async def test_null_event_key_rows_cluster_by_market_id(self, exposure_env):
        """A row without an event_key is its own singleton keyed by market_id
        (equity.cluster_key is the shared keying rule)."""
        client, db = exposure_env
        db["open_positions"].append(_row("mkt-solo", 500.0, event_key=None))
        snap = await fetch_portfolio_exposure("politics", "mkt-solo")
        # The snapshot carries a mark for the solo-market key.
        assert snap.cluster_pct == pytest.approx(0.05)

    @pytest.mark.anyio
    async def test_unknown_or_missing_proposed_key_yields_zero(self, exposure_env):
        """A proposed key nothing shares (or no key at all) joins no cluster —
        the gate then sees only the proposed trade itself."""
        client, db = exposure_env
        db["open_positions"].append(_row("mkt-a", 900.0, event_key="fed|ecb"))
        snap = await fetch_portfolio_exposure("politics", "unrelated|key")
        assert snap.cluster_pct == pytest.approx(0.0)
        snap = await fetch_portfolio_exposure("politics", None)
        assert snap.cluster_pct == pytest.approx(0.0)

    @pytest.mark.anyio
    async def test_exactly_one_supabase_read_per_call(self, exposure_env):
        """The entry-path call-order ordinals treat this as the exposure read
        (Call 7) — the read count must not grow."""
        client, db = exposure_env
        db["open_positions"].append(_row("mkt-a", 100.0))
        await fetch_portfolio_exposure("politics", "fed|ecb")
        assert client.get_client_calls == 1


class TestFailClosed:
    @pytest.mark.anyio
    async def test_read_timeout_halts(self, exposure_env, caplog):
        client, db = exposure_env

        class _SlowBuilder:
            def select(self, cols: str) -> "_SlowBuilder":
                return self

            def execute(self) -> Any:
                # Blocks the worker thread like a hung HTTP call — the
                # timeout must fire from the awaiting side (to_thread).
                import time
                time.sleep(5.0)

                class _Result:
                    data: list[dict[str, Any]] = []

                return _Result()

        def slow_table(name: str) -> Any:
            return _SlowBuilder()

        with patch.object(config, "SUPABASE_TIMEOUT_SECONDS", 0.05), \
             patch.object(client, "table", slow_table), \
             patch.object(exposure, "alert_supabase_degradation", AsyncMock()) as alert:
            with pytest.raises(RuntimeError, match="open_positions read timeout"):
                await fetch_portfolio_exposure("politics", "k")
            await asyncio.sleep(0)  # let the fire-and-forget alert task run
            alert.assert_awaited_once()

    @pytest.mark.anyio
    async def test_read_failure_halts(self, exposure_env):
        client, db = exposure_env

        def broken_table(name: str) -> Any:
            raise ConnectionError("supabase down")

        with patch.object(client, "table", broken_table):
            with pytest.raises(RuntimeError, match="exposure check failure"):
                await fetch_portfolio_exposure("politics", "k")


def test_no_frozen_portfolio_constant_divides_anywhere():
    """DoD grep gate: the executor's frozen-constant denominator (E-B6) is
    gone — equity.py:248's paper-cash bootstrap is the one sanctioned use of
    PAPER_TRADING_PORTFOLIO_USDC, and NOTHING divides by it."""
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    hits: list[str] = []
    for path in root.rglob("*.py"):
        spath = str(path)
        if ".venv" in spath or "tests" in spath or spath.endswith("config.py"):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for i, line in enumerate(text.splitlines(), 1):
            if "PAPER_TRADING_PORTFOLIO_USDC" in line and "/" in line:
                hits.append(f"{path.name}:{i}: {line.strip()}")
    # The paper-cash formula (constant + realized − committed) contains no
    # division; any hit here means the frozen constant leaked back into a
    # denominator.
    assert hits == []
