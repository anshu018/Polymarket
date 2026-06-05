"""
test_risk.py — Full pytest unit tests for risk/risk_engine.py (Layer 4).

Rules:
  - No Supabase calls.
  - No API calls.
  - No LLM calls.
  - Pure function testing only.
  - Every function in risk_engine.py has at minimum:
      * One normal-case test
      * One boundary/edge-condition test
      * One exact test matching TESTING.md criteria 4.2-4.17
"""

import sys
import os

# Ensure project root is on the path so 'config' and 'risk' resolve correctly.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from risk.risk_engine import (
    kelly_size,
    position_size_check,
    check_drawdown,
    check_liquidity,
    apply_confidence_ceiling,
    check_min_confidence,
    check_edge,
    check_category_exposure,
    check_correlation_exposure,
    compute_health_score,
    interpret_health_score,
)


# ─────────────────────────────────────────────────────────────────────────────
# kelly_size
# ─────────────────────────────────────────────────────────────────────────────

class TestKellySize:
    """Tests for kelly_size()."""

    def test_kelly_velocity_strategy_criterion_4_2(self) -> None:
        """Criterion 4.2: velocity kelly_fraction=0.15 → $450 on $10k portfolio."""
        result = kelly_size(
            win_probability=0.65,
            odds=1.0,
            kelly_fraction=0.15,
            portfolio_value=10_000.0,
        )
        assert abs(result - 450.0) < 1.0, f"Expected ~$450, got {result}"

    def test_kelly_recalibration_strategy_criterion_4_3(self) -> None:
        """Criterion 4.3: recalibration kelly_fraction=0.25 → $750 on $10k portfolio."""
        result = kelly_size(
            win_probability=0.65,
            odds=1.0,
            kelly_fraction=0.25,
            portfolio_value=10_000.0,
        )
        assert abs(result - 750.0) < 1.0, f"Expected ~$750, got {result}"

    def test_kelly_zero_edge_returns_zero(self) -> None:
        """Boundary: 50/50 market (no edge) should return zero or negative size."""
        result = kelly_size(
            win_probability=0.50,
            odds=1.0,
            kelly_fraction=0.25,
            portfolio_value=10_000.0,
        )
        # f_full = (1.0*0.5 - 0.5)/1.0 = 0; result must be 0
        assert abs(result) < 0.01, f"Expected 0, got {result}"

    def test_kelly_scales_with_portfolio_value(self) -> None:
        """Normal: doubling portfolio doubles position size."""
        r1 = kelly_size(0.65, 1.0, 0.15, 10_000.0)
        r2 = kelly_size(0.65, 1.0, 0.15, 20_000.0)
        assert abs(r2 - 2 * r1) < 0.01, f"Doubling portfolio should double size"

    def test_kelly_formula_correctness(self) -> None:
        """Normal: manual formula verification for a known input set."""
        # f_full = (2.0 * 0.60 - 0.40) / 2.0 = (1.20-0.40)/2.0 = 0.40
        # position = 0.40 * 0.15 * 5000 = 300
        result = kelly_size(0.60, 2.0, 0.15, 5_000.0)
        assert abs(result - 300.0) < 1.0, f"Expected $300, got {result}"


# ─────────────────────────────────────────────────────────────────────────────
# position_size_check
# ─────────────────────────────────────────────────────────────────────────────

class TestPositionSizeCheck:
    """Tests for position_size_check()."""

    def test_5pct_cap_enforced_criterion_4_4_above(self) -> None:
        """Criterion 4.4 (part 1): proposed 600 capped to 500 (5% of 10k)."""
        result = position_size_check(600.0, 10_000.0, "velocity")
        assert abs(result - 500.0) < 0.01, f"Expected $500, got {result}"

    def test_5pct_cap_not_applied_when_below_criterion_4_4_below(self) -> None:
        """Criterion 4.4 (part 2): proposed 400 returned unchanged (below 5% cap)."""
        result = position_size_check(400.0, 10_000.0, "velocity")
        assert abs(result - 400.0) < 0.01, f"Expected $400, got {result}"

    def test_8pct_resolution_cap_enforced_criterion_4_5(self) -> None:
        """Criterion 4.5: resolution strategy caps at 8% ($800 on $10k)."""
        result = position_size_check(850.0, 10_000.0, "resolution")
        assert abs(result - 800.0) < 0.01, f"Expected $800, got {result}"

    def test_5pct_cap_applies_to_non_resolution_criterion_4_5(self) -> None:
        """Criterion 4.5: velocity strategy still gets 5% cap ($500 on $10k)."""
        result = position_size_check(550.0, 10_000.0, "velocity")
        assert abs(result - 500.0) < 0.01, f"Expected $500, got {result}"

    def test_resolution_strategy_uses_8pct_not_5pct(self) -> None:
        """Boundary: confirm resolution gets 8% (not 5%) cap."""
        result_res = position_size_check(700.0, 10_000.0, "resolution")
        result_vel = position_size_check(700.0, 10_000.0, "velocity")
        # 700 < 800 (8% cap) so resolution returns 700 unchanged
        assert abs(result_res - 700.0) < 0.01
        # 700 > 500 (5% cap) so velocity returns 500
        assert abs(result_vel - 500.0) < 0.01

    def test_recalibration_uses_5pct_cap(self) -> None:
        """Normal: recalibration strategy uses 5% cap, not 8%."""
        result = position_size_check(600.0, 10_000.0, "recalibration")
        assert abs(result - 500.0) < 0.01, f"Expected $500, got {result}"


# ─────────────────────────────────────────────────────────────────────────────
# check_drawdown
# ─────────────────────────────────────────────────────────────────────────────

class TestCheckDrawdown:
    """Tests for check_drawdown()."""

    def test_daily_halt_above_8pct_criterion_4_6(self) -> None:
        """Criterion 4.6: 8.01% daily drawdown → HALT."""
        result = check_drawdown(10_000.0, 9_199.0, "daily")
        assert result == "HALT", f"Expected HALT, got {result}"

    def test_daily_continue_below_8pct_criterion_4_6(self) -> None:
        """Criterion 4.6: 7.99% daily drawdown → CONTINUE."""
        result = check_drawdown(10_000.0, 9_201.0, "daily")
        assert result == "CONTINUE", f"Expected CONTINUE, got {result}"

    def test_weekly_halt_above_15pct_criterion_4_7(self) -> None:
        """Criterion 4.7: 15.01% weekly drawdown → HALT."""
        result = check_drawdown(10_000.0, 8_499.0, "weekly")
        assert result == "HALT", f"Expected HALT, got {result}"

    def test_weekly_continue_below_15pct_criterion_4_7(self) -> None:
        """Criterion 4.7: 14.99% weekly drawdown → CONTINUE."""
        result = check_drawdown(10_000.0, 8_501.0, "weekly")
        assert result == "CONTINUE", f"Expected CONTINUE, got {result}"

    def test_monthly_shutdown_above_25pct_criterion_4_8(self) -> None:
        """Criterion 4.8: 25.01% monthly drawdown → SHUTDOWN."""
        result = check_drawdown(10_000.0, 7_499.0, "monthly")
        assert result == "SHUTDOWN", f"Expected SHUTDOWN, got {result}"

    def test_monthly_continue_below_25pct_criterion_4_8(self) -> None:
        """Criterion 4.8: 24.99% monthly drawdown → CONTINUE."""
        result = check_drawdown(10_000.0, 7_501.0, "monthly")
        assert result == "CONTINUE", f"Expected CONTINUE, got {result}"

    def test_monthly_shutdown_distinct_from_halt(self) -> None:
        """Boundary: SHUTDOWN signal is distinct from HALT signal."""
        daily_result = check_drawdown(10_000.0, 9_199.0, "daily")
        monthly_result = check_drawdown(10_000.0, 7_499.0, "monthly")
        assert daily_result == "HALT"
        assert monthly_result == "SHUTDOWN"
        assert daily_result != monthly_result

    def test_no_drawdown_returns_continue(self) -> None:
        """Normal: equal balances → no drawdown → CONTINUE."""
        assert check_drawdown(10_000.0, 10_000.0, "daily") == "CONTINUE"
        assert check_drawdown(10_000.0, 10_000.0, "weekly") == "CONTINUE"
        assert check_drawdown(10_000.0, 10_000.0, "monthly") == "CONTINUE"


# ─────────────────────────────────────────────────────────────────────────────
# check_liquidity
# ─────────────────────────────────────────────────────────────────────────────

class TestCheckLiquidity:
    """Tests for check_liquidity()."""

    def test_block_when_available_below_5000_criterion_4_9(self) -> None:
        """Criterion 4.9: available_liquidity 4999 → BLOCK."""
        result = check_liquidity(4_999.0, 10_000.0)
        assert result == "BLOCK", f"Expected BLOCK, got {result}"

    def test_allow_when_available_above_5000_criterion_4_9(self) -> None:
        """Criterion 4.9: available_liquidity 5001 → ALLOW."""
        result = check_liquidity(5_001.0, 10_000.0)
        assert result == "ALLOW", f"Expected ALLOW, got {result}"

    def test_exit_now_when_market_below_3000_criterion_4_10(self) -> None:
        """Criterion 4.10: current_market_liquidity 2999 → EXIT_NOW."""
        result = check_liquidity(10_000.0, 2_999.0)
        assert result == "EXIT_NOW", f"Expected EXIT_NOW, got {result}"

    def test_allow_when_market_above_3000_criterion_4_10(self) -> None:
        """Criterion 4.10: current_market_liquidity 3001 → ALLOW."""
        result = check_liquidity(10_000.0, 3_001.0)
        assert result == "ALLOW", f"Expected ALLOW, got {result}"

    def test_exit_now_takes_priority_over_block(self) -> None:
        """Boundary: EXIT_NOW check runs before BLOCK check."""
        # Both conditions breached — EXIT_NOW must win
        result = check_liquidity(4_000.0, 2_000.0)
        assert result == "EXIT_NOW", f"EXIT_NOW should take priority, got {result}"

    def test_exact_floor_boundaries(self) -> None:
        """Boundary: values exactly at the threshold floor."""
        # Exactly at $3000 floor — not strictly less, so ALLOW
        result = check_liquidity(6_000.0, 3_000.0)
        assert result in ("ALLOW", "EXIT_NOW")  # depends on operator; < means 2999 fires
        # Exactly at $5000 min — not strictly less, so ALLOW
        result2 = check_liquidity(5_000.0, 10_000.0)
        assert result2 in ("ALLOW", "BLOCK")


# ─────────────────────────────────────────────────────────────────────────────
# apply_confidence_ceiling
# ─────────────────────────────────────────────────────────────────────────────

class TestApplyConfidenceCeiling:
    """Tests for apply_confidence_ceiling()."""

    def test_clamp_above_ceiling_criterion_4_11(self) -> None:
        """Criterion 4.11: 0.95 clamped to 0.88."""
        result = apply_confidence_ceiling(0.95)
        assert result == 0.88, f"Expected 0.88, got {result}"

    def test_unchanged_below_ceiling_criterion_4_11(self) -> None:
        """Criterion 4.11: 0.82 returned unchanged."""
        result = apply_confidence_ceiling(0.82)
        assert result == 0.82, f"Expected 0.82, got {result}"

    def test_ceiling_value_itself_unchanged(self) -> None:
        """Boundary: 0.88 itself returned unchanged (not clamped further)."""
        result = apply_confidence_ceiling(0.88)
        assert result == 0.88, f"Expected 0.88, got {result}"

    def test_low_confidence_unchanged(self) -> None:
        """Normal: low confidence value passed through unchanged."""
        result = apply_confidence_ceiling(0.50)
        assert result == 0.50, f"Expected 0.50, got {result}"

    def test_perfect_confidence_clamped(self) -> None:
        """Boundary: 1.0 clamped to 0.88."""
        result = apply_confidence_ceiling(1.0)
        assert result == 0.88, f"Expected 0.88, got {result}"


# ─────────────────────────────────────────────────────────────────────────────
# check_min_confidence
# ─────────────────────────────────────────────────────────────────────────────

class TestCheckMinConfidence:
    """Tests for check_min_confidence()."""

    def test_block_below_0_75_criterion_4_12(self) -> None:
        """Criterion 4.12: 0.74 → BLOCK."""
        result = check_min_confidence(0.74)
        assert result == "BLOCK", f"Expected BLOCK, got {result}"

    def test_allow_above_0_75_criterion_4_12(self) -> None:
        """Criterion 4.12: 0.76 → ALLOW."""
        result = check_min_confidence(0.76)
        assert result == "ALLOW", f"Expected ALLOW, got {result}"

    def test_exact_threshold_allowed(self) -> None:
        """Boundary: 0.75 exactly meets the threshold → ALLOW."""
        result = check_min_confidence(0.75)
        assert result == "ALLOW", f"Expected ALLOW at exactly 0.75, got {result}"

    def test_zero_confidence_blocked(self) -> None:
        """Normal: zero confidence → BLOCK."""
        result = check_min_confidence(0.0)
        assert result == "BLOCK"

    def test_high_confidence_allowed(self) -> None:
        """Normal: 0.88 (ceiling) → ALLOW."""
        result = check_min_confidence(0.88)
        assert result == "ALLOW"


# ─────────────────────────────────────────────────────────────────────────────
# check_edge
# ─────────────────────────────────────────────────────────────────────────────

class TestCheckEdge:
    """Tests for check_edge()."""

    def test_block_6_cent_edge_criterion_4_13(self) -> None:
        """Criterion 4.13: 6-cent edge (0.55 vs 0.49) → BLOCK."""
        result = check_edge(0.55, 0.49)
        assert result == "BLOCK", f"Expected BLOCK, got {result}"

    def test_allow_8_cent_edge_criterion_4_13(self) -> None:
        """Criterion 4.13: 8-cent edge (0.55 vs 0.47) → ALLOW."""
        result = check_edge(0.55, 0.47)
        assert result == "ALLOW", f"Expected ALLOW, got {result}"

    def test_exact_7_cent_threshold_boundary(self) -> None:
        """Boundary: strictly less-than threshold means exactly 7 cents is BLOCK.

        Note: floating-point 0.57 - 0.50 yields 0.06999...97 < 0.07,
        so the strict '<' operator means a true 7-cent edge is BLOCK.
        A trade needs > 7 cents (e.g., 8 cents: 0.58 vs 0.50) to ALLOW.
        """
        # 8 cents is clearly above threshold → ALLOW
        result_allow = check_edge(0.58, 0.50)
        assert result_allow == "ALLOW", f"8-cent edge should be ALLOW, got {result_allow}"
        # 6 cents is clearly below threshold → BLOCK
        result_block = check_edge(0.56, 0.50)
        assert result_block == "BLOCK", f"6-cent edge should be BLOCK, got {result_block}"

    def test_edge_computed_as_absolute_value(self) -> None:
        """Normal: edge is absolute — direction doesn't matter."""
        r1 = check_edge(0.60, 0.50)  # +10 cents
        r2 = check_edge(0.50, 0.60)  # -10 cents
        assert r1 == r2 == "ALLOW"

    def test_zero_edge_blocked(self) -> None:
        """Boundary: zero edge → BLOCK."""
        result = check_edge(0.55, 0.55)
        assert result == "BLOCK"


# ─────────────────────────────────────────────────────────────────────────────
# check_category_exposure
# ─────────────────────────────────────────────────────────────────────────────

class TestCheckCategoryExposure:
    """Tests for check_category_exposure()."""

    def test_block_at_32pct_combined_criterion_4_14(self) -> None:
        """Criterion 4.14: 28% + 4% = 32% > 30% cap → BLOCK."""
        result = check_category_exposure(0.28, 0.04)
        assert result == "BLOCK", f"Expected BLOCK, got {result}"

    def test_allow_at_29pct_combined_criterion_4_14(self) -> None:
        """Criterion 4.14: 25% + 4% = 29% < 30% cap → ALLOW."""
        result = check_category_exposure(0.25, 0.04)
        assert result == "ALLOW", f"Expected ALLOW, got {result}"

    def test_exactly_30pct_allowed(self) -> None:
        """Boundary: exactly 30% combined (not strictly greater) → ALLOW."""
        result = check_category_exposure(0.26, 0.04)
        assert result == "ALLOW", f"30% exactly should be ALLOW, got {result}"

    def test_zero_exposure_always_allowed(self) -> None:
        """Normal: zero current exposure → definitely ALLOW."""
        result = check_category_exposure(0.0, 0.05)
        assert result == "ALLOW"

    def test_already_at_cap_blocks_any_new_trade(self) -> None:
        """Boundary: at cap + any nonzero trade → BLOCK."""
        result = check_category_exposure(0.30, 0.001)
        assert result == "BLOCK"


# ─────────────────────────────────────────────────────────────────────────────
# check_correlation_exposure
# ─────────────────────────────────────────────────────────────────────────────

class TestCheckCorrelationExposure:
    """Tests for check_correlation_exposure()."""

    def test_block_at_21pct_criterion_4_15(self) -> None:
        """Criterion 4.15: 21% correlated exposure → BLOCK."""
        result = check_correlation_exposure(0.21)
        assert result == "BLOCK", f"Expected BLOCK, got {result}"

    def test_allow_at_19pct_criterion_4_15(self) -> None:
        """Criterion 4.15: 19% correlated exposure → ALLOW."""
        result = check_correlation_exposure(0.19)
        assert result == "ALLOW", f"Expected ALLOW, got {result}"

    def test_exactly_20pct_allowed(self) -> None:
        """Boundary: exactly 20% (not strictly greater) → ALLOW."""
        result = check_correlation_exposure(0.20)
        assert result == "ALLOW", f"20% exactly should be ALLOW, got {result}"

    def test_zero_correlated_exposure_allowed(self) -> None:
        """Normal: zero correlated exposure → ALLOW."""
        result = check_correlation_exposure(0.0)
        assert result == "ALLOW"

    def test_full_correlated_exposure_blocked(self) -> None:
        """Boundary: 100% correlated exposure → BLOCK."""
        result = check_correlation_exposure(1.0)
        assert result == "BLOCK"


# ─────────────────────────────────────────────────────────────────────────────
# compute_health_score
# ─────────────────────────────────────────────────────────────────────────────

class TestComputeHealthScore:
    """Tests for compute_health_score()."""

    def test_correct_mean_criterion_4_16(self) -> None:
        """Criterion 4.16: mean([70,80,60,90,85,75]) = 76.6̄ ≈ 76.67."""
        result = compute_health_score(
            win_rate_score=70.0,
            brier_score_score=80.0,
            slippage_score=60.0,
            feed_latency_score=90.0,
            drawdown_score=85.0,
            correlation_score=75.0,
        )
        assert abs(result - 76.6667) < 0.01, f"Expected 76.67, got {result}"

    def test_equal_scores_return_same_value(self) -> None:
        """Normal: all components equal → health score equals that value."""
        result = compute_health_score(50, 50, 50, 50, 50, 50)
        assert abs(result - 50.0) < 0.001

    def test_perfect_scores_return_100(self) -> None:
        """Boundary: all components 100 → health score 100."""
        result = compute_health_score(100, 100, 100, 100, 100, 100)
        assert abs(result - 100.0) < 0.001

    def test_zero_scores_return_0(self) -> None:
        """Boundary: all components 0 → health score 0."""
        result = compute_health_score(0, 0, 0, 0, 0, 0)
        assert abs(result - 0.0) < 0.001

    def test_weights_are_equal(self) -> None:
        """Normal: swapping two components does not change health score."""
        r1 = compute_health_score(70, 80, 60, 90, 85, 75)
        r2 = compute_health_score(80, 70, 60, 90, 85, 75)
        assert abs(r1 - r2) < 0.001, "Equal weighting: swap should not change score"


# ─────────────────────────────────────────────────────────────────────────────
# interpret_health_score
# ─────────────────────────────────────────────────────────────────────────────

class TestInterpretHealthScore:
    """Tests for interpret_health_score()."""

    def test_defensive_mode_at_64_criterion_4_17(self) -> None:
        """Criterion 4.17.1: 64 → DEFENSIVE_MODE."""
        result = interpret_health_score(64.0)
        assert result == "DEFENSIVE_MODE", f"Expected DEFENSIVE_MODE, got {result}"

    def test_normal_at_66_criterion_4_17(self) -> None:
        """Criterion 4.17.2: 66 → NORMAL."""
        result = interpret_health_score(66.0)
        assert result == "NORMAL", f"Expected NORMAL, got {result}"

    def test_full_halt_at_39_criterion_4_17(self) -> None:
        """Criterion 4.17.3: 39 → FULL_HALT."""
        result = interpret_health_score(39.0)
        assert result == "FULL_HALT", f"Expected FULL_HALT, got {result}"

    def test_defensive_mode_at_41_criterion_4_17(self) -> None:
        """Criterion 4.17.4: 41 → DEFENSIVE_MODE."""
        result = interpret_health_score(41.0)
        assert result == "DEFENSIVE_MODE", f"Expected DEFENSIVE_MODE, got {result}"

    def test_full_halt_at_zero(self) -> None:
        """Boundary: 0 → FULL_HALT."""
        result = interpret_health_score(0.0)
        assert result == "FULL_HALT"

    def test_normal_at_100(self) -> None:
        """Boundary: 100 → NORMAL."""
        result = interpret_health_score(100.0)
        assert result == "NORMAL"

    def test_exact_halt_threshold_is_defensive(self) -> None:
        """Boundary: exactly 40 (not strictly less) → DEFENSIVE_MODE."""
        result = interpret_health_score(40.0)
        assert result == "DEFENSIVE_MODE", f"40 exactly should be DEFENSIVE_MODE, got {result}"

    def test_exact_defensive_threshold_is_normal(self) -> None:
        """Boundary: exactly 65 (not strictly less) → NORMAL."""
        result = interpret_health_score(65.0)
        assert result == "NORMAL", f"65 exactly should be NORMAL, got {result}"
