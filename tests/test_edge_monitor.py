"""
tests/test_edge_monitor.py — Unit tests for risk/edge_monitor.py (List A.md
Step 3 — A7). Pure math on synthetic per-trade net-edge series; seeded RNG for
the noise scenarios so the suite is fully deterministic.

The CUSUM parameters under test are the TUNED values from config
(Decision Log D-10): a 30-trade zero-edge run must NOT trigger, a persistent
small-negative drift MUST, a healthy edge must never be flagged.
"""

import random

import pytest
import config
from risk.edge_monitor import (
    CusumResult,
    clean_edges,
    cusum_fade,
    rolling_net_edge,
    rolling_net_sharpe,
)


def noisy_series(seed: int, mean: float, n: int, sigma: float = 0.03) -> list[float]:
    """Deterministic synthetic per-trade net-edge series (Decision Log D-10 noise scale)."""
    rng = random.Random(seed)
    return [rng.gauss(mean, sigma) for _ in range(n)]


# ── clean_edges ───────────────────────────────────────────────────────────────

def test_clean_edges_drops_none_and_nan_keeps_measurements():
    series = [0.05, None, float("nan"), 0.03, 0.04]
    assert clean_edges(series) == [0.05, 0.03, 0.04]


def test_clean_edges_handles_ints_and_empty():
    assert clean_edges([]) == []
    assert clean_edges([1, 2]) == [1.0, 2.0]  # ledger numerics may decode as int


# ── rolling_net_edge ──────────────────────────────────────────────────────────

def test_rolling_net_edge_mean_of_full_window():
    series = [0.02, 0.04, 0.06]
    assert rolling_net_edge(series, window=3) == pytest.approx(0.04)


def test_rolling_net_edge_is_trailing():
    series = [0.10, 0.02, 0.04, 0.06]
    assert rolling_net_edge(series, window=2) == pytest.approx(0.05)  # last two


def test_rolling_net_edge_skips_nulls_not_zero_them():
    """A NULL measurement is not a zero-edge trade."""
    series = [0.10, None, None, 0.06]
    assert rolling_net_edge(series, window=4) == pytest.approx(0.08)


def test_rolling_net_edge_empty_series_is_zero():
    assert rolling_net_edge([], window=30) == 0.0
    assert rolling_net_edge([None, None], window=30) == 0.0


def test_rolling_net_edge_default_window_is_config():
    assert config.EDGE_ROLLING_WINDOW == 30
    series = [9.99] + [0.01] * 29 + [0.31]  # 31st value (oldest) outside default window
    assert rolling_net_edge(series) == pytest.approx(0.02)  # mean of the last 30


# ── rolling_net_sharpe ────────────────────────────────────────────────────────

def test_rolling_net_sharpe_mean_over_sample_std():
    series = [0.02, 0.04, 0.06, 0.08]  # mean 0.05
    mean = 0.05
    var = ((0.02 - mean) ** 2 + (0.04 - mean) ** 2 + (0.06 - mean) ** 2 + (0.08 - mean) ** 2) / 3
    assert rolling_net_sharpe(series, window=4) == pytest.approx(mean / var ** 0.5)


def test_rolling_net_sharpe_degenerate_cases_are_zero():
    assert rolling_net_sharpe([], window=30) == 0.0
    assert rolling_net_sharpe([0.05], window=30) == 0.0           # n < 2
    assert rolling_net_sharpe([0.05, 0.05, 0.05], window=30) == 0.0  # zero variance


# ── cusum_fade — tuned behavior (Decision Log D-10) ──────────────────────────

def test_exact_zero_edge_run_never_triggers():
    """Spec criterion: a 30-trade zero-edge run must NOT trigger (exact zero)."""
    result = cusum_fade([0.0] * 30)
    assert result.faded is False
    assert result.score == 0.0


def test_noisy_zero_edge_runs_do_not_trigger():
    """Spec criterion under realistic noise (seeds verified safe at the tuned
    operating point; the sweep false-trigger rate is ~2-3%)."""
    for seed in range(8):
        result = cusum_fade(noisy_series(seed, mean=0.0, n=30))
        assert result.faded is False, f"seed {seed} falsely triggered (score={result.score})"


def test_persistent_small_negative_drift_triggers():
    """Spec criterion: a persistent −1¢/trade fade MUST trigger. Flat series
    accumulates 0.5¢/trade and crosses the 0.25 threshold around trade 50
    (the exact boundary trade is float-sensitive — assert the bracket)."""
    before = cusum_fade([-0.01] * 49)
    assert before.faded is False
    at_alarm = cusum_fade([-0.01] * 51)
    assert at_alarm.faded is True
    assert at_alarm.score == pytest.approx(0.255)


def test_noisy_persistent_fades_trigger():
    """Seeded noisy −1¢/trade fades are still flagged at the end of a 120-trade
    window (persistent — S keeps accumulating)."""
    for seed in range(8):
        result = cusum_fade(noisy_series(seed, mean=-0.01, n=120))
        assert result.faded is True, f"seed {seed} fade missed (score={result.score})"


def test_healthy_edge_never_triggers():
    """A steady +5¢ edge over 100 trades must never be flagged."""
    for seed in range(8):
        result = cusum_fade(noisy_series(seed, mean=0.05, n=100))
        assert result.faded is False, f"seed {seed} healthy flagged (score={result.score})"


def test_recovery_resets_the_cusum():
    """A fade followed by a recovered edge clears the flag: S decays back to 0
    on healthy trades, so `faded` (S at the end) is False even though `score`
    still records the worst excursion within the fed window."""
    faded_then_recovered = [-0.01] * 50 + [0.05] * 20
    result = cusum_fade(faded_then_recovered)
    assert result.faded is False
    assert result.score == pytest.approx(0.25)  # the fade episode is still visible


def test_ongoing_fade_stays_flagged_at_window_end():
    """A fade that has not recovered at the end of the fed window is flagged —
    `faded` answers 'is it fading NOW'."""
    result = cusum_fade([-0.01] * 55)
    assert result.faded is True   # S = 0.275, still accumulating
    assert result.score == pytest.approx(0.275)


def test_cusum_skips_nulls():
    """NULL entries carry no evidence: they neither accumulate nor reset S."""
    with_nulls = [0.05, None] * 10
    without = [0.05] * 10
    assert cusum_fade(with_nulls).score == pytest.approx(cusum_fade(without).score)


def test_cusum_explicit_params_override_config():
    """threshold/drift kwargs exist for the tuning harness; None → config."""
    result = cusum_fade([-0.01] * 10, threshold=100.0, drift=0.005)
    assert result.faded is False
    assert isinstance(result, CusumResult)
