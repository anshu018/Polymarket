"""
scripts/gate_report.py — A5: the honest gate report (List A.md Step 5).

Weekly offline report + Telegram. THE GATE PASSES OR FAILS ON PRINTED NUMBERS —
never on judgment. It prints exactly the conditions List A.md §5 Step 5c froze:

    PASS requires ALL of:
      1. n resolved samples >= config.GATE_MIN_RESOLVED_SAMPLES (composite:
         backtest + paper — backtest data does not exist until Step 6, so the
         report states the composition explicitly),
      2. bootstrap 95% CI lower bound of mean per-thesis net P&L > 0
         (thesis-cluster bootstrap: clusters count once — Step 5d),
      3. agent Brier < market Brier on the identical resolved sample, with
         non-overlapping bootstrap CIs (overlap → EDGE_CASE verdict, printed,
         never silently promoted to a pass).

Data sources:
  - closed_trades  — net P&L, thesis clusters, per-strategy breakdown, agent
                     (p_model_at_entry) vs market (market_price_at_entry) Brier
                     on the identical traded sample, decision-vs-fill slippage
                     (fill_price, fill_type), maker/taker ratio.
  - signal_outcomes — the market baseline on ALL evaluated signals (traded or
                     not): price_t0 is the market's own probability at decision
                     time (recorded since Step 2); resolved_outcome is backfilled
                     HERE (offline, idempotent) from the Gamma API.

Script rules (CLAUDE.md): loads .env.test (override=True) BEFORE any project
import; pure core / I/O split; markdown → scratch/; graceful DB failure →
exit 1; Telegram via monitoring.telegram_alerts._send_to_telegram.
"""

import argparse
import asyncio
import json
import logging
import random
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv

# Make the script runnable from any CWD (`python scripts/gate_report.py`):
# put the repo root on sys.path so `import config` resolves like a project module.
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# CLAUDE.md rule: scripts load .env.test (override=True) BEFORE any project import.
load_dotenv(dotenv_path=".env.test", override=True)

import config  # noqa: E402
from risk.fill_simulator import signed_slippage_units  # noqa: E402

logger = logging.getLogger("gate_report")

_GAMMA_MARKET_URL = f"{config.GAMMA_API_URL}/markets"
# Per-run cap on resolution backfill fetches: the report must finish in bounded
# time; unresolved markets beyond the cap stay NULL and are picked up next week.
_DEFAULT_MAX_RESOLUTION_FETCHES = 200


# ── Pure core: numeric coercion ───────────────────────────────────────────────

def to_float(value: Any) -> Optional[float]:
    """Coerce a DB numeric (often DECIMAL-as-string) to float; None on failure.

    Every ledger numeric passes through here before math — a "NaN" string or
    None must never poison an aggregate (the clean_edges lesson, Step 3)."""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    # NaN compares False to everything — treat as missing.
    return f if f == f else None


# ── Pure core: bootstrap statistics ───────────────────────────────────────────

def cluster_values(rows: list[dict[str, Any]]) -> list[list[float]]:
    """Group rows' net P&L into clusters by thesis_id (Step 5d: clusters count
    once). Rows without a thesis_id become singleton clusters — they still
    count, honestly, as their own sample."""
    clusters: dict[str, list[float]] = {}
    for i, row in enumerate(rows):
        value = to_float(row.get("pnl_usdc"))
        if value is None:
            continue
        thesis = row.get("thesis_id")
        key = str(thesis) if thesis else f"__singleton_{i}"
        clusters.setdefault(key, []).append(value)
    return list(clusters.values())


def cluster_mean_bootstrap_ci(
    rows: list[dict[str, Any]],
    resamples: int = config.GATE_BOOTSTRAP_RESAMPLES,
    seed: int = config.GATE_BOOTSTRAP_SEED,
    level: float = 0.95,
) -> Optional[tuple[float, float]]:
    """Bootstrap CI of the mean PER-THESIS net P&L (cluster bootstrap).

    Resampling thesis clusters (not trades) keeps correlated trades within one
    event thread from faking sample size — the anti-double-counting rule. The
    total net P&L is printed alongside; the CI is on the mean because a CI on a
    fixed total is degenerate (the total either is or is not positive)."""
    clusters = cluster_values(rows)
    if not clusters:
        return None
    cluster_totals = [sum(c) for c in clusters]
    if resamples <= 0 or not 0.0 < level < 1.0:
        return None
    n = len(cluster_totals)
    rng = random.Random(seed + 1)
    alpha = (1.0 - level) / 2.0
    means: list[float] = []
    for _ in range(resamples):
        total = 0.0
        for _ in range(n):
            total += cluster_totals[rng.randrange(n)]
        means.append(total / n)
    means.sort()
    lo_idx = max(0, int(alpha * resamples))
    hi_idx = min(resamples - 1, int((1.0 - alpha) * resamples) - 1)
    return means[lo_idx], means[hi_idx]


# ── Pure core: Brier (agent vs market on the identical sample) ────────────────

def yes_line_outcome(row: dict[str, Any]) -> Optional[int]:
    """Map a closed trade's (direction, outcome) to the YES-line resolution.

    win & YES → 1, win & NO → 0, loss & YES → 0, loss & NO → 1. Anything else
    (voided, ambiguous, missing) → None — the row leaves the Brier sample."""
    outcome = (row.get("outcome") or "").strip().lower()
    direction = (row.get("direction") or "").strip().upper()
    if outcome not in ("win", "loss") or direction not in ("YES", "NO"):
        return None
    won = outcome == "win"
    if direction == "YES":
        return 1 if won else 0
    return 0 if won else 1


def brier_pairs(rows: list[dict[str, Any]]) -> Optional[list[tuple[float, float, float]]]:
    """(agent_p, market_p, yes_outcome) triples on the identical resolved sample.

    Agent prediction = p_model_at_entry; market prediction = market_price_at_entry
    (the book mid the decision math used). Rows missing either side are excluded
    whole — the sample stays IDENTICAL for both predictors (Class A rows carry
    p_model NULL and leave honestly, never with a fabricated probability)."""
    pairs: list[tuple[float, float, float]] = []
    for row in rows:
        outcome = yes_line_outcome(row)
        agent_p = to_float(row.get("p_model_at_entry"))
        market_p = to_float(row.get("market_price_at_entry"))
        if outcome is None or agent_p is None or market_p is None:
            continue
        pairs.append((agent_p, market_p, float(outcome)))
    return pairs or None


def mean_brier(values: list[float], outcomes: list[int]) -> float:
    """Mean Brier score mean((p − o)^2); raises on mismatch (caller contract)."""
    if len(values) != len(outcomes):
        raise ValueError("predictions/outcomes length mismatch")
    if not values:
        return 0.0
    return statistics.fmean((p - o) ** 2 for p, o in zip(values, outcomes))


def brier_bootstrap_ci(
    triples: list[tuple[float, float, float]],
    pick: str,
    resamples: int = config.GATE_BOOTSTRAP_RESAMPLES,
    seed: int = config.GATE_BOOTSTRAP_SEED,
    level: float = 0.95,
) -> Optional[tuple[float, float]]:
    """Bootstrap CI of the mean Brier score over the shared sample.

    pick='agent' uses element 0, pick='market' element 1; the SAME resampled
    indices are NOT required across the two calls — the non-overlap check
    compares the marginal distributions on the identical underlying sample."""
    if not triples or pick not in ("agent", "market"):
        return None
    idx = 0 if pick == "agent" else 1
    values = [t[idx] for t in triples]
    outcomes = [int(t[2]) for t in triples]
    if resamples <= 0 or not 0.0 < level < 1.0:
        return None
    n = len(values)
    rng = random.Random(seed + (2 if pick == "agent" else 3))
    alpha = (1.0 - level) / 2.0
    scores: list[float] = []
    for _ in range(resamples):
        total = 0.0
        for _ in range(n):
            j = rng.randrange(n)
            total += (values[j] - outcomes[j]) ** 2
        scores.append(total / n)
    scores.sort()
    lo_idx = max(0, int(alpha * resamples))
    hi_idx = min(resamples - 1, int((1.0 - alpha) * resamples) - 1)
    return scores[lo_idx], scores[hi_idx]


# ── Pure core: slippage / order-type stats ────────────────────────────────────

def slippage_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Decision-vs-fill slippage over CLOSED trades (Step 5a dataset).

    Signed units via risk.fill_simulator.signed_slippage_units (fill is on the
    traded token's line, market_price_at_entry is the YES-line decision mid;
    positive = paid worse). NULL fills (resting/cancelled) leave the sample."""
    observed: list[float] = []
    for row in rows:
        fill = to_float(row.get("fill_price"))
        decision = to_float(row.get("market_price_at_entry"))
        direction = (row.get("direction") or "").strip().upper()
        if fill is None or decision is None:
            continue
        slip = signed_slippage_units(fill, decision, direction)
        if slip is not None:
            observed.append(slip)
    if not observed:
        return {"n": 0, "mean": None, "median": None, "max_adverse": None}
    return {
        "n": len(observed),
        "mean": statistics.fmean(observed),
        "median": statistics.median(observed),
        "max_adverse": max(observed),
    }


def order_type_stats(rows: list[dict[str, Any]]) -> dict[str, int]:
    """Actual fill-type counts over closed trades (maker/taker ratio)."""
    counts = {"taker": 0, "maker_fill": 0, "taker_fallback": 0, "other_or_null": 0}
    for row in rows:
        fill_type = (row.get("fill_type") or "").strip()
        if fill_type in counts:
            counts[fill_type] += 1
        else:
            counts["other_or_null"] += 1
    return counts


def thesis_dedup_count(rows: list[dict[str, Any]]) -> dict[str, int]:
    """Distinct thesis count vs raw rows (Step 5d visibility)."""
    theses = {str(r["thesis_id"]) for r in rows if r.get("thesis_id")}
    null_thesis_rows = sum(1 for r in rows if not r.get("thesis_id"))
    return {"rows": len(rows), "distinct_theses": len(theses), "null_thesis_rows": null_thesis_rows}


def aggregate_per_strategy(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per-strategy: n, net P&L, mean net edge, agent-vs-market Brier on that
    strategy's own resolved sample (Class A renders honestly as no-Brier)."""
    strategies: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        strategy = (row.get("strategy") or "unknown").strip() or "unknown"
        strategies.setdefault(strategy, []).append(row)
    out: list[dict[str, Any]] = []
    for strategy, strat_rows in strategies.items():
        pnl_values = [v for v in (to_float(r.get("pnl_usdc")) for r in strat_rows) if v is not None]
        edge_values = [v for v in (to_float(r.get("net_edge_at_entry")) for r in strat_rows) if v is not None]
        pairs = brier_pairs(strat_rows)
        agent_brier = mean_brier([p[0] for p in pairs], [int(p[2]) for p in pairs]) if pairs else None
        market_brier = mean_brier([p[1] for p in pairs], [int(p[2]) for p in pairs]) if pairs else None
        out.append({
            "strategy": strategy,
            "n": len(strat_rows),
            "net_pnl": sum(pnl_values) if pnl_values else None,
            "mean_net_edge": statistics.fmean(edge_values) if edge_values else None,
            "agent_brier": agent_brier,
            "market_brier": market_brier,
        })
    out.sort(key=lambda s: s["strategy"])
    return out


# ── Pure core: the gate itself ────────────────────────────────────────────────

def evaluate_gate(
    n_resolved: int,
    pnl_ci: Optional[tuple[float, float]],
    agent_brier: Optional[float],
    market_brier: Optional[float],
    agent_ci: Optional[tuple[float, float]],
    market_ci: Optional[tuple[float, float]],
) -> dict[str, Any]:
    """The gate verdict, strictly from printed numbers (List A.md §5 Step 5c).

    PASS requires ALL of:
      1. n_resolved >= config.GATE_MIN_RESOLVED_SAMPLES,
      2. pnl_ci lower bound > 0,
      3. agent_brier < market_brier AND non-overlapping CIs (agent_hi < market_lo).
    Condition 3 with overlapping CIs → the report says EDGE_CASE, never PASS."""
    failed: list[str] = []
    sample_ok = n_resolved >= config.GATE_MIN_RESOLVED_SAMPLES
    if not sample_ok:
        failed.append(
            f"n={n_resolved} < GATE_MIN_RESOLVED_SAMPLES={config.GATE_MIN_RESOLVED_SAMPLES}"
        )
    pnl_ok = pnl_ci is not None and pnl_ci[0] > 0.0
    if not pnl_ok:
        failed.append("net P&L 95% CI lower bound is not > 0")
    brier_ok = (
        agent_brier is not None
        and market_brier is not None
        and agent_brier < market_brier
        and agent_ci is not None
        and market_ci is not None
        and agent_ci[1] < market_ci[0]
    )
    brier_edge_case = (
        agent_brier is not None
        and market_brier is not None
        and agent_brier < market_brier
        and not brier_ok
    )
    if not brier_ok:
        failed.append(
            "agent Brier is not below market Brier with non-overlapping CIs"
        )
    verdict = "PASS" if not failed else "FAIL"
    return {
        "verdict": verdict,
        "failed_conditions": failed,
        "sample_ok": sample_ok,
        "pnl_ok": pnl_ok,
        "brier_ok": brier_ok,
        "brier_edge_case": brier_edge_case,
    }


# ── Pure core: rendering ──────────────────────────────────────────────────────

def _fmt(value: Optional[float], digits: int = 4, suffix: str = "") -> str:
    return "—" if value is None else f"{value:.{digits}f}{suffix}"


def _fmt_ci(ci: Optional[tuple[float, float]], digits: int = 4) -> str:
    return "—" if ci is None else f"[{ci[0]:.{digits}f}, {ci[1]:.{digits}f}]"


def render_markdown(
    report: dict[str, Any],
    window_days: int,
    generated_at: datetime,
) -> str:
    """The full weekly markdown report (scratch/ output)."""
    lines: list[str] = []
    lines.append("# ZERO-ALPHA Honest Gate Report (List A.md Step 5 — A5)")
    lines.append("")
    lines.append(f"Generated: {generated_at.isoformat()} | Window: last {window_days} days")
    lines.append(f"Sample composition: {report['sample_composition']}")
    lines.append("")
    lines.append("## GATE VERDICT")
    lines.append("")
    lines.append(f"**GATE: {report['verdict']}**")
    if report["failed_conditions"]:
        lines.append("")
        lines.append("Failed conditions:")
        for cond in report["failed_conditions"]:
            lines.append(f"- {cond}")
    if report["brier_edge_case"]:
        lines.append("")
        lines.append(
            "Documented edge case: agent Brier IS below market Brier but the "
            "bootstrap CIs overlap — not sufficient evidence at this sample size."
        )
    lines.append("")
    lines.append("## Gate conditions (printed numbers only)")
    lines.append("")
    lines.append(f"| Condition | Value | Threshold |")
    lines.append("|---|---|---|")
    lines.append(
        f"| Resolved samples (n) | {report['n_resolved']} | >= {config.GATE_MIN_RESOLVED_SAMPLES} |"
    )
    lines.append(
        f"| Total net P&L | {_fmt(report['total_net_pnl'], 2)} USDC | > 0 |"
    )
    lines.append(
        f"| Mean per-thesis net P&L + 95% CI | {_fmt(report['mean_thesis_pnl'], 4)} "
        f"{_fmt_ci(report['pnl_ci'])} | CI lower bound > 0 |"
    )
    lines.append(
        f"| Agent Brier | {_fmt(report['agent_brier'])} {_fmt_ci(report['agent_brier_ci'])} "
        f"| < market Brier, non-overlapping CI |"
    )
    lines.append(
        f"| Market Brier (identical traded sample) | {_fmt(report['market_brier'])} "
        f"{_fmt_ci(report['market_brier_ci'])} | — |"
    )
    lines.append(
        f"| Market Brier (all evaluated signals) | {_fmt(report['evaluated_market_brier'])} "
        f"(n={report['n_evaluated']}) | — |"
    )
    lines.append("")
    lines.append("## Fill quality (decision-vs-fill — the empirical slippage dataset)")
    lines.append("")
    slip = report["slippage"]
    lines.append(f"| Metric | Value |")
    lines.append("|---|---|")
    lines.append(f"| Filled closed trades with both prices | {slip['n']} |")
    lines.append(f"| Mean signed slippage (prob. units) | {_fmt(slip['mean'])} |")
    lines.append(f"| Median signed slippage | {_fmt(slip['median'])} |")
    lines.append(f"| Worst adverse slippage | {_fmt(slip['max_adverse'])} |")
    ot = report["order_types"]
    filled = ot["taker"] + ot["maker_fill"] + ot["taker_fallback"]
    maker_share = (
        f"{(ot['maker_fill']) / filled:.1%}" if filled else "—"
    )
    lines.append(f"| Taker fills | {ot['taker']} |")
    lines.append(f"| Maker fills (queue won) | {ot['maker_fill']} |")
    lines.append(f"| Taker fallbacks (maker timed out) | {ot['taker_fallback']} |")
    lines.append(f"| Other/NULL fill_type rows | {ot['other_or_null']} |")
    lines.append(f"| Maker share of fills | {maker_share} |")
    lines.append("")
    lines.append("## Thesis accounting (Step 5d — clusters count once)")
    lines.append("")
    dedup = report["thesis_dedup"]
    lines.append(
        f"{dedup['rows']} closed rows → {dedup['distinct_theses']} distinct theses "
        f"({dedup['null_thesis_rows']} rows without a thesis_id, counted as singletons)."
    )
    lines.append("")
    lines.append("## Per-strategy breakdown")
    lines.append("")
    lines.append("| Strategy | n | Net P&L | Mean net edge | Agent Brier | Market Brier |")
    lines.append("|---|---|---|---|---|---|")
    for s in report["per_strategy"]:
        lines.append(
            f"| {s['strategy']} | {s['n']} | {_fmt(s['net_pnl'], 2)} | "
            f"{_fmt(s['mean_net_edge'])} | {_fmt(s['agent_brier'])} | {_fmt(s['market_brier'])} |"
        )
    lines.append("")
    lines.append(
        "*Bootstrap CIs: pure-Python percentile bootstrap, "
        f"{config.GATE_BOOTSTRAP_RESAMPLES} resamples, fixed seed "
        f"{config.GATE_BOOTSTRAP_SEED} (reproducible). P&L CI is a thesis-cluster "
        "bootstrap (mean per-thesis net P&L). Brier CIs are marginal bootstrap CIs "
        "on the identical resolved sample.*"
    )
    lines.append("")
    return "\n".join(lines)


def render_telegram_summary(report: dict[str, Any], window_days: int) -> str:
    """Compact Telegram summary; first line follows the alert convention."""
    lines = ["[ZERO-ALPHA] INFO | GATE_REPORT"]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines.append(f"Time: {now}")
    lines.append(f"Window: {window_days}d | GATE: {report['verdict']}")
    lines.append(
        f"n={report['n_resolved']} (need {config.GATE_MIN_RESOLVED_SAMPLES}) | "
        f"net P&L {_fmt(report['total_net_pnl'], 2)} | "
        f"mean/thesis CI {_fmt_ci(report['pnl_ci'])}"
    )
    lines.append(
        f"Brier agent {_fmt(report['agent_brier'])} vs market "
        f"{_fmt(report['market_brier'])} | evaluated-market "
        f"{_fmt(report['evaluated_market_brier'])} (n={report['n_evaluated']})"
    )
    slip = report["slippage"]
    ot = report["order_types"]
    lines.append(
        f"Slippage mean {_fmt(slip['mean'])} (n={slip['n']}) | "
        f"taker {ot['taker']} / maker {ot['maker_fill']} / fallback {ot['taker_fallback']}"
    )
    dedup = report["thesis_dedup"]
    lines.append(
        f"Theses: {dedup['distinct_theses']} distinct over {dedup['rows']} rows"
    )
    if report["failed_conditions"]:
        lines.append("FAILED: " + "; ".join(report["failed_conditions"]))
    return "\n".join(lines)


# ── Pure core: Gamma resolution parsing (mirrors reconciliation's paper path) ─

def market_yes_outcome(market_json: dict[str, Any]) -> Optional[int]:
    """YES-line resolution from a Gamma market payload: 1 / 0 / None (unresolved).

    Resolved = umaResolutionStatus 'resolved' OR closed OR resolved (the exact
    predicate the startup reconciliation uses). Outcome read from the
    outcomePrices/outcomes pair (both may arrive as JSON strings)."""
    is_resolved = (
        market_json.get("umaResolutionStatus") == "resolved"
        or market_json.get("closed") is True
        or market_json.get("resolved") is True
    )
    if not is_resolved:
        return None
    outcome_prices = market_json.get("outcomePrices")
    outcomes = market_json.get("outcomes")
    if isinstance(outcome_prices, str):
        try:
            outcome_prices = json.loads(outcome_prices)
        except json.JSONDecodeError:
            return None
    if isinstance(outcomes, str):
        try:
            outcomes = json.loads(outcomes)
        except json.JSONDecodeError:
            return None
    if not outcomes or not outcome_prices:
        return None
    try:
        idx = [str(o).strip().title() for o in outcomes].index("Yes")
        price = float(outcome_prices[idx])
    except (ValueError, IndexError, TypeError):
        return None
    if price not in (0.0, 1.0):
        return None
    return int(price)


# ── I/O layer ─────────────────────────────────────────────────────────────────

async def fetch_closed_trades(days: int, limit: int) -> list[dict[str, Any]]:
    """Closed trades in the window (the gate's agent sample)."""
    from memory.supabase_client import get_client

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    client = await get_client()
    res = (
        client.table("closed_trades")
        .select(
            "market_id,direction,strategy,outcome,thesis_id,pnl_usdc,net_edge_at_entry,"
            "p_model_at_entry,market_price_at_entry,fill_price,fill_type,closed_at"
        )
        .gte("closed_at", cutoff)
        .order("closed_at")
        .limit(limit)
        .execute()
    )
    return res.data or []


async def fetch_signal_outcomes(days: int, limit: int) -> list[dict[str, Any]]:
    """Evaluated-signal baseline rows in the window (market probability at t0)."""
    from memory.supabase_client import get_client

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    client = await get_client()
    res = (
        client.table("signal_outcomes")
        .select("market_id,t0,price_t0,resolved_outcome")
        .gte("t0", cutoff)
        .order("t0")
        .limit(limit)
        .execute()
    )
    return res.data or []


async def backfill_resolutions(
    outcomes_rows: list[dict[str, Any]],
    max_fetches: int = _DEFAULT_MAX_RESOLUTION_FETCHES,
) -> int:
    """Record YES/NO resolutions onto signal_outcomes (offline, idempotent).

    For each distinct market with unresolved rows, fetch Gamma once; on a
    resolved market update ALL its unresolved rows (resolved_outcome SMALLINT,
    resolved_at). The predicate (resolved_outcome IS NULL) makes reruns no-ops.
    Returns the number of markets recorded."""
    from memory.supabase_client import get_client

    unresolved_markets: list[str] = []
    seen: set[str] = set()
    for row in outcomes_rows:
        if row.get("resolved_outcome") is not None:
            continue
        market_id = row.get("market_id")
        if market_id and market_id not in seen:
            seen.add(market_id)
            unresolved_markets.append(market_id)

    client = await get_client()
    import httpx

    recorded = 0
    async with httpx.AsyncClient() as http:
        for market_id in unresolved_markets[:max_fetches]:
            try:
                res = await http.get(
                    f"{_GAMMA_MARKET_URL}/{market_id}",
                    timeout=config.POLYMARKET_API_TIMEOUT_SECONDS,
                )
                if res.status_code != 200:
                    logger.warning("Gamma fetch %s → HTTP %s", market_id[:16], res.status_code)
                    continue
                outcome = market_yes_outcome(res.json())
            except Exception as e:
                logger.warning("Gamma fetch failed for %s: %s", market_id[:16], e)
                continue
            if outcome is None:
                continue  # not resolved yet — stays NULL, retried next run
            try:
                async with asyncio.timeout(config.SUPABASE_TIMEOUT_SECONDS):
                    (
                        client.table("signal_outcomes")
                        .update({
                            "resolved_outcome": outcome,
                            "resolved_at": datetime.now(timezone.utc).isoformat(),
                        })
                        .eq("market_id", market_id)
                        .is_("resolved_outcome", "null")
                        .execute()
                    )
                recorded += 1
            except Exception as e:
                logger.warning("Resolution write failed for %s: %s", market_id[:16], e)
    return recorded


def evaluate_market_brier(
    outcomes_rows: list[dict[str, Any]],
) -> tuple[Optional[float], int]:
    """Market-only Brier over ALL evaluated signals with a resolution (the
    honest null at large — traded or not)."""
    values: list[float] = []
    outcomes: list[int] = []
    for row in outcomes_rows:
        price_t0 = to_float(row.get("price_t0"))
        resolved = to_float(row.get("resolved_outcome"))
        if price_t0 is None or resolved is None:
            continue
        if resolved not in (0.0, 1.0):
            continue
        values.append(price_t0)
        outcomes.append(int(resolved))
    if not values:
        return None, 0
    return mean_brier(values, outcomes), len(values)


def build_report(
    closed_rows: list[dict[str, Any]],
    outcomes_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Pure report assembly: every number the gate verdict rests on."""
    resolved_rows = [r for r in closed_rows if yes_line_outcome(r) is not None]
    n_resolved = len(resolved_rows)

    pnl_values = [v for v in (to_float(r.get("pnl_usdc")) for r in closed_rows) if v is not None]
    total_net_pnl = sum(pnl_values) if pnl_values else None

    clusters = cluster_values(resolved_rows)
    mean_thesis_pnl = (
        statistics.fmean(sum(c) for c in clusters) if clusters else None
    )
    pnl_ci = cluster_mean_bootstrap_ci(resolved_rows)

    pairs = brier_pairs(resolved_rows)
    agent_brier = mean_brier([p[0] for p in pairs], [int(p[2]) for p in pairs]) if pairs else None
    market_brier = mean_brier([p[1] for p in pairs], [int(p[2]) for p in pairs]) if pairs else None
    agent_ci = brier_bootstrap_ci(pairs, "agent") if pairs else None
    market_ci = brier_bootstrap_ci(pairs, "market") if pairs else None

    evaluated_market_brier, n_evaluated = evaluate_market_brier(outcomes_rows)

    gate = evaluate_gate(
        n_resolved=n_resolved,
        pnl_ci=pnl_ci,
        agent_brier=agent_brier,
        market_brier=market_brier,
        agent_ci=agent_ci,
        market_ci=market_ci,
    )

    return {
        "verdict": gate["verdict"],
        "failed_conditions": gate["failed_conditions"],
        "brier_edge_case": gate["brier_edge_case"],
        "n_resolved": n_resolved,
        "sample_composition": (
            f"paper closed trades only (n={n_resolved}); no backtest sample exists "
            "until Step 6 — the composite gate counts paper toward "
            f"GATE_MIN_RESOLVED_SAMPLES={config.GATE_MIN_RESOLVED_SAMPLES}"
        ),
        "total_net_pnl": total_net_pnl,
        "mean_thesis_pnl": mean_thesis_pnl,
        "pnl_ci": pnl_ci,
        "agent_brier": agent_brier,
        "market_brier": market_brier,
        "agent_brier_ci": agent_ci,
        "market_brier_ci": market_ci,
        "evaluated_market_brier": evaluated_market_brier,
        "n_evaluated": n_evaluated,
        "slippage": slippage_stats(closed_rows),
        "order_types": order_type_stats(closed_rows),
        "thesis_dedup": thesis_dedup_count(closed_rows),
        "per_strategy": aggregate_per_strategy(closed_rows),
    }


async def send_telegram(message: str) -> None:
    from monitoring.telegram_alerts import _send_to_telegram
    await _send_to_telegram(message)


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ZERO-ALPHA honest gate report (Step 5)")
    parser.add_argument("--days", type=int, default=7, help="lookback window in days")
    parser.add_argument("--limit", type=int, default=5000, help="max rows per table fetch")
    parser.add_argument("--out", type=str, default=None, help="markdown output path")
    parser.add_argument("--no-telegram", action="store_true", help="skip the Telegram send")
    parser.add_argument(
        "--max-resolution-fetches", type=int, default=_DEFAULT_MAX_RESOLUTION_FETCHES,
        help="cap on Gamma resolution backfill fetches this run",
    )
    return parser.parse_args(argv)


async def _collect_data(
    days: int, limit: int, max_fetches: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    """Fetch both tables, backfill resolutions, refetch when the backfill
    recorded anything (fresh resolutions belong in this report too)."""
    closed_rows = await fetch_closed_trades(days, limit)
    outcomes_rows = await fetch_signal_outcomes(days, limit)
    recorded = await backfill_resolutions(outcomes_rows, max_fetches)
    logger.info("Resolution backfill recorded %d market(s)", recorded)
    if recorded:
        outcomes_rows = await fetch_signal_outcomes(days, limit)
    return closed_rows, outcomes_rows, recorded


def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(name)s | %(levelname)s | %(message)s")
    args = parse_args(argv)
    generated_at = datetime.now(timezone.utc)

    try:
        closed_rows, outcomes_rows, _ = asyncio.run(
            _collect_data(args.days, args.limit, args.max_resolution_fetches)
        )
    except Exception as e:
        logger.error(
            "Supabase read failed (%s). Check SUPABASE_URL/SUPABASE_KEY in .env.test "
            "and that the Step 5 migration (fill/baseline columns) has been applied.", e
        )
        return 1

    report = build_report(closed_rows, outcomes_rows)
    markdown = render_markdown(report, args.days, generated_at)

    out_path = Path(args.out) if args.out else (
        Path("scratch") / f"gate_report_{generated_at.date().isoformat()}.md"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(markdown, encoding="utf-8")
    logger.info("Gate report written to %s (verdict: %s)", out_path, report["verdict"])

    if not args.no_telegram:
        try:
            summary = render_telegram_summary(report, args.days)
            asyncio.run(send_telegram(summary))
        except Exception as e:
            logger.warning("Telegram summary send failed (report file is still valid): %s", e)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
