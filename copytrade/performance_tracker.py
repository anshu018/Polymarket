"""
copytrade/performance_tracker.py — Strategy 5: Copy Edge

Per CopyTrade.md (finalized design, supersedes original PRD):

Trust Score Formula (CopyTrade.md §3.2):
    bayesian_win_rate = (wins + 5) / (wins + losses + 10)

    Rationale:
        - The +5/+10 terms are a weak prior — assumes 50% until real evidence accumulates.
        - A wallet with 2 wins / 0 losses scores 7/12 = 0.58 (not 1.0).
        - A wallet with 18 wins / 2 losses scores 23/30 = 0.77 — real evidence, real trust.

Wallet States (CopyTrade.md §3.4):
    NEW       — < 20 resolved copy-trades. 0.5× size multiplier.
    ACTIVE    — ≥ 20 resolved trades, win_rate ≥ 52%, avg_roi ≥ -2%. Full size.
    PROBATION — win_rate drops < 52% OR avg_roi drops < -2% while ACTIVE. 0.5× size.
    RETIRED   — Stays PROBATION for 20 more resolved trades without recovering. is_active=false.

Priority Wallet (CopyTrade.md §3.6):
    is_priority = trust_score >= 0.80 AND resolved_trades_count >= 30
    Recomputed on every trade resolution. Wins conflicts automatically.

Safety rules:
    - All DB writes are non-blocking on failure (best-effort, never crash bot).
    - All DB reads have 2-second timeouts with fail-open fallback (return 0.5).
    - In-memory trust score cache + priority cache (refreshed every wallet reload cycle).
    - No LLM calls. No external API calls.
    - Single source of truth: tracked_wallets table only (no trader_performance).
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

import config
from memory.supabase_client import get_client

logger = logging.getLogger(__name__)

# ── Trust score constants (CopyTrade.md §3.2) ─────────────────────────────────
TRUST_WIN_PRIOR: int = 5          # Bayesian prior wins
TRUST_TOTAL_PRIOR: int = 10       # Bayesian prior total trades
TRUST_DEFAULT_SCORE: float = 0.50 # Score for wallets with no history (5/10)
TRUST_MIN_SCORE: float = 0.0
TRUST_MAX_SCORE: float = 1.0

# ── Wallet state thresholds (CopyTrade.md §3.4) ───────────────────────────────
WALLET_MIN_RESOLVED_FOR_ACTIVE: int = 20    # Trades needed to graduate from NEW
WALLET_WIN_RATE_FLOOR: float = 0.52         # Below this → PROBATION
WALLET_WIN_RATE_REINSTATE: float = 0.55     # Must recover to this to exit PROBATION
WALLET_AVG_ROI_FLOOR: float = -0.02         # -2% avg ROI → PROBATION trigger
WALLET_PROBATION_RETIREMENT_TRADES: int = 20 # Trades in PROBATION before RETIREMENT
WALLET_PROBATION_REINSTATE_WINDOW: int = 10  # Trades window for reinstatement check

# ── Priority wallet thresholds (CopyTrade.md §3.6) ────────────────────────────
PRIORITY_TRUST_THRESHOLD: float = 0.80
PRIORITY_MIN_RESOLVED_TRADES: int = 30

# ── Size state multipliers (CopyTrade.md §4) ──────────────────────────────────
STATE_MULTIPLIER_NEW: float = 0.5
STATE_MULTIPLIER_PROBATION: float = 0.5
STATE_MULTIPLIER_ACTIVE: float = 1.0

# ── In-memory caches ──────────────────────────────────────────────────────────
# Refreshed every wallet reload cycle so classifier never hits DB per-signal.
_TRUST_CACHE: dict[str, float] = {}       # {wallet_address: trust_score}
_PRIORITY_CACHE: dict[str, bool] = {}     # {wallet_address: is_priority}
_STATE_CACHE: dict[str, str] = {}         # {wallet_address: state}


# ── Core formula (CopyTrade.md §3.2) ─────────────────────────────────────────

def compute_trust_score(wins: int, losses: int) -> float:
    """
    Compute the Bayesian-damped trust score for a wallet.

    Formula: (wins + 5) / (wins + losses + 10)

    This is the ONLY formula used — replacing the old confidence-blend approach.

    Returns a float in [0.0, 1.0]:
        0.50 = neutral / unknown (0 wins, 0 losses)
        0.77 = 18W/2L (high evidence, real trust)
        0.58 = 2W/0L (suspicious perfection, dampened correctly)
    """
    return float((wins + TRUST_WIN_PRIOR) / (wins + losses + TRUST_TOTAL_PRIOR))


def compute_is_priority(trust_score: float, resolved_trades_count: int) -> bool:
    """
    Derive the is_priority flag per CopyTrade.md §3.6.
    A derived label — not separately maintained.
    """
    return trust_score >= PRIORITY_TRUST_THRESHOLD and resolved_trades_count >= PRIORITY_MIN_RESOLVED_TRADES


def compute_state_multiplier(state: str) -> float:
    """Return the size multiplier for a given wallet state (CopyTrade.md §4)."""
    if state in ("NEW", "PROBATION"):
        return 0.5
    return 1.0


# ── Cache accessors ───────────────────────────────────────────────────────────

def get_trust_score(wallet_address: str) -> float:
    """Return cached trust score. Falls back to 0.50 for unknown wallets."""
    return _TRUST_CACHE.get(wallet_address, TRUST_DEFAULT_SCORE)


def get_is_priority(wallet_address: str) -> bool:
    """Return cached priority flag. False for unknown wallets."""
    return _PRIORITY_CACHE.get(wallet_address, False)


def get_wallet_state(wallet_address: str) -> str:
    """Return cached wallet state. Falls back to 'NEW' for unknown wallets."""
    return _STATE_CACHE.get(wallet_address, "NEW")


# ── Conflict resolution (CopyTrade.md §3.6) ───────────────────────────────────

def resolve_conflict(wallet_a: str, wallet_b: str) -> str:
    """
    Given two wallet addresses competing for the same market signal,
    return the address of the wallet that should win.

    Priority resolution rules (CopyTrade.md §3.6):
        1. If exactly one wallet is Priority → it wins automatically.
        2. If both are Priority → highest trust score wins (scoped to Priority pool).
        3. If neither is Priority → highest trust score wins (original behaviour).
        4. Equal scores → wallet_a wins (first-come).
    """
    priority_a = get_is_priority(wallet_a)
    priority_b = get_is_priority(wallet_b)
    score_a = get_trust_score(wallet_a)
    score_b = get_trust_score(wallet_b)

    # Case 1: Exactly one is Priority
    if priority_a and not priority_b:
        logger.info(
            "[TRUST] Conflict resolved: %s (PRIORITY) wins over %s (%.3f)",
            wallet_a[:10], wallet_b[:10], score_b,
        )
        return wallet_a
    if priority_b and not priority_a:
        logger.info(
            "[TRUST] Conflict resolved: %s (PRIORITY) wins over %s (%.3f)",
            wallet_b[:10], wallet_a[:10], score_a,
        )
        return wallet_b

    # Case 2 & 3: Both Priority or neither — use trust score
    if score_b > score_a:
        logger.info(
            "[TRUST] Conflict resolved: %s (%.3f) preferred over %s (%.3f)%s",
            wallet_b[:10], score_b, wallet_a[:10], score_a,
            " [both PRIORITY]" if priority_a and priority_b else "",
        )
        return wallet_b

    logger.info(
        "[TRUST] Conflict resolved: %s (%.3f) preferred over %s (%.3f)%s",
        wallet_a[:10], score_a, wallet_b[:10], score_b,
        " [both PRIORITY]" if priority_a and priority_b else "",
    )
    return wallet_a


# ── DB helpers ────────────────────────────────────────────────────────────────

async def _load_wallets_from_db() -> list[dict]:
    """
    Load all rows from tracked_wallets (single source of truth per §7.3).
    Fail-open: return [] on error.
    """
    async def _q():
        client = await get_client()
        res = client.table("tracked_wallets").select("*").execute()
        return res.data or []

    try:
        return await asyncio.wait_for(_q(), timeout=config.SUPABASE_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        logger.error("[TRUST] Supabase tracked_wallets read timed out — using cached scores.")
        return []
    except Exception as e:
        logger.error("[TRUST] Failed to load tracked_wallets: %s", e)
        return []


async def refresh_trust_cache() -> None:
    """
    Reload trust scores, priority flags, and states from Supabase into memory.
    Called by the poller during wallet reload so the classifier never
    blocks on a DB call per-signal.
    """
    rows = await _load_wallets_from_db()
    updated = 0
    for row in rows:
        addr = row.get("wallet_address", "")
        if not addr:
            continue
        wins = int(row.get("wins_count", 0) or 0)
        losses = int(row.get("losses_count", 0) or 0)
        resolved = int(row.get("resolved_trades_count", 0) or 0)
        state = str(row.get("state", "NEW") or "NEW")

        score = compute_trust_score(wins, losses)
        is_priority = compute_is_priority(score, resolved)

        _TRUST_CACHE[addr] = score
        _PRIORITY_CACHE[addr] = is_priority
        _STATE_CACHE[addr] = state
        updated += 1

    logger.info("[TRUST] Trust cache refreshed: %d wallets scored.", updated)


# ── Wallet state machine (CopyTrade.md §3.4) ──────────────────────────────────

def _compute_new_state(
    current_state: str,
    resolved_trades_count: int,
    wins_count: int,
    losses_count: int,
    avg_roi_per_trade: float,
    probation_resolved_at_entry: int,
) -> str:
    """
    Pure function — no network calls. Computes the next wallet state
    given current stats. Mirrors the state diagram in CopyTrade.md §3.5.

    Args:
        current_state:              Current wallet state string.
        resolved_trades_count:      Total resolved copy-trades for this wallet.
        wins_count / losses_count:  Win/loss counts.
        avg_roi_per_trade:          Rolling average PnL percent.
        probation_resolved_at_entry: resolved_trades_count when PROBATION started.
                                     0 if not currently in PROBATION.

    Returns:
        New state string.
    """
    total = wins_count + losses_count
    win_rate = wins_count / total if total > 0 else 0.0

    if current_state == "NEW":
        if resolved_trades_count >= WALLET_MIN_RESOLVED_FOR_ACTIVE:
            if win_rate >= WALLET_WIN_RATE_FLOOR and avg_roi_per_trade >= WALLET_AVG_ROI_FLOOR:
                return "ACTIVE"
        return "NEW"

    if current_state == "ACTIVE":
        if win_rate < WALLET_WIN_RATE_FLOOR or avg_roi_per_trade < WALLET_AVG_ROI_FLOOR:
            return "PROBATION"
        return "ACTIVE"

    if current_state == "PROBATION":
        trades_since_entry = resolved_trades_count - probation_resolved_at_entry
        # Retirement check: 20+ trades in PROBATION without recovering
        if trades_since_entry >= WALLET_PROBATION_RETIREMENT_TRADES:
            if win_rate < WALLET_WIN_RATE_FLOOR or avg_roi_per_trade < WALLET_AVG_ROI_FLOOR:
                return "RETIRED"
        # Reinstatement check: 10+ trades at reinstatement win rate
        if trades_since_entry >= WALLET_PROBATION_REINSTATE_WINDOW:
            if win_rate >= WALLET_WIN_RATE_REINSTATE and avg_roi_per_trade >= WALLET_AVG_ROI_FLOOR:
                return "ACTIVE"
        return "PROBATION"

    # RETIRED — stays RETIRED (audit trail preserved, is_active=false)
    return "RETIRED"


# ── Trade logging ─────────────────────────────────────────────────────────────

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
    was_priority_pick: bool = False,
) -> Optional[str]:
    """
    Log a newly executed copy trade to the `copytrade_log` table.

    Returns the UUID of the newly created row (for later outcome linking).
    Returns None on failure (non-critical — bot continues).

    Args:
        was_priority_pick: True only when this trade was taken because its
                           source wallet won a conflict under Priority rules (§3.6).
    """
    async def _insert():
        import uuid as _uuid
        client = await get_client()
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
            "was_priority_pick": was_priority_pick,
            "status": "open",
            "exit_price": None,
            "pnl_usdc": None,
            "pnl_percent": None,
            "opened_at": datetime.now(timezone.utc).isoformat(),
            "resolved_at": None,
        }).execute()
        return row_id

    try:
        return await asyncio.wait_for(_insert(), timeout=config.SUPABASE_TIMEOUT_SECONDS)
    except Exception as e:
        logger.error(
            "[TRUST] Failed to log copy trade for %s/%s: %s",
            wallet_address[:10], market_id[:12], e,
        )
        return None


# ── Trade resolution & state transitions ──────────────────────────────────────

async def resolve_copy_trade(
    market_id: str,
    direction: str,
    exit_price: float,
    entry_price: float,
    size_usdc: float,
) -> None:
    """
    Called when a copy-traded position is closed (won or lost).

    Steps:
        1. Find the open copytrade_log row for this market/direction.
        2. Compute PnL and pnl_percent.
        3. Update copytrade_log with outcome.
        4. Update tracked_wallets: wins/losses/avg_roi/trust_score/is_priority/state.
        5. Refresh in-memory cache for this wallet.
    """
    if entry_price <= 0:
        return

    shares = size_usdc / entry_price
    pnl = shares * (exit_price - entry_price)
    pnl_percent = (exit_price - entry_price) / entry_price  # Return per dollar invested
    won = pnl > 0
    outcome = "won" if won else "lost"

    # ── Step 1: Find open copytrade_log row ───────────────────────────────────
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
        return  # Not a copy trade — nothing to update

    log_id = row["id"]
    wallet_address = row["wallet_address"]
    trader_name = row.get("trader_name", "unknown")

    # ── Step 2: Close the copytrade_log row ───────────────────────────────────
    async def _close_log():
        client = await get_client()
        client.table("copytrade_log").update({
            "status": outcome,
            "exit_price": exit_price,
            "pnl_usdc": round(pnl, 4),
            "pnl_percent": round(pnl_percent, 6),
            "resolved_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", log_id).execute()

    try:
        await asyncio.wait_for(_close_log(), timeout=config.SUPABASE_TIMEOUT_SECONDS)
    except Exception as e:
        logger.error("[TRUST] Failed to close copytrade_log row %s: %s", log_id, e)

    # ── Step 3: Update tracked_wallets ────────────────────────────────────────
    async def _update_wallet():
        client = await get_client()

        # Fetch current wallet row
        res = (
            client.table("tracked_wallets")
            .select("*")
            .eq("wallet_address", wallet_address)
            .execute()
        )
        if not res.data:
            logger.warning("[TRUST] No tracked_wallets row found for %s", wallet_address[:10])
            return

        current = res.data[0]
        new_wins = int(current.get("wins_count", 0) or 0) + (1 if won else 0)
        new_losses = int(current.get("losses_count", 0) or 0) + (0 if won else 1)
        new_resolved = int(current.get("resolved_trades_count", 0) or 0) + 1

        # Recompute avg_roi_per_trade (rolling average)
        prev_avg_roi = float(current.get("avg_roi_per_trade", 0.0) or 0.0)
        prev_resolved = new_resolved - 1
        if prev_resolved > 0:
            new_avg_roi = (prev_avg_roi * prev_resolved + pnl_percent) / new_resolved
        else:
            new_avg_roi = pnl_percent

        # Recompute trust score and priority
        new_score = compute_trust_score(new_wins, new_losses)
        new_is_priority = compute_is_priority(new_score, new_resolved)

        # Compute new state via state machine
        current_state = str(current.get("state", "NEW") or "NEW")
        probation_resolved_at_entry = int(current.get("probation_resolved_at_entry", 0) or 0)
        new_state = _compute_new_state(
            current_state=current_state,
            resolved_trades_count=new_resolved,
            wins_count=new_wins,
            losses_count=new_losses,
            avg_roi_per_trade=new_avg_roi,
            probation_resolved_at_entry=probation_resolved_at_entry,
        )

        # Handle state transitions
        now_iso = datetime.now(timezone.utc).isoformat()
        update_payload: dict = {
            "wins_count": new_wins,
            "losses_count": new_losses,
            "resolved_trades_count": new_resolved,
            "avg_roi_per_trade": round(new_avg_roi, 6),
            "trust_score": round(new_score, 4),
            "is_priority": new_is_priority,
            "state": new_state,
            "is_active": new_state != "RETIRED",
            "last_updated_at": now_iso,
        }

        if new_state == "PROBATION" and current_state != "PROBATION":
            # Entering PROBATION: record the entry point for the retirement counter
            update_payload["probation_entered_at"] = now_iso
            update_payload["probation_resolved_at_entry"] = new_resolved
            logger.warning(
                "[TRUST] %s entered PROBATION at %d resolved trades (win_rate=%.3f avg_roi=%.4f)",
                trader_name, new_resolved, new_wins / (new_wins + new_losses) if (new_wins + new_losses) > 0 else 0.0,
                new_avg_roi,
            )
        elif new_state == "ACTIVE" and current_state == "PROBATION":
            # Exiting PROBATION: clear entry marker
            update_payload["probation_entered_at"] = None
            update_payload["probation_resolved_at_entry"] = 0
            logger.info(
                "[TRUST] %s reinstated to ACTIVE from PROBATION at %d resolved trades",
                trader_name, new_resolved,
            )
        elif new_state == "RETIRED":
            logger.warning(
                "[TRUST] %s RETIRED at %d resolved trades — is_active set to false. "
                "Audit trail preserved.",
                trader_name, new_resolved,
            )

        client.table("tracked_wallets").update(update_payload).eq("wallet_address", wallet_address).execute()

        # Update in-memory cache immediately
        _TRUST_CACHE[wallet_address] = new_score
        _PRIORITY_CACHE[wallet_address] = new_is_priority
        _STATE_CACHE[wallet_address] = new_state

        logger.info(
            "[TRUST] %s resolved %s | wins=%d losses=%d resolved=%d | "
            "trust=%.3f priority=%s state=%s | roi=%.4f",
            trader_name, outcome.upper(),
            new_wins, new_losses, new_resolved,
            new_score, new_is_priority, new_state,
            new_avg_roi,
        )

    try:
        await asyncio.wait_for(_update_wallet(), timeout=config.SUPABASE_TIMEOUT_SECONDS)
    except Exception as e:
        logger.error("[TRUST] Failed to update tracked_wallets for %s: %s", wallet_address[:10], e)


# ── Ranked wallet view ────────────────────────────────────────────────────────

async def get_ranked_wallets() -> list[dict]:
    """
    Return all wallets ranked by trust score, highest first.
    Reads from tracked_wallets (single source of truth).

    Format:
        [{
            "wallet_address": ..., "trader_name": ..., "state": ...,
            "trust_score": ..., "is_priority": ...,
            "resolved_trades_count": ..., "wins_count": ..., "losses_count": ...,
            "avg_roi_per_trade": ...
        }]
    """
    rows = await _load_wallets_from_db()
    if not rows:
        return []

    ranked = sorted(
        rows,
        key=lambda r: compute_trust_score(
            int(r.get("wins_count", 0) or 0),
            int(r.get("losses_count", 0) or 0),
        ),
        reverse=True,
    )
    return ranked
