"""
tests/test_fill_simulator.py — unit tests for risk/fill_simulator.py (Step 5).

Pure math only: no DB, no network, no mocks needed. Every side transform
(YES-line ↔ NO-line mirroring at 1 − p) is tested explicitly — the NO-side
mirror logic is where a sign slip would silently book impossible fills.
"""

import pytest

from risk.cost_model import BookSnapshot, FeeConfig
from risk.fill_simulator import (
    maker_fallback_net_edge,
    maker_is_filled,
    maker_resting_price,
    signed_slippage_units,
    taker_depth_ok,
    taker_fill_price,
)

SANE_BOOK = BookSnapshot(best_bid=0.48, best_ask=0.52, depth_usd=5000.0)
THIN_BOOK = BookSnapshot(best_bid=0.48, best_ask=0.52, depth_usd=100.0)
POLITICS = FeeConfig(
    taker_rate=0.04,
    maker_rate=0.0,
    maker_haircut=0.01,
    slippage_multiple=1.2,
)


class TestTakerFillPrice:
    def test_yes_fills_at_the_ask(self):
        assert taker_fill_price(SANE_BOOK, "YES") == 0.52

    def test_no_fills_at_the_mirror_of_the_yes_bid(self):
        # NO ask = 1 − YES bid = 0.52... on a 0.48/0.52 book both touches are 0.52;
        # use an asymmetric book to prove the mirroring, not coincidence.
        book = BookSnapshot(best_bid=0.40, best_ask=0.55, depth_usd=5000.0)
        assert taker_fill_price(book, "NO") == pytest.approx(0.60)

    def test_yes_uses_the_ask_not_the_bid_on_asymmetric_book(self):
        book = BookSnapshot(best_bid=0.40, best_ask=0.55, depth_usd=5000.0)
        assert taker_fill_price(book, "YES") == 0.55

    @pytest.mark.parametrize("side", ["YES", "NO"])
    def test_invalid_book_fails_closed(self, side):
        crossed = BookSnapshot(best_bid=0.60, best_ask=0.40, depth_usd=100.0)
        assert taker_fill_price(crossed, side) is None

    def test_unknown_side_fails_closed(self):
        assert taker_fill_price(SANE_BOOK, "yes") is None
        assert taker_fill_price(SANE_BOOK, "BUY") is None


class TestTakerDepthOk:
    def test_size_within_depth_ok(self):
        assert taker_depth_ok(SANE_BOOK, 4000.0) is True

    def test_size_exactly_at_depth_ok(self):
        assert taker_depth_ok(SANE_BOOK, 5000.0) is True

    def test_size_above_depth_rejected(self):
        assert taker_depth_ok(THIN_BOOK, 101.0) is False

    def test_zero_depth_rejects_any_real_size(self):
        empty = BookSnapshot(best_bid=0.48, best_ask=0.52, depth_usd=0.0)
        assert taker_depth_ok(empty, 1.0) is False

    def test_non_positive_size_vacuously_ok(self):
        assert taker_depth_ok(SANE_BOOK, 0.0) is True
        assert taker_depth_ok(THIN_BOOK, -5.0) is True


class TestMakerRestingPrice:
    def test_yes_rests_at_the_bid(self):
        assert maker_resting_price(SANE_BOOK, "YES") == 0.48

    def test_no_rests_at_the_mirror_of_the_yes_ask(self):
        book = BookSnapshot(best_bid=0.40, best_ask=0.55, depth_usd=5000.0)
        assert maker_resting_price(book, "NO") == pytest.approx(0.45)

    @pytest.mark.parametrize("side", ["YES", "NO"])
    def test_invalid_book_fails_closed(self, side):
        zero_bid = BookSnapshot(best_bid=0.0, best_ask=0.52, depth_usd=100.0)
        assert maker_resting_price(zero_bid, side) is None

    def test_unknown_side_fails_closed(self):
        assert maker_resting_price(SANE_BOOK, "MAKER") is None


class TestMakerIsFilled:
    def test_yes_not_filled_on_unchanged_book(self):
        # Rested a YES buy at the bid 0.48; the book is unchanged (ask 0.52) —
        # the market never came to us. Resting at the touch does NOT jump the queue.
        assert maker_is_filled(SANE_BOOK, "YES", 0.48) is False

    def test_yes_fills_when_ask_trades_down_to_level(self):
        # Rested at 0.48; the ask later traded down to 0.48 → a seller crossed to us.
        came_to_us = BookSnapshot(best_bid=0.44, best_ask=0.48, depth_usd=5000.0)
        assert maker_is_filled(came_to_us, "YES", 0.48) is True

    def test_yes_fill_when_ask_trades_strictly_through_level(self):
        # The ask crashed THROUGH the resting level (0.49 < 0.50) — still a fill.
        through = BookSnapshot(best_bid=0.45, best_ask=0.49, depth_usd=5000.0)
        assert maker_is_filled(through, "YES", 0.50) is True

    def test_no_not_filled_on_unchanged_book(self):
        # NO buy rested at 0.45 (mirror of YES ask 0.55); the YES bid is still
        # 0.48 < 0.55 → the NO ask (0.52) is above our level. No fill.
        assert maker_is_filled(SANE_BOOK, "NO", 0.45) is False

    def test_no_fills_when_yes_bid_rises_through_mirror(self):
        # NO buy rested at 0.48 (mirror of YES ask 0.52). The YES bid later rises
        # to 0.52 → NO ask = 0.48 = our level → a NO seller hit our bid.
        ran_up = BookSnapshot(best_bid=0.52, best_ask=0.56, depth_usd=5000.0)
        assert maker_is_filled(ran_up, "NO", 0.48) is True

    def test_resting_price_out_of_range_raises(self):
        with pytest.raises(ValueError):
            maker_is_filled(SANE_BOOK, "YES", 0.0)
        with pytest.raises(ValueError):
            maker_is_filled(SANE_BOOK, "NO", 1.0)
        with pytest.raises(ValueError):
            maker_is_filled(SANE_BOOK, "YES", float("nan"))

    def test_invalid_current_book_reads_as_no_fill(self):
        crossed = BookSnapshot(best_bid=0.70, best_ask=0.30, depth_usd=100.0)
        assert maker_is_filled(crossed, "YES", 0.48) is False

    def test_round_trip_resting_level_is_the_fill_level(self):
        # The level the order rested at is exactly the level whose touch fills it:
        # move each touch to its resting level and both sides must fill.
        for side in ("YES", "NO"):
            resting = maker_resting_price(SANE_BOOK, side)
            assert resting is not None
            assert maker_is_filled(SANE_BOOK, side, resting) is False
            if side == "YES":
                reached = BookSnapshot(best_bid=0.44, best_ask=resting, depth_usd=5000.0)
            else:
                reached = BookSnapshot(best_bid=1.0 - resting, best_ask=0.56, depth_usd=5000.0)
            assert maker_is_filled(reached, side, resting) is True


class TestMakerFallbackNetEdge:
    def test_positive_edge_when_estimate_clears_current_book(self):
        # p_model 0.65 vs mid 0.50, tight book: gross 15¢ ≫ costs → positive.
        assert maker_fallback_net_edge(0.65, SANE_BOOK, "YES", POLITICS) > 0.0

    def test_negative_when_price_overran_estimate(self):
        # The market ran from 0.50 to 0.64 mid since entry: a YES taker now pays
        # the ask 0.66 for a 0.65 estimate → net edge must be negative.
        moved = BookSnapshot(best_bid=0.62, best_ask=0.66, depth_usd=5000.0)
        assert maker_fallback_net_edge(0.65, moved, "YES", POLITICS) < 0.0

    def test_no_side_symmetry_on_same_book(self):
        # YES-line p=0.35 for a NO buy on a 0.48/0.52 book mirrors p=0.65 YES.
        no_edge = maker_fallback_net_edge(0.35, SANE_BOOK, "NO", POLITICS)
        yes_edge = maker_fallback_net_edge(0.65, SANE_BOOK, "YES", POLITICS)
        assert no_edge == pytest.approx(yes_edge)

    def test_fail_closed_on_invalid_inputs(self):
        assert maker_fallback_net_edge(1.5, SANE_BOOK, "YES", POLITICS) == -1.0
        assert maker_fallback_net_edge(0.65, SANE_BOOK, "SIDEWAYS", POLITICS) == -1.0
        crossed = BookSnapshot(best_bid=0.9, best_ask=0.1, depth_usd=100.0)
        assert maker_fallback_net_edge(0.65, crossed, "YES", POLITICS) == -1.0


class TestSignedSlippage:
    def test_yes_taker_pays_half_spread_over_mid(self):
        # Decision at mid 0.50, fill at ask 0.52 → +2¢ adverse.
        assert signed_slippage_units(0.52, 0.50, "YES") == pytest.approx(0.02)

    def test_no_taker_pays_half_spread_over_mirror(self):
        # Decision YES-mid 0.50 → NO reference 0.50; NO fill at 0.52 = +2¢ adverse.
        assert signed_slippage_units(0.52, 0.50, "NO") == pytest.approx(0.02)

    def test_maker_fill_at_improved_price_is_negative(self):
        # NO buy rested at 0.45 (mirror of YES ask 0.55) and filled: 5¢ better
        # than the NO reference 0.50 → −5¢ (price improvement).
        assert signed_slippage_units(0.45, 0.50, "NO") == pytest.approx(-0.05)

    def test_yes_price_improvement_negative(self):
        assert signed_slippage_units(0.49, 0.50, "YES") == pytest.approx(-0.01)

    def test_invalid_inputs_fail_closed(self):
        assert signed_slippage_units(0.0, 0.50, "YES") is None
        assert signed_slippage_units(1.0, 0.50, "YES") is None
        assert signed_slippage_units(0.52, 1.5, "YES") is None
        assert signed_slippage_units(0.52, 0.50, "LONG") is None
        assert signed_slippage_units(float("nan"), 0.50, "YES") is None
