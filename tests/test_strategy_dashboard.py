"""
tests/test_strategy_dashboard.py — Tests for scripts/strategy_dashboard.py
(List A.md Step 3 — A7). Pure core only, synthetic closed_trades rows — no
Supabase, matching the signal_drift_report test pattern.

The key property under test: the dashboard reuses the runtime's own
derivation (risk/edge_monitor.derive_allocations), so a strategy the sizing
path would retire/probate shows exactly that on the dashboard, and the
Brier-vs-market column (Step 5) reuses the gate report's identical-sample
math — rows without estimates leave the sample and render an explicit "—",
never a fabricated number.
"""

from datetime import datetime, timedelta, timezone

import pytest
import config

from scripts.strategy_dashboard import (
    _BRIER_PLACEHOLDER,
    aggregate_strategies,
    render_markdown,
    render_telegram_summary,
)

_NOW = datetime(2026, 10, 7, 12, 0, 0, tzinfo=timezone.utc)


def make_row(strategy: str, net_edge: float | None, pnl: float | None,
             minutes: int, thesis_id: str | None = None) -> dict:
    """Synthetic closed_trades row mirroring the fetch shape."""
    return {
        "strategy": strategy,
        "net_edge_at_entry": net_edge,
        "pnl_usdc": pnl,
        "thesis_id": thesis_id,
        "closed_at": (_NOW - timedelta(minutes=minutes)).isoformat(),
    }


# ── aggregate_strategies ──────────────────────────────────────────────────────

def test_aggregate_empty():
    assert aggregate_strategies([]) == []


def test_aggregate_groups_and_sums_pnl():
    rows = [
        make_row("recalibration", 0.14, 5.0, minutes=300),
        make_row("recalibration", 0.10, -2.0, minutes=200),
        make_row("velocity", 0.08, 1.0, minutes=100),
    ]
    report = aggregate_strategies(rows)
    assert [g["strategy"] for g in report] == ["recalibration", "velocity"]
    rec = report[0]
    assert rec["n"] == 2
    assert rec["net_pnl"] == pytest.approx(3.0)
    assert rec["total_rows"] == 2


def test_aggregate_null_pnl_excluded_from_sum_not_from_n():
    rows = [make_row("s", 0.10, None, minutes=30), make_row("s", 0.10, 4.0, minutes=10)]
    (row,) = aggregate_strategies(rows)
    assert row["n"] == 2
    assert row["net_pnl"] == pytest.approx(4.0)


def test_aggregate_mean_edge_sharpe_match_monitor():
    """The dashboard numbers ARE the runtime's derivation (single source)."""
    from risk.edge_monitor import rolling_net_edge, rolling_net_sharpe
    edges = [0.02, 0.04] * 15
    rows = [make_row("s", e, 1.0, minutes=10_000 - i) for i, e in enumerate(edges)]
    (row,) = aggregate_strategies(rows)
    assert row["mean_net_edge"] == pytest.approx(rolling_net_edge(edges))
    assert row["rolling_sharpe"] == pytest.approx(rolling_net_sharpe(edges))


def test_aggregate_thesis_count_dedups():
    """Thesis-level visibility: clustered entries count once per thesis_id."""
    rows = [
        make_row("s", 0.10, 1.0, minutes=300, thesis_id="M1:s:fed"),
        make_row("s", 0.10, 1.0, minutes=200, thesis_id="M1:s:fed"),
        make_row("s", 0.10, 1.0, minutes=100, thesis_id="M2:s:ecb"),
    ]
    (row,) = aggregate_strategies(rows)
    assert row["n"] == 3
    assert row["thesis_count"] == 2


def test_aggregate_probation_and_retired_flags():
    """Same mechanical rule as enforcement: 20 thin edges → probation;
    40 → retired."""
    thin20 = [make_row("s", 0.025, 1.0, minutes=60) for _ in range(20)]
    assert aggregate_strategies(thin20)[0]["probation"] is True
    assert aggregate_strategies(thin20)[0]["retired"] is False

    thin40 = [make_row("s", 0.02, 1.0, minutes=10_000) for _ in range(40)]
    assert aggregate_strategies(thin40)[0]["retired"] is True


def test_aggregate_unknown_strategy_bucket():
    rows = [make_row(None, 0.10, 1.0, minutes=30)]
    (row,) = aggregate_strategies(rows)
    assert row["strategy"] == "unknown"
    assert row["n"] == 1


# ── rendering ─────────────────────────────────────────────────────────────────

def build_report() -> list[dict]:
    return aggregate_strategies([
        make_row("recalibration", 0.14, 12.0, minutes=300, thesis_id="M1:recalibration:fed"),
        make_row("recalibration", 0.10, -2.0, minutes=200, thesis_id="M1:recalibration:fed"),
        make_row("velocity", 0.08, 1.0, minutes=100, thesis_id="M2:velocity:ecb"),
    ])


def test_markdown_has_all_spec_columns_and_brier_column():
    md = render_markdown(build_report(), total_rows=3, window_days=7, generated_at=_NOW)
    for header in ("strategy", "n", "net P&L (USDC)", "mean net edge (¢)",
                   "rolling Sharpe", "CUSUM", "Brier-vs-market"):
        assert header in md
    assert "recalibration" in md and "velocity" in md
    # Rows without estimates leave the Brier sample — the dash renders
    # explicitly and the note explains it (never a fake number).
    assert _BRIER_PLACEHOLDER in md
    assert "no sample" in md and "Step 5" in md


def test_markdown_empty_window_is_explicit():
    md = render_markdown([], total_rows=0, window_days=7, generated_at=_NOW)
    assert "No closed trades in the window" in md
    assert "ledger clock" in md


def test_telegram_summary_format_and_content():
    summary = render_telegram_summary(build_report(), total_rows=3, window_days=7)
    assert summary.startswith("[ZERO-ALPHA] INFO | STRATEGY_DASHBOARD")
    assert "Time:" in summary
    for fragment in ("recalibration:", "n=2", "pnl=", "edge=", "sharpe=",
                     "cusum=", "brier_agent/mkt=—(no sample)"):
        assert fragment in summary


def test_telegram_summary_empty_window():
    summary = render_telegram_summary([], total_rows=0, window_days=7)
    assert summary.startswith("[ZERO-ALPHA] INFO | STRATEGY_DASHBOARD")
    assert "No closed trades in window" in summary


def test_brier_column_computed_when_sample_exists():
    """Step 5: with p_model/market_price/outcome present, the dashboard renders
    the same agent-vs-market Brier math the gate report uses — not a dash."""
    rows = [
        make_row("recalibration", 0.14, 5.0, minutes=300, thesis_id="M1:recalibration:a"),
        make_row("recalibration", 0.10, -2.0, minutes=200, thesis_id="M1:recalibration:b"),
    ]
    for row in rows:
        row.update({
            "direction": "YES", "outcome": "win",
            "p_model_at_entry": 0.70, "market_price_at_entry": 0.50,
        })
    report = aggregate_strategies(rows)
    rec = report[0]
    assert rec["brier_sample_n"] == 2
    assert rec["agent_brier"] == pytest.approx((0.70 - 1.0) ** 2)
    assert rec["market_brier"] == pytest.approx((0.50 - 1.0) ** 2)
    assert rec["agent_brier"] < rec["market_brier"]
    md = render_markdown(report, total_rows=2, window_days=7, generated_at=_NOW)
    assert "0.0900 vs 0.2500 (n=2)" in md
    tg = render_telegram_summary(report, total_rows=2, window_days=7)
    assert "brier_agent/mkt=0.090 vs 0.250" in tg


def test_cusum_faded_visible_in_renders():
    """A strategy whose recent edges fade shows the FADED status in the
    markdown CUSUM column. In the compact Telegram status the precedence is
    RETIRED > PROBATION > FADED > OK — this scenario is on probation (last-20
    mean = −2¢) but NOT retired (previous window healthy), so it shows
    PROBATION there. 20 trades at −2¢ accumulate 1.5¢/trade → CUSUM crosses
    0.25 within the window."""
    rows = [make_row("s", 0.05, 1.0, minutes=10_000) for _ in range(30)]
    rows += [make_row("s", -0.02, 1.0, minutes=10_000 - 30 - i) for i in range(20)]
    report = aggregate_strategies(rows)
    assert report[0]["cusum_faded"] is True
    assert report[0]["probation"] is True
    assert report[0]["retired"] is False
    md = render_markdown(report, total_rows=len(rows), window_days=7, generated_at=_NOW)
    tg = render_telegram_summary(report, total_rows=len(rows), window_days=7)
    assert "FADED" in md
    assert "cusum=PROBATION" in tg


def test_retired_status_takes_precedence_in_telegram():
    """Two consecutive failing windows → RETIRED outranks everything else."""
    rows = [make_row("s", 0.02, 1.0, minutes=10_000 - i) for i in range(40)]
    report = aggregate_strategies(rows)
    assert report[0]["retired"] is True
    tg = render_telegram_summary(report, total_rows=len(rows), window_days=7)
    assert "cusum=RETIRED" in tg


def test_cusum_ok_when_no_fade():
    """A healthy strategy shows OK in both renders."""
    rows = [make_row("s", 0.05, 1.0, minutes=10_000 - i) for i in range(40)]
    report = aggregate_strategies(rows)
    assert report[0]["cusum_faded"] is False
    md = render_markdown(report, total_rows=len(rows), window_days=7, generated_at=_NOW)
    tg = render_telegram_summary(report, total_rows=len(rows), window_days=7)
    assert "OK (" in md
    assert "cusum=OK" in tg
