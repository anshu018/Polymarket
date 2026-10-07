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
from dataclasses import dataclass
from typing import Optional, Sequence

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
