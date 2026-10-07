"""
scripts/strategy_dashboard.py — A7: weekly per-strategy dashboard (List A.md
Step 3).

The attribution ledger, read back: for every strategy with closed trades in
the window — n, net P&L, mean net edge (trailing window), rolling per-trade
Sharpe, CUSUM fade status, and the allocation state the runtime derives
(weight / probation / retired). The math is the SAME pure code the runtime
enforces at the sizing read path (risk/edge_monitor.py) — the dashboard can
never disagree with the enforcement.

OFFLINE ANALYSIS ONLY — never imported by, or run in, the runtime hot path.
Reads Supabase, writes a markdown report, and sends a Telegram summary.

Usage:
    python -m scripts.strategy_dashboard [--days 7] [--out PATH]
                                         [--limit 5000] [--no-telegram]

Brier-vs-market column: PLACEHOLDER until Step 5 (the market baseline — the
market's own probability at decision time on the identical sample — does not
exist yet). It renders as "—", never as an invented number.
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
from risk.edge_monitor import derive_allocations  # noqa: E402

logger = logging.getLogger("strategy_dashboard")

_BRIER_PLACEHOLDER = "—"  # Step 5: agent-vs-market Brier needs the baseline


# ── Pure core (unit-testable, no I/O) ─────────────────────────────────────────

def _sort_key(row: dict[str, Any]) -> str:
    """Chronological key; rows without closed_at sort first (never crash)."""
    return str(row.get("closed_at") or "")


def aggregate_strategies(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Group closed-trade rows by strategy and derive the dashboard columns with
    the runtime's own math (risk/edge_monitor.derive_allocations).

    Rows are sorted oldest-first before derivation (the CUSUM/rolling windows
    are order-sensitive). Net P&L sums the measurable `pnl_usdc` values; a
    NULL P&L counts in n but not in the sum — missing money is not zero money.

    Args:
        rows: closed_trades rows (strategy, pnl_usdc, net_edge_at_entry,
              thesis_id, closed_at).

    Returns:
        One report dict per strategy, sorted by strategy name.
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        strategy = str(row.get("strategy") or "unknown")
        groups.setdefault(strategy, []).append(row)

    ordered = {s: sorted(g, key=_sort_key) for s, g in groups.items()}
    allocations = derive_allocations(ordered)

    report: list[dict[str, Any]] = []
    for strategy in sorted(groups):
        group_rows = ordered[strategy]
        alloc = allocations[strategy]
        pnls = [float(r["pnl_usdc"]) for r in group_rows
                if r.get("pnl_usdc") is not None]
        theses = {str(r.get("thesis_id")) for r in group_rows
                  if r.get("thesis_id") is not None}
        report.append({
            "strategy": strategy,
            "n": alloc.n,
            "total_rows": len(group_rows),
            "net_pnl": sum(pnls) if pnls else None,
            "mean_net_edge": alloc.rolling_net_edge,
            "rolling_sharpe": alloc.rolling_sharpe,
            "cusum_faded": alloc.faded,
            "cusum_score": alloc.cusum_score,
            "weight": alloc.weight,
            "probation": alloc.probation,
            "retired": alloc.retired,
            "thesis_count": len(theses),
        })
    return report


def _cusum_label(row: dict[str, Any]) -> str:
    """Human CUSUM status: FADED when ongoing, else OK with the worst excursion."""
    if row["cusum_faded"]:
        return f"FADED ({row['cusum_score']:.2f})"
    return f"OK ({row['cusum_score']:.2f})"


def _pnl_label(net_pnl: Optional[float]) -> str:
    return f"{net_pnl:+.2f}" if net_pnl is not None else "—"


def render_markdown(
    report: list[dict[str, Any]],
    total_rows: int,
    window_days: int,
    generated_at: datetime,
) -> str:
    """
    Render the full markdown dashboard.

    Args:
        report:       aggregate_strategies() output.
        total_rows:   Number of closed trades in the window.
        window_days:  Window length in days (for the header).
        generated_at: Report generation timestamp (UTC).

    Returns:
        Markdown document as a string.
    """
    lines: list[str] = [
        "# Strategy Dashboard — attribution ledger weekly view",
        "",
        f"Generated: {generated_at.isoformat()}",
        f"Window: closed trades of the last {window_days} day(s); "
        f"{total_rows} row(s) in closed_trades.",
        f"Edge/Sharpe/CUSUM use the trailing config.EDGE_ROLLING_WINDOW = "
        f"{config.EDGE_ROLLING_WINDOW} window via risk/edge_monitor.py — the "
        f"same math the sizing read path enforces.",
        "",
        "**Brier-vs-market is a placeholder (—) until Step 5's market baseline "
        "exists; no number is invented.**",
        "",
    ]
    if not report:
        lines.append("**No closed trades in the window.** The ledger clock "
                     "starts with the first paper trade after the Step 3 "
                     "attribution migration lands (List A.md).")
        return "\n".join(lines) + "\n"

    lines.append("| strategy | n | theses | net P&L (USDC) | mean net edge (¢) | "
                 "rolling Sharpe | CUSUM | weight | probation | retired | "
                 "Brier-vs-market |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for row in report:
        lines.append(
            f"| {row['strategy']} | {row['n']} | {row['thesis_count']} | "
            f"{_pnl_label(row['net_pnl'])} | {row['mean_net_edge'] * 100:+.2f} | "
            f"{row['rolling_sharpe']:+.2f} | {_cusum_label(row)} | "
            f"{row['weight']:.2f} | {'YES' if row['probation'] else 'no'} | "
            f"{'YES' if row['retired'] else 'no'} | {_BRIER_PLACEHOLDER} |"
        )
    lines.append("")
    lines.append(f"Allocation: weight ∝ rolling net Sharpe (mean 1.0 across "
                 f"positive-Sharpe strategies, clamp {config.REALLOC_WEIGHT_MAX}); "
                 f"n ≥ {config.REALLOC_MIN_SAMPLES} to be weighted; probation "
                 f"(mean net edge < {config.STRATEGY_PROBATION_EDGE_THRESHOLD_CENTS:.2f} "
                 f"over {config.STRATEGY_PROBATION_TRADE_COUNT} trades) → size × "
                 f"{config.PROBATION_SIZE_MULTIPLIER}; two consecutive probations → "
                 f"retired (flagged, never deleted).")
    lines.append("")
    return "\n".join(lines) + "\n"


def render_telegram_summary(
    report: list[dict[str, Any]],
    total_rows: int,
    window_days: int,
) -> str:
    """
    Render the compact Telegram summary: one line per strategy.

    Returns:
        Telegram message text ([ZERO-ALPHA] format per CLAUDE.md).
    """
    now = datetime.now(timezone.utc).isoformat()
    lines = [
        "[ZERO-ALPHA] INFO | STRATEGY_DASHBOARD",
        f"Time: {now}",
        f"Window: {window_days}d, {total_rows} closed trades",
    ]
    if not report:
        lines.append("No closed trades in window (ledger clock: first paper "
                     "trade after the Step 3 migration).")
        lines.append("Brier-vs-market: placeholder until Step 5.")
        return "\n".join(lines)
    for row in report:
        status = ("RETIRED" if row["retired"]
                  else "PROBATION" if row["probation"]
                  else "FADED" if row["cusum_faded"] else "OK")
        lines.append(
            f"{row['strategy']}: n={row['n']} "
            f"pnl={_pnl_label(row['net_pnl'])} "
            f"edge={row['mean_net_edge'] * 100:+.1f}¢ "
            f"sharpe={row['rolling_sharpe']:+.2f} cusum={status} "
            f"w={row['weight']:.2f} brier_mkt=—(Step5)"
        )
    lines.append("Weekly offline analysis — allocation math matches the "
                 "sizing read path (risk/edge_monitor.py).")
    return "\n".join(lines)


# ── I/O layer ─────────────────────────────────────────────────────────────────

async def fetch_closed_trades(days: int, limit: int) -> list[dict[str, Any]]:
    """
    Read closed trades in the window (attribution columns included).

    Args:
        days:  Window length in days (from now, inclusive).
        limit: Maximum rows to fetch (safety bound).

    Returns:
        closed_trades rows.
    """
    from memory.supabase_client import get_client

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    client = await get_client()
    res = (
        client.table("closed_trades")
        .select("strategy,pnl_usdc,net_edge_at_entry,thesis_id,closed_at")
        .gte("closed_at", cutoff)
        .order("closed_at")
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
        description="Weekly per-strategy attribution dashboard (offline)."
    )
    parser.add_argument("--days", type=int, default=7,
                        help="Window length in days (default 7).")
    parser.add_argument("--limit", type=int, default=5000,
                        help="Max closed_trades rows to fetch (default 5000).")
    parser.add_argument("--out", type=str, default=None,
                        help="Markdown output path (default "
                             "scratch/strategy_dashboard_<date>.md).")
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
        rows = asyncio.run(fetch_closed_trades(args.days, args.limit))
    except Exception as e:
        logger.error(
            "Could not read closed_trades from Supabase (%s: %s). "
            "Check SUPABASE_URL/KEY (scripts read .env.test) and that the "
            "Step 3 attribution migration has been applied.",
            type(e).__name__, e,
        )
        return 1
    logger.info("Fetched %d closed trade(s) from the last %d day(s).",
                len(rows), args.days)

    report = aggregate_strategies(rows)
    markdown = render_markdown(report, len(rows), args.days, generated_at)

    out_path = Path(args.out) if args.out else Path(
        f"scratch/strategy_dashboard_{generated_at.date().isoformat()}.md"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(markdown, encoding="utf-8")
    logger.info("Markdown dashboard written to %s", out_path)

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
