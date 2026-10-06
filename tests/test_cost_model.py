"""
tests/test_cost_model.py — Unit tests for risk/cost_model.py (List A.md Step 1).

Covers:
  1. Fee math at 2¢ / 50¢ / 98¢ with the verified schedule (politics 0.04, crypto 0.07,
     maker 0.0), and the operator's ~3.9%-of-trade-value-at-2¢ sanity check.
  2. Half-spread, conservative slippage rules (zero-depth and oversize fail-closed),
     maker haircut.
  3. net_edge sign correctness YES/NO and exact decomposition
     (net = gross − fee − spread − slippage − haircut).
  4. Fail-closed: zero book, crossed book, NaN inputs, unfillable size → net_edge = −1.
  5. check_net_edge: strict > MIN_NET_EDGE_CENTS, price band for takers only,
     maker band bypass.
  6. decide_order_type per-strategy config map (unknown → taker fail-safe).
  7. The DoD "2¢ taker-fee trap": a 7¢ gross edge at a 2¢ price is blocked.
"""

import math

import pytest
import config
from risk.cost_model import (
    BookSnapshot,
    CostBreakdown,
    FeeConfig,
    check_net_edge,
    compute_cost_breakdown,
    decide_order_type,
    expected_slippage_units,
    fee_config_for,
    half_spread_units,
    maker_fee_units,
    maker_fill_haircut_units,
    net_edge,
    taker_fee_units,
)

# Standard test fee config: politics rate (verified 0.04), maker free,
# 1¢ maker haircut, 1.2× slippage multiple — mirrors config defaults.
POLITICS = FeeConfig(taker_rate=0.04, maker_rate=0.0, maker_haircut=0.01, slippage_multiple=1.2)
CRYPTO = FeeConfig(taker_rate=0.07, maker_rate=0.0, maker_haircut=0.01, slippage_multiple=1.2)

# A sane, reasonably tight book: bid 0.48 / ask 0.52, $5,000 visible depth.
SANE_BOOK = BookSnapshot(best_bid=0.48, best_ask=0.52, depth_usd=5000.0)


# ── 1. Fee math (verified schedule) ───────────────────────────────────────────

class TestTakerFeeUnits:
    def test_politics_at_2_cents(self):
        # 0.04 × 0.02 × 0.98 = 0.000784/share → 3.92% of trade value (operator's figure)
        fee = taker_fee_units(0.02, POLITICS)
        assert fee == pytest.approx(0.04 * 0.02 * 0.98)
        assert fee / 0.02 == pytest.approx(0.0392)

    def test_politics_at_50_cents(self):
        assert taker_fee_units(0.50, POLITICS) == pytest.approx(0.04 * 0.25)

    def test_politics_at_98_cents(self):
        assert taker_fee_units(0.98, POLITICS) == pytest.approx(0.04 * 0.98 * 0.02)

    def test_crypto_peaks_at_175_cents_per_share(self):
        # Verified docs example: crypto peak = $1.75 on 100 shares = 0.0175/share
        assert taker_fee_units(0.50, CRYPTO) == pytest.approx(0.0175)

    def test_fee_is_symmetric_around_mid(self):
        assert taker_fee_units(0.02, POLITICS) == pytest.approx(taker_fee_units(0.98, POLITICS))

    def test_invalid_price_raises(self):
        with pytest.raises(ValueError):
            taker_fee_units(1.5, POLITICS)
        with pytest.raises(ValueError):
            taker_fee_units(-0.1, POLITICS)


class TestMakerFeeUnits:
    def test_maker_never_charged_with_verified_schedule(self):
        assert maker_fee_units(0.50, POLITICS) == pytest.approx(0.0)

    def test_invalid_price_raises(self):
        with pytest.raises(ValueError):
            maker_fee_units(1.2, POLITICS)


# ── 2. Spread, slippage, haircut ──────────────────────────────────────────────

class TestSpreadSlippageHaircut:
    def test_half_spread(self):
        assert half_spread_units(0.48, 0.52) == pytest.approx(0.02)

    def test_slippage_default_is_full_spread_times_multiple(self):
        slip = expected_slippage_units(0.0, 5000.0, 0.04, POLITICS)
        assert slip == pytest.approx(0.04 * 1.2)

    def test_slippage_zero_depth_fails_closed(self):
        assert expected_slippage_units(0.0, 0.0, 0.04, POLITICS) == 1.0
        assert expected_slippage_units(0.0, -1.0, 0.04, POLITICS) == 1.0

    def test_slippage_oversize_fails_closed(self):
        assert expected_slippage_units(5001.0, 5000.0, 0.04, POLITICS) == 1.0

    def test_slippage_size_equal_to_depth_is_fillable(self):
        assert expected_slippage_units(5000.0, 5000.0, 0.04, POLITICS) == pytest.approx(0.048)

    def test_maker_haircut_from_config(self):
        assert maker_fill_haircut_units(POLITICS) == pytest.approx(0.01)


# ── 3. Net edge math and sign correctness ─────────────────────────────────────

class TestNetEdge:
    def test_yes_taker_decomposition_exact(self):
        p, price = 0.60, 0.50
        breakdown = compute_cost_breakdown(
            p_model=p, price=price, side="YES", order_type="taker",
            book=SANE_BOOK, fee_cfg=POLITICS,
        )
        gross = p - price
        spread = 0.02
        fee = 0.04 * 0.52 * 0.48  # executed at the ask 0.52
        slip = 0.04 * 1.2
        assert breakdown.gross_edge == pytest.approx(gross)
        assert breakdown.spread_units == pytest.approx(spread)
        assert breakdown.fee_units == pytest.approx(fee)
        assert breakdown.slippage_units == pytest.approx(slip)
        assert breakdown.haircut_units == pytest.approx(0.0)
        assert breakdown.net_edge == pytest.approx(gross - fee - spread - slip)

    def test_no_taker_sign_correctness(self):
        """NO buy: (1−p) − (1−price) − costs — positive exactly when p < price."""
        p, price = 0.40, 0.50
        breakdown = compute_cost_breakdown(
            p_model=p, price=price, side="NO", order_type="taker",
            book=SANE_BOOK, fee_cfg=POLITICS,
        )
        gross = price - p
        fee = 0.04 * 0.52 * 0.48  # NO token bought at 1 − bid = 0.52
        assert breakdown.gross_edge == pytest.approx(gross)
        assert breakdown.fee_units == pytest.approx(fee)
        assert breakdown.net_edge == pytest.approx(gross - fee - 0.02 - 0.048)
        assert breakdown.net_edge > config.MIN_NET_EDGE_CENTS

    def test_no_side_negative_when_model_agrees_with_price(self):
        """A NO trade when the model says YES is 0.60 must have negative net edge."""
        breakdown = compute_cost_breakdown(
            p_model=0.60, price=0.50, side="NO", order_type="taker",
            book=SANE_BOOK, fee_cfg=POLITICS,
        )
        assert breakdown.net_edge < 0.0

    def test_yes_side_negative_when_model_agrees_with_price(self):
        breakdown = compute_cost_breakdown(
            p_model=0.40, price=0.50, side="YES", order_type="taker",
            book=SANE_BOOK, fee_cfg=POLITICS,
        )
        assert breakdown.net_edge < 0.0

    def test_maker_pays_no_spread_or_slippage_only_haircut(self):
        breakdown = compute_cost_breakdown(
            p_model=0.60, price=0.50, side="YES", order_type="maker",
            book=SANE_BOOK, fee_cfg=POLITICS,
        )
        assert breakdown.spread_units == pytest.approx(0.0)
        assert breakdown.slippage_units == pytest.approx(0.0)
        assert breakdown.fee_units == pytest.approx(0.0)
        assert breakdown.haircut_units == pytest.approx(0.01)
        assert breakdown.net_edge == pytest.approx(0.10 - 0.01)

    def test_net_edge_function_matches_breakdown(self):
        assert net_edge(
            p_model=0.60, price=0.50, side="YES", order_type="taker",
            book=SANE_BOOK, fee_cfg=POLITICS,
        ) == pytest.approx(
            compute_cost_breakdown(
                p_model=0.60, price=0.50, side="YES", order_type="taker",
                book=SANE_BOOK, fee_cfg=POLITICS,
            ).net_edge
        )

    def test_wide_spread_eats_the_edge(self):
        """A 7¢ gross edge against a wide book is fiction after spread + slippage."""
        wide_book = BookSnapshot(best_bid=0.20, best_ask=0.80, depth_usd=5000.0)
        value = net_edge(
            p_model=0.57, price=0.50, side="YES", order_type="taker",
            book=wide_book, fee_cfg=POLITICS,
        )
        assert value < 0.0


# ── 4. Fail-closed paths ──────────────────────────────────────────────────────

class TestFailClosed:
    def test_zero_book_blocked(self):
        zero_book = BookSnapshot(best_bid=0.0, best_ask=0.0, depth_usd=0.0)
        assert net_edge(
            p_model=0.60, price=0.50, side="YES", order_type="taker",
            book=zero_book, fee_cfg=POLITICS,
        ) == -1.0

    def test_crossed_book_blocked(self):
        crossed = BookSnapshot(best_bid=0.55, best_ask=0.45, depth_usd=5000.0)
        assert net_edge(
            p_model=0.60, price=0.50, side="YES", order_type="taker",
            book=crossed, fee_cfg=POLITICS,
        ) == -1.0

    def test_out_of_range_book_blocked(self):
        bad = BookSnapshot(best_bid=0.40, best_ask=1.0, depth_usd=5000.0)
        assert net_edge(
            p_model=0.60, price=0.50, side="YES", order_type="taker",
            book=bad, fee_cfg=POLITICS,
        ) == -1.0

    def test_nan_p_model_blocked(self):
        assert net_edge(
            p_model=math.nan, price=0.50, side="YES", order_type="taker",
            book=SANE_BOOK, fee_cfg=POLITICS,
        ) == -1.0

    def test_invalid_side_blocked(self):
        assert net_edge(
            p_model=0.60, price=0.50, side="MAYBE", order_type="taker",
            book=SANE_BOOK, fee_cfg=POLITICS,
        ) == -1.0

    def test_invalid_order_type_blocked(self):
        assert net_edge(
            p_model=0.60, price=0.50, side="YES", order_type="iceberg",
            book=SANE_BOOK, fee_cfg=POLITICS,
        ) == -1.0

    def test_unfillable_size_slippage_kills_edge(self):
        """$5,001 requested against $5,000 depth → fail-closed slippage → blocked."""
        breakdown = compute_cost_breakdown(
            p_model=0.60, price=0.50, side="YES", order_type="taker",
            book=SANE_BOOK, fee_cfg=POLITICS, size_usd=5001.0,
        )
        assert breakdown.slippage_units == 1.0
        assert breakdown.net_edge < 0.0


# ── 5. Gate ───────────────────────────────────────────────────────────────────

class TestCheckNetEdge:
    def test_allow_when_net_edge_above_minimum_and_in_band(self):
        assert check_net_edge(0.05, 0.50, "taker") == "ALLOW"

    def test_block_at_exact_minimum_strict_inequality(self):
        assert check_net_edge(config.MIN_NET_EDGE_CENTS, 0.50, "taker") == "BLOCK_NET_EDGE"

    def test_block_just_below_minimum(self):
        assert check_net_edge(config.MIN_NET_EDGE_CENTS - 0.001, 0.50, "taker") == "BLOCK_NET_EDGE"

    def test_block_fail_closed_on_nan(self):
        assert check_net_edge(math.nan, 0.50, "taker") == "BLOCK_NET_EDGE"

    def test_low_price_taker_blocked_by_band(self):
        assert check_net_edge(0.10, 0.02, "taker") == "BLOCK_PRICE_BAND"

    def test_high_price_taker_blocked_by_band(self):
        assert check_net_edge(0.10, 0.95, "taker") == "BLOCK_PRICE_BAND"

    def test_maker_bypasses_price_band(self):
        assert check_net_edge(0.10, 0.02, "maker") == "ALLOW"
        assert check_net_edge(0.10, 0.95, "maker") == "ALLOW"


# ── 6. Maker/taker rule ───────────────────────────────────────────────────────

class TestDecideOrderType:
    @pytest.mark.parametrize("strategy,expected", [
        ("velocity", "taker"),
        ("copy_edge_class_a", "taker"),
        ("recalibration", "maker"),
        ("resolution", "maker"),
        ("copy_edge_class_b", "maker"),
        ("unknown_strategy", "taker"),  # fail-safe: worst-case costs
        ("", "taker"),
    ])
    def test_strategy_map(self, strategy: str, expected: str):
        assert decide_order_type(strategy) == expected


# ── 7. Config plumbing + the DoD 2¢ trap ──────────────────────────────────────

class TestFeeConfigFor:
    def test_politics_rate_from_verified_map(self):
        assert fee_config_for("politics").taker_rate == pytest.approx(0.04)

    def test_category_lookup_case_insensitive(self):
        assert fee_config_for("CRYPTO").taker_rate == pytest.approx(0.07)

    def test_unknown_category_uses_default_rate(self):
        assert fee_config_for("quantum_aztec_history").taker_rate == pytest.approx(
            config.TAKER_FEE_RATE)

    def test_config_defaults_carried(self):
        cfg = fee_config_for("politics")
        assert cfg.maker_rate == pytest.approx(config.MAKER_FEE_RATE)
        assert cfg.maker_haircut == pytest.approx(config.MAKER_FILL_HAIRCUT)
        assert cfg.slippage_multiple == pytest.approx(config.SLIPPAGE_SPREAD_MULTIPLE)


class TestTwoCentTakerFeeTrap:
    def test_seven_cent_gross_edge_at_two_cents_is_blocked(self):
        """
        DoD: the 2¢ taker-fee trap is blocked. p_model 0.09 at a 2¢ market looks like
        a 7¢ gross edge; the net edge survives the costs (≈3.5¢) but the tradeable
        price band blocks the taker — extreme low prices carry structural adverse
        selection the gross gate never saw.
        """
        thin_book = BookSnapshot(best_bid=0.01, best_ask=0.03, depth_usd=5000.0)
        breakdown = compute_cost_breakdown(
            p_model=0.09, price=0.02, side="YES", order_type="taker",
            book=thin_book, fee_cfg=POLITICS,
        )
        # The net edge itself passes the 2¢ minimum...
        assert breakdown.net_edge == pytest.approx(
            0.07 - 0.001164 - 0.01 - 0.024, abs=1e-4)
        assert breakdown.net_edge > config.MIN_NET_EDGE_CENTS
        # ...but the band blocks the taker.
        assert check_net_edge(breakdown.net_edge, 0.02, "taker") == "BLOCK_PRICE_BAND"
