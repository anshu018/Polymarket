"""
scripts/signal_drift_report.py — A6: weekly signal drift report (List A.md Step 2).

The empirical answer to "does a headline still predict a tradable move after
OUR latency?": for every instrumented signal (signal_outcomes), compute the
signed drift sign(signal_direction) × (price_tX − price_t0) per horizon, group
by category × event-type, and issue a VELOCITY VIABILITY VERDICT — would the
mean drift, if real, clear the Step 1 honest entry gate for a velocity taker?

OFFLINE ANALYSIS ONLY — never imported by, or run in, the runtime hot path.
Reads Supabase, writes a markdown report, and sends a Telegram summary.

Usage:
    python -m scripts.signal_drift_report [--days 7] [--out PATH]
                                          [--limit 5000] [--no-telegram]

Cost model: fees are EXACT (verified schedule, D-09 — risk.cost_model); the
synthetic book spread is the DRIFT_VIABILITY_ASSUMED_SPREAD_CENTS assumption
because an offline report has no live order book.
"""

import argparse
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv

# CLAUDE.md rule: scripts load .env.test (override=True) BEFORE any project
# import. .env is permanently off-limits to tests and scripts.
load_dotenv(dotenv_path=".env.test", override=True)

import config  # noqa: E402  (must follow the .env.test load)
from risk import cost_model  # noqa: E402
from risk.cost_model import BookSnapshot  # noqa: E402

logger = logging.getLogger("signal_drift_report")

# Horizon columns paired with display labels (positionally, like the sampler).
_HORIZONS: tuple[tuple[str, str], ...] = (
    ("p_m1", "+1m"),
    ("p_m5", "+5m"),
    ("p_m15", "+15m"),
    ("p_m60", "+60m"),
)

_VERDICT_LABELS = {
    "ALLOW": "VIABLE",
    "BLOCK_NET_EDGE": "NOT VIABLE",
    "BLOCK_PRICE_BAND": "OUT OF BAND",
}


# ── Pure core (unit-testable, no I/O) ─────────────────────────────────────────

def signed_drift(row: dict[str, Any], column: str) -> Optional[float]:
    """
    Signed drift for one signal at one horizon: sign × (price_tX − price_t0).

    sign is +1 when the signal asserted YES (confirmed_direction True) and −1
    for NO. Rows missing the direction or either price are excluded (None) —
    explicit NULLs are valid sampler data, not zeros.

    Args:
        row:    One signal_outcomes row (market_signals embed optional).
        column: Horizon column name (p_m1/p_m5/p_m15/p_m60).

    Returns:
        Drift in probability units (cents), or None when not measurable.
    """
    direction = row.get("confirmed_direction")
    price_t0 = row.get("price_t0")
    price_tx = row.get(column)
    if direction is None or price_t0 is None or price_tx is None:
        return None
    sign = 1.0 if direction else -1.0
    return sign * (float(price_tx) - float(price_t0))


def viability_verdict(
    mean_drift: float,
    reference_price: float,
    category: str,
) -> tuple[str, float]:
    """
    Velocity viability verdict for one group's mean drift (Step 2 spec).

    Reuses the Step 1 cost model with the velocity order type (taker): the
    mean drift is treated as the gross edge (p_model = price_t0 + drift) and
    priced against a synthetic book whose full spread is
    DRIFT_VIABILITY_ASSUMED_SPREAD_CENTS (offline reports have no live book —
    fees remain exact per D-09). The verdict is the real gate outcome.

    Args:
        mean_drift:      Mean signed drift for the group/horizon (cents).
        reference_price: Group's mean price_t0 (the synthetic book mid).
        category:        Category for the per-category taker fee rate.

    Returns:
        (gate, net_edge) — gate is 'ALLOW' | 'BLOCK_NET_EDGE' |
        'BLOCK_PRICE_BAND' (check_net_edge outcomes; a fail-closed breakdown
        surfaces as BLOCK_NET_EDGE).
    """
    mid = float(reference_price)
    spread = float(config.DRIFT_VIABILITY_ASSUMED_SPREAD_CENTS)
    book = BookSnapshot(best_bid=mid - spread / 2.0, best_ask=mid + spread / 2.0,
                        depth_usd=1.0)
    fee_cfg = cost_model.fee_config_for(category)
    p_model = min(1.0, max(0.0, mid + mean_drift))
    breakdown = cost_model.compute_cost_breakdown(
        p_model=p_model,
        price=mid,
        side="YES",
        order_type="taker",
        book=book,
        fee_cfg=fee_cfg,
        size_usd=0.0,
    )
    gate = cost_model.check_net_edge(breakdown.net_edge, mid, "taker")
    return gate, breakdown.net_edge


def aggregate_drift(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Group instrumented rows by (category, event-type) and compute per-horizon
    drift statistics.

    Category/event-type come from the embedded market_signals row (PostgREST
    resource embed); rows without a resolvable embed count under 'unknown'.
    Duplicates of the same headline (novelty dups are recorded too) are NOT
    deduped here — the first-occurrence-only view is a --days/--limit artifact
    left to the operator; sample counts are printed so correlated rows stay
    visible.

    Args:
        rows: signal_outcomes rows with the market_signals embed.

    Returns:
        One report dict per group, sorted by (category, event_type).
    """
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        embed = row.get("market_signals")
        embed = embed if isinstance(embed, dict) else {}
        category = embed.get("category") or "unknown"
        event_type = embed.get("event_type") or category
        groups.setdefault((category, event_type), []).append(row)

    report: list[dict[str, Any]] = []
    for (category, event_type), group_rows in sorted(groups.items()):
        horizons: list[dict[str, Any]] = []
        for column, label in _HORIZONS:
            drifts = [
                d for d in (signed_drift(r, column) for r in group_rows)
                if d is not None
            ]
            n = len(drifts)
            horizons.append({
                "label": label,
                "column": column,
                "n": n,
                "mean": (sum(drifts) / n) if n else None,
                "pass_rate": (sum(1 for d in drifts if d > 0) / n) if n else None,
            })
        prices = [float(r["price_t0"]) for r in group_rows
                  if r.get("price_t0") is not None]
        report.append({
            "category": category,
            "event_type": event_type,
            "total": len(group_rows),
            "ref_price": (sum(prices) / len(prices)) if prices else None,
            "horizons": horizons,
        })
    return report


def render_markdown(
    report: list[dict[str, Any]],
    total_rows: int,
    window_days: int,
    generated_at: datetime,
) -> str:
    """
    Render the full markdown report (per-group tables + verdicts).

    Args:
        report:       aggregate_drift() output.
        total_rows:   Number of instrumented rows in the window.
        window_days:  Window length in days (for the header).
        generated_at: Report generation timestamp (UTC).

    Returns:
        Markdown document as a string.
    """
    lines: list[str] = [
        "# Signal Drift Report — velocity viability after pipeline latency",
        "",
        f"Generated: {generated_at.isoformat()}",
        f"Window: last {window_days} day(s) of instrumented signals; "
        f"{total_rows} row(s) in signal_outcomes.",
        f"Verdict model: mean signed drift as gross edge for a velocity TAKER, "
        f"fees exact (D-09), synthetic full spread "
        f"{float(config.DRIFT_VIABILITY_ASSUMED_SPREAD_CENTS) * 100:.1f}¢, "
        f"gate: net_edge > {config.MIN_NET_EDGE_CENTS:.2f}.",
        "",
        "Drift is sign(signal_direction) × (price_tX − price_t0), in cents; "
        "pass rate = share of samples drifting in the signal's direction.",
        "",
    ]
    if not report:
        lines.append("**No instrumented signals in the window.** The data clock "
                     "starts when the sampler first runs (List A.md Step 2).")
        return "\n".join(lines) + "\n"

    for group in report:
        if group["ref_price"] is not None:
            header = (f"## {group['category']} / {group['event_type']} "
                      f"(n={group['total']}, mean price_t0={group['ref_price']:.3f})")
        else:
            header = (f"## {group['category']} / {group['event_type']} "
                      f"(n={group['total']}, mean price_t0=n/a)")
        lines.append(header)
        lines.append("")
        lines.append("| horizon | n | mean drift (¢) | pass rate | net edge (¢) | verdict |")
        lines.append("|---|---|---|---|---|---|")
        for h in group["horizons"]:
            if h["n"] == 0 or h["mean"] is None:
                lines.append(f"| {h['label']} | 0 | — | — | — | NO DATA |")
                continue
            if group["ref_price"] is None:
                lines.append(
                    f"| {h['label']} | {h['n']} | {h['mean'] * 100:+.2f} | "
                    f"{h['pass_rate']:.2f} | — | NO REF PRICE |"
                )
                continue
            gate, net_edge = viability_verdict(h["mean"], group["ref_price"],
                                               group["category"])
            lines.append(
                f"| {h['label']} | {h['n']} | {h['mean'] * 100:+.2f} | "
                f"{h['pass_rate']:.2f} | {net_edge * 100:+.2f} | "
                f"{_VERDICT_LABELS[gate]} |"
            )
        lines.append("")
    return "\n".join(lines) + "\n"


def render_telegram_summary(
    report: list[dict[str, Any]],
    total_rows: int,
    window_days: int,
) -> str:
    """
    Render the compact Telegram summary: one line per group at the +1m and
    +60m horizons with its verdict.

    Returns:
        Telegram message text ([ZERO-ALPHA] format per CLAUDE.md).
    """
    now = datetime.now(timezone.utc).isoformat()
    lines = [
        "[ZERO-ALPHA] INFO | SIGNAL_DRIFT_REPORT",
        f"Time: {now}",
        f"Window: {window_days}d, {total_rows} instrumented signals",
    ]
    if not report:
        lines.append("No instrumented signals in window (sampler data clock).")
        return "\n".join(lines)
    for group in report:
        for h in group["horizons"]:
            if h["label"] not in ("+1m", "+60m"):
                continue
            if h["n"] == 0 or h["mean"] is None or group["ref_price"] is None:
                lines.append(f"{group['category']}/{group['event_type']} "
                             f"{h['label']}: NO DATA (n=0)")
                continue
            gate, net_edge = viability_verdict(h["mean"], group["ref_price"],
                                               group["category"])
            lines.append(
                f"{group['category']}/{group['event_type']} {h['label']}: "
                f"n={h['n']} drift={h['mean'] * 100:+.1f}¢ "
                f"net={net_edge * 100:+.1f}¢ → {_VERDICT_LABELS[gate]}"
            )
    lines.append("Weekly offline analysis — velocity estimator feeds on this (Step 6).")
    return "\n".join(lines)


# ── I/O layer ─────────────────────────────────────────────────────────────────

async def fetch_signal_outcomes(days: int, limit: int) -> list[dict[str, Any]]:
    """
    Read instrumented signals in the window, with the market_signals embed for
    category/event-type (the FK makes PostgREST resource embedding available).

    Args:
        days:  Window length in days (from now, inclusive).
        limit: Maximum rows to fetch (safety bound).

    Returns:
        signal_outcomes rows.
    """
    from memory.supabase_client import get_client

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    client = await get_client()
    res = (
        client.table("signal_outcomes")
        .select("*, market_signals(category,event_type)")
        .gte("t0", cutoff)
        .order("t0")
        .limit(limit)
        .execute()
    )
    return res.data or []


async def send_telegram(message: str) -> None:
    """Send the summary via the repo's alert channel (best-effort)."""
    from monitoring.telegram_alerts import _send_to_telegram
    await _send_to_telegram(message)


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    """Parse CLI arguments (see module docstring)."""
    parser = argparse.ArgumentParser(
        description="Weekly signal drift + velocity viability report (offline)."
    )
    parser.add_argument("--days", type=int, default=7,
                        help="Window length in days (default 7).")
    parser.add_argument("--limit", type=int, default=5000,
                        help="Max signal_outcomes rows to fetch (default 5000).")
    parser.add_argument("--out", type=str, default=None,
                        help="Markdown output path (default "
                             "scratch/signal_drift_report_<date>.md).")
    parser.add_argument("--no-telegram", action="store_true",
                        help="Skip the Telegram summary send.")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    """CLI entry point. Returns a process exit code."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    args = parse_args(argv)
    generated_at = datetime.now(timezone.utc)

    try:
        rows = asyncio.run(fetch_signal_outcomes(args.days, args.limit))
    except Exception as e:
        logger.error(
            "Could not read signal_outcomes from Supabase (%s: %s). "
            "Check SUPABASE_URL/KEY (scripts read .env.test) and that the "
            "signal_outcomes migration has been applied.",
            type(e).__name__, e,
        )
        return 1
    logger.info("Fetched %d instrumented signal(s) from the last %d day(s).",
                len(rows), args.days)

    report = aggregate_drift(rows)
    markdown = render_markdown(report, len(rows), args.days, generated_at)

    out_path = Path(args.out) if args.out else Path(
        f"scratch/signal_drift_report_{generated_at.date().isoformat()}.md"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(markdown, encoding="utf-8")
    logger.info("Markdown report written to %s", out_path)

    if not args.no_telegram:
        summary = render_telegram_summary(report, len(rows), args.days)
        try:
            asyncio.run(send_telegram(summary))
            logger.info("Telegram summary sent.")
        except Exception as e:
            logger.error("Telegram summary send failed (report file is still valid): %s", e)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
