"""
execution/paper_fills.py — A5: paper maker-fill monitor (List A.md Step 5).

Resolves resting paper maker orders against the live CLOB book. The open_positions
row IS the state (order_type='maker', fill_price NULL = still resting), so the
monitor is restart-safe by construction: a restart simply re-scans the same rows —
no in-memory registry to lose. Started as a supervisor task in main.py.

Per resting order, each tick (config.PAPER_MAKER_CHECK_SECONDS):
    - inside the wait window (opened_at + MAKER_FALLBACK_SECONDS):
        filled when the book trades through the resting level
        (risk/fill_simulator.maker_is_filled) → fill_type='maker_fill'.
    - past the wait window: the Step 1 fallback rule —
        taker net edge recomputed at the CURRENT book
        (maker_fallback_net_edge); strictly above
        MAKER_FALLBACK_MIN_NET_EDGE_CENTS AND the taker size fits the depth
        → fill at the taker price, fill_type='taker_fallback';
        otherwise the order is cancelled: the open_positions row is deleted
        (the position never existed) and the idempotency log entry is marked
        'cancelled' (best-effort).

Every row is handled in its own try/except — one bad row can never stall the
others — and every DB/HTTP failure is fail-soft: the order simply stays resting
and the next tick retries. This is a resolution service for simulated paper
orders, not a capital gate; nothing here can block live trading.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Optional

import config
from data.market_discovery import get_market_book
from memory.supabase_client import get_client
from risk import cost_model, fill_simulator

logger = logging.getLogger(__name__)

# A resting scan larger than this means something is wrong upstream (e.g. the
# monitor has been down for a very long time); the oldest orders surface first
# via the opened_at index and the rest wait for the next tick.
_MAX_SCAN_ROWS = 100


async def _fetch_pending_maker_orders() -> list[dict[str, Any]]:
    """Unfilled resting maker orders (2s timeout; failure → fail-soft empty)."""
    async def _read() -> list[dict[str, Any]]:
        client = await get_client()
        res = (
            client.table("open_positions")
            .select(
                "id,market_id,direction,category,position_size_usdc,"
                "p_model_at_entry,order_type,fill_price,resting_price,"
                "opened_at,idempotency_uuid"
            )
            .eq("order_type", "maker")
            .is_("fill_price", "null")
            .order("opened_at")
            .limit(_MAX_SCAN_ROWS)
            .execute()
        )
        return res.data or []

    try:
        async with asyncio.timeout(config.SUPABASE_TIMEOUT_SECONDS):
            return await _read()
    except Exception as e:
        logger.warning(
            "[PAPER_FILLS] Pending-maker scan failed (fail-soft, retries next tick): %s", e
        )
        return []


def _parse_opened_at(row: dict[str, Any]) -> Optional[datetime]:
    """Parse the row's opened_at timestamp; None when missing/unparseable."""
    raw = row.get("opened_at")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


async def _mark_filled(row_id: str, fill_price: float, fill_type: str) -> bool:
    """Persist a fill on the resting row (2s timeout). True on success."""
    async def _update() -> None:
        client = await get_client()
        (
            client.table("open_positions")
            .update({"fill_price": fill_price, "fill_type": fill_type})
            .eq("id", row_id)
            .execute()
        )

    try:
        async with asyncio.timeout(config.SUPABASE_TIMEOUT_SECONDS):
            await _update()
        return True
    except Exception as e:
        logger.error("[PAPER_FILLS] Failed to record fill for %s: %s", row_id, e)
        return False


async def _cancel_order(row: dict[str, Any]) -> None:
    """Delete the never-filled position and mark its idempotency entry cancelled.

    Best-effort on both writes: a failed delete leaves the row resting and the
    next tick retries (fill_price still NULL); a failed idempotency mark is
    harmless — 'pending' entries do not block future order UUIDs.
    """
    row_id = str(row.get("id"))
    market_id = str(row.get("market_id") or "")

    async def _delete() -> None:
        client = await get_client()
        client.table("open_positions").delete().eq("id", row_id).execute()

    try:
        async with asyncio.timeout(config.SUPABASE_TIMEOUT_SECONDS):
            await _delete()
        logger.info(
            "[PAPER_FILLS] Cancelled unfilled maker order %s (market=%s)",
            row_id, market_id[:16],
        )
    except Exception as e:
        logger.error("[PAPER_FILLS] Failed to delete cancelled order %s: %s", row_id, e)
        return

    idempotency_uuid = row.get("idempotency_uuid")
    if not idempotency_uuid:
        return

    async def _mark() -> None:
        client = await get_client()
        (
            client.table("idempotency_log")
            .update({"status": "cancelled"})
            .eq("id", str(idempotency_uuid))
            .execute()
        )

    try:
        async with asyncio.timeout(config.SUPABASE_TIMEOUT_SECONDS):
            await _mark()
    except Exception as e:
        logger.warning(
            "[PAPER_FILLS] Idempotency cancel-mark failed for %s: %s", idempotency_uuid, e
        )


async def _resolve_row(row: dict[str, Any], now: datetime) -> None:
    """Resolve one resting maker order: fill, taker-fallback, or cancel."""
    row_id = str(row.get("id"))
    market_id = str(row.get("market_id") or "")
    direction = str(row.get("direction") or "")
    resting_raw = row.get("resting_price")
    try:
        resting = float(resting_raw) if resting_raw is not None else None
    except (TypeError, ValueError):
        resting = None
    if direction not in ("YES", "NO") or resting is None or not 0.0 < resting < 1.0:
        # A resting row without a usable level/direction cannot ever be
        # honestly resolved — remove it from the resting set rather than
        # letting it sit (and log loudly: this means an upstream write bug).
        logger.error(
            "[PAPER_FILLS] Corrupt resting order %s (direction=%s resting=%s) — cancelling",
            row_id, direction, resting_raw,
        )
        await _cancel_order(row)
        return

    opened_at = _parse_opened_at(row)
    deadline = (
        opened_at.timestamp() + config.MAKER_FALLBACK_SECONDS
        if opened_at is not None else None
    )

    book = await get_market_book(market_id)
    if book is None:
        # Book unavailable: no honest fill verdict this tick — stay resting.
        logger.warning(
            "[PAPER_FILLS] Book unavailable for resting order %s (market=%s)",
            row_id, market_id[:16],
        )
        return

    if deadline is not None and now.timestamp() < deadline:
        if fill_simulator.maker_is_filled(book, direction, resting):
            if await _mark_filled(row_id, resting, "maker_fill"):
                logger.info(
                    "[PAPER_FILLS] Maker filled: market=%s level=%.4f fill_type=maker_fill",
                    market_id[:16], resting,
                )
        return

    # Past the wait window: Step 1 fallback rule at the CURRENT book.
    p_model_raw = row.get("p_model_at_entry")
    try:
        p_model = float(p_model_raw) if p_model_raw is not None else None
    except (TypeError, ValueError):
        p_model = None
    if p_model is None or not 0.0 <= p_model <= 1.0:
        # Without a model probability the fallback's net-positivity cannot be
        # evaluated — fail closed to cancel (never take blind).
        logger.info(
            "[PAPER_FILLS] Maker order %s expired without a usable p_model — cancelling (fail-closed)",
            row_id,
        )
        await _cancel_order(row)
        return

    fee_cfg = cost_model.fee_config_for(str(row.get("category") or ""))
    edge = fill_simulator.maker_fallback_net_edge(p_model, book, direction, fee_cfg)
    taker_price = fill_simulator.taker_fill_price(book, direction)
    try:
        size_usdc = float(row.get("position_size_usdc") or 0.0)
    except (TypeError, ValueError):
        size_usdc = 0.0
    if (
        edge > config.MAKER_FALLBACK_MIN_NET_EDGE_CENTS
        and taker_price is not None
        and fill_simulator.taker_depth_ok(book, size_usdc)
    ):
        if await _mark_filled(row_id, taker_price, "taker_fallback"):
            logger.info(
                "[PAPER_FILLS] Maker fallback: market=%s taker_fill=%.4f net_edge=%.4f "
                "fill_type=taker_fallback",
                market_id[:16], taker_price, edge,
            )
        return

    logger.info(
        "[PAPER_FILLS] Maker order %s expired unprofitable (net_edge=%.4f min=%.4f) — cancelling",
        row_id, edge, config.MAKER_FALLBACK_MIN_NET_EDGE_CENTS,
    )
    await _cancel_order(row)


async def check_pending_maker_orders_once(now: Optional[datetime] = None) -> int:
    """One monitor pass over all resting maker orders. Returns rows examined."""
    current = now or datetime.now(timezone.utc)
    rows = await _fetch_pending_maker_orders()
    for row in rows:
        try:
            await _resolve_row(row, current)
        except Exception as row_err:
            logger.error(
                "[PAPER_FILLS] Row resolution failed for %s (isolated, continues): %s",
                row.get("id"), row_err,
            )
    return len(rows)


async def run_paper_fill_monitor() -> None:
    """Supervisor entry: resolve resting paper maker orders forever."""
    logger.info(
        "[PAPER_FILLS] Paper maker-fill monitor started (interval=%.1fs, fallback=%ds)",
        config.PAPER_MAKER_CHECK_SECONDS, config.MAKER_FALLBACK_SECONDS,
    )
    while True:
        try:
            await check_pending_maker_orders_once()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("[PAPER_FILLS] Monitor tick failed (continues): %s", e)
        await asyncio.sleep(config.PAPER_MAKER_CHECK_SECONDS)
