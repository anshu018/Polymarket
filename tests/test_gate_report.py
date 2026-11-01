"""
tests/test_gate_report.py — Step 5 (A5) gate report tests.

Pure-core only: bootstrap CIs on synthetic data with known signs, thesis
clustering, the Brier sample construction, the gate verdict matrix, rendering
and the Gamma resolution parser. No DB, no network (repo test rules).
"""

from typing import Any

import pytest

from scripts.gate_report import (
    aggregate_per_exit_type,
    aggregate_per_strategy,
    brier_bootstrap_ci,
    brier_pairs,
    build_report,
    cluster_mean_bootstrap_ci,
    cluster_values,
    evaluate_gate,
    evaluate_market_brier,
    exit_type_stats,
    is_resolution_row,
    market_yes_outcome,
    mean_brier,
    order_type_stats,
    render_markdown,
    render_telegram_summary,
    slippage_stats,
    thesis_dedup_count,
    yes_line_outcome,
    to_float,
)


def closed_row(
    pnl: float = 10.0,
    thesis: str | None = "t1",
    strategy: str = "recalibration",
    outcome: str = "win",
    direction: str = "YES",
    p_model: float | None = 0.65,
    market_p: float | None = 0.50,
    fill: float | None = 0.52,
    fill_type: str | None = "taker",
    exit_type: str | None = None,
) -> dict[str, Any]:
    return {
        "market_id": "mkt",
        "direction": direction,
        "strategy": strategy,
        "outcome": outcome,
        "exit_type": exit_type,
        "thesis_id": thesis,
        "pnl_usdc": pnl,
        "net_edge_at_entry": 0.05,
        "p_model_at_entry": p_model,
        "market_price_at_entry": market_p,
        "fill_price": fill,
        "fill_type": fill_type,
    }


# ── Coercion ──────────────────────────────────────────────────────────────────

class TestToFloat:
    def test_decimal_strings_coerce(self):
        assert to_float("0.5200") == pytest.approx(0.52)

    def test_garbage_and_nan_are_none(self):
        assert to_float("NaN") is None
        assert to_float(None) is None
        assert to_float("abc") is None


# ── Bootstrap CIs on synthetic data ───────────────────────────────────────────

class TestClusterBootstrap:
    def test_positive_pnl_ci_entirely_positive(self):
        rows = [closed_row(pnl=100.0 + i, thesis=f"t{i}") for i in range(30)]
        ci = cluster_mean_bootstrap_ci(rows)
        assert ci is not None
        assert ci[0] > 0.0, "an all-positive P&L sample must have CI low > 0"

    def test_negative_pnl_ci_entirely_negative(self):
        rows = [closed_row(pnl=-50.0 - i, thesis=f"t{i}") for i in range(30)]
        ci = cluster_mean_bootstrap_ci(rows)
        assert ci is not None
        assert ci[1] < 0.0

    def test_deterministic_fixed_seed(self):
        rows = [closed_row(pnl=10.0 + i, thesis=f"t{i}") for i in range(20)]
        assert cluster_mean_bootstrap_ci(rows) == cluster_mean_bootstrap_ci(rows)

    def test_empty_sample_gives_no_invented_interval(self):
        assert cluster_mean_bootstrap_ci([]) is None

    def test_clusters_count_once(self):
        """Two trades inside one thesis = ONE cluster (Step 5d), two theses = two."""
        one_thesis = [closed_row(pnl=10.0, thesis="t1"), closed_row(pnl=30.0, thesis="t1")]
        assert len(cluster_values(one_thesis)) == 1
        two_theses = [closed_row(pnl=10.0, thesis="t1"), closed_row(pnl=30.0, thesis="t2")]
        assert len(cluster_values(two_theses)) == 2

    def test_null_thesis_rows_are_singletons_not_dropped(self):
        rows = [closed_row(pnl=5.0, thesis=None), closed_row(pnl=7.0, thesis=None)]
        assert len(cluster_values(rows)) == 2

    def test_cluster_mean_weights_thesis_once(self):
        """Mean per-thesis P&L of [(10,30) in t1, 20 in t2] = 20 — a thesis with
        two trades contributes its total once, not twice."""
        rows = [closed_row(pnl=10.0, thesis="t1"), closed_row(pnl=30.0, thesis="t1"),
                closed_row(pnl=20.0, thesis="t2")]
        ci = cluster_mean_bootstrap_ci(rows)
        assert ci is not None
        assert ci[0] <= 20.0 <= ci[1]


# ── Brier sample construction ─────────────────────────────────────────────────

class TestBrier:
    def test_yes_line_outcome_matrix(self):
        assert yes_line_outcome(closed_row(outcome="win", direction="YES")) == 1
        assert yes_line_outcome(closed_row(outcome="win", direction="NO")) == 0
        assert yes_line_outcome(closed_row(outcome="loss", direction="YES")) == 0
        assert yes_line_outcome(closed_row(outcome="loss", direction="NO")) == 1

    def test_non_standard_outcomes_leave_the_sample(self):
        assert yes_line_outcome(closed_row(outcome="void")) is None
        assert yes_line_outcome(closed_row(outcome=None)) is None
        assert yes_line_outcome(closed_row(direction="SIDEWAYS")) is None

    def test_brier_perfect_and_worst(self):
        assert mean_brier([1.0, 0.0], [1, 0]) == pytest.approx(0.0)
        assert mean_brier([0.0, 1.0], [1, 0]) == pytest.approx(1.0)

    def test_brier_length_mismatch_raises(self):
        with pytest.raises(ValueError):
            mean_brier([0.5], [1, 0])

    def test_identical_sample_class_a_excluded_whole(self):
        """A Class A row (p_model NULL) is excluded from BOTH sides — the sample
        stays identical; no fabricated probability replaces the missing estimate."""
        rows = [
            closed_row(p_model=0.65, market_p=0.50, outcome="win", direction="YES"),
            closed_row(p_model=None, market_p=0.50, outcome="win", direction="YES"),
        ]
        pairs = brier_pairs(rows)
        assert pairs is not None
        assert len(pairs) == 1

    def test_agent_beats_market_on_synthetic_sample(self):
        """Agent says 0.70/0.30 (perfect calls), market says 0.50/0.50 — agent
        Brier strictly lower and the CIs do not overlap."""
        rows = [
            closed_row(p_model=0.70, market_p=0.50, outcome="win", direction="YES", thesis=f"t{i}")
            for i in range(40)
        ] + [
            closed_row(p_model=0.30, market_p=0.50, outcome="win", direction="NO", thesis=f"t{i}")
            for i in range(40)
        ]
        pairs = brier_pairs(rows)
        agent = mean_brier([p[0] for p in pairs], [int(p[2]) for p in pairs])
        market = mean_brier([p[1] for p in pairs], [int(p[2]) for p in pairs])
        agent_ci = brier_bootstrap_ci(pairs, "agent")
        market_ci = brier_bootstrap_ci(pairs, "market")
        assert agent < market
        assert agent_ci is not None and market_ci is not None
        assert agent_ci[1] < market_ci[0], "clearly-skilled sample must separate"

    def test_no_skill_sample_cis_overlap(self):
        """Agent ≈ market (both 0.50 coin-flip predictions on random outcomes):
        the CIs must overlap — the gate must NOT pass on luck."""
        rows = [
            closed_row(p_model=0.5, market_p=0.5, outcome="win", direction="YES", thesis=f"t{i}")
            for i in range(50)
        ] + [
            closed_row(p_model=0.5, market_p=0.5, outcome="loss", direction="YES", thesis=f"t{i}")
            for i in range(50)
        ]
        pairs = brier_pairs(rows)
        agent_ci = brier_bootstrap_ci(pairs, "agent")
        market_ci = brier_bootstrap_ci(pairs, "market")
        assert agent_ci[0] <= market_ci[1], "identical predictors must overlap"


# ── The gate verdict matrix ───────────────────────────────────────────────────

class TestEvaluateGate:
    MIN = 200

    def test_full_pass(self):
        gate = evaluate_gate(
            n_resolved=250,
            pnl_ci=(0.01, 5.0),
            agent_brier=0.15,
            market_brier=0.25,
            agent_ci=(0.12, 0.18),
            market_ci=(0.22, 0.28),
        )
        assert gate["verdict"] == "PASS"
        assert gate["failed_conditions"] == []

    def test_sample_short_fail(self):
        gate = evaluate_gate(
            n_resolved=199, pnl_ci=(0.01, 5.0),
            agent_brier=0.15, market_brier=0.25,
            agent_ci=(0.12, 0.18), market_ci=(0.22, 0.28),
        )
        assert gate["verdict"] == "FAIL"
        assert any("GATE_MIN_RESOLVED_SAMPLES" in c for c in gate["failed_conditions"])

    def test_pnl_ci_crossing_zero_fails(self):
        gate = evaluate_gate(
            n_resolved=250, pnl_ci=(-0.5, 5.0),
            agent_brier=0.15, market_brier=0.25,
            agent_ci=(0.12, 0.18), market_ci=(0.22, 0.28),
        )
        assert gate["verdict"] == "FAIL"

    def test_overlapping_brier_cis_fail_not_pass(self):
        gate = evaluate_gate(
            n_resolved=250, pnl_ci=(0.01, 5.0),
            agent_brier=0.20, market_brier=0.21,
            agent_ci=(0.15, 0.25), market_ci=(0.16, 0.26),
        )
        assert gate["verdict"] == "FAIL"
        assert gate["brier_edge_case"] is True, "agent<market with overlap = documented edge case"

    def test_market_better_than_agent_fails_hard(self):
        gate = evaluate_gate(
            n_resolved=250, pnl_ci=(0.01, 5.0),
            agent_brier=0.30, market_brier=0.20,
            agent_ci=(0.25, 0.35), market_ci=(0.17, 0.23),
        )
        assert gate["verdict"] == "FAIL"
        assert gate["brier_edge_case"] is False

    def test_empty_sample_fails_every_condition(self):
        gate = evaluate_gate(
            n_resolved=0, pnl_ci=None,
            agent_brier=None, market_brier=None,
            agent_ci=None, market_ci=None,
        )
        assert gate["verdict"] == "FAIL"
        assert len(gate["failed_conditions"]) == 3


# ── Slippage / order-type / thesis stats ──────────────────────────────────────

class TestFillStats:
    def test_slippage_uses_the_traded_line(self):
        rows = [
            closed_row(direction="YES", market_p=0.50, fill=0.52),   # +2¢ adverse
            closed_row(direction="NO", market_p=0.50, fill=0.52),    # NO ref 0.50 → +2¢
        ]
        stats = slippage_stats(rows)
        assert stats["n"] == 2
        assert stats["mean"] == pytest.approx(0.02)

    def test_slippage_null_fills_excluded(self):
        rows = [closed_row(fill=None), closed_row(fill=0.52)]
        assert slippage_stats(rows)["n"] == 1

    def test_order_type_counts(self):
        rows = [
            closed_row(fill_type="taker"),
            closed_row(fill_type="maker_fill"),
            closed_row(fill_type="taker_fallback"),
            closed_row(fill_type=None),
        ]
        counts = order_type_stats(rows)
        assert counts == {"taker": 1, "maker_fill": 1, "taker_fallback": 1, "other_or_null": 1}

    def test_thesis_dedup_counts(self):
        rows = [
            closed_row(thesis="t1"),
            closed_row(thesis="t1"),
            closed_row(thesis=None),
        ]
        dedup = thesis_dedup_count(rows)
        assert dedup == {"rows": 3, "distinct_theses": 1, "null_thesis_rows": 1}


# ── Market baseline over all evaluated signals ────────────────────────────────

class TestEvaluatedMarketBrier:
    def test_market_brier_over_evaluated_sample(self):
        rows = [
            {"price_t0": "0.50", "resolved_outcome": 1},
            {"price_t0": "0.50", "resolved_outcome": 0},
            {"price_t0": "1.00", "resolved_outcome": 1},
        ]
        brier, n = evaluate_market_brier(rows)
        assert n == 3
        assert brier == pytest.approx((0.25 + 0.25 + 0.0) / 3)

    def test_unresolved_rows_leave_the_sample(self):
        rows = [{"price_t0": "0.50", "resolved_outcome": None}]
        assert evaluate_market_brier(rows) == (None, 0)


# ── Gamma resolution parser ───────────────────────────────────────────────────

class TestMarketYesOutcome:
    def test_resolved_yes(self):
        payload = {"umaResolutionStatus": "resolved",
                   "outcomes": '["Yes", "No"]', "outcomePrices": '["1", "0"]'}
        assert market_yes_outcome(payload) == 1

    def test_resolved_no(self):
        payload = {"closed": True, "outcomes": ["Yes", "No"], "outcomePrices": ["0", "1"]}
        assert market_yes_outcome(payload) == 0

    def test_unresolved_market_stays_none(self):
        assert market_yes_outcome({"umaResolutionStatus": "pending"}) is None

    def test_void_price_stays_none(self):
        """A resolved market paying 0.5 (void) is NOT a YES/NO resolution — the
        baseline must not ingest it as one."""
        payload = {"resolved": True, "outcomes": ["Yes", "No"], "outcomePrices": ["0.5", "0.5"]}
        assert market_yes_outcome(payload) is None


# ── Report assembly + rendering ───────────────────────────────────────────────

class TestBuildReportAndRender:
    def _winning_rows(self, n: int) -> list[dict[str, Any]]:
        return [
            closed_row(pnl=50.0, thesis=f"t{i}", outcome="win", direction="YES",
                       p_model=0.70, market_p=0.50)
            for i in range(n)
        ]

    def test_build_report_fields(self):
        closed = self._winning_rows(10)
        outcomes = [{"price_t0": "0.5", "resolved_outcome": 1}]
        report = build_report(closed, outcomes)
        assert report["n_resolved"] == 10
        assert report["total_net_pnl"] == pytest.approx(500.0)
        assert report["agent_brier"] is not None
        assert report["agent_brier"] < report["market_brier"]
        assert report["n_evaluated"] == 1
        assert report["thesis_dedup"]["distinct_theses"] == 10

    def test_render_markdown_contains_gate_and_placeholders(self):
        closed = self._winning_rows(10)
        report = build_report(closed, [])
        md = render_markdown(report, window_days=7, generated_at=datetime_now())
        assert "**GATE:" in md
        assert "GATE_MIN_RESOLVED_SAMPLES" in md
        assert "Per-strategy breakdown" in md
        assert "recalibration" in md

    def test_render_telegram_format(self):
        closed = self._winning_rows(10)
        report = build_report(closed, [])
        msg = render_telegram_summary(report, window_days=7)
        assert msg.startswith("[ZERO-ALPHA] INFO | GATE_REPORT")
        assert "GATE:" in msg

    def test_render_failed_gate_lists_conditions(self):
        report = build_report([], [])
        md = render_markdown(report, window_days=7, generated_at=datetime_now())
        assert "**GATE: FAIL**" in md
        msg = render_telegram_summary(report, window_days=7)
        assert "FAILED:" in msg

    def test_per_strategy_class_a_renders_no_brier(self):
        """Class A rows carry no p_model — its Brier cells render '—', never an
        invented number."""
        rows = [closed_row(strategy="copy_edge_class_a", p_model=None, outcome="win")]
        strategies = aggregate_per_strategy(rows)
        assert strategies[0]["strategy"] == "copy_edge_class_a"
        assert strategies[0]["agent_brier"] is None
        assert strategies[0]["market_brier"] is None


def datetime_now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)



# ââ Step 5 (B5): exit accounting â early exits leave the Brier sample, never the P&L ââ

class TestExitAccounting:
    def test_legacy_null_rows_are_resolutions(self):
        # Rows predating the Step 5 column are all resolutions.
        assert is_resolution_row({"exit_type": None}) is True
        assert is_resolution_row({}) is True
        assert is_resolution_row({"exit_type": "resolved"}) is True
        assert is_resolution_row({"exit_type": "exit_swap"}) is False

    def test_early_exits_leave_the_brier_sample_but_not_the_pnl(self):
        rows = [
            closed_row(),
            closed_row(pnl=-4.0, outcome="loss", exit_type="exit_stop"),
        ]
        report = build_report(rows, [])
        assert report["n_resolved"] == 1  # only the resolution feeds Brier/CI
        assert report["exit_buckets"]["n_early"] == 1
        assert report["exit_buckets"]["early"] == {"exit_stop": 1}
        # Early exits COUNT in P&L (D-B05) â total over both rows.
        assert report["total_net_pnl"] == pytest.approx(6.0)

    def test_explicit_resolved_tag_stays_in_the_sample(self):
        report = build_report([closed_row(exit_type="resolved")], [])
        assert report["n_resolved"] == 1
        assert report["exit_buckets"]["n_early"] == 0

    def test_exit_type_stats_buckets_every_tag(self):
        rows = [
            closed_row(),
            closed_row(exit_type="exit_stop"),
            closed_row(exit_type="exit_stop"),
            closed_row(exit_type="exit_swap"),
        ]
        stats = exit_type_stats(rows)
        assert stats["resolved"] == 1
        assert stats["early"] == {"exit_stop": 2, "exit_swap": 1}
        assert stats["n_early"] == 3

    def test_per_exit_type_buckets_with_pnl(self):
        rows = [
            closed_row(exit_type="exit_stop", pnl=-4.0),
            closed_row(exit_type="exit_stop", pnl=-2.0),
            closed_row(exit_type="exit_liquidity", pnl=1.0),
            closed_row(),  # resolution â never bucketed
        ]
        assert aggregate_per_exit_type(rows) == [
            {"exit_type": "exit_liquidity", "n": 1, "net_pnl": 1.0},
            {"exit_type": "exit_stop", "n": 2, "net_pnl": -6.0},
        ]

    def test_per_strategy_counts_early_exits(self):
        rows = [
            closed_row(strategy="recalibration"),
            closed_row(strategy="recalibration", exit_type="exit_stop"),
        ]
        agg = aggregate_per_strategy(rows)
        assert agg[0]["n"] == 2
        assert agg[0]["early_exits"] == 1

    def test_render_separates_early_exits_printed_never_merged(self):
        rows = [
            closed_row(),
            closed_row(pnl=-4.0, outcome="loss", exit_type="exit_stop"),
        ]
        report = build_report(rows, [])
        md = render_markdown(report, 7, datetime_now())
        assert "Exit accounting (D-B05" in md
        assert "exit_stop" in md
        assert "EXCLUDED from the agent-vs-market" in md
        assert "| exit_stop | 1 | -4.00 |" in md
        tg = render_telegram_summary(report, 7)
        assert "Early exits: 1" in tg

    def test_render_without_early_exits_still_prints_the_buckets(self):
        report = build_report([closed_row()], [])
        md = render_markdown(report, 7, datetime_now())
        assert "Early exits: 0" in md
        tg = render_telegram_summary(report, 7)
        assert "Early exits" not in tg  # the Telegram line stays quiet at zero
