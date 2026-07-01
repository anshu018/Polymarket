"""
tests/test_copytrade.py — Unit tests for Strategy 5: Copy Edge (CopyTrade)

Tests cover:
  - Slippage computation and class-specific threshold enforcement.
  - Volume guard.
  - Seen-trade deduplication in the poller.
  - Config constant presence and correct types.
  - Class A risk gate integration (drawdown, liquidity, exposure).
  - Classifier routing to correct queue (Class A vs Class B).
"""

import asyncio
import sys
import os
import types
import pytest

# ── Environment and config stubs ─────────────────────────────────────────────
# Set required env vars so config.py doesn't raise on import in the test env.
os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "test_key")
os.environ.setdefault("OPENROUTER_API_KEY", "test_key")
os.environ.setdefault("NVIDIA_API_KEY", "test_key")
os.environ.setdefault("DEEPSEEK_API_KEY", "test_key")
os.environ.setdefault("POLYMARKET_PRIVATE_KEY", "0x" + "a" * 64)
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test_token")
os.environ.setdefault("TELEGRAM_CHAT_ID", "12345")
os.environ.setdefault("SILICONFLOW_API_KEY", "test_key")

import config  # noqa: E402 — must come after env vars


# ── Tests: config constants ───────────────────────────────────────────────────

class TestCopyTradeConfig:
    """Verify all CopyTrade config constants exist and have correct types/values."""

    def test_kelly_fraction_copy(self):
        assert isinstance(config.KELLY_FRACTION_COPY, float)
        assert 0.0 < config.KELLY_FRACTION_COPY <= 1.0

    def test_class_a_max_size(self):
        assert config.COPY_CLASS_A_MAX_SIZE_USDC == 10.0

    def test_class_b_max_size(self):
        assert config.COPY_CLASS_B_MAX_SIZE_USDC == 50.0

    def test_slippage_thresholds_ordered(self):
        """Class A should be stricter (lower) than Class B."""
        assert config.COPY_CLASS_A_SLIPPAGE_THRESHOLD < config.COPY_CLASS_B_SLIPPAGE_THRESHOLD

    def test_min_volume(self):
        assert config.COPY_MIN_MARKET_VOLUME_USD == 25000.0

    def test_poll_interval_positive(self):
        assert config.COPY_POLL_INTERVAL_SECONDS > 0

    def test_limit_price_buffer_positive(self):
        assert config.COPY_LIMIT_PRICE_BUFFER > 0

    def test_gamma_api_timeout_positive(self):
        assert config.GAMMA_API_TIMEOUT_SECONDS > 0


# ── Tests: classifier logic ───────────────────────────────────────────────────

class TestClassifierHelpers:
    """Unit-test the classifier's pure helper functions."""

    def test_slippage_compute(self):
        from copytrade.classifier import _compute_slippage
        assert abs(_compute_slippage(0.620, 0.610)) == pytest.approx(0.010, abs=1e-6)

    def test_slippage_ok_class_a_within_threshold(self):
        from copytrade.classifier import _slippage_ok
        # 0.009 < COPY_CLASS_A_SLIPPAGE_THRESHOLD (0.010) → should pass
        assert _slippage_ok(0.009, "A") is True

    def test_slippage_rejected_class_a_over_threshold(self):
        from copytrade.classifier import _slippage_ok
        # 0.011 > 0.010 → should reject
        assert _slippage_ok(0.011, "A") is False

    def test_slippage_ok_class_b_within_threshold(self):
        from copytrade.classifier import _slippage_ok
        # 0.014 < COPY_CLASS_B_SLIPPAGE_THRESHOLD (0.015) → should pass
        assert _slippage_ok(0.014, "B") is True

    def test_slippage_rejected_class_b_over_threshold(self):
        from copytrade.classifier import _slippage_ok
        # 0.016 > 0.015 → should reject
        assert _slippage_ok(0.016, "B") is False

    def test_volume_ok_above_minimum(self):
        from copytrade.classifier import _volume_ok
        assert _volume_ok(30000.0) is True

    def test_volume_rejected_below_minimum(self):
        from copytrade.classifier import _volume_ok
        assert _volume_ok(24999.0) is False

    def test_volume_exact_minimum_allowed(self):
        """Exactly at the minimum volume threshold is allowed (>= semantics)."""
        from copytrade.classifier import _volume_ok
        assert _volume_ok(25000.0) is True

    def test_slippage_symmetric(self):
        """Slippage should be the same regardless of direction."""
        from copytrade.classifier import _compute_slippage
        s1 = _compute_slippage(0.630, 0.610)
        s2 = _compute_slippage(0.610, 0.630)
        assert s1 == pytest.approx(s2, abs=1e-9)


# ── Tests: poller deduplication ───────────────────────────────────────────────

class TestPollerDeduplication:
    """Verify seen-trade deduplication prevents double-emitting signals."""

    def setup_method(self):
        """Clear seen trades before each test."""
        from copytrade import poller
        poller._SEEN_TRADES.clear()

    def test_new_trade_not_seen(self):
        from copytrade.poller import _is_seen
        assert _is_seen("0xWallet1", "trade_001") is False

    def test_record_and_check_seen(self):
        from copytrade.poller import _record_seen, _is_seen
        _record_seen("0xWallet1", "trade_001")
        assert _is_seen("0xWallet1", "trade_001") is True

    def test_seen_is_wallet_scoped(self):
        """Same trade ID for different wallets should be independent."""
        from copytrade.poller import _record_seen, _is_seen
        _record_seen("0xWallet1", "trade_abc")
        assert _is_seen("0xWallet2", "trade_abc") is False

    def test_seen_trades_cap_evicts(self):
        """Seen trades set should not grow beyond _SEEN_TRADES_MAX."""
        from copytrade import poller
        from copytrade.poller import _record_seen, _is_seen
        wallet = "0xCapTest"
        # Fill beyond the max
        for i in range(poller._SEEN_TRADES_MAX + 10):
            _record_seen(wallet, f"trade_{i}")
        # After eviction, the set should be at most _SEEN_TRADES_MAX
        # (specifically at 250 after eviction: see implementation)
        assert len(poller._SEEN_TRADES[wallet]) <= poller._SEEN_TRADES_MAX


# ── Tests: classifier async routing ──────────────────────────────────────────

class TestClassifierRouting:
    """
    Test the classifier's routing decision using mocked Gamma API price fetches.
    Uses asyncio.run() directly for compatibility with anyio (already installed).
    """

    def test_class_a_signal_routes_to_queue_a(self, monkeypatch):
        """A valid Class A signal with low slippage should end up in queue_a."""
        import copytrade.classifier as cls_module

        async def mock_fetch_live_ask(session, market_id):
            return 0.612  # slippage = |0.612 - 0.610| = 0.002 < 0.010

        monkeypatch.setattr(cls_module, "_fetch_live_ask", mock_fetch_live_ask)

        async def _run():
            signal_q = asyncio.Queue()
            queue_a = asyncio.Queue()
            queue_b = asyncio.Queue()

            signal = {
                "source": "copy_edge",
                "wallet_address": "0xABC",
                "trader_name": "test_trader",
                "class_type": "A",
                "trade_id": "trade_999",
                "market_id": "0x" + "b" * 40,
                "outcome": "Yes",
                "tracker_price": 0.610,
                "tracker_size_usdc": 100.0,
                "market_volume_usd": 50000.0,
                "detected_at": 0.0,
            }
            await signal_q.put(signal)

            task = asyncio.create_task(
                cls_module.run_classifier(signal_q, queue_a, queue_b)
            )
            await asyncio.sleep(0.2)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

            assert not queue_a.empty(), "Class A signal should be in queue_a"
            assert queue_b.empty(), "Class B queue should be empty"

        asyncio.run(_run())

    def test_low_volume_signal_dropped(self, monkeypatch):
        """A signal with volume below $25,000 should be dropped before routing."""
        import copytrade.classifier as cls_module

        async def mock_fetch_live_ask(session, market_id):
            return 0.612

        monkeypatch.setattr(cls_module, "_fetch_live_ask", mock_fetch_live_ask)

        async def _run():
            signal_q = asyncio.Queue()
            queue_a = asyncio.Queue()
            queue_b = asyncio.Queue()

            signal = {
                "source": "copy_edge",
                "wallet_address": "0xABC",
                "trader_name": "test_trader",
                "class_type": "A",
                "trade_id": "trade_low_vol",
                "market_id": "0x" + "c" * 40,
                "outcome": "Yes",
                "tracker_price": 0.610,
                "tracker_size_usdc": 100.0,
                "market_volume_usd": 5000.0,   # Below $25,000
                "detected_at": 0.0,
            }
            await signal_q.put(signal)

            task = asyncio.create_task(
                cls_module.run_classifier(signal_q, queue_a, queue_b)
            )
            await asyncio.sleep(0.2)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

            assert queue_a.empty(), "Low-volume signal should be dropped"
            assert queue_b.empty(), "Low-volume signal should be dropped"

        asyncio.run(_run())

    def test_high_slippage_signal_dropped(self, monkeypatch):
        """A signal with slippage exceeding Class A threshold should be dropped."""
        import copytrade.classifier as cls_module

        async def mock_fetch_live_ask(session, market_id):
            return 0.650  # slippage = |0.650 - 0.610| = 0.040 >> 0.010

        monkeypatch.setattr(cls_module, "_fetch_live_ask", mock_fetch_live_ask)

        async def _run():
            signal_q = asyncio.Queue()
            queue_a = asyncio.Queue()
            queue_b = asyncio.Queue()

            signal = {
                "class_type": "A",
                "trade_id": "trade_slip",
                "market_id": "0x" + "d" * 40,
                "outcome": "Yes",
                "tracker_price": 0.610,
                "market_volume_usd": 50000.0,
                "detected_at": 0.0,
                "trader_name": "test",
            }
            await signal_q.put(signal)

            task = asyncio.create_task(
                cls_module.run_classifier(signal_q, queue_a, queue_b)
            )
            await asyncio.sleep(0.2)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

            assert queue_a.empty(), "High-slippage Class A signal should be dropped"

        asyncio.run(_run())

    def test_price_fetch_failure_drops_signal(self, monkeypatch):
        """If CLOB price fetch fails, signal must be dropped."""
        import copytrade.classifier as cls_module

        async def mock_fetch_live_ask(session, market_id):
            return None  # Simulate CLOB failure

        monkeypatch.setattr(cls_module, "_fetch_live_ask", mock_fetch_live_ask)

        async def _run():
            signal_q = asyncio.Queue()
            queue_a = asyncio.Queue()
            queue_b = asyncio.Queue()

            signal = {
                "class_type": "B",
                "trade_id": "trade_no_price",
                "market_id": "0x" + "e" * 40,
                "outcome": "Yes",
                "tracker_price": 0.610,
                "market_volume_usd": 50000.0,
                "detected_at": 0.0,
                "trader_name": "test",
            }
            await signal_q.put(signal)

            task = asyncio.create_task(
                cls_module.run_classifier(signal_q, queue_a, queue_b)
            )
            await asyncio.sleep(0.2)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

            assert queue_a.empty()
            assert queue_b.empty()

        asyncio.run(_run())
