"""
tests/test_lease.py — Step 3 (B4) lease service tests.

Covers coordinator/lease.py against a mock Supabase client whose
claim_market_lease RPC mirrors the SQL function's atomic conditional upsert
(tests/lease_mock.py — one implementation of the semantics, no drift):

  claim / conflict / expiry-steal / same-owner renewal (atomic re-claim),
  sequential double-claim (the mock-level shape of the upsert's atomicity —
  true concurrency is PostgreSQL's row-locked WHERE re-evaluation),
  release deletes only the owner's row, drop counters, and the singleton
  guard (claim free / wait-then-exit when held / renewer success, transient
  error tolerance, and definitive-loss exit).

Lease failure is fail-closed everywhere: a claim that cannot be verified
never reads as CLAIMED.
"""

import asyncio
import sys
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Generator
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import config
from coordinator import lease
from tests.lease_mock import MockRpcBuilder


def _iso(offset_seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)).isoformat()


class MockSupabaseClient:
    """Minimal mock client: table builders for the release DELETE + the
    shared claim RPC semantics. Reuses tests.lease_mock so the claim
    behavior cannot drift from the other test files' mocks."""

    def __init__(self, db_state: dict[str, list[dict[str, Any]]]) -> None:
        self.db_state = db_state
        self.fail_rpc = False
        self.rpc_call_count = 0

    def table(self, name: str) -> Any:
        return _MockTableBuilder(self.db_state, name)

    def rpc(self, fn_name: str, params: dict[str, Any]) -> MockRpcBuilder:
        if self.fail_rpc:
            raise ConnectionError("mocked RPC outage")
        self.rpc_call_count += 1
        return MockRpcBuilder(self.db_state, fn_name, params)


class _MockTableBuilder:
    """eq-filtered DELETE returning the matched rows (release path)."""

    def __init__(self, db_state: dict[str, list[dict[str, Any]]], table_name: str) -> None:
        self.db_state = db_state
        self.table_name = table_name
        self.filters: list[tuple[str, Any]] = []

    def delete(self) -> "_MockTableBuilder":
        return self

    def eq(self, col: str, val: Any) -> "_MockTableBuilder":
        self.filters.append((col, val))
        return self

    def execute(self) -> Any:
        class Result:
            def __init__(self, data: list[dict[str, Any]]) -> None:
                self.data = data

        rows = self.db_state.get(self.table_name, [])
        matched = [r for r in rows if all(r.get(c) == v for c, v in self.filters)]
        self.db_state[self.table_name] = [r for r in rows if r not in matched]
        return Result(matched)


@pytest.fixture
def lease_db() -> Generator[tuple[MockSupabaseClient, dict[str, list[dict[str, Any]]]], None, None]:
    """Fresh mock DB with coordinator.lease.get_client patched to it."""
    db_state: dict[str, list[dict[str, Any]]] = {"market_leases": []}
    client = MockSupabaseClient(db_state)

    async def fake_get_client() -> MockSupabaseClient:
        return client

    with patch("coordinator.lease.get_client", fake_get_client):
        yield client, db_state


@pytest.fixture(autouse=True)
def clean_lease_state() -> Generator[None, None, None]:
    """Isolate the module-level counters and identity between tests."""
    lease._drop_counters.update({"lock:timeout": 0, "lease:conflict": 0, "lease:error": 0})
    lease.reset_instance_id_for_tests()
    yield
    lease._drop_counters.update({"lock:timeout": 0, "lease:conflict": 0, "lease:error": 0})
    lease.reset_instance_id_for_tests()


# ── Claim semantics ──────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_claim_fresh_market_succeeds(lease_db) -> None:
    client, db = lease_db
    outcome = await lease.acquire_lease("mkt-1", "inst-A", config.LEASE_TTL_SECONDS)
    assert outcome is lease.LeaseOutcome.CLAIMED
    assert db["market_leases"][0]["market_id"] == "mkt-1"
    assert db["market_leases"][0]["owner"] == "inst-A"
    assert lease.get_drop_counters() == {"lock:timeout": 0, "lease:conflict": 0, "lease:error": 0}


@pytest.mark.anyio
async def test_claim_conflict_on_live_foreign_lease(lease_db) -> None:
    client, db = lease_db
    db["market_leases"].append({"market_id": "mkt-1", "owner": "inst-A", "lease_until": _iso(60)})
    outcome = await lease.acquire_lease("mkt-1", "inst-B", config.LEASE_TTL_SECONDS)
    assert outcome is lease.LeaseOutcome.CONFLICT
    # The foreign lease is untouched.
    assert db["market_leases"][0]["owner"] == "inst-A"
    assert lease.get_drop_counters()["lease:conflict"] == 1


@pytest.mark.anyio
async def test_claim_steals_expired_lease(lease_db) -> None:
    client, db = lease_db
    db["market_leases"].append({"market_id": "mkt-1", "owner": "inst-A", "lease_until": _iso(-5)})
    outcome = await lease.acquire_lease("mkt-1", "inst-B", config.LEASE_TTL_SECONDS)
    assert outcome is lease.LeaseOutcome.CLAIMED
    assert db["market_leases"][0]["owner"] == "inst-B"
    # A steal is a legitimate claim, not a conflict drop.
    assert lease.get_drop_counters()["lease:conflict"] == 0


@pytest.mark.anyio
async def test_same_owner_reclaim_succeeds_and_refreshes(lease_db) -> None:
    """Renewal: the same owner re-claims its own unexpired lease (atomic
    TTL refresh — the renew_lease primitive)."""
    client, db = lease_db
    db["market_leases"].append(
        {"market_id": "mkt-1", "owner": "inst-A", "lease_until": _iso(1)}
    )
    outcome = await lease.renew_lease("mkt-1", "inst-A", config.LEASE_TTL_SECONDS)
    assert outcome is lease.LeaseOutcome.CLAIMED
    assert db["market_leases"][0]["owner"] == "inst-A"
    renewed_until = datetime.fromisoformat(db["market_leases"][0]["lease_until"])
    assert renewed_until > datetime.now(timezone.utc) + timedelta(
        seconds=config.LEASE_TTL_SECONDS - 5
    )


@pytest.mark.anyio
async def test_sequential_double_claim_has_one_winner(lease_db) -> None:
    """Atomicity, mock-level shape: while inst-A's claim is live, inst-B's
    claim must not also succeed. (True concurrency is the DB's row-locked
    ON CONFLICT ... WHERE re-evaluation — the SQL under review; the mock
    mirrors the same conditional semantics.)"""
    client, db = lease_db
    assert (
        await lease.acquire_lease("mkt-1", "inst-A", config.LEASE_TTL_SECONDS)
        is lease.LeaseOutcome.CLAIMED
    )
    assert (
        await lease.acquire_lease("mkt-1", "inst-B", config.LEASE_TTL_SECONDS)
        is lease.LeaseOutcome.CONFLICT
    )
    assert len([r for r in db["market_leases"] if r["market_id"] == "mkt-1"]) == 1


@pytest.mark.anyio
async def test_claim_rpc_error_is_lease_error_not_conflict(lease_db) -> None:
    """An unverifiable claim must be counted and surfaced as ERROR (the
    singleton renewer depends on this distinction) — never as CONFLICT."""
    client, db = lease_db
    client.fail_rpc = True
    outcome = await lease.acquire_lease("mkt-1", "inst-A", config.LEASE_TTL_SECONDS)
    assert outcome is lease.LeaseOutcome.ERROR
    assert lease.get_drop_counters()["lease:error"] == 1
    assert lease.get_drop_counters()["lease:conflict"] == 0
    assert db["market_leases"] == []


@pytest.mark.anyio
async def test_claim_non_positive_ttl_raises(lease_db) -> None:
    client, _db = lease_db
    with pytest.raises(ValueError):
        await lease.acquire_lease("mkt-1", "inst-A", 0)


@pytest.mark.anyio
async def test_claim_wrapped_scalar_result_still_claims(lease_db) -> None:
    """Older supabase-py versions wrap scalar RPC results in a list — the
    coercion must still read a claim (and never read None as one)."""
    assert lease._coerce_claim_result(True) is True
    assert lease._coerce_claim_result([True]) is True
    assert lease._coerce_claim_result(None) is False
    assert lease._coerce_claim_result([]) is False
    assert lease._coerce_claim_result([False]) is False


# ── Release semantics ────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_release_deletes_only_own_row(lease_db) -> None:
    client, db = lease_db
    db["market_leases"].extend([
        {"market_id": "mkt-1", "owner": "inst-A", "lease_until": _iso(60)},
        {"market_id": "mkt-1", "owner": "inst-B", "lease_until": _iso(60)},
        {"market_id": "mkt-2", "owner": "inst-A", "lease_until": _iso(60)},
    ])
    assert await lease.release_lease("mkt-1", "inst-A") is True
    remaining = {(r["market_id"], r["owner"]) for r in db["market_leases"]}
    assert remaining == {("mkt-1", "inst-B"), ("mkt-2", "inst-A")}


@pytest.mark.anyio
async def test_release_never_raises_on_rpc_failure(lease_db) -> None:
    """The one fail-soft path: a release outage must not raise through a
    caller's finally (it would mask the critical section's own exception)."""
    client, _db = lease_db
    client.fail_rpc = True
    assert await lease.release_lease("mkt-1", "inst-A") is False


@pytest.mark.anyio
async def test_release_matching_no_rows_returns_false(lease_db) -> None:
    client, _db = lease_db
    assert await lease.release_lease("never-claimed", "inst-A") is False


# ── Singleton guard ──────────────────────────────────────────────────────────

@pytest.fixture
def fast_singleton_timings():
    """Shrink the singleton waits so the retry/exit paths run in milliseconds."""
    with patch.object(config, "SINGLETON_CLAIM_MAX_WAIT_SECONDS", 0.05), \
         patch.object(config, "LEASE_RENEW_INTERVAL_SECONDS", 0.01):
        yield


@pytest.mark.anyio
async def test_singleton_claims_when_free(lease_db, fast_singleton_timings) -> None:
    client, db = lease_db
    await lease.acquire_singleton_or_exit()
    assert db["market_leases"][0]["market_id"] == lease.SINGLETON_LEASE_KEY


@pytest.mark.anyio
async def test_singleton_waits_then_exits_when_held(
    lease_db, fast_singleton_timings
) -> None:
    """Second instance: another owner holds a live __singleton__ lease — the
    guard retries until the max wait, then alert + exit(1)."""
    client, db = lease_db
    db["market_leases"].append({
        "market_id": lease.SINGLETON_LEASE_KEY,
        "owner": "inst-OLD",
        "lease_until": _iso(3600),  # never expires inside the test window
    })
    with patch.object(lease, "alert_system_halt", AsyncMock()) as mock_alert:
        with pytest.raises(SystemExit) as excinfo:
            await lease.acquire_singleton_or_exit()
    assert excinfo.value.code == 1
    # Fire-and-forget alert (codebase pattern): dispatched via create_task;
    # sys.exit(1) runs before the scheduled task can await, so assert on the
    # dispatch, not the await.
    assert mock_alert.call_count == 1
    args = mock_alert.call_args.args
    assert args[0] == "singleton_claim_timeout"
    # The foreign lease is untouched (we never steal a live singleton).
    assert db["market_leases"][0]["owner"] == "inst-OLD"


@pytest.mark.anyio
async def test_singleton_takes_over_after_holder_expiry(
    lease_db, fast_singleton_timings
) -> None:
    """The deploy-overlap takeover: the old holder's lease expires mid-wait
    (hard-killed process), the next retry claims — the guard must not exit."""
    client, db = lease_db
    # Expires in 20ms — inside the first retry interval (10ms) + claim time.
    db["market_leases"].append({
        "market_id": lease.SINGLETON_LEASE_KEY,
        "owner": "inst-OLD",
        "lease_until": _iso(0.02),
    })
    await lease.acquire_singleton_or_exit()
    assert db["market_leases"][0]["owner"] != "inst-OLD"


@pytest.mark.anyio
async def test_renewer_survives_transient_rpc_errors(lease_db) -> None:
    """A renewal that cannot be verified must NOT kill the process — the 90s
    TTL tolerates missed ticks while the DB recovers."""
    client, _db = lease_db
    client.fail_rpc = True
    with patch.object(config, "LEASE_RENEW_INTERVAL_SECONDS", 0.01):
        task = asyncio.create_task(lease.run_singleton_lease_renewer())
        await asyncio.sleep(0.05)
        assert not task.done(), "renewer exited on a transient RPC error"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.anyio
async def test_renewer_exits_when_lease_lost_to_another_owner(lease_db) -> None:
    """Split-brain signal: another instance claimed __singleton__ after our
    expiry — the renewer alerts and exits(1) (fail-closed)."""
    client, db = lease_db
    # Claim first so the renewer owns the lease...
    await lease.acquire_lease(
        lease.SINGLETON_LEASE_KEY, lease.get_instance_id(), config.SINGLETON_LEASE_TTL_SECONDS
    )
    # ...then simulate another instance stealing it.
    db["market_leases"][0].update({"owner": "inst-OTHER", "lease_until": _iso(3600)})
    with patch.object(lease, "alert_system_halt", AsyncMock()) as mock_alert:
        with patch.object(config, "LEASE_RENEW_INTERVAL_SECONDS", 0.01):
            with pytest.raises(SystemExit) as excinfo:
                await lease.run_singleton_lease_renewer()
    assert excinfo.value.code == 1
    assert mock_alert.call_count == 1
    assert mock_alert.call_args.args[0] == "singleton_lease_lost"


@pytest.mark.anyio
async def test_renewer_keeps_lease_alive(lease_db) -> None:
    """Happy path: the renewer ticks and its same-owner re-claims succeed."""
    client, db = lease_db
    await lease.acquire_lease(
        lease.SINGLETON_LEASE_KEY, lease.get_instance_id(), config.SINGLETON_LEASE_TTL_SECONDS
    )
    with patch.object(config, "LEASE_RENEW_INTERVAL_SECONDS", 0.01):
        task = asyncio.create_task(lease.run_singleton_lease_renewer())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert db["market_leases"][0]["owner"] == lease.get_instance_id()


# ── Instance identity ────────────────────────────────────────────────────────

def test_instance_id_stable_unique_per_boot_and_logged(caplog) -> None:
    import socket

    first = lease.get_instance_id()
    assert first == lease.get_instance_id(), "identity must be stable within a boot"
    assert first.startswith(socket.gethostname())
    with caplog.at_level("INFO", logger="coordinator.lease"):
        lease.reset_instance_id_for_tests()
        second = lease.get_instance_id()
    assert second != first, "a fresh boot identity must differ (reset simulates a new boot)"
    assert "Instance identity" in caplog.text
