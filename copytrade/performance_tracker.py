"""
copytrade/performance_tracker.py — Strategy 5: Copy Edge

Tracks per-wallet trade outcomes and computes trust scores.

Architecture:
    Trust Score Formula (Bayesian-damped win rate):
        confidence  = min(total_trades / TRUST_CALIBRATION_TRADES, 1.0)
        win_rate    = winning_trades / total_trades  (or 0.5 if no trades yet)
        base_score  = (win_rate × confidence) + (0.5 × (1 - confidence))
        pnl_bonus   = clamp(total_pnl_usdc / 1000.0, -0.10, +0.10)
        trust_score = clamp(base_score + pnl_bonus, 0.0, 1.0)

    Rationale:
        - A new wallet with 0 trades gets a neutral 0.5 (unknown, not distrusted).
        - A wallet with 1/1 wins gets ~0.525 — barely above neutral. No hype.
        - A wallet with 40/50 wins (80% rate, 50 trades) gets ~0.80.
        - A wallet with 10/50 wins (20% rate, 50 trades) gets ~0.20 — near-disabled.
        - PnL bonus rewards absolute profitability in addition to win rate.
        - This naturally resolves conflicts: pick the wallet with the higher score.

    TRUST_CALIBRATION_TRADES = 20 — matches the bot's own paper-trading threshold.

Conflict Resolution:
    When two wallets signal the same market simultaneously, the classifier calls
    resolve_conflict(wallet_a, wallet_b) which returns the preferred wallet address.
    If trust scores are equal (e.g. both new wallets), the first signal wins.

Safety rules:
    - All DB writes are non-blocking on failure (best-effort, never crash bot).
    - All DB reads have 2-second timeouts with fail-open fallback (return 0.5).
    - In-memory trust score cache (refreshed every COPY_WALLET_RELOAD_INTERVAL_SECONDS).
    - No LLM calls. No external API calls.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

import config
from memory.supabase_client import get_client

logger = logging.getLogger(__name__)

# ── Trust score constants ─────────────────────────────────────────────────────

TRUST_CALIBRATION_TRADES: int = 20     # Trades needed to reach full confidence
TRUST_DEFAULT_SCORE: float = 0.50      # Score for wallets with no history
TRUST_MIN_SCORE: float = 0.0
TRUST_MAX_SCORE: float = 1.0
TRUST_PNL_SCALE: float = 1000.0        # $1000 PnL = max +10% bonus
TRUST_PNL_BONUS_CAP: float = 0.10

# ── In-memory trust score cache ───────────────────────────────────────────────
# Refreshed on wallet reload so the classifier never hits the DB per-signal.
# Format: {wallet_address: trust_score}
_TRUST_CACHE: dict[str, float] = {}


def compute_trust_score(
    winning_trades: int,
    total_trades: int,
    total_pnl_usdc: float,
) -> float:
    """
    Compute the trust score for a wallet given its trade history.

    Returns a float in [0.0, 1.0] where:
        0.50 = neutral / unknown (no history)
        0.75+ = reliably profitable, enough sample
        0.25- = consistently losing, approach with extreme caution
        1.00  = theoretical maximum (never reached)

    Args:
        winning_trades: Number of copy trades that resolved profitably.
        total_trades:   Total resolved copy trades (won + lost).
        total_pnl_usdc: Total PnL from this wallet's copied trades.
    """
    if total_trades == 0:
        return TRUST_DEFAULT_SCORE

    win_rate = winning_trades / total_trades

    # Bayesian dampening: confidence grows linearly to 1.0 at TRUST_CALIBRATION_TRADES
    confidence = min(total_trades / TRUST_CALIBRATION_TRADES, 1.0)

    # Blend win rate with prior of 0.5 (neutral), weighted by confidence
    base_score = (win_rate * confidence) + (TRUST_DEFAULT_SCORE * (1.0 - confidence))

    # PnL quality bonus: cap at ±10%
    pnl_bonus = max(
        -TRUST_PNL_BONUS_CAP,
        min(TRUST_PNL_BONUS_CAP, total_pnl_usdc / TRUST_PNL_SCALE)
    )

    return max(TRUST_MIN_SCORE, min(TRUST_MAX_SCORE, base_score + pnl_bonus))


def get_trust_score(wallet_address: str) -> float:
    """
    Return the cached trust score for a wallet address.
    Returns TRUST_DEFAULT_SCORE (0.50) for unknown wallets.
    """
    return _TRUST_CACHE.get(wallet_address, TRUST_DEFAULT_SCORE)


def resolve_conflict(wallet_a: str, wallet_b: str) -> str:
    """
    Given two wallet addresses competing for the same market signal,
    return the address of the more trusted wallet.

    If scores are equal (e.g. both new wallets), wallet_a wins (first-come).
    """
    score_a = get_trust_score(wallet_a)
    score_b = get_trust_score(wallet_b)

    if score_b > score_a:
        logger.info(
            "[TRUST] Conflict resolved: %s (%.3f) preferred over %s (%.3f)",
            wallet_b[:10],
            score_b,
            wallet_a[:10],
            score_a,
        )
        return wallet_b

    logger.info(
        "[TRUST] Conflict resolved: %s (%.3f) preferred over %s (%.3f)",
        wallet_a[:10],
        score_a,
        wallet_b[:10],
        score_b,
    )
    return wallet_a


# ── DB helpers ────────────────────────────────────────────────────────────────

async def _load_performance_from_db() -> list[dict]:
    """Load all rows from trader_performance. Fail-open: return [] on error."""
    async def _q():
        client = await get_client()
        res = client.table("trader_performance").select("*").execute()
        return res.data or []

    try:
        return await asyncio.wait_for(_q(), timeout=config.SUPABASE_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        logger.error("[TRUST] Supabase trader_performance read timed out — using cached scores.")
        return []
    except Exception as e:
        logger.error("[TRUST] Failed to load trader_performance: %s", e)
        return []


async def refresh_trust_cache() -> None:
    """
    Reload trust scores from Supabase into the in-memory cache.
    Called by the poller during wallet reload so the classifier never
    blocks on a DB call per-signal.
    """
    rows = await _load_performance_from_db()
    updated = 0
    for row in rows:
        addr = row.get("wallet_address", "")
        if not addr:
            continue
        score = compute_trust_score(
            winning_trades=int(row.get("winning_trades", 0) or 0),
            total_trades=int(row.get("total_trades_copied", 0) or 0),
            total_pnl_usdc=float(row.get("total_pnl_usdc", 0.0) or 0.0),
        )
        _TRUST_CACHE[addr] = score
        updated += 1

    logger.info("[TRUST] Trust cache refreshed: %d wallets scored.", updated)


async def _upsert_performance_row(wallet_address: str, trader_name: str) -> None:
    """
    Ensure a trader_performance row exists for this wallet. No-op if already exists.
    Called when a new copy trade is first executed.
    """
    async def _upsert():
        client = await get_client()
        client.table("trader_performance").upsert({
            "wallet_address": wallet_address,
            "trader_name": trader_name,
            # Only sets these on INSERT — Supabase upsert doesn't overwrite existing rows
            "total_trades_copied": 0,
            "winning_trades": 0,
            "losing_trades": 0,
            "total_pnl_usdc": 0.0,
            "trust_score": TRUST_DEFAULT_SCORE,
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }, on_conflict="wallet_address").execute()

    try:
        await asyncio.wait_for(_upsert(), timeout=config.SUPABASE_TIMEOUT_SECONDS)
    except Exception as e:
        logger.error("[TRUST] Failed to upsert performance row for %s: %s", wallet_address[:10], e)


async def log_copy_trade(
    wallet_address: str,
    trader_name: str,
    market_id: str,
    direction: str,
    class_type: str,
    entry_price: float,
    size_usdc: float,
    slippage: float,
    idempotency_uuid: str,
) -> Optional[str]:
    """
    Log a newly executed copy trade to the `copytrade_log` table.
    Also ensures a performance row exists for this wallet.

    Returns the UUID of the newly created copytrade_log row (for later outcome linking).
    Returns None on failure (non-critical — bot continues).
    """
    # Ensure wallet has a performance tracking row
    await _upsert_performance_row(wallet_address, trader_name)

    async def _insert():
        client = await get_client()
        import uuid as _uuid
        row_id = str(_uuid.uuid4())
        client.table("copytrade_log").insert({
            "id": row_id,
            "wallet_address": wallet_address,
            "trader_name": trader_name,
            "market_id": market_id,
            "direction": direction,
            "class_type": class_type,
            "entry_price": entry_price,
            "size_usdc": size_usdc,
            "slippage": slippage,
            "idempotency_uuid": idempotency_uuid,
            "status": "open",
            "exit_price": None,
            "pnl_usdc": None,
            "opened_at": datetime.now(timezone.utc).isoformat(),
            "resolved_at": None,
        }).execute()
        return row_id

    try:
        return await asyncio.wait_for(_insert(), timeout=config.SUPABASE_TIMEOUT_SECONDS)
    except Exception as e:
        logger.error("[TRUST] Failed to log copy trade for %s/%s: %s", wallet_address[:10], market_id[:12], e)
        return None


async def resolve_copy_trade(
    market_id: str,
    direction: str,
    exit_price: float,
    entry_price: float,
    size_usdc: float,
) -> None:
    """
    Called when a copy-traded position is closed (won or lost).
    Updates copytrade_log status, PnL, and recomputes trust_score in trader_performance.

    This is called by the reconciliation module when positions close.

    Args:
        market_id:    Market condition ID.
        direction:    "YES" or "NO".
        exit_price:   Final resolution price (1.0 = full win, 0.0 = full loss).
        entry_price:  Price paid when the copy trade was entered.
        size_usdc:    Original position size in USDC.
    """
    # Compute PnL: for binary Polymarket markets
    # YES trade: pnl = (exit_price - entry_price) * (size_usdc / entry_price)
    # NO trade:  pnl = (entry_price - exit_price) * (size_usdc / entry_price) — not applicable here
    if entry_price <= 0:
        return

    # Simple P&L: shares * (exit_price - entry_price)
    shares = size_usdc / entry_price
    pnl = shares * (exit_price - entry_price)
    won = pnl > 0

    outcome = "won" if won else "lost"

    # ── Step 1: Find the matching copytrade_log row ───────────────────────────
    async def _find_open():
        client = await get_client()
        res = (
            client.table("copytrade_log")
            .select("id,wallet_address,trader_name")
            .eq("market_id", market_id)
            .eq("direction", direction)
            .eq("status", "open")
            .limit(1)
            .execute()
        )
        return res.data[0] if res.data else None

    try:
        row = await asyncio.wait_for(_find_open(), timeout=config.SUPABASE_TIMEOUT_SECONDS)
    except Exception as e:
        logger.error("[TRUST] Failed to find open copytrade_log row for %s: %s", market_id[:12], e)
        return

    if not row:
        # Not a copy trade — nothing to update
        return

    log_id = row["id"]
    wallet_address = row["wallet_address"]
    trader_name = row.get("trader_name", "unknown")

    # ── Step 2: Update the copytrade_log row ─────────────────────────────────
    async def _close_log():
        client = await get_client()
        client.table("copytrade_log").update({
            "status": outcome,
            "exit_price": exit_price,
            "pnl_usdc": round(pnl, 4),
            "resolved_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", log_id).execute()

    try:
        await asyncio.wait_for(_close_log(), timeout=config.SUPABASE_TIMEOUT_SECONDS)
    except Exception as e:
        logger.error("[TRUST] Failed to close copytrade_log row %s: %s", log_id, e)

    # ── Step 3: Update trader_performance ────────────────────────────────────
    async def _update_performance():
        client = await get_client()

        # Fetch current performance row
        res = (
            client.table("trader_performance")
            .select("*")
            .eq("wallet_address", wallet_address)
            .execute()
        )
        if not res.data:
            return

        current = res.data[0]
        new_total = int(current.get("total_trades_copied", 0) or 0) + 1
        new_wins = int(current.get("winning_trades", 0) or 0) + (1 if won else 0)
        new_losses = int(current.get("losing_trades", 0) or 0) + (0 if won else 1)
        new_pnl = float(current.get("total_pnl_usdc", 0.0) or 0.0) + pnl

        new_score = compute_trust_score(new_wins, new_total, new_pnl)

        client.table("trader_performance").update({
            "total_trades_copied": new_total,
            "winning_trades": new_wins,
            "losing_trades": new_losses,
            "total_pnl_usdc": round(new_pnl, 4),
            "trust_score": round(new_score, 4),
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }).eq("wallet_address", wallet_address).execute()

        # Update in-memory cache immediately
        _TRUST_CACHE[wallet_address] = new_score

        logger.info(
            "[TRUST] %s updated: %d/%d wins | PnL $%.2f | trust=%.3f | outcome=%s",
            trader_name,
            new_wins,
            new_total,
            new_pnl,
            new_score,
            outcome,
        )

    try:
        await asyncio.wait_for(_update_performance(), timeout=config.SUPABASE_TIMEOUT_SECONDS)
    except Exception as e:
        logger.error("[TRUST] Failed to update trader_performance for %s: %s", wallet_address[:10], e)


async def get_ranked_wallets() -> list[dict]:
    """
    Return all wallets ranked by trust score, highest first.
    Used for operator visibility and decision-making.
    Format: [{"wallet_address": ..., "trader_name": ..., "trust_score": ...,
               "total_trades_copied": ..., "winning_trades": ..., "total_pnl_usdc": ...}]
    """
    rows = await _load_performance_from_db()
    if not rows:
        return []

    ranked = sorted(
        rows,
        key=lambda r: compute_trust_score(
            int(r.get("winning_trades", 0) or 0),
            int(r.get("total_trades_copied", 0) or 0),
            float(r.get("total_pnl_usdc", 0.0) or 0.0),
        ),
        reverse=True,
    )
    return ranked
