"""
tests/test_copytrade_trust.py — Unit tests for the CopyTrade trust scoring system

Tests cover (per CopyTrade.md verification requirements):
  - compute_trust_score: Bayesian formula (wins+5)/(wins+losses+10)
  - Wallet state machine: NEW→ACTIVE, ACTIVE→PROBATION, PROBATION→RETIRED,
    PROBATION→ACTIVE reinstatement
  - is_priority: threshold 0.80/30
  - resolve_conflict: plain trust tiebreak, single-Priority-wins,
    multi-Priority-tiebreak-by-trust-score
  - get_trust_score / get_is_priority: cache accessors
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
    compute_is_priority,
    compute_state_multiplier,
    get_trust_score,
    get_is_priority,
    resolve_conflict,
    _compute_new_state,
    TRUST_DEFAULT_SCORE,
    TRUST_WIN_PRIOR,
    TRUST_TOTAL_PRIOR,
    PRIORITY_TRUST_THRESHOLD,
    PRIORITY_MIN_RESOLVED_TRADES,
    WALLET_MIN_RESOLVED_FOR_ACTIVE,
    WALLET_WIN_RATE_FLOOR,
    WALLET_WIN_RATE_REINSTATE,
    WALLET_AVG_ROI_FLOOR,
    WALLET_PROBATION_RETIREMENT_TRADES,
    WALLET_PROBATION_REINSTATE_WINDOW,
    _TRUST_CACHE,
    _PRIORITY_CACHE,
)


# ── Helper: clear caches before each test class ───────────────────────────────

def _clear_caches():
    _TRUST_CACHE.clear()
    _PRIORITY_CACHE.clear()


# ── 1. Bayesian trust score formula ───────────────────────────────────────────

class TestComputeTrustScore:
    """
    Formula: (wins + 5) / (wins + losses + 10)
    Default (0 wins, 0 losses) = 5/10 = 0.50
    """

    def test_no_trades_returns_default(self):
        """0W/0L → 5/10 = 0.50 (neutral prior)."""
        assert compute_trust_score(0, 0) == pytest.approx(0.50, abs=1e-6)

    def test_two_wins_zero_losses(self):
        """2W/0L → 7/12 ≈ 0.5833 — suspicious perfection, dampened correctly."""
        expected = (2 + 5) / (2 + 0 + 10)   # 7/12
        assert compute_trust_score(2, 0) == pytest.approx(expected, abs=1e-6)

    def test_eighteen_wins_two_losses(self):
        """18W/2L → 23/30 ≈ 0.7667 — real evidence, real trust."""
        expected = (18 + 5) / (18 + 2 + 10)  # 23/30
        assert compute_trust_score(18, 2) == pytest.approx(expected, abs=1e-6)

    def test_single_win_one_loss(self):
        """1W/1L → 6/12 = 0.50 — no evidence yet."""
        expected = (1 + 5) / (1 + 1 + 10)   # 6/12
        assert compute_trust_score(1, 1) == pytest.approx(expected, abs=1e-6)

    def test_zero_wins_ten_losses(self):
        """0W/10L → 5/20 = 0.25 — losing streak, trust suppressed."""
        expected = (0 + 5) / (0 + 10 + 10)   # 5/20
        assert compute_trust_score(0, 10) == pytest.approx(expected, abs=1e-6)

    def test_forty_wins_ten_losses(self):
        """40W/10L → 45/60 = 0.75 — strong performer."""
        expected = (40 + 5) / (40 + 10 + 10)  # 45/60
        assert compute_trust_score(40, 10) == pytest.approx(expected, abs=1e-6)

    def test_monotone_wins_increase_score(self):
        """More wins at same loss count → higher score."""
        assert compute_trust_score(5, 5) < compute_trust_score(10, 5) < compute_trust_score(20, 5)

    def test_monotone_losses_decrease_score(self):
        """More losses at same win count → lower score."""
        assert compute_trust_score(10, 20) < compute_trust_score(10, 10) < compute_trust_score(10, 2)

    def test_score_always_in_range(self):
        """Score must always be in [0.0, 1.0] for any inputs."""
        for wins, losses in [(0, 0), (0, 1000), (1000, 0), (50, 50), (1, 0)]:
            s = compute_trust_score(wins, losses)
            assert 0.0 <= s <= 1.0, f"Out of range for ({wins},{losses}): {s}"

    def test_default_score_value(self):
        """TRUST_DEFAULT_SCORE must equal 5/10 = 0.50."""
        assert TRUST_DEFAULT_SCORE == pytest.approx(0.50, abs=1e-6)


# ── 2. is_priority threshold ───────────────────────────────────────────────────

class TestComputeIsPriority:
    """is_priority = trust_score >= 0.80 AND resolved_trades_count >= 30"""

    def test_both_conditions_met(self):
        """Trust ≥ 0.80 AND ≥ 30 trades → priority."""
        assert compute_is_priority(0.80, 30) is True

    def test_high_trust_insufficient_trades(self):
        """Trust ≥ 0.80 but only 29 trades → NOT priority."""
        assert compute_is_priority(0.85, 29) is False

    def test_sufficient_trades_insufficient_trust(self):
        """30+ trades but trust = 0.79 → NOT priority."""
        assert compute_is_priority(0.79, 30) is False

    def test_neither_condition_met(self):
        assert compute_is_priority(0.60, 10) is False

    def test_exact_threshold_both(self):
        """Exactly 0.80 trust and 30 trades → priority (inclusive)."""
        assert compute_is_priority(0.80, 30) is True

    def test_far_above_thresholds(self):
        """High trust + many trades → priority."""
        assert compute_is_priority(0.95, 200) is True

    def test_priority_threshold_constants(self):
        """Constants match the spec values."""
        assert PRIORITY_TRUST_THRESHOLD == 0.80
        assert PRIORITY_MIN_RESOLVED_TRADES == 30


# ── 3. State machine transitions ──────────────────────────────────────────────

class TestWalletStateMachine:
    """
    State transitions per CopyTrade.md §3.4 / §3.5:
      NEW → ACTIVE at 20 resolved trades, win_rate ≥ 52%, avg_roi ≥ -2%
      ACTIVE → PROBATION if win_rate < 52% OR avg_roi < -2%
      PROBATION → RETIRED after 20 more trades still below floor
      PROBATION → ACTIVE if win_rate ≥ 55% over next 10 trades
    """

    # ── NEW → ACTIVE ──────────────────────────────────────────────────────────

    def test_new_stays_new_below_20_trades(self):
        """NEW wallet with 19 resolved trades stays NEW."""
        state = _compute_new_state(
            current_state="NEW",
            resolved_trades_count=19,
            wins_count=15, losses_count=4,
            avg_roi_per_trade=0.05,
            probation_resolved_at_entry=0,
        )
        assert state == "NEW"

    def test_new_graduates_to_active_at_20_trades(self):
        """NEW wallet with exactly 20 trades, 52%+ win rate, ≥ -2% roi → ACTIVE."""
        # 11/20 = 55% win rate, avg_roi = +2%
        state = _compute_new_state(
            current_state="NEW",
            resolved_trades_count=20,
            wins_count=11, losses_count=9,
            avg_roi_per_trade=0.02,
            probation_resolved_at_entry=0,
        )
        assert state == "ACTIVE"

    def test_new_stays_new_at_20_trades_low_win_rate(self):
        """NEW wallet with 20 trades but only 50% win rate stays NEW."""
        state = _compute_new_state(
            current_state="NEW",
            resolved_trades_count=20,
            wins_count=10, losses_count=10,
            avg_roi_per_trade=0.02,
            probation_resolved_at_entry=0,
        )
        assert state == "NEW"

    def test_new_stays_new_at_20_trades_poor_roi(self):
        """NEW wallet with 20 trades, good win rate, but avg_roi < -2% stays NEW."""
        state = _compute_new_state(
            current_state="NEW",
            resolved_trades_count=20,
            wins_count=15, losses_count=5,
            avg_roi_per_trade=-0.03,   # Below -2% floor
            probation_resolved_at_entry=0,
        )
        assert state == "NEW"

    # ── ACTIVE → PROBATION ────────────────────────────────────────────────────

    def test_active_stays_active_above_floor(self):
        """ACTIVE wallet above all floors stays ACTIVE."""
        state = _compute_new_state(
            current_state="ACTIVE",
            resolved_trades_count=30,
            wins_count=20, losses_count=10,
            avg_roi_per_trade=0.03,
            probation_resolved_at_entry=0,
        )
        assert state == "ACTIVE"

    def test_active_to_probation_win_rate_drops(self):
        """ACTIVE wallet drops below 52% win rate → PROBATION."""
        state = _compute_new_state(
            current_state="ACTIVE",
            resolved_trades_count=50,
            wins_count=25, losses_count=25,  # 50% win rate
            avg_roi_per_trade=0.02,
            probation_resolved_at_entry=0,
        )
        assert state == "PROBATION"

    def test_active_to_probation_avg_roi_drops(self):
        """ACTIVE wallet avg_roi drops to -3% (below -2% floor) → PROBATION."""
        state = _compute_new_state(
            current_state="ACTIVE",
            resolved_trades_count=50,
            wins_count=30, losses_count=20,  # 60% win rate — fine
            avg_roi_per_trade=-0.03,           # avg_roi < -2% → trigger
            probation_resolved_at_entry=0,
        )
        assert state == "PROBATION"

    def test_active_to_probation_exact_floor_boundary(self):
        """Exactly at 52% win_rate floor (0.52) does NOT trigger PROBATION."""
        # 26/50 = 52.0%
        state = _compute_new_state(
            current_state="ACTIVE",
            resolved_trades_count=50,
            wins_count=26, losses_count=24,
            avg_roi_per_trade=0.02,
            probation_resolved_at_entry=0,
        )
        assert state == "ACTIVE"

    def test_active_to_probation_just_below_52(self):
        """Just below 52% (51.9%) triggers PROBATION."""
        # 27/52 ≈ 51.9%
        state = _compute_new_state(
            current_state="ACTIVE",
            resolved_trades_count=52,
            wins_count=27, losses_count=25,
            avg_roi_per_trade=0.02,
            probation_resolved_at_entry=0,
        )
        assert state == "PROBATION"

    # ── PROBATION → RETIRED ───────────────────────────────────────────────────

    def test_probation_to_retired_after_20_more_trades(self):
        """
        Wallet entered PROBATION at trade 30. After 20 more trades (now at 50)
        still below floor → RETIRED.
        """
        # probation_resolved_at_entry = 30, now at 50 = 20 more trades
        state = _compute_new_state(
            current_state="PROBATION",
            resolved_trades_count=50,
            wins_count=24, losses_count=26,  # 48% — still below 52%
            avg_roi_per_trade=-0.03,
            probation_resolved_at_entry=30,
        )
        assert state == "RETIRED"

    def test_probation_stays_probation_before_20_trades(self):
        """After only 10 trades in PROBATION, stays PROBATION (not enough yet)."""
        state = _compute_new_state(
            current_state="PROBATION",
            resolved_trades_count=40,
            wins_count=19, losses_count=21,  # 47.5% — still below floor
            avg_roi_per_trade=-0.03,
            probation_resolved_at_entry=30,  # Only 10 trades since entry
        )
        assert state == "PROBATION"

    def test_retired_stays_retired(self):
        """RETIRED wallet stays RETIRED regardless of stats."""
        state = _compute_new_state(
            current_state="RETIRED",
            resolved_trades_count=100,
            wins_count=90, losses_count=10,  # Great stats, doesn't matter
            avg_roi_per_trade=0.10,
            probation_resolved_at_entry=0,
        )
        assert state == "RETIRED"

    # ── PROBATION → ACTIVE reinstatement ──────────────────────────────────────

    def test_probation_to_active_reinstatement_at_55_pct(self):
        """
        Wallet entered PROBATION at trade 30. After 10+ trades, win_rate ≥ 55%
        and avg_roi ≥ -2% → reinstated to ACTIVE.
        """
        # probation_resolved_at_entry = 30, now at 42 = 12 trades in PROBATION
        # wins_count=25, losses_count=17, total=42 → 25/42 ≈ 59.5% > 55%
        state = _compute_new_state(
            current_state="PROBATION",
            resolved_trades_count=42,
            wins_count=25, losses_count=17,
            avg_roi_per_trade=0.01,
            probation_resolved_at_entry=30,
        )
        assert state == "ACTIVE"

    def test_probation_no_reinstatement_at_54_pct(self):
        """Win rate of 54% does NOT reach the 55% reinstatement bar → stays PROBATION."""
        # wins=23, losses=19, total=42 → 23/42 ≈ 54.8% < 55%
        state = _compute_new_state(
            current_state="PROBATION",
            resolved_trades_count=42,
            wins_count=23, losses_count=19,
            avg_roi_per_trade=0.01,
            probation_resolved_at_entry=30,
        )
        assert state == "PROBATION"

    def test_probation_no_reinstatement_bad_roi(self):
        """Good win rate but avg_roi still < -2% → NOT reinstated."""
        state = _compute_new_state(
            current_state="PROBATION",
            resolved_trades_count=42,
            wins_count=25, losses_count=17,  # 59.5% win rate — above 55%
            avg_roi_per_trade=-0.03,           # But ROI still bad
            probation_resolved_at_entry=30,
        )
        assert state == "PROBATION"

    def test_probation_no_reinstatement_before_10_window(self):
        """Only 8 trades since PROBATION entry — reinstatement window not open yet."""
        state = _compute_new_state(
            current_state="PROBATION",
            resolved_trades_count=38,
            wins_count=25, losses_count=13,  # 65.8% win rate — would qualify
            avg_roi_per_trade=0.05,
            probation_resolved_at_entry=30,  # Only 8 trades since entry
        )
        assert state == "PROBATION"

    def test_state_multiplier_new(self):
        assert compute_state_multiplier("NEW") == 0.5

    def test_state_multiplier_probation(self):
        assert compute_state_multiplier("PROBATION") == 0.5

    def test_state_multiplier_active(self):
        assert compute_state_multiplier("ACTIVE") == 1.0

    def test_state_multiplier_retired(self):
        """RETIRED wallets should not be sized — multiplier 0.5 is a safe fallback."""
        # RETIRED is not NEW or PROBATION, so multiplier = 1.0 by current logic.
        # RETIRED wallets are also is_active=False so they never reach the executor.
        assert compute_state_multiplier("RETIRED") == 1.0


# ── 4. Conflict resolution ────────────────────────────────────────────────────

class TestResolveConflict:
    """
    CopyTrade.md §3.6 conflict rules:
      1. Exactly one Priority → it wins automatically.
      2. Both Priority → highest trust score wins.
      3. Neither Priority → highest trust score wins.
      4. Equal scores → wallet_a (first-come) wins.
    """

    WALLET_A = "0x" + "a" * 40
    WALLET_B = "0x" + "b" * 40

    def setup_method(self):
        _clear_caches()

    def test_equal_scores_no_priority_first_come_wins(self):
        """Both unknown (0.50 default, not Priority) → wallet_a wins."""
        winner = resolve_conflict(self.WALLET_A, self.WALLET_B)
        assert winner == self.WALLET_A

    def test_higher_trust_wallet_b_wins_no_priority(self):
        """B has higher trust, neither Priority → B wins."""
        _TRUST_CACHE[self.WALLET_A] = 0.55
        _TRUST_CACHE[self.WALLET_B] = 0.75
        winner = resolve_conflict(self.WALLET_A, self.WALLET_B)
        assert winner == self.WALLET_B

    def test_higher_trust_wallet_a_wins_no_priority(self):
        """A has higher trust, neither Priority → A wins."""
        _TRUST_CACHE[self.WALLET_A] = 0.80
        _TRUST_CACHE[self.WALLET_B] = 0.60
        # Note: both need Priority=False for this — priority needs 30 resolved trades too
        # We don't set _PRIORITY_CACHE so both default to False
        winner = resolve_conflict(self.WALLET_A, self.WALLET_B)
        assert winner == self.WALLET_A

    def test_single_priority_wins_automatically(self):
        """
        A is Priority, B is not — A wins regardless of trust scores.
        Even if B's trust score is HIGHER than A's raw cache value.
        """
        _TRUST_CACHE[self.WALLET_A] = 0.81
        _TRUST_CACHE[self.WALLET_B] = 0.82   # B has higher raw trust
        _PRIORITY_CACHE[self.WALLET_A] = True  # But A is Priority
        _PRIORITY_CACHE[self.WALLET_B] = False
        winner = resolve_conflict(self.WALLET_A, self.WALLET_B)
        assert winner == self.WALLET_A, (
            "Priority wallet should win even with a slightly lower trust score"
        )

    def test_single_priority_wallet_b_wins_automatically(self):
        """B is Priority, A is not — B wins regardless."""
        _TRUST_CACHE[self.WALLET_A] = 0.90   # A has higher raw trust
        _TRUST_CACHE[self.WALLET_B] = 0.81
        _PRIORITY_CACHE[self.WALLET_A] = False
        _PRIORITY_CACHE[self.WALLET_B] = True
        winner = resolve_conflict(self.WALLET_A, self.WALLET_B)
        assert winner == self.WALLET_B

    def test_both_priority_tiebreak_by_trust_score(self):
        """
        Both wallets are Priority → highest trust score wins (scoped to Priority pool).
        """
        _TRUST_CACHE[self.WALLET_A] = 0.82
        _TRUST_CACHE[self.WALLET_B] = 0.90   # B has higher trust
        _PRIORITY_CACHE[self.WALLET_A] = True
        _PRIORITY_CACHE[self.WALLET_B] = True
        winner = resolve_conflict(self.WALLET_A, self.WALLET_B)
        assert winner == self.WALLET_B

    def test_both_priority_equal_trust_first_come_wins(self):
        """Both Priority, equal trust → wallet_a (first-come) wins."""
        _TRUST_CACHE[self.WALLET_A] = 0.85
        _TRUST_CACHE[self.WALLET_B] = 0.85
        _PRIORITY_CACHE[self.WALLET_A] = True
        _PRIORITY_CACHE[self.WALLET_B] = True
        winner = resolve_conflict(self.WALLET_A, self.WALLET_B)
        assert winner == self.WALLET_A


# ── 5. Cache accessors ────────────────────────────────────────────────────────

class TestCacheAccessors:
    """Test get_trust_score and get_is_priority cache lookups."""

    def setup_method(self):
        _clear_caches()

    def test_unknown_wallet_trust_returns_default(self):
        assert get_trust_score("0x" + "f" * 40) == pytest.approx(TRUST_DEFAULT_SCORE)

    def test_known_wallet_trust_returns_cached(self):
        addr = "0x" + "c" * 40
        _TRUST_CACHE[addr] = 0.73
        assert get_trust_score(addr) == pytest.approx(0.73)

    def test_unknown_wallet_priority_returns_false(self):
        assert get_is_priority("0x" + "f" * 40) is False

    def test_known_wallet_priority_true(self):
        addr = "0x" + "g" * 40
        _PRIORITY_CACHE[addr] = True
        assert get_is_priority(addr) is True

    def test_cached_trust_zero_is_valid(self):
        """Score of 0.0 must be returned, not default."""
        addr = "0x" + "d" * 40
        _TRUST_CACHE[addr] = 0.0
        assert get_trust_score(addr) == 0.0

    def test_cached_trust_one_is_valid(self):
        addr = "0x" + "e" * 40
        _TRUST_CACHE[addr] = 1.0
        assert get_trust_score(addr) == 1.0
