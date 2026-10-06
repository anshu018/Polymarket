"""
tests/test_forward_sampler.py — Step 2 sampler tests (List A.md Step 2 — A6).

Covers: outcome-row insert on consume, price reads filling p_m1/p_m5/p_m15/p_m60,
timeout/exception misses recorded as NULL, the never-crash guarantee (a failing
insert or price fetch must not kill the supervisor), queue-full job dropping,
and the direction update with signal_id backfill.
"""

import asyncio
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import patch

import pytest
import config

import data.forward_sampler as forward_sampler
from data.forward_sampler import (
    SamplerJob,
    apply_signal_direction,
    get_sampler_queue,
    get_sampler_stats,
    run_forward_sampler_supervisor,
    schedule_forward_sampling,
)


# ── Mock Supabase plumbing (self-contained, per repo convention) ─────────────

class MockTableBuilder:
    def __init__(self, db_state: dict[str, list[dict[str, Any]]], table_name: str,
                 fail_tables: set[str]) -> None:
        self.db_state = db_state
        self.table_name = table_name
        self.fail_tables = fail_tables
        self.filters: list[tuple[str, Any]] = []

    def _fail(self) -> bool:
        return self.table_name in self.fail_tables

    def select(self, cols: str) -> "MockTableBuilder":
        return self

    def eq(self, col: str, val: Any) -> "MockTableBuilder":
        self.filters.append((col, val))
        return self

    def order(self, col: str, desc: bool = False) -> "MockTableBuilder":
        return self

    def limit(self, val: int) -> "MockTableBuilder":
        return self

    def execute(self) -> Any:
        class Result:
            def __init__(self, data: list[dict[str, Any]]) -> None:
                self.data = data

        if self._fail():
            raise ConnectionError(f"mock outage on {self.table_name}")
        rows = self.db_state.get(self.table_name, [])
        return Result([r.copy() for r in rows
                       if all(r.get(col) == val for col, val in self.filters)])

    def insert(self, data: Any) -> "MockTableBuilder":
        if self._fail():
            return self
        rows = data if isinstance(data, list) else [data]
        for item in rows:
            self.db_state.setdefault(self.table_name, []).append(item.copy())
        return self

    def update(self, data: dict[str, Any]) -> "MockTableBuilder":
        if self._fail():
            return self
        for row in self.db_state.get(self.table_name, []):
            if all(row.get(col) == val for col, val in self.filters):
                row.update(data)
        return self

    def delete(self) -> "MockTableBuilder":
        return self


class MockSupabaseClient:
    def __init__(self, db_state: dict[str, list[dict[str, Any]]],
                 fail_tables: set[str] | None = None) -> None:
        self.db_state = db_state
        self.fail_tables = fail_tables or set()

    def table(self, name: str) -> MockTableBuilder:
        return MockTableBuilder(self.db_state, name, self.fail_tables)


# ── Fixtures + helpers ────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Tests must never touch the network: stub the CLOB price read globally.

    Individual tests that care about the price override this via their own
    patch of data.market_discovery.get_market_price."""
    async def stub_price(token_id: str) -> float:
        return 0.50

    monkeypatch.setattr("data.market_discovery.get_market_price", stub_price)


@pytest.fixture(autouse=True)
def fast_horizons(monkeypatch):
    """Short horizons + a fresh stats dict per test; restored afterwards."""
    monkeypatch.setattr(config, "FORWARD_SAMPLER_HORIZONS_SECONDS", (0.05, 0.1, 0.15, 0.2))
    monkeypatch.setattr(forward_sampler, "_sampler_stats", {
        key: 0 for key in get_sampler_stats()
    })


def make_job(**overrides) -> SamplerJob:
    defaults = dict(
        outcome_id=str(uuid.uuid4()),
        market_id="mkt-sampler",
        token_id="token-sampler",
        t0_iso=datetime.now(timezone.utc).isoformat(),
        price_t0=0.50,
        headline_hash="abc123",
        entities=["Fed", "Rates"],
        strategy="news_velocity",
        headline="Fed cuts rates",
        source="Reuters",
    )
    defaults.update(overrides)
    return SamplerJob(**defaults)


async def wait_for(condition, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        await asyncio.sleep(0.01)
    return False


def patch_db(db_state: dict[str, list[dict[str, Any]]], fail_tables: set[str] | None = None):
    client = MockSupabaseClient(db_state, fail_tables)

    async def fake_get_client():
        return client

    return patch("memory.supabase_client.get_client", fake_get_client)


# ── 1. Outcome row recorded on consume ────────────────────────────────────────

@pytest.mark.anyio
async def test_supervisor_records_outcome_row():
    db_state: dict[str, list[dict[str, Any]]] = {"signal_outcomes": [], "market_signals": []}
    job = make_job()
    schedule_forward_sampling(job)

    with patch_db(db_state):
        supervisor = asyncio.create_task(run_forward_sampler_supervisor())
        try:
            inserted = await wait_for(
                lambda: len(db_state["signal_outcomes"]) == 1
            )
            assert inserted, "supervisor never recorded the outcome row"
            row = db_state["signal_outcomes"][0]
            assert row["id"] == job.outcome_id
            assert row["market_id"] == "mkt-sampler"
            assert row["price_t0"] == 0.50
            assert row["headline_hash"] == "abc123"
            assert row["entities_json"] == ["Fed", "Rates"]
            assert row["strategy"] == "news_velocity"
            assert row["signal_id"] is None
            assert row["confirmed_direction"] is None
            assert get_sampler_stats()["rows_inserted"] == 1
        finally:
            supervisor.cancel()
            with pytest.raises(asyncio.CancelledError):
                await supervisor


# ── 2. Price reads fill all four horizons ─────────────────────────────────────

@pytest.mark.anyio
async def test_reads_fill_all_horizon_columns():
    db_state: dict[str, list[dict[str, Any]]] = {"signal_outcomes": [], "market_signals": []}
    job = make_job()
    schedule_forward_sampling(job)

    async def fake_price(token_id: str) -> float:
        assert token_id == "token-sampler"
        return 0.62

    with patch_db(db_state), patch("data.market_discovery.get_market_price", fake_price):
        supervisor = asyncio.create_task(run_forward_sampler_supervisor())
        try:
            done = await wait_for(
                lambda: get_sampler_stats()["reads_succeeded"] == 4
            )
            assert done, "expected 4 successful horizon reads"
            row = db_state["signal_outcomes"][0]
            for column in ("p_m1", "p_m5", "p_m15", "p_m60"):
                assert row[column] == 0.62, f"{column} not recorded"
        finally:
            supervisor.cancel()
            with pytest.raises(asyncio.CancelledError):
                await supervisor


# ── 3. Misses recorded as explicit NULLs ──────────────────────────────────────

@pytest.mark.anyio
async def test_read_miss_leaves_null_and_counts():
    db_state: dict[str, list[dict[str, Any]]] = {"signal_outcomes": [], "market_signals": []}
    job = make_job()
    schedule_forward_sampling(job)

    async def failing_price(token_id: str) -> float:
        raise TimeoutError("clob unavailable")

    with patch_db(db_state), patch("data.market_discovery.get_market_price", failing_price):
        supervisor = asyncio.create_task(run_forward_sampler_supervisor())
        try:
            done = await wait_for(
                lambda: get_sampler_stats()["reads_missed"] == 4
            )
            assert done, "expected 4 missed reads"
            row = db_state["signal_outcomes"][0]
            for column in ("p_m1", "p_m5", "p_m15", "p_m60"):
                assert row.get(column) is None, f"{column} should stay NULL"
            assert get_sampler_stats()["reads_succeeded"] == 0
        finally:
            supervisor.cancel()
            with pytest.raises(asyncio.CancelledError):
                await supervisor


# ── 4. Never-crash: supervisor keeps processing after a bad job ───────────────

@pytest.mark.anyio
async def test_supervisor_survives_price_crash_and_processes_next_job():
    db_state: dict[str, list[dict[str, Any]]] = {"signal_outcomes": [], "market_signals": []}
    first = make_job(market_id="mkt-bad", token_id="token-bad")
    second = make_job(market_id="mkt-good", token_id="token-good")
    schedule_forward_sampling(first)

    async def exploding_price(token_id: str) -> float:
        raise RuntimeError("unexpected shape of the universe")

    with patch_db(db_state), patch("data.market_discovery.get_market_price", exploding_price):
        supervisor = asyncio.create_task(run_forward_sampler_supervisor())
        try:
            assert await wait_for(
                lambda: get_sampler_stats()["reads_missed"] == 4
            ), "first job's reads never attempted"
        finally:
            supervisor.cancel()
            with pytest.raises(asyncio.CancelledError):
                await supervisor

    # A fresh supervisor still works: the crash was contained to the job task.
    async def good_price(token_id: str) -> float:
        return 0.55

    schedule_forward_sampling(second)
    with patch_db(db_state), patch("data.market_discovery.get_market_price", good_price):
        supervisor = asyncio.create_task(run_forward_sampler_supervisor())
        try:
            assert await wait_for(
                lambda: len(db_state["signal_outcomes"]) == 2
                and db_state["signal_outcomes"][1]["p_m1"] == 0.55
            ), "supervisor did not recover for the next job"
        finally:
            supervisor.cancel()
            with pytest.raises(asyncio.CancelledError):
                await supervisor


# ── 5. Failed row insert skips sampling ───────────────────────────────────────

@pytest.mark.anyio
async def test_failed_insert_skips_sampling():
    db_state: dict[str, list[dict[str, Any]]] = {"signal_outcomes": [], "market_signals": []}
    job = make_job()
    schedule_forward_sampling(job)

    reads_attempted = {"count": 0}

    async def counting_price(token_id: str) -> float:
        reads_attempted["count"] += 1
        return 0.5

    with patch_db(db_state, fail_tables={"signal_outcomes"}), \
         patch("data.market_discovery.get_market_price", counting_price):
        supervisor = asyncio.create_task(run_forward_sampler_supervisor())
        try:
            assert await wait_for(
                lambda: get_sampler_stats()["rows_failed"] == 1
            ), "insert failure never surfaced in stats"
            await asyncio.sleep(0.3)  # give any (wrongly) spawned sampler time
            assert reads_attempted["count"] == 0, "reads ran without a row to record into"
            assert db_state["signal_outcomes"] == []
        finally:
            supervisor.cancel()
            with pytest.raises(asyncio.CancelledError):
                await supervisor


# ── 6. Queue full drops the job without raising ───────────────────────────────

@pytest.mark.anyio
async def test_queue_full_drops_job(monkeypatch):
    monkeypatch.setattr(config, "FORWARD_SAMPLER_QUEUE_MAXSIZE", 1)
    monkeypatch.setattr(forward_sampler, "_SAMPLER_QUEUE", None)  # fresh tiny queue
    schedule_forward_sampling(make_job(market_id="mkt-a"))
    schedule_forward_sampling(make_job(market_id="mkt-b"))  # must drop, not raise

    stats = get_sampler_stats()
    assert stats["jobs_enqueued"] == 1
    assert stats["jobs_dropped"] == 1


# ── 7. Direction update + signal_id backfill ──────────────────────────────────

@pytest.mark.anyio
async def test_apply_signal_direction_backfills_signal_id():
    db_state: dict[str, list[dict[str, Any]]] = {
        "signal_outcomes": [{
            "id": "outcome-1", "market_id": "mkt-1", "signal_id": None,
            "confirmed_direction": None, "price_t0": 0.5,
        }],
        "market_signals": [{
            "id": "sig-9", "raw_headline": "Fed cuts rates",
            "source_name": "Reuters", "detected_at": "2026-10-06T00:00:00+00:00",
        }],
    }
    with patch_db(db_state):
        await apply_signal_direction("outcome-1", "Fed cuts rates", "Reuters", True)

    row = db_state["signal_outcomes"][0]
    assert row["confirmed_direction"] is True
    assert row["signal_id"] == "sig-9"
    assert get_sampler_stats()["direction_updates_ok"] == 1


@pytest.mark.anyio
async def test_apply_signal_direction_missing_row_stays_null():
    """No market_signals row (analyst failed pre-insert) → NULL, never raises."""
    db_state: dict[str, list[dict[str, Any]]] = {
        "signal_outcomes": [{
            "id": "outcome-2", "market_id": "mkt-1", "signal_id": None,
            "confirmed_direction": None, "price_t0": 0.5,
        }],
        "market_signals": [],
    }
    with patch_db(db_state):
        await apply_signal_direction("outcome-2", "Ghost headline", "Nowhere", False)

    row = db_state["signal_outcomes"][0]
    assert row["confirmed_direction"] is False
    assert row["signal_id"] is None


@pytest.mark.anyio
async def test_apply_signal_direction_db_failure_is_contained():
    db_state: dict[str, list[dict[str, Any]]] = {"signal_outcomes": [], "market_signals": []}
    with patch_db(db_state, fail_tables={"signal_outcomes"}):
        await apply_signal_direction("outcome-3", "h", "s", True)  # must not raise

    assert get_sampler_stats()["direction_updates_failed"] == 1
    assert get_sampler_stats()["direction_updates_ok"] == 0


# ── 8. Supervisor consumes a queued DirectionUpdate message ──────────────────

@pytest.mark.anyio
async def test_supervisor_applies_queued_direction_update():
    db_state: dict[str, list[dict[str, Any]]] = {
        "signal_outcomes": [{
            "id": "outcome-9", "market_id": "mkt-1", "signal_id": None,
            "confirmed_direction": None, "price_t0": 0.5,
        }],
        "market_signals": [{
            "id": "sig-5", "raw_headline": "Fed cuts rates",
            "source_name": "Reuters", "detected_at": "2026-10-06T00:00:00+00:00",
        }],
    }
    forward_sampler.get_sampler_queue().put_nowait(
        forward_sampler.DirectionUpdate(
            outcome_id="outcome-9", headline="Fed cuts rates",
            source="Reuters", confirmed_yes=True,
        )
    )

    with patch_db(db_state):
        supervisor = asyncio.create_task(run_forward_sampler_supervisor())
        try:
            applied = await wait_for(
                lambda: db_state["signal_outcomes"][0]["confirmed_direction"] is True
            )
            assert applied, "supervisor never applied the direction update"
            assert db_state["signal_outcomes"][0]["signal_id"] == "sig-5"
            assert get_sampler_stats()["direction_updates_ok"] == 1
        finally:
            supervisor.cancel()
            try:
                await supervisor
            except asyncio.CancelledError:
                pass
