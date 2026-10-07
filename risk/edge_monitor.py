"""
risk/edge_monitor.py — A7: edge-fade monitor (List A.md Step 3).

Pure Python, zero I/O, zero LLM: the per-trade net edges (from the Step 3
attribution ledger, `closed_trades.net_edge_at_entry`) are passed in by the
caller. Every function is deterministic, under 1ms, and independently
unit-testable; all thresholds come from config.

What this module answers:
    - Is this strategy's edge still there, on the trades it actually closed?
      (`rolling_net_edge` — mean net edge over the trailing window.)
    - Is the edge fading — a persistent drift toward/below zero that a single
      noisy window would hide? (One-sided CUSUM, `cusum_fade`.)
    - How consistent is the edge? (`rolling_net_sharpe` — per-trade Sharpe of
      the net edge, the weight signal for the allocation rule.)

CUSUM model (one-sided, downward — tuned ONCE on synthetic series, recorded in
List A.md Decision Log D-10):

    S_0 = 0
    S_t = max(0, S_{t-1} + (−e_t − CUSUM_DRIFT))
    faded  ⇔  S_T > CUSUM_THRESHOLD at the end of the fed window (ongoing fade)
    score  =  max_t S_t (worst excursion within the window, observability)

    A healthy edge (e_t ≳ 0) pulls S toward 0; a persistent fade (e_t running
    below −CUSUM_DRIFT) accumulates S until it crosses CUSUM_THRESHOLD. With
    CUSUM_DRIFT = 0.005 a persistent −1¢/trade fade accumulates 0.5¢/trade and
    alarms within tens of trades, while a zero-edge run (e_t ~ N(0, σ)) has
    negative expected increment and stays at 0. The zero-reference is
    deliberate: MIN_NET_EDGE_CENTS (2¢) already guarantees entries carry
    positive modeled edge, so "consistently below ~0" IS the fade signature.

NULL handling: `net_edge_at_entry` is NULL on trades without a cost model
(e.g. Class A copies). A missing measurement is not a bad trade — NULLs (and
NaNs) are skipped, never treated as 0.
"""

import logging
import math
from dataclasses import dataclass, replace
from typing import Any, Optional, Sequence

import config

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CusumResult:
    """
    Outcome of the one-sided CUSUM over one strategy's trailing net edges.

    Attributes:
        faded:     True when the CUSUM statistic at the END of the fed window
                   exceeds CUSUM_THRESHOLD — the fade is ONGOING as of the
                   latest trade. A recovered edge decays S back to 0 and
                   clears the flag (old fades age out of the trailing window
                   the caller feeds).
        score:     The worst excursion — max_t S_t over the fed window.
                   Observability: a strategy can show score > 0 with
                   faded=False (a fade episode that has since recovered).
    """
    faded: bool
    score: float


def clean_edges(edges: Sequence[Optional[float]]) -> list[float]:
    """
    Drop non-measurements from a per-trade net-edge series.

    NULL (`net_edge_at_entry` absent — no cost model ran) and NaN (corrupt
    row) carry no evidence and are skipped, never coerced to 0 — treating a
    missing measurement as a zero-edge trade would fabricate a fade.

    Args:
        edges: Per-trade net edges in chronological order (oldest first).

    Returns:
        The measured (finite) edges, order preserved.
    """
    clean: list[float] = []
    for edge in edges:
        if edge is None:
            continue
        if isinstance(edge, float) and math.isnan(edge):
            continue
        clean.append(float(edge))
    return clean


def rolling_net_edge(edges: Sequence[Optional[float]], window: Optional[int] = None) -> float:
    """
    Mean net edge over the last `window` measured trades.

    The List A.md Step 3 signature's `window=30` default lives in config
    (`EDGE_ROLLING_WINDOW`) — config.py is the live truth for thresholds.

    Args:
        edges:  Per-trade net edges in chronological order; NULLs skipped.
        window: Trailing window size; None → config.EDGE_ROLLING_WINDOW.

    Returns:
        Mean of the last `window` measured edges; 0.0 when nothing measured.
    """
    w = window if window is not None else config.EDGE_ROLLING_WINDOW
    clean = clean_edges(edges)
    if not clean or w <= 0:
        return 0.0
    return sum(clean[-w:]) / min(w, len(clean))


def rolling_net_sharpe(edges: Sequence[Optional[float]], window: Optional[int] = None) -> float:
    """
    Per-trade Sharpe of the net edge over the last `window` measured trades:
    mean / sample std (ddof=1). Not annualized — the allocation rule (Step 3)
    only ever compares strategies on the same per-trade scale.

    Args:
        edges:  Per-trade net edges in chronological order; NULLs skipped.
        window: Trailing window size; None → config.EDGE_ROLLING_WINDOW.

    Returns:
        Per-trade Sharpe; 0.0 when fewer than 2 measured trades or an exactly
        constant series (zero real variance — float noise in the variance of
        repeated identical values must not read as infinite Sharpe).
    """
    w = window if window is not None else config.EDGE_ROLLING_WINDOW
    clean = clean_edges(edges)[-w:] if w > 0 else []
    n = len(clean)
    if n < 2:
        return 0.0
    if max(clean) == min(clean):
        return 0.0
    mean = sum(clean) / n
    variance = sum((e - mean) ** 2 for e in clean) / (n - 1)
    std = math.sqrt(variance)
    if std <= 0.0:
        return 0.0
    return mean / std


def cusum_fade(
    edges: Sequence[Optional[float]],
    threshold: Optional[float] = None,
    drift: Optional[float] = None,
) -> CusumResult:
    """
    One-sided downward CUSUM on per-trade net edges (module docstring has the
    recurrence and the tuning story — Decision Log D-10).

    Feed a TRAILING window (e.g. the strategy's last N closed trades): the
    flag answers "is the edge fading NOW, as of the latest trade in the
    window" — `faded` is S at the end of the series vs CUSUM_THRESHOLD, and
    `score` records the worst excursion. A recovered edge decays S back to 0
    and clears the flag; feeding the full history would blur that into "has
    this strategy EVER faded".

    Args:
        edges:     Per-trade net edges in chronological order; NULLs skipped.
        threshold: Alarm level; None → config.CUSUM_THRESHOLD.
        drift:     Allowance (the fade rate below 0 the monitor tolerates);
                   None → config.CUSUM_DRIFT.

    Returns:
        CusumResult(faded, score) — score = max_t S_t over the window,
        faded = S at the end of the window > threshold.
    """
    h = threshold if threshold is not None else config.CUSUM_THRESHOLD
    k = drift if drift is not None else config.CUSUM_DRIFT

    score = 0.0
    max_score = 0.0
    for edge in clean_edges(edges):
        score = max(0.0, score + (-edge - k))
        if score > max_score:
            max_score = score

    return CusumResult(faded=score > h, score=max_score)


# ── Allocation rule (mechanical — List A.md Step 3) ───────────────────────────

@dataclass(frozen=True)
class StrategyAllocation:
    """
    The mechanical allocation state of one strategy, derived from its closed
    trades (List A.md Step 3). The caller enforces it at the sizing read path:
    size ×= weight, then ×= config.PROBATION_SIZE_MULTIPLIER when probation;
    retired strategies are dropped before sizing.

    Attributes:
        strategy:         Strategy key.
        n:                Measured (non-NULL) net edges seen in the history.
        rolling_net_edge: Mean net edge over the trailing window.
        rolling_sharpe:   Per-trade net-edge Sharpe over the trailing window.
        weight:           Sharpe-proportional size multiplier (1.0 = unweighted
                          default below REALLOC_MIN_SAMPLES).
        probation:        Last STRATEGY_PROBATION_TRADE_COUNT trades mean net
                          edge < STRATEGY_PROBATION_EDGE_THRESHOLD_CENTS.
        retired:          Two consecutive complete probation windows failing.
                          A flag, never a deletion — trades stay in the ledger.
        faded:            CUSUM fade flag (observability only in Step 3).
        cusum_score:      Worst CUSUM excursion in the fed history.
    """
    strategy: str
    n: int
    rolling_net_edge: float
    rolling_sharpe: float
    weight: float
    probation: bool
    retired: bool
    faded: bool
    cusum_score: float


def neutral_allocation(strategy: str) -> StrategyAllocation:
    """
    The no-evidence state: unweighted default, no probation, not retired.

    Used for strategies with no closed trades yet and as the fail-open
    fallback when the ledger read fails (advisory throttle, not a capital
    gate — the hard gates stay fail-closed).
    """
    return StrategyAllocation(
        strategy=strategy, n=0, rolling_net_edge=0.0, rolling_sharpe=0.0,
        weight=1.0, probation=False, retired=False, faded=False, cusum_score=0.0,
    )


def _window_mean(edges: list[float], count: int) -> Optional[float]:
    """Mean of the last `count` edges; None while the window is incomplete."""
    if len(edges) < count:
        return None
    return sum(edges[-count:]) / count


def _in_probation(edges: list[float]) -> bool:
    """Existing rule made live: mean net edge of the last
    STRATEGY_PROBATION_TRADE_COUNT trades < STRATEGY_PROBATION_EDGE_THRESHOLD_CENTS."""
    mean = _window_mean(edges, config.STRATEGY_PROBATION_TRADE_COUNT)
    return mean is not None and mean < config.STRATEGY_PROBATION_EDGE_THRESHOLD_CENTS


def _is_retired(edges: list[float]) -> bool:
    """Two consecutive complete probation windows failing → retired (flag, never delete)."""
    count = config.STRATEGY_PROBATION_TRADE_COUNT
    threshold = config.STRATEGY_PROBATION_EDGE_THRESHOLD_CENTS
    latest = _window_mean(edges, count)
    previous = _window_mean(edges[:-count], count) if len(edges) >= 2 * count else None
    return (
        latest is not None and previous is not None
        and latest < threshold and previous < threshold
    )


def derive_allocations(
    trades_by_strategy: dict[str, Sequence[dict[str, Any]]],
) -> dict[str, StrategyAllocation]:
    """
    Derive the per-strategy allocation state from the closed-trades ledger.

    Pure math — the caller fetches rows (2s timeout, ALLOCATION_HISTORY_LIMIT
    cap) and groups them by strategy, OLDEST FIRST (chronological by
    closed_at); each row needs `net_edge_at_entry` (NULLs are skipped — they
    carry no measurement; a strategy whose trades all lack edges, e.g. the
    estimate-free Class A copy path, stays unweighted and unjudged).

    Weight rules (mechanical, config-driven):
        - n < REALLOC_MIN_SAMPLES               → weight 1.0 (default).
        - n ≥ REALLOC_MIN_SAMPLES ("eligible")  → weight = sharpe / mean(sharpe
          of positive-Sharpe eligible strategies), clamped to
          [0, REALLOC_WEIGHT_MAX]; Sharpe ≤ 0 → 0.0; when NO eligible strategy
          has a positive Sharpe, all eligible fall back to 1.0 (probation and
          retirement then do the throttling).

    Args:
        trades_by_strategy: strategy → closed-trade rows, oldest first.

    Returns:
        One StrategyAllocation per strategy present in the input.
    """
    allocations: dict[str, StrategyAllocation] = {}
    for strategy, trades in trades_by_strategy.items():
        edges = clean_edges([t.get("net_edge_at_entry") for t in trades])
        cusum = cusum_fade(edges)
        allocations[strategy] = StrategyAllocation(
            strategy=strategy,
            n=len(edges),
            rolling_net_edge=rolling_net_edge(edges),
            rolling_sharpe=rolling_net_sharpe(edges),
            weight=1.0,
            probation=_in_probation(edges),
            retired=_is_retired(edges),
            faded=cusum.faded,
            cusum_score=cusum.score,
        )

    eligible = {
        strategy: alloc for strategy, alloc in allocations.items()
        if alloc.n >= config.REALLOC_MIN_SAMPLES
    }
    positive = {
        strategy: alloc.rolling_sharpe for strategy, alloc in eligible.items()
        if alloc.rolling_sharpe > 0.0
    }
    if positive:
        mean_sharpe = sum(positive.values()) / len(positive)
        for strategy, sharpe in positive.items():
            weight = min(sharpe / mean_sharpe, config.REALLOC_WEIGHT_MAX)
            allocations[strategy] = replace(allocations[strategy], weight=weight)
        for strategy in eligible:
            if strategy not in positive:
                allocations[strategy] = replace(allocations[strategy], weight=0.0)
    else:
        # No eligible strategy earns capital on its own Sharpe — fall back to
        # the unweighted default for all of them instead of starving everyone.
        for strategy in eligible:
            allocations[strategy] = replace(allocations[strategy], weight=1.0)

    return allocations
