"""
copytrade/poller.py — Strategy 5: Copy Edge

Polls the Polymarket Gamma API for each wallet in the `tracked_wallets`
Supabase table every COPY_POLL_INTERVAL_SECONDS (5) seconds.

Architecture:
    run_copy_poller()    — Public entry point. Starts per-wallet subtasks.
    _reload_wallets()    — Refreshes wallet list from Supabase every 5 min.
    _poll_wallet()       — Polls one wallet in a tight loop.
    _fetch_gamma_trades()— Performs the actual HTTP call to Gamma API.

Safety rules (mirrors GEMINI.md):
    - All Supabase reads have 2-second timeouts. Fail closed on DB timeout.
    - All Gamma API calls have GAMMA_API_TIMEOUT_SECONDS timeout.
    - Seen-trade deduplication via in-memory set per wallet (max 500 entries).
    - No LLM calls from this file.
    - All config sourced from config.py — nothing hardcoded.
"""

import asyncio
import logging
import time
from typing import Optional

import aiohttp

import config
from memory.supabase_client import get_client
from copytrade.performance_tracker import refresh_trust_cache

logger = logging.getLogger(__name__)

# ── In-memory seen-trade sets (keyed by wallet_address) ──────────────────────
# Prevents the same trade from being re-emitted across polling cycles.
# Capped at 500 entries per wallet to prevent unbounded memory growth.
_SEEN_TRADES: dict[str, set[str]] = {}
_SEEN_TRADES_MAX = 500


def _record_seen(wallet: str, trade_id: str) -> None:
    """Mark a trade ID as seen for a given wallet."""
    seen = _SEEN_TRADES.setdefault(wallet, set())
    if len(seen) >= _SEEN_TRADES_MAX:
        # Evict oldest half by converting to list and slicing.
        # This is an approximation — sets have no guaranteed order, but we only
        # need "seen or not seen", so evicting any 250 is acceptable.
        items = list(seen)
        _SEEN_TRADES[wallet] = set(items[250:])
        seen = _SEEN_TRADES[wallet]
    seen.add(trade_id)


def _is_seen(wallet: str, trade_id: str) -> bool:
    return trade_id in _SEEN_TRADES.get(wallet, set())


# ── Gamma API fetch ───────────────────────────────────────────────────────────

async def _fetch_gamma_trades(
    session: aiohttp.ClientSession,
    wallet_address: str,
) -> list[dict]:
    """
    Fetch the latest trades for a wallet from the Gamma API.

    Endpoint: GET /trades?maker={wallet}
    Returns: list of trade dicts, or [] on any error.

    Gamma trade dict keys of interest:
        id          — Unique trade identifier
        market      — Market condition ID (maps to our market_id)
        outcome     — "Yes" | "No"
        price       — Execution price as float string (e.g. "0.62")
        size        — USDC size of the trade
        side        — "BUY" | "SELL"
        timestamp   — Unix timestamp of execution
        volume      — Total market volume at time of trade
    """
    url = f"{config.GAMMA_API_URL}/trades"
    params = {"maker": wallet_address, "limit": 20}

    try:
        async with asyncio.timeout(config.GAMMA_API_TIMEOUT_SECONDS):
            async with session.get(url, params=params) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    # Gamma returns either a list or {"results": [...]}
                    if isinstance(data, list):
                        return data
                    if isinstance(data, dict):
                        return data.get("results", data.get("data", []))
                else:
                    logger.warning(
                        "[COPY_POLLER] Gamma API returned HTTP %d for wallet %s",
                        resp.status,
                        wallet_address[:10] + "...",
                    )
    except asyncio.TimeoutError:
        logger.warning(
            "[COPY_POLLER] Gamma API timed out for wallet %s (>%ds)",
            wallet_address[:10] + "...",
            config.GAMMA_API_TIMEOUT_SECONDS,
        )
    except Exception as e:
        logger.error(
            "[COPY_POLLER] Gamma API fetch failed for wallet %s: %s",
            wallet_address[:10] + "...",
            e,
        )
    return []


# ── Per-wallet polling loop ───────────────────────────────────────────────────

async def _poll_wallet(
    wallet: dict,
    signal_queue: asyncio.Queue,
) -> None:
    """
    Poll a single wallet in a tight loop every COPY_POLL_INTERVAL_SECONDS.

    For each new trade detected:
        1. Check it hasn't been seen before.
        2. Filter out SELL-side transactions (we don't copy exits).
        3. Push a signal dict onto signal_queue for the classifier to consume.

    Args:
        wallet: Row from tracked_wallets table.
        signal_queue: Shared queue consumed by copytrade.classifier.
    """
    address = wallet["wallet_address"]
    trader_name = wallet.get("trader_name", address[:8])
    class_type = wallet.get("class_type", "B")

    logger.info(
        "[COPY_POLLER] Starting polling loop for %s (%s) — Class %s",
        trader_name,
        address[:10] + "...",
        class_type,
    )

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                trades = await _fetch_gamma_trades(session, address)

                for trade in trades:
                    trade_id = str(trade.get("id", ""))
                    if not trade_id:
                        continue

                    # Skip already-seen trades
                    if _is_seen(address, trade_id):
                        continue

                    # Mark as seen immediately to prevent double-emit
                    _record_seen(address, trade_id)

                    # Filter: only copy BUY-side entries
                    side = str(trade.get("side", "BUY")).upper()
                    if side != "BUY":
                        logger.debug(
                            "[COPY_POLLER] Skipping SELL from %s (trade_id=%s)",
                            trader_name,
                            trade_id,
                        )
                        continue

                    # Emit signal to classifier queue
                    signal = {
                        "source": "copy_edge",
                        "wallet_address": address,
                        "trader_name": trader_name,
                        "class_type": class_type,
                        "trade_id": trade_id,
                        "market_id": str(trade.get("market", "") or trade.get("conditionId", "")),
                        "outcome": str(trade.get("outcome", "Yes")),
                        "tracker_price": float(trade.get("price", 0.50) or 0.50),
                        "tracker_size_usdc": float(trade.get("size", 0.0) or 0.0),
                        "market_volume_usd": float(trade.get("volume", 0.0) or 0.0),
                        "detected_at": time.time(),
                    }
                    logger.info(
                        "[COPY_POLLER] New signal from %s | market=%s | outcome=%s | price=%.3f | vol=$%.0f",
                        trader_name,
                        signal["market_id"][:12] + "...",
                        signal["outcome"],
                        signal["tracker_price"],
                        signal["market_volume_usd"],
                    )

                    try:
                        signal_queue.put_nowait(signal)
                    except asyncio.QueueFull:
                        logger.warning(
                            "[COPY_POLLER] Signal queue full — dropping trade %s from %s",
                            trade_id,
                            trader_name,
                        )

            except asyncio.CancelledError:
                logger.info("[COPY_POLLER] Wallet polling loop cancelled for %s", trader_name)
                raise
            except Exception as e:
                logger.error("[COPY_POLLER] Unexpected error in poll loop for %s: %s", trader_name, e)

            await asyncio.sleep(config.COPY_POLL_INTERVAL_SECONDS)


# ── Wallet loader ─────────────────────────────────────────────────────────────

async def _load_active_wallets() -> list[dict]:
    """
    Fetch active wallets from the Supabase `tracked_wallets` table.
    Returns [] if DB is unreachable (fail-open for wallet loading — the bot
    just polls no one rather than crashing entirely).
    """
    async def _query() -> list[dict]:
        client = await get_client()
        res = (
            client.table("tracked_wallets")
            .select("wallet_address,trader_name,class_type")
            .eq("is_active", True)
            .execute()
        )
        return res.data or []

    try:
        return await asyncio.wait_for(_query(), timeout=config.SUPABASE_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        logger.error("[COPY_POLLER] Supabase timed out loading tracked_wallets. Retrying next cycle.")
        return []
    except Exception as e:
        logger.error("[COPY_POLLER] Failed to load tracked_wallets: %s", e)
        return []


# ── Public entry point ────────────────────────────────────────────────────────

async def run_copy_poller(signal_queue: asyncio.Queue) -> None:
    """
    Main CopyTrade poller task. Reloads wallets from Supabase every
    COPY_WALLET_RELOAD_INTERVAL_SECONDS (300) and (re)starts per-wallet tasks.

    This function runs indefinitely as a background asyncio task.

    Args:
        signal_queue: Queue shared with copytrade.classifier. Max size set
                      by config.COPY_SIGNAL_QUEUE_MAXSIZE.
    """
    logger.info("[COPY_POLLER] Starting CopyTrade polling engine (Strategy 5: Copy Edge).")

    wallet_tasks: dict[str, asyncio.Task] = {}

    while True:
        try:
            wallets = await _load_active_wallets()
            active_addresses = {w["wallet_address"] for w in wallets}

            # Refresh trust score cache so classifier has fresh scores each cycle
            await refresh_trust_cache()

            # Cancel tasks for wallets that are no longer active
            for addr, task in list(wallet_tasks.items()):
                if addr not in active_addresses:
                    logger.info("[COPY_POLLER] Deactivating wallet %s", addr[:10] + "...")
                    task.cancel()
                    del wallet_tasks[addr]

            # Start tasks for newly added wallets
            for wallet in wallets:
                addr = wallet["wallet_address"]
                if addr not in wallet_tasks or wallet_tasks[addr].done():
                    task = asyncio.create_task(
                        _poll_wallet(wallet, signal_queue),
                        name=f"copy_poll_{addr[:8]}",
                    )
                    wallet_tasks[addr] = task
                    logger.info(
                        "[COPY_POLLER] Started polling task for %s (%s)",
                        wallet.get("trader_name", "unknown"),
                        addr[:10] + "...",
                    )

            if not wallets:
                logger.info(
                    "[COPY_POLLER] No active wallets in tracked_wallets table. "
                    "Add wallets via Supabase to begin copy-trading."
                )

        except asyncio.CancelledError:
            logger.info("[COPY_POLLER] Main poller loop cancelled. Cleaning up wallet tasks.")
            for task in wallet_tasks.values():
                task.cancel()
            raise
        except Exception as e:
            logger.error("[COPY_POLLER] Wallet reload error: %s", e)

        await asyncio.sleep(config.COPY_WALLET_RELOAD_INTERVAL_SECONDS)
