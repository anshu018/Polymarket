"""
tests/lease_mock.py — shared mock semantics for the claim_market_lease RPC.

The repo's module-local Supabase mock clients (one per test file) must answer
`client.rpc("claim_market_lease", {...})` once Step 3's lease guard sits
inside both entry critical sections. The conditional-upsert semantics live
HERE so every mock mirrors the SQL function identically (one implementation,
no drift): insert when free; on conflict, update only when the existing lease
expired or belongs to the same owner (renewal); otherwise return no row —
the JSON null a real PostgREST call returns for a scalar RPC that yielded no
row, which coordinator.lease reads as NOT claimed (fail-closed).
"""

from datetime import datetime, timedelta, timezone
from typing import Any


class LeaseRpcResult:
    """Mimics supabase-py's response object for a scalar-returning RPC."""

    def __init__(self, data: Any) -> None:
        self.data = data


def apply_claim_market_lease(
    db_state: dict[str, list[dict[str, Any]]], params: dict[str, Any]
) -> LeaseRpcResult:
    """Mirror of the claim_market_lease SQL function (D-B13) against db_state."""
    market_id = params["p_market_id"]
    owner = params["p_owner"]
    ttl_seconds = params["p_ttl_seconds"]
    now = datetime.now(timezone.utc)
    lease_until = (now + timedelta(seconds=ttl_seconds)).isoformat()

    table = db_state.setdefault("market_leases", [])
    for row in table:
        if row.get("market_id") != market_id:
            continue
        raw_until = row.get("lease_until")
        try:
            expired = datetime.fromisoformat(raw_until) < now
        except (TypeError, ValueError):
            expired = False
        if not expired and row.get("owner") != owner:
            return LeaseRpcResult(None)
        row["owner"] = owner
        row["lease_until"] = lease_until
        return LeaseRpcResult(True)
    table.append({"market_id": market_id, "owner": owner, "lease_until": lease_until})
    return LeaseRpcResult(True)


class MockRpcBuilder:
    """Fluent builder mirroring client.rpc(fn, params).execute()."""

    def __init__(
        self,
        db_state: dict[str, list[dict[str, Any]]],
        fn_name: str,
        params: dict[str, Any],
    ) -> None:
        self._db_state = db_state
        self._fn_name = fn_name
        self._params = params

    def execute(self) -> LeaseRpcResult:
        if self._fn_name == "claim_market_lease":
            return apply_claim_market_lease(self._db_state, self._params)
        raise AssertionError(f"Mock client does not implement rpc '{self._fn_name}'")
