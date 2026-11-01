"""
tests/test_schema_exit_engine.py — List-B Step 5 (B5) migration contract tests.

Locks the exit-engine schema contract in the committed canonical DDL
(memory/migrations.py SQL_MIGRATIONS):
  - closed_trades carries exit_type (the exit-rule bucket — D-B05: early
    exits are tagged, counted in P&L, never merged into the Brier sample);
  - copytrade_log carries exit_type (D-B06: the copied position's row records
    what happened while wallet W/L/trust stays untouched) and its status CHECK
    accepts 'exited_early';
  - the scratch/migration_exit_engine.sql mirror is in sync (when present
    locally — scratch/ is gitignored by design).
"""

from pathlib import Path

import pytest

from memory.migrations import SQL_MIGRATIONS

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRATCH_MIGRATION = PROJECT_ROOT / "scratch" / "migration_exit_engine.sql"

STATUS_VALUES = ("'open'", "'won'", "'lost'", "'cancelled'", "'exited_early'")


def _migration_sql(table: str) -> str:
    """Return the registered migration SQL for one table (fail loudly if absent)."""
    entries = [m for m in SQL_MIGRATIONS if m["table"] == table]
    assert len(entries) == 1, f"expected exactly one migration for {table}"
    return entries[0]["sql"]


def test_exit_type_declared_in_closed_trades_canonical_ddl() -> None:
    """exit_type is in the canonical CREATE TABLE for closed_trades."""
    assert "exit_type TEXT" in _migration_sql("closed_trades")


def test_exit_type_reached_by_idempotent_alter_on_closed_trades() -> None:
    """Pre-Step-5 databases converge via one ADD COLUMN IF NOT EXISTS."""
    sql = _migration_sql("closed_trades")
    assert "ADD COLUMN IF NOT EXISTS exit_type TEXT" in sql


def test_exit_type_declared_in_copytrade_log_canonical_ddl() -> None:
    """exit_type is in the canonical CREATE TABLE for copytrade_log (D-B20)."""
    assert "exit_type TEXT" in _migration_sql("copytrade_log")


def test_exit_type_reached_by_idempotent_alter_on_copytrade_log() -> None:
    """Pre-Step-5 databases converge via one ADD COLUMN IF NOT EXISTS."""
    sql = _migration_sql("copytrade_log")
    assert "ADD COLUMN IF NOT EXISTS exit_type TEXT" in sql


def test_copytrade_log_status_check_accepts_exited_early() -> None:
    """The status CHECK (canonical CREATE + the widening DO block) accepts
    'exited_early' — a full early exit must be recordable without touching
    the wallet (D-B06), and the live DB's existing named constraint must be
    widenable in place, idempotently."""
    sql = _migration_sql("copytrade_log")
    for value in STATUS_VALUES:
        assert value in sql, f"canonical copytrade_log status CHECK lacks {value}"
    # The named constraint is widened in place for existing databases.
    assert "copytrade_log_status_check" in sql
    assert "DROP CONSTRAINT copytrade_log_status_check" in sql
    assert "NOT LIKE '%exited_early%'" in sql


def test_open_positions_gains_no_exit_columns() -> None:
    """Spec: open_positions needs no new column — full exits delete the row,
    partial exits update it. Only closed_trades and copytrade_log change."""
    sql = _migration_sql("open_positions")
    assert "exit_type" not in sql


def test_scratch_sql_mirror_in_sync() -> None:
    """The operator-facing scratch mirror carries the same contract (skip on a
    fresh clone — scratch/ is gitignored by design)."""
    if not SCRATCH_MIGRATION.exists():
        pytest.skip("scratch/migration_exit_engine.sql not present (gitignored)")
    sql = SCRATCH_MIGRATION.read_text(encoding="utf-8")
    for table in ("closed_trades", "copytrade_log"):
        assert f"ALTER TABLE public.{table} ADD COLUMN IF NOT EXISTS exit_type TEXT;" in sql, (
            f"scratch mirror lacks {table}.exit_type ALTER"
        )
    for value in STATUS_VALUES:
        assert value in sql, f"scratch mirror status CHECK lacks {value}"
    assert "DROP CONSTRAINT copytrade_log_status_check" in sql
