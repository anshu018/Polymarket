"""
tests/test_schema_attribution.py — Step 3 (A7) migration contract tests.

Locks the attribution-ledger schema contract in the committed canonical DDL
(memory/migrations.py SQL_MIGRATIONS): both `open_positions` and `closed_trades`
must carry the Step 3 attribution columns, reached by idempotent ALTER
statements (so pre-Step-3 databases converge on the same schema as fresh
installs). The scratch/migration_*.sql mirror is operator-facing and
gitignored by design; when present locally, it is checked for sync too.
"""

from pathlib import Path

import pytest

from memory.migrations import SQL_MIGRATIONS

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRATCH_MIGRATION = PROJECT_ROOT / "scratch" / "migration_attribution_columns.sql"

ATTRIBUTION_COLUMNS = (
    "thesis_id",
    "p_model_at_entry",
    "market_price_at_entry",
    "net_edge_at_entry",
    "costs_paid_usdc",
    "order_type",
)


def _migration_sql(table: str) -> str:
    """Return the registered migration SQL for one table (fail loudly if absent)."""
    entries = [m for m in SQL_MIGRATIONS if m["table"] == table]
    assert len(entries) == 1, f"expected exactly one migration for {table}"
    return entries[0]["sql"]


@pytest.mark.parametrize("table", ["open_positions", "closed_trades"])
def test_attribution_columns_declared_in_canonical_ddl(table: str) -> None:
    """Every Step 3 attribution column is in the canonical CREATE TABLE."""
    sql = _migration_sql(table)
    for column in ATTRIBUTION_COLUMNS:
        assert column in sql, f"{table} canonical DDL is missing {column}"


@pytest.mark.parametrize("table", ["open_positions", "closed_trades"])
def test_attribution_columns_reached_by_idempotent_alters(table: str) -> None:
    """Pre-Step-3 databases converge: one ADD COLUMN IF NOT EXISTS per column."""
    sql = _migration_sql(table)
    for column in ATTRIBUTION_COLUMNS:
        assert f"ADD COLUMN IF NOT EXISTS {column}" in sql, (
            f"{table} migration lacks the idempotent ALTER for {column}"
        )


def test_strategy_column_exists_on_both_tables() -> None:
    """`strategy` is the attribution key and predates Step 3 — it must stay."""
    for table in ("open_positions", "closed_trades"):
        assert "strategy TEXT" in _migration_sql(table)


def test_closed_trades_allocation_read_index_registered() -> None:
    """The per-strategy allocation/dashboard read path has its index."""
    sql = _migration_sql("closed_trades")
    assert "idx_closed_trades_strategy_closed_at" in sql
    assert "ON closed_trades (strategy, closed_at DESC)" in sql


def test_scratch_sql_mirror_in_sync() -> None:
    """The operator-facing scratch mirror carries the same columns (skip on a
    fresh clone — scratch/ is gitignored by design)."""
    if not SCRATCH_MIGRATION.exists():
        pytest.skip("scratch/migration_attribution_columns.sql not present (gitignored)")
    sql = SCRATCH_MIGRATION.read_text(encoding="utf-8")
    for table in ("open_positions", "closed_trades"):
        for column in ATTRIBUTION_COLUMNS:
            assert f"ADD COLUMN IF NOT EXISTS {column}" in sql, (
                f"scratch mirror lacks {table}.{column} ALTER"
            )
    assert "idx_closed_trades_strategy_closed_at" in sql
