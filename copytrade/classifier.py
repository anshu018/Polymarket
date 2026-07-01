"""
copytrade/classifier.py — Strategy 5: Copy Edge

Consumes signals from the poller queue, classifies them as Class A or B,
applies slippage and volume guards, then routes to the correct executor.

Classification rules (from CopyTrade.md §7.1):
    Class A (Speed/Alpha):
        - Fixed hard cap: $10 USDC. No Kelly.
        - Slippage threshold: ≤ COPY_CLASS_A_SLIPPAGE_THRESHOLD (0.010).
        - Market volume: ≥ COPY_MIN_MARKET_VOLUME_USD (25,000).
        - Bypass LLM. Execute immediately.

    Class B (Macro/Deep Value):
        - Dynamic Kelly capped at $50 USDC.
        - Slippage threshold: ≤ COPY_CLASS_B_SLIPPAGE_THRESHOLD (0.015).
        - Routed through coordinator/pipeline.run_pipeline() for LLM validation.

Conflict Resolution (trust score system):
    When two wallets signal the same market in the same polling cycle, the
    classifier consults the in-memory trust score cache. The wallet with the
    higher trust score wins. Equal trust → first-come wins.
    Trust scores are Bayesian-damped win rates (see copytrade/performance_tracker.py).

Safety rules:
    - Live ask price fetched from Polymarket CLOB, not stale cache.
    - Slippage computed as |live_ask - tracker_entry_price|.
    - Fail closed: if live price fetch fails, the signal is dropped.
    - No LLM calls from this file.
    - All config sourced from config.py.
"""

import asyncio
import logging
import time

import aiohttp

import config
from copytrade.performance_tracker import get_trust_score, resolve_conflict

logger = logging.getLogger(__name__)

# ── Per-cycle conflict resolution map ────────────────────────────────────────
# Tracks which wallet currently "owns" a market signal in the current poll cycle.
# Format: {market_id: wallet_address_that_won_the_conflict}
# Reset every COPY_CONFLICT_MAP_TTL_SECONDS seconds to avoid stale locks.
_CONFLICT_MAP: dict[str, tuple[str, float]] = {}  # {market_id: (wallet_addr, timestamp)}
COPY_CONFLICT_MAP_TTL_SECONDS: float = config.COPY_POLL_INTERVAL_SECONDS * 3  # 15s window


# ── Live price fetch ──────────────────────────────────────────────────────────

async def _fetch_live_ask(session: aiohttp.ClientSession, market_id: str) -> float | None:
    """
    Fetch the current best ask price from the Polymarket CLOB for a given market.

    We query the order book's best asks. This gives the real live price at the
    moment we want to enter — not a stale cached value.

    Returns:
        Best ask price as a float (0.0-1.0), or None if unavailable.
    """
    url = f"{config.CLOB_HOST}/book"
    params = {"token_id": market_id}

    try:
        async with asyncio.timeout(config.POLYMARKET_API_TIMEOUT_SECONDS):
            async with session.get(url, params=params) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    asks = data.get("asks", [])
                    if asks:
                        # CLOB returns asks sorted ascending by price
                        best_ask = float(asks[0].get("price", 0.0))
                        return best_ask
                    logger.warning(
                        "[COPY_CLASSIFIER] Empty asks for market %s",
                        market_id[:12] + "...",
                    )
                else:
                    logger.warning(
                        "[COPY_CLASSIFIER] CLOB order book returned HTTP %d for market %s",
                        resp.status,
                        market_id[:12] + "...",
                    )
    except asyncio.TimeoutError:
        logger.warning(
            "[COPY_CLASSIFIER] CLOB price fetch timed out for market %s (>%ds)",
            market_id[:12] + "...",
            config.POLYMARKET_API_TIMEOUT_SECONDS,
        )
    except Exception as e:
        logger.error(
            "[COPY_CLASSIFIER] CLOB fetch error for market %s: %s",
            market_id[:12] + "...",
            e,
        )
    return None


# ── Slippage check ────────────────────────────────────────────────────────────

def _compute_slippage(live_ask: float, tracker_price: float) -> float:
    """
    Slippage = live ask price − tracker's entry price.
    Positive means market moved up since the tracker entered (we'd pay more).
    """
    return abs(live_ask - tracker_price)


def _slippage_ok(slippage: float, class_type: str) -> bool:
    """Return True if slippage is within the class-specific threshold."""
    threshold = (
        config.COPY_CLASS_A_SLIPPAGE_THRESHOLD
        if class_type == "A"
        else config.COPY_CLASS_B_SLIPPAGE_THRESHOLD
    )
    if slippage > threshold:
        logger.warning(
            "[COPY_CLASSIFIER] Slippage %.4f exceeds Class %s threshold %.4f — REJECTED",
            slippage,
            class_type,
            threshold,
        )
        return False
    return True


# ── Volume check ──────────────────────────────────────────────────────────────

def _volume_ok(market_volume_usd: float) -> bool:
    """Guard against thin markets and wash-trading manipulation."""
    if market_volume_usd < config.COPY_MIN_MARKET_VOLUME_USD:
        logger.warning(
            "[COPY_CLASSIFIER] Market volume $%.0f below minimum $%.0f — REJECTED",
            market_volume_usd,
            config.COPY_MIN_MARKET_VOLUME_USD,
        )
        return False
    return True


# ── Classifier loop ───────────────────────────────────────────────────────────

async def run_classifier(
    signal_queue: asyncio.Queue,
    execution_queue_a: asyncio.Queue,
    execution_queue_b: asyncio.Queue,
) -> None:
    """
    Continuously consume signals from the poller, apply guards, and route them
    to the appropriate execution queue.

    Conflict resolution: if two wallets signal the same market_id within
    COPY_CONFLICT_MAP_TTL_SECONDS, the one with the higher trust score wins.
    The loser is silently dropped and logged.

    Args:
        signal_queue:      Input — signals from copytrade.poller.
        execution_queue_a: Output — Class A (fast-path) signals.
        execution_queue_b: Output — Class B (LLM-validated) signals.
    """
    logger.info("[COPY_CLASSIFIER] Classifier started.")

    async with aiohttp.ClientSession() as session:
        while True:
            signal: dict = await signal_queue.get()

            try:
                market_id: str = signal.get("market_id", "")
                tracker_price: float = signal.get("tracker_price", 0.5)
                market_volume_usd: float = signal.get("market_volume_usd", 0.0)
                class_type: str = signal.get("class_type", "B")
                trader_name: str = signal.get("trader_name", "unknown")
                wallet_address: str = signal.get("wallet_address", "")

                if not market_id:
                    logger.warning(
                        "[COPY_CLASSIFIER] Signal from %s has no market_id — dropping",
                        trader_name,
                    )
                    continue

                # ── Conflict resolution (trust-based dedup per market) ─────────
                now = time.monotonic()
                if market_id in _CONFLICT_MAP:
                    incumbent_wallet, ts = _CONFLICT_MAP[market_id]
                    if now - ts < COPY_CONFLICT_MAP_TTL_SECONDS:
                        # A signal for this market is already in-flight.
                        # Check trust scores to decide who wins.
                        winner = resolve_conflict(incumbent_wallet, wallet_address)
                        if winner != wallet_address:
                            logger.info(
                                "[COPY_CLASSIFIER][DROP:conflict_lost] market=%s "
                                "incumbent=%s (score=%.3f) beats challenger=%s (score=%.3f)",
                                market_id[:12],
                                incumbent_wallet[:10],
                                get_trust_score(incumbent_wallet),
                                wallet_address[:10],
                                get_trust_score(wallet_address),
                            )
                            continue
                        else:
                            # Challenger has higher trust — replace incumbent
                            logger.info(
                                "[COPY_CLASSIFIER][CONFLICT_OVERRIDE] market=%s "
                                "new wallet=%s (%.3f) > old wallet=%s (%.3f)",
                                market_id[:12],
                                wallet_address[:10],
                                get_trust_score(wallet_address),
                                incumbent_wallet[:10],
                                get_trust_score(incumbent_wallet),
                            )

                # Register this wallet as the current holder of this market signal
                _CONFLICT_MAP[market_id] = (wallet_address, now)

                # Guard 1: Volume check (both classes)
                if not _volume_ok(market_volume_usd):
                    logger.info(
                        "[COPY_CLASSIFIER][DROP:low_volume] market=%s vol=$%.0f trader=%s",
                        market_id[:12],
                        market_volume_usd,
                        trader_name,
                    )
                    continue

                # Guard 2: Fetch live ask price from CLOB
                live_ask = await _fetch_live_ask(session, market_id)
                if live_ask is None:
                    logger.warning(
                        "[COPY_CLASSIFIER][DROP:price_fetch_failed] market=%s trader=%s",
                        market_id[:12],
                        trader_name,
                    )
                    continue

                # Attach live ask to signal for executor use
                signal["live_ask"] = live_ask

                # Guard 3: Slippage check
                slippage = _compute_slippage(live_ask, tracker_price)
                signal["slippage"] = slippage

                if not _slippage_ok(slippage, class_type):
                    logger.info(
                        "[COPY_CLASSIFIER][DROP:slippage] market=%s slippage=%.4f class=%s trader=%s",
                        market_id[:12],
                        slippage,
                        class_type,
                        trader_name,
                    )
                    continue

                # Attach trust score for executor logging
                signal["trust_score"] = get_trust_score(wallet_address)

                # Route to correct execution queue
                if class_type == "A":
                    logger.info(
                        "[COPY_CLASSIFIER] → CLASS A | market=%s ask=%.4f slip=%.4f trust=%.3f trader=%s",
                        market_id[:12],
                        live_ask,
                        slippage,
                        signal["trust_score"],
                        trader_name,
                    )
                    try:
                        execution_queue_a.put_nowait(signal)
                    except asyncio.QueueFull:
                        logger.warning(
                            "[COPY_CLASSIFIER] Class A queue full — dropping signal from %s",
                            trader_name,
                        )
                else:
                    logger.info(
                        "[COPY_CLASSIFIER] → CLASS B | market=%s ask=%.4f slip=%.4f trust=%.3f trader=%s",
                        market_id[:12],
                        live_ask,
                        slippage,
                        signal["trust_score"],
                        trader_name,
                    )
                    try:
                        execution_queue_b.put_nowait(signal)
                    except asyncio.QueueFull:
                        logger.warning(
                            "[COPY_CLASSIFIER] Class B queue full — dropping signal from %s",
                            trader_name,
                        )

            except asyncio.CancelledError:
                logger.info("[COPY_CLASSIFIER] Classifier loop cancelled.")
                raise
            except Exception as e:
                logger.error("[COPY_CLASSIFIER] Unexpected error processing signal: %s", e)
            finally:
                signal_queue.task_done()

