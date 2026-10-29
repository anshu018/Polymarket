"""
tests/test_schema_step5.py — Step 5 (A5) migration contract tests.

Locks the fill/baseline schema contract in the committed canonical DDL
(memory/migrations.py SQL_MIGRATIONS):
  - open_positions + closed_trades carry fill_price / fill_type (book-aware
    paper fills, passed through the close path);
  - open_positions has the unfilled-maker partial index the restart-safe
    monitor scan reads;
  - signal_outcomes carries the market-baseline resolution columns
    (resolved_outcome / resolved_at, backfilled offline by the gate report).
The scratch/migration_*.sql mirror is operator-facing and gitignored by
design; when present locally, it is checked for sync too.
"""

from pathlib import Path

import pytest

from memory.migrations import SQL_MIGRATIONS

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRATCH_MIGRATION = PROJECT_ROOT / "scratch" / "migration_step5_fill_and_baseline.sql"

FILL_COLUMNS = ("fill_price", "fill_type")
BASELINE_COLUMNS = ("resolved_outcome", "resolved_at")


def _migration_sql(table: str) -> str:
    """Return the registered migration SQL for one table (fail loudly if absent)."""
    entries = [m for m in SQL_MIGRATIONS if m["table"] == table]
    assert len(entries) == 1, f"expected exactly one migration for {table}"
    return entries[0]["sql"]


@pytest.mark.parametrize("table", ["open_positions", "closed_trades"])
def test_fill_columns_declared_in_canonical_ddl(table: str) -> None:
    """Every Step 5 fill column is in the canonical CREATE TABLE."""
    sql = _migration_sql(table)
    for column in FILL_COLUMNS:
        assert column in sql, f"{table} canonical DDL is missing {column}"


@pytest.mark.parametrize("table", ["open_positions", "closed_trades"])
def test_fill_columns_reached_by_idempotent_alters(table: str) -> None:
    """Pre-Step-5 databases converge: one ADD COLUMN IF NOT EXISTS per column."""
    sql = _migration_sql(table)
    for column in FILL_COLUMNS:
        assert f"ADD COLUMN IF NOT EXISTS {column}" in sql, (
            f"{table} migration lacks the idempotent ALTER for {column}"
        )


def test_unfilled_maker_index_registered() -> None:
    """The maker-fill monitor scans unfilled resting orders — its partial index
    must exist in the canonical DDL."""
    sql = _migration_sql("open_positions")
    assert "idx_open_positions_unfilled_maker" in sql
    assert "WHERE order_type = 'maker' AND fill_price IS NULL" in sql


def test_resting_price_column_registered_on_open_positions() -> None:
    """The monitor needs the persisted maker level — it is not recomputable
    from later books. open_positions only (closed trades never rest)."""
    sql = _migration_sql("open_positions")
    assert "resting_price DECIMAL(10,4)" in sql
    assert "ADD COLUMN IF NOT EXISTS resting_price" in sql
    assert "resting_price" not in _migration_sql("closed_trades")


@pytest.mark.parametrize("column", BASELINE_COLUMNS)
def test_baseline_columns_declared_in_signal_outcomes(column: str) -> None:
    """The market-baseline resolution columns are in the canonical CREATE TABLE."""
    assert column in _migration_sql("signal_outcomes")


@pytest.mark.parametrize("column", BASELINE_COLUMNS)
def test_baseline_columns_reached_by_idempotent_alters(column: str) -> None:
    """Pre-Step-5 signal_outcomes tables converge via idempotent ALTERs."""
    sql = _migration_sql("signal_outcomes")
    assert f"ADD COLUMN IF NOT EXISTS {column}" in sql


def test_scratch_sql_mirror_in_sync() -> None:
    """The operator-facing scratch mirror carries the same columns (skip on a
    fresh clone — scratch/ is gitignored by design)."""
    if not SCRATCH_MIGRATION.exists():
        pytest.skip("scratch/migration_step5_fill_and_baseline.sql not present (gitignored)")
    sql = SCRATCH_MIGRATION.read_text(encoding="utf-8")
    for table in ("open_positions", "closed_trades"):
        for column in FILL_COLUMNS:
            assert f"ADD COLUMN IF NOT EXISTS {column}" in sql, (
                f"scratch mirror lacks {table}.{column} ALTER"
            )
    for column in BASELINE_COLUMNS:
        assert f"ADD COLUMN IF NOT EXISTS {column}" in sql, (
            f"scratch mirror lacks signal_outcomes.{column} ALTER"
        )
    assert "idx_open_positions_unfilled_maker" in sql
