"""
tests/test_copytrade_trust.py — Unit tests for the CopyTrade trust scoring system

Tests cover:
  - compute_trust_score formula: neutral default, Bayesian dampening, PnL bonus, caps.
  - resolve_conflict: picks higher-trust wallet, ties go to wallet_a.
  - get_trust_score: returns default for unknown wallets, cached score for known ones.
  - _TRUST_CACHE: cache is writable and readable correctly.
  - Edge cases: 0 trades, all wins, all losses, large PnL.
"""

import os
import sys

# Set required env vars before any project import
os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "test_key")
os.environ.setdefault("OPENROUTER_API_KEY", "test_key")
os.environ.setdefault("NVIDIA_API_KEY", "test_key")
os.environ.setdefault("DEEPSEEK_API_KEY", "test_key")
os.environ.setdefault("POLYMARKET_PRIVATE_KEY", "0x" + "a" * 64)
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test_token")
os.environ.setdefault("TELEGRAM_CHAT_ID", "12345")
os.environ.setdefault("SILICONFLOW_API_KEY", "test_key")

import pytest
from copytrade.performance_tracker import (
    compute_trust_score,
    get_trust_score,
    resolve_conflict,
    TRUST_DEFAULT_SCORE,
    TRUST_CALIBRATION_TRADES,
    TRUST_PNL_BONUS_CAP,
    _TRUST_CACHE,
)


class TestComputeTrustScore:
    """Unit tests for the core Bayesian trust score formula."""

    def setup_method(self):
        """Clear cache before each test."""
        _TRUST_CACHE.clear()

    def test_no_trades_returns_default(self):
        """Zero trades → neutral 0.50 (unknown, not distrusted)."""
        assert compute_trust_score(0, 0, 0.0) == pytest.approx(TRUST_DEFAULT_SCORE)

    def test_single_win_barely_above_neutral(self):
        """1/1 wins should be barely above 0.5 — not trusted yet."""
        score = compute_trust_score(1, 1, 0.0)
        # confidence = 1/20 = 0.05; base = (1.0 * 0.05) + (0.5 * 0.95) = 0.525
        assert score == pytest.approx(0.525, abs=0.001)

    def test_single_loss_barely_below_neutral(self):
        """1/1 losses should be barely below 0.5 — not distrusted yet."""
        score = compute_trust_score(0, 1, 0.0)
        # confidence = 1/20 = 0.05; base = (0.0 * 0.05) + (0.5 * 0.95) = 0.475
        assert score == pytest.approx(0.475, abs=0.001)

    def test_full_confidence_reached_at_calibration_trades(self):
        """At TRUST_CALIBRATION_TRADES, confidence = 1.0 so win_rate = score."""
        n = TRUST_CALIBRATION_TRADES
        # 80% win rate, no PnL bonus → trust_score ≈ 0.80
        score = compute_trust_score(int(n * 0.8), n, 0.0)
        assert score == pytest.approx(0.80, abs=0.001)

    def test_beyond_calibration_confidence_caps_at_1(self):
        """100 trades still has confidence = 1.0 (not > 1.0)."""
        score = compute_trust_score(80, 100, 0.0)
        # confidence = min(100/20, 1) = 1.0; base = 0.80
        assert score == pytest.approx(0.80, abs=0.001)

    def test_perfect_record_approaches_1_not_reaches(self):
        """100% win rate with 100 trades + max PnL bonus → ≤ 1.0."""
        score = compute_trust_score(100, 100, 10000.0)
        assert score <= 1.0
        assert score >= 0.90  # Should be high

    def test_terrible_record_approaches_0_not_below(self):
        """0% win rate with 100 trades + max PnL loss → ≥ 0.0."""
        score = compute_trust_score(0, 100, -10000.0)
        assert score >= 0.0
        assert score <= 0.10  # Should be low

    def test_pnl_bonus_positive(self):
        """Positive PnL adds bonus to trust score (below cap)."""
        base = compute_trust_score(10, 20, 0.0)
        # $50 PnL: bonus = min(0.10, 50/1000) = 0.05 (below cap, exact)
        with_pnl = compute_trust_score(10, 20, 50.0)
        assert with_pnl > base
        assert with_pnl == pytest.approx(base + 0.05, abs=0.001)

    def test_pnl_bonus_capped(self):
        """PnL bonus cannot exceed TRUST_PNL_BONUS_CAP (+10%)."""
        base = compute_trust_score(10, 20, 0.0)
        with_huge_pnl = compute_trust_score(10, 20, 1_000_000.0)
        assert with_huge_pnl == pytest.approx(base + TRUST_PNL_BONUS_CAP, abs=0.001)

    def test_pnl_penalty_capped(self):
        """PnL penalty cannot exceed TRUST_PNL_BONUS_CAP (-10%)."""
        base = compute_trust_score(10, 20, 0.0)
        with_huge_loss = compute_trust_score(10, 20, -1_000_000.0)
        assert with_huge_loss == pytest.approx(base - TRUST_PNL_BONUS_CAP, abs=0.001)

    def test_score_always_in_range(self):
        """Trust score must always be in [0.0, 1.0]."""
        for wins, total, pnl in [
            (0, 0, 0), (0, 1, -1e6), (1, 1, 1e6),
            (100, 100, 1e6), (0, 100, -1e6),
        ]:
            score = compute_trust_score(wins, total, pnl)
            assert 0.0 <= score <= 1.0, f"Out of range for ({wins},{total},{pnl}): {score}"

    def test_monotone_in_win_rate(self):
        """Higher win rates (same trades) should produce higher trust scores."""
        score_20pct = compute_trust_score(4, 20, 0.0)
        score_50pct = compute_trust_score(10, 20, 0.0)
        score_80pct = compute_trust_score(16, 20, 0.0)
        assert score_20pct < score_50pct < score_80pct

    def test_monotone_in_trade_count(self):
        """Same 80% win rate but more trades → closer to true win rate."""
        low_confidence = compute_trust_score(8, 10, 0.0)   # 8/10, little data
        high_confidence = compute_trust_score(80, 100, 0.0)  # 80/100, lots of data
        # Both are 80% win rate, but high_confidence score ≈ 0.80 (closer to reality)
        # low_confidence score is closer to 0.5 (dampened)
        assert high_confidence > low_confidence


class TestResolveConflict:
    """Unit tests for conflict resolution between wallets."""

    WALLET_A = "0x" + "a" * 40
    WALLET_B = "0x" + "b" * 40

    def setup_method(self):
        _TRUST_CACHE.clear()

    def test_equal_scores_first_come_wins(self):
        """Both wallets unknown → wallet_a (first-come) wins."""
        winner = resolve_conflict(self.WALLET_A, self.WALLET_B)
        assert winner == self.WALLET_A

    def test_higher_trust_wallet_b_wins(self):
        """Wallet B with higher trust score beats wallet A."""
        _TRUST_CACHE[self.WALLET_A] = 0.50
        _TRUST_CACHE[self.WALLET_B] = 0.75
        winner = resolve_conflict(self.WALLET_A, self.WALLET_B)
        assert winner == self.WALLET_B

    def test_higher_trust_wallet_a_wins(self):
        """Wallet A with higher trust score beats wallet B."""
        _TRUST_CACHE[self.WALLET_A] = 0.80
        _TRUST_CACHE[self.WALLET_B] = 0.60
        winner = resolve_conflict(self.WALLET_A, self.WALLET_B)
        assert winner == self.WALLET_A

    def test_both_high_trust_higher_one_wins(self):
        """Between two well-trusted wallets, the better one wins."""
        _TRUST_CACHE[self.WALLET_A] = 0.77
        _TRUST_CACHE[self.WALLET_B] = 0.82
        winner = resolve_conflict(self.WALLET_A, self.WALLET_B)
        assert winner == self.WALLET_B

    def test_both_low_trust_first_come_wins(self):
        """Both with equal low scores → wallet_a wins."""
        _TRUST_CACHE[self.WALLET_A] = 0.30
        _TRUST_CACHE[self.WALLET_B] = 0.30
        winner = resolve_conflict(self.WALLET_A, self.WALLET_B)
        assert winner == self.WALLET_A


class TestGetTrustScore:
    """Unit tests for trust score cache lookup."""

    def setup_method(self):
        _TRUST_CACHE.clear()

    def test_unknown_wallet_returns_default(self):
        assert get_trust_score("0x" + "f" * 40) == TRUST_DEFAULT_SCORE

    def test_known_wallet_returns_cached_score(self):
        addr = "0x" + "c" * 40
        _TRUST_CACHE[addr] = 0.73
        assert get_trust_score(addr) == pytest.approx(0.73)

    def test_cached_score_zero_is_valid(self):
        """Score of 0.0 (completely distrusted) must be returned, not default."""
        addr = "0x" + "d" * 40
        _TRUST_CACHE[addr] = 0.0
        assert get_trust_score(addr) == 0.0

    def test_cached_score_one_is_valid(self):
        """Score of 1.0 must be returned, not default."""
        addr = "0x" + "e" * 40
        _TRUST_CACHE[addr] = 1.0
        assert get_trust_score(addr) == 1.0
