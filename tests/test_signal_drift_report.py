"""
tests/test_signal_drift_report.py — Step 2 drift report tests (List A.md A6).

Synthetic-data tests of the report's pure core: signed-drift math (YES/NO
signs, NULL exclusion), category × event-type grouping, the velocity viability
verdict against the Step 1 cost model (both directions of the verdict), and
markdown rendering. No network, no Supabase — the I/O layer is deliberately
thin and unexercised here.
"""

from datetime import datetime, timezone

import pytest
import config

from scripts.signal_drift_report import (
    aggregate_drift,
    render_markdown,
    render_telegram_summary,
    signed_drift,
    viability_verdict,
)


def make_row(direction=True, price_t0=0.50, p_m1=0.52, p_m60=0.55,
             category="politics", event_type="politics") -> dict:
    """Synthetic signal_outcomes row mirroring the PostgREST embed shape."""
    return {
        "id": "row-1",
        "market_id": "mkt-1",
        "confirmed_direction": direction,
        "price_t0": price_t0,
        "p_m1": p_m1,
        "p_m5": None,
        "p_m15": None,
        "p_m60": p_m60,
        "market_signals": {"category": category, "event_type": event_type},
    }


# ── signed_drift ──────────────────────────────────────────────────────────────

def test_signed_drift_positive_for_yes_signal():
    """A YES signal whose price rose → positive drift."""
    drift = signed_drift(make_row(direction=True, price_t0=0.50, p_m1=0.53), "p_m1")
    assert drift == pytest.approx(0.03)


def test_signed_drift_flips_sign_for_no_signal():
    """The same price RISE on a NO signal is a NEGATIVE drift (sign flips)."""
    drift = signed_drift(make_row(direction=False, price_t0=0.50, p_m1=0.53), "p_m1")
    assert drift == pytest.approx(-0.03)


def test_signed_drift_no_signal_price_drop_is_positive():
    """A NO signal whose price FELL (NO side winning) → positive drift."""
    drift = signed_drift(make_row(direction=False, price_t0=0.50, p_m1=0.47), "p_m1")
    assert drift == pytest.approx(0.03)


def test_signed_drift_excludes_nulls():
    """Missing direction or prices → None (NULLs are excluded, never zero)."""
    assert signed_drift(make_row(direction=None), "p_m1") is None
    assert signed_drift(make_row(p_m1=None), "p_m1") is None
    assert signed_drift(make_row(price_t0=None), "p_m1") is None


# ── aggregate_drift ───────────────────────────────────────────────────────────

def test_aggregate_groups_by_category_and_event_type():
    rows = [
        make_row(category="politics", event_type="election"),
        make_row(category="politics", event_type="election", p_m1=0.56),
        make_row(category="crypto", event_type="crypto"),
    ]
    report = aggregate_drift(rows)
    keys = [(g["category"], g["event_type"]) for g in report]
    assert ("crypto", "crypto") in keys
    assert ("politics", "election") in keys
    politics = next(g for g in report if g["category"] == "politics")
    assert politics["total"] == 2


def test_aggregate_missing_embed_lands_in_unknown():
    """Rows whose market_signals embed is missing still count (unknown group)."""
    row = make_row()
    row["market_signals"] = None
    report = aggregate_drift([row])
    assert len(report) == 1
    assert report[0]["category"] == "unknown"
    assert report[0]["event_type"] == "unknown"  # falls back to category


def test_aggregate_horizon_stats_math():
    """n, mean and pass rate on hand-checkable synthetic drifts."""
    rows = [
        make_row(direction=True, price_t0=0.50, p_m1=0.52),   # +0.02 (positive)
        make_row(direction=True, price_t0=0.50, p_m1=0.51),   # +0.01 (positive)
        make_row(direction=False, price_t0=0.50, p_m1=0.53),  # −0.03 (negative)
        make_row(direction=True, price_t0=0.50, p_m1=None),   # excluded (NULL)
    ]
    report = aggregate_drift(rows)
    group = report[0]
    h1 = next(h for h in group["horizons"] if h["label"] == "+1m")
    assert h1["n"] == 3
    assert h1["mean"] == pytest.approx((0.02 + 0.01 - 0.03) / 3)
    assert h1["pass_rate"] == pytest.approx(2 / 3)


def test_aggregate_ref_price_ignores_nulls():
    rows = [
        make_row(price_t0=0.40),
        make_row(price_t0=0.60),
        make_row(price_t0=None),
    ]
    report = aggregate_drift(rows)
    assert report[0]["ref_price"] == pytest.approx(0.50)


# ── viability_verdict ─────────────────────────────────────────────────────────

def test_verdict_viable_when_drift_clears_costs():
    """A fat drift (10¢) at mid 0.50 must clear the full taker cost stack."""
    gate, net_edge = viability_verdict(0.10, 0.50, "politics")
    assert gate == "ALLOW"
    # Costs: fee ≈ 0.010 + half-spread 0.015 + slippage 0.036 → net ≈ +3.9¢
    assert net_edge > config.MIN_NET_EDGE_CENTS


def test_verdict_not_viable_when_costs_eat_the_drift():
    """The 2¢ drift at mid 0.50 (the old gross-edge fiction) must NOT clear."""
    gate, net_edge = viability_verdict(0.02, 0.50, "politics")
    assert gate == "BLOCK_NET_EDGE"
    assert net_edge <= config.MIN_NET_EDGE_CENTS


def test_verdict_uses_category_fee_schedule():
    """Crypto's higher taker feeRate (0.07) must yield a lower net edge than
    politics (0.04) for the identical drift — the verdict is fee-aware."""
    _, net_politics = viability_verdict(0.10, 0.50, "politics")
    _, net_crypto = viability_verdict(0.10, 0.50, "crypto")
    assert net_crypto < net_politics


def test_verdict_out_of_price_band():
    """A group whose signals sit at an extreme price → OUT OF BAND (takers)."""
    gate, _ = viability_verdict(0.10, 0.05, "politics")
    assert gate == "BLOCK_PRICE_BAND"


# ── rendering ─────────────────────────────────────────────────────────────────

def test_render_markdown_contains_required_fields():
    report = aggregate_drift([
        make_row(category="politics", event_type="election", p_m1=0.60, p_m60=0.62),
    ])
    md = render_markdown(report, total_rows=1, window_days=7,
                         generated_at=datetime.now(timezone.utc))
    assert "# Signal Drift Report" in md
    assert "politics / election" in md
    for label in ("+1m", "+5m", "+15m", "+60m"):
        assert label in md
    assert "VIABLE" in md or "NOT VIABLE" in md
    assert "mean signed drift" in md


def test_render_markdown_empty_window():
    md = render_markdown([], total_rows=0, window_days=7,
                         generated_at=datetime.now(timezone.utc))
    assert "No instrumented signals" in md


def test_render_telegram_summary_lines():
    report = aggregate_drift([
        make_row(category="politics", event_type="election", p_m1=0.60, p_m60=0.62),
    ])
    summary = render_telegram_summary(report, total_rows=1, window_days=7)
    assert summary.startswith("[ZERO-ALPHA] INFO | SIGNAL_DRIFT_REPORT")
    assert "politics/election +1m:" in summary
    assert "politics/election +60m:" in summary
