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


# ── derive_allocations (Step 3 — A7 allocation rule) ─────────────────────────

from risk.edge_monitor import StrategyAllocation, derive_allocations, neutral_allocation


def make_rows(edges) -> list[dict]:
    """Closed-trade rows (oldest first) carrying only what derivation reads."""
    return [{"net_edge_at_entry": e} for e in edges]


TWO_LEVEL_A = [0.02, 0.04] * 15   # mean 0.03, per-trade sharpe ≈ 2.95
TWO_LEVEL_B = [0.02, 0.06] * 15   # mean 0.04, per-trade sharpe ≈ 1.97


def test_derive_allocations_empty_input():
    assert derive_allocations({}) == {}


def test_below_min_samples_stays_unweighted():
    """n < REALLOC_MIN_SAMPLES → weight 1.0 regardless of edge quality."""
    alloc = derive_allocations({"s": make_rows([0.02] * 10)})["s"]
    assert alloc.n == 10
    assert alloc.weight == 1.0
    assert alloc.probation is False          # window incomplete (10 < 20)
    assert alloc.retired is False


def test_probation_fires_on_thin_edge_window():
    """Existing rule made live: mean net edge of the last 20 trades < 4¢."""
    alloc = derive_allocations({"s": make_rows([0.025] * 20)})["s"]
    assert alloc.probation is True
    assert alloc.weight == 1.0               # still below min samples


def test_no_probation_when_edge_healthy():
    alloc = derive_allocations({"s": make_rows([0.05] * 20)})["s"]
    assert alloc.probation is False


def test_retired_after_two_consecutive_probation_windows():
    alloc = derive_allocations({"s": make_rows([0.02] * 40)})["s"]
    assert alloc.retired is True
    assert alloc.probation is True


def test_one_bad_window_then_healthy_is_probation_not_retired():
    edges = [0.02] * 20 + [0.05] * 20        # latest window healthy
    alloc = derive_allocations({"s": make_rows(edges)})["s"]
    assert alloc.probation is False
    assert alloc.retired is False


def test_weight_proportional_to_rolling_sharpe_normalized_to_mean_one():
    allocs = derive_allocations({
        "a": make_rows(TWO_LEVEL_A),
        "b": make_rows(TWO_LEVEL_B),
    })
    sa = rolling_net_sharpe(TWO_LEVEL_A)
    sb = rolling_net_sharpe(TWO_LEVEL_B)
    assert sa > 0 and sb > 0
    # ∝ Sharpe: the weight ratio equals the Sharpe ratio...
    assert allocs["a"].weight / allocs["b"].weight == pytest.approx(sa / sb)
    # ...normalized so the mean weight across positive-Sharpe strategies is 1.0
    assert (allocs["a"].weight + allocs["b"].weight) / 2 == pytest.approx(1.0)


def test_negative_sharpe_eligible_strategy_gets_zero_weight():
    constant = [0.05] * 30                   # exactly constant → sharpe 0
    allocs = derive_allocations({
        "good": make_rows(TWO_LEVEL_A),
        "flat": make_rows(constant),
    })
    assert allocs["flat"].n == 30
    assert allocs["flat"].rolling_sharpe == 0.0
    assert allocs["flat"].weight == 0.0
    assert allocs["good"].weight > 0.0


def test_all_eligible_nonpositive_fall_back_to_unweighted():
    allocs = derive_allocations({
        "flat1": make_rows([0.05] * 30),
        "flat2": make_rows([0.03] * 30),
    })
    assert allocs["flat1"].weight == 1.0
    assert allocs["flat2"].weight == 1.0


def test_weight_clamped_at_realloc_max():
    """A tiny-variance strategy cannot take an unbounded multiplier."""
    sharp = [0.0495, 0.0505] * 15            # mean 0.05, sharpe ≈ 49
    allocs = derive_allocations({
        "sharp": make_rows(sharp),
        "b": make_rows(TWO_LEVEL_A),
        "c": make_rows(TWO_LEVEL_B),
    })
    assert allocs["sharp"].weight == config.REALLOC_WEIGHT_MAX
    # The others are scaled by the UNCLAMPED mean sharpe — cap applies per strategy.
    sa, sb, sc = (rolling_net_sharpe(s) for s in (sharp, TWO_LEVEL_A, TWO_LEVEL_B))
    assert allocs["b"].weight == pytest.approx(sb / ((sa + sb + sc) / 3))


def test_null_edge_trades_stay_unweighted_and_unjudged():
    """Class A semantics: trades without a cost model are never fabricated."""
    alloc = derive_allocations({"s": make_rows([None] * 40)})["s"]
    assert alloc.n == 0
    assert alloc.weight == 1.0
    assert alloc.probation is False
    assert alloc.retired is False


def test_cusum_fade_surfaced_on_allocation():
    edges = [0.05] * 30 + [-0.01] * 55       # persistent fade in the recent tail
    alloc = derive_allocations({"s": make_rows(edges)})["s"]
    assert alloc.faded is True
    assert alloc.cusum_score > config.CUSUM_THRESHOLD
    assert alloc.probation is True           # last 20 mean = −1¢


def test_neutral_allocation_shape():
    a = neutral_allocation("copy_edge_class_a")
    assert a == StrategyAllocation(
        strategy="copy_edge_class_a", n=0, rolling_net_edge=0.0, rolling_sharpe=0.0,
        weight=1.0, probation=False, retired=False, faded=False, cusum_score=0.0,
    )
