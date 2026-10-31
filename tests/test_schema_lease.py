"""
tests/test_schema_lease.py — Step 3 (B4) migration contract tests.

Locks the market-lease schema contract in the committed canonical DDL
(memory/migrations.py SQL_MIGRATIONS):
  - the market_leases table (market_id PK / owner / lease_until) — the
    PostgREST-compatible cross-process mutex (D-B02);
  - the claim_market_lease function — the ONLY write path, one atomic
    conditional upsert returning true iff the claim succeeded (D-B13: a
    dedicated scalar-returning function, because the execute_sql RPC returns
    VOID and cannot return rows).
The scratch/migration_*.sql mirror is operator-facing and gitignored by
design; when present locally, it is checked for sync too.
"""

from pathlib import Path

import pytest

from memory.migrations import SQL_MIGRATIONS

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRATCH_MIGRATION = PROJECT_ROOT / "scratch" / "migration_market_leases.sql"


def _migration_sql() -> str:
    """Return the registered migration SQL (fail loudly if absent/duplicated)."""
    entries = [m for m in SQL_MIGRATIONS if m["table"] == "market_leases"]
    assert len(entries) == 1, "expected exactly one market_leases migration"
    return entries[0]["sql"]


def test_market_leases_columns_declared_in_canonical_ddl() -> None:
    """The lease table's three columns and their constraints are in the DDL."""
    sql = _migration_sql()
    assert "CREATE TABLE IF NOT EXISTS market_leases" in sql
    assert "market_id   TEXT PRIMARY KEY" in sql
    assert "owner       TEXT NOT NULL" in sql
    assert "lease_until TIMESTAMPTZ NOT NULL" in sql


def test_claim_function_is_the_atomic_conditional_upsert() -> None:
    """claim_market_lease returns a scalar BOOLEAN and carries every clause the
    single-statement atomicity depends on (D-B13)."""
    sql = _migration_sql()
    assert "CREATE OR REPLACE FUNCTION claim_market_lease(" in sql
    assert "RETURNS BOOLEAN" in sql
    # The conditional-upsert clauses — a conflict must update ONLY when the
    # existing lease expired or belongs to the same owner (renewal).
    assert "ON CONFLICT (market_id) DO UPDATE" in sql
    assert "WHERE market_leases.lease_until < now()" in sql
    assert "OR market_leases.owner = EXCLUDED.owner" in sql
    assert "RETURNING true" in sql


def test_claim_function_parameters_match_rpc_call_shape() -> None:
    """The Python caller passes p_market_id / p_owner / p_ttl_seconds — the
    RPC parameter names are the contract with coordinator/lease.py."""
    sql = _migration_sql()
    assert "p_market_id   TEXT" in sql
    assert "p_owner       TEXT" in sql
    assert "p_ttl_seconds INTEGER" in sql


def test_rls_convergence_guard_registered() -> None:
    """The migration converges a bare-RLS table (platform default, zero
    policies — blocks the anon-key claim RPC entirely) back to the project's
    RLS-off state, WITHOUT touching a table that has real policies (the
    owner's future RLS enablement must never be fought by startup)."""
    sql = _migration_sql()
    assert "DISABLE ROW LEVEL SECURITY" in sql
    assert "pg_policy" in sql, "guard must check for real policies, not blind-disable"
    assert "relrowsecurity" in sql


def test_scratch_sql_mirror_in_sync() -> None:
    """The operator-facing scratch mirror carries the same DDL (skip on a
    fresh clone — scratch/ is gitignored by design)."""
    if not SCRATCH_MIGRATION.exists():
        pytest.skip("scratch/migration_market_leases.sql not present (gitignored)")
    scratch = SCRATCH_MIGRATION.read_text(encoding="utf-8")
    registered = _migration_sql()
    for clause in (
        "CREATE TABLE IF NOT EXISTS market_leases",
        "market_id   TEXT PRIMARY KEY",
        "owner       TEXT NOT NULL",
        "lease_until TIMESTAMPTZ NOT NULL",
        "CREATE OR REPLACE FUNCTION claim_market_lease(",
        "RETURNS BOOLEAN",
        "ON CONFLICT (market_id) DO UPDATE",
        "WHERE market_leases.lease_until < now()",
        "OR market_leases.owner = EXCLUDED.owner",
        "RETURNING true",
        "GRANT EXECUTE ON FUNCTION claim_market_lease(TEXT, TEXT, INTEGER) TO anon",
    ):
        assert clause in registered, f"canonical DDL lacks: {clause}"
        assert clause in scratch, f"scratch mirror lacks: {clause}"
