"""
tests/test_schema_event_key.py — List-B Step 6 (B6) migration contract tests.

Locks the event-cluster schema contract in the committed canonical DDL
(memory/migrations.py SQL_MIGRATIONS):
  - open_positions carries event_key (the correlated-exposure gate groups
    open positions by the real-world event they ride on — E-B6);
  - the idempotent backfill derives the key from thesis_id's third segment
    ("{market_id}:{strategy}:{event_key}") and falls back to market_id;
  - the scratch/migration_event_key.sql mirror is in sync (when present
    locally — scratch/ is gitignored by design).
"""

from pathlib import Path

import pytest

from memory.migrations import SQL_MIGRATIONS

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRATCH_MIGRATION = PROJECT_ROOT / "scratch" / "migration_event_key.sql"

BACKFILL = "split_part(thesis_id, ':', 3)"


def _migration_sql(table: str) -> str:
    """Return the registered migration SQL for one table (fail loudly if absent)."""
    entries = [m for m in SQL_MIGRATIONS if m["table"] == table]
    assert len(entries) == 1, f"expected exactly one migration for {table}"
    return entries[0]["sql"]


def test_event_key_declared_in_canonical_ddl() -> None:
    """event_key is in the canonical CREATE TABLE for open_positions."""
    assert "event_key TEXT" in _migration_sql("open_positions")


def test_event_key_reached_by_idempotent_alter() -> None:
    """Pre-Step-6 databases converge via one ADD COLUMN IF NOT EXISTS."""
    assert "ADD COLUMN IF NOT EXISTS event_key TEXT" in _migration_sql("open_positions")


def test_backfill_derives_key_from_thesis_else_market() -> None:
    """Legacy rows: the event key rides in thesis_id's third segment since
    List A Step 3; rows without one cluster alone by market_id. The backfill
    must be NULL-guarded so reruns are no-ops."""
    sql = _migration_sql("open_positions")
    assert BACKFILL in sql
    assert "COALESCE(NULLIF(" in sql
    assert ", market_id)" in sql
    assert "WHERE event_key IS NULL" in sql


def test_no_other_table_gains_event_key_in_this_entry() -> None:
    """The column belongs to open_positions only — closed_trades rows carry
    their thesis context already, and no reader needs it there."""
    assert "event_key" not in _migration_sql("closed_trades")


def test_scratch_sql_mirror_in_sync() -> None:
    """The operator-facing scratch mirror carries the same contract (skip on
    a fresh clone — scratch/ is gitignored by design)."""
    if not SCRATCH_MIGRATION.exists():
        pytest.skip("scratch/migration_event_key.sql not present (gitignored)")
    sql = SCRATCH_MIGRATION.read_text(encoding="utf-8")
    assert "ALTER TABLE public.open_positions ADD COLUMN IF NOT EXISTS event_key TEXT;" in sql
    assert BACKFILL in sql
    assert "WHERE event_key IS NULL" in sql
