"""
tests/test_estimator.py — Unit tests for strategies/estimator.py (List A.md Step 0).

Covers:
  1. EstimateResult contract: frozen dataclass; p_point is None ⟺ sample_size == 0.
  2. Fail-closed stubs: recalibration / velocity / resolution yield no data.
  3. Unknown strategy keys fail closed.
  4. Laplace math: (wins+1)/(wins+losses+2), including the zero-history edge.
  5. Side adjustment: p_side = p for YES, 1−p for NO.
  6. copy_edge_class_b live estimator: Laplace hit rate, YES/NO mapping, and
     fail-closed on missing wallet, missing row, zero history, DB timeout, DB error,
     and estimator crashes.
"""

import asyncio
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
import config
from strategies.estimator import (
    EstimateResult,
    METHOD_COPY_WALLET_HITRATE,
    METHOD_NONE,
    _has_data,
    _no_data,
    get_estimate,
    laplace_hit_rate,
    side_probability,
)


# ── Fixtures & helpers ────────────────────────────────────────────────────────

class FakeTableBuilder:
    """Minimal query builder for the tracked_wallets read chain used by the estimator."""

    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def select(self, cols: str) -> "FakeTableBuilder":
        return self

    def eq(self, col: str, val) -> "FakeTableBuilder":
        return self

    def limit(self, val: int) -> "FakeTableBuilder":
        return self

    def execute(self):
        class Result:
            def __init__(self, data: list[dict]) -> None:
                self.data = data

        return Result([r.copy() for r in self._rows])


def make_wallet_rows(wins: int, losses: int) -> list[dict]:
    return [{"wallet_address": "wal-abc", "wins_count": wins, "losses_count": losses}]


def fake_get_client_factory(rows: list[dict]):
    async def fake_get_client():
        class FakeClient:
            def table(self, name: str) -> FakeTableBuilder:
                return FakeTableBuilder(rows)

        return FakeClient()

    return fake_get_client


# ── 1. EstimateResult contract ────────────────────────────────────────────────

class TestEstimateResultContract:
    def test_is_frozen(self):
        """EstimateResult must be immutable — estimates are snapshots, not state."""
        result = EstimateResult(0.6, 10, METHOD_COPY_WALLET_HITRATE, datetime.now(timezone.utc))
        with pytest.raises(Exception):
            result.p_point = 0.9

    def test_no_data_contract(self):
        """The no-data result carries p_point=None, sample_size=0, method='none'."""
        result = _no_data()
        assert result.p_point is None
        assert result.sample_size == 0
        assert result.method == METHOD_NONE

    def test_has_data_requires_both_point_and_sample(self):
        """p_point without sample evidence (or vice versa) is not a usable estimate."""
        now = datetime.now(timezone.utc)
        assert _has_data(EstimateResult(0.6, 10, "m", now))
        assert not _has_data(EstimateResult(None, 0, METHOD_NONE, now))
        assert not _has_data(EstimateResult(None, 10, "m", now))
        assert not _has_data(EstimateResult(0.6, 0, "m", now))


# ── 2. Fail-closed stubs ──────────────────────────────────────────────────────

class TestStubEstimatorsFailClosed:
    @pytest.mark.anyio
    @pytest.mark.parametrize("strategy", ["recalibration", "velocity", "resolution"])
    async def test_stubs_return_no_data(self, strategy: str):
        """Steps 2/6 estimators are intentionally dormant — they must never emit a number."""
        result = await get_estimate(
            strategy=strategy,
            category="politics",
            market_id="mkt-1",
            market_price=0.50,
            side="YES",
        )
        assert result.p_point is None
        assert result.sample_size == 0
        assert result.method == METHOD_NONE

    @pytest.mark.anyio
    async def test_unknown_strategy_fails_closed(self):
        """An unregistered strategy must never trade — drop the signal."""
        result = await get_estimate(
            strategy="copy_edge_class_a",
            category="politics",
            market_id="mkt-1",
            market_price=0.50,
            side="YES",
        )
        assert result.p_point is None
        assert result.sample_size == 0
        assert result.method == METHOD_NONE

    @pytest.mark.anyio
    async def test_estimator_crash_fails_closed(self):
        """An estimator exception must yield no-data, never a fabricated probability."""
        async def exploding_estimator(*args, **kwargs):
            raise RuntimeError("boom")

        with patch.dict("strategies.estimator._ESTIMATORS",
                        {"recalibration": exploding_estimator}):
            result = await get_estimate(
                strategy="recalibration",
                category="politics",
                market_id="mkt-1",
                market_price=0.50,
                side="YES",
            )
        assert result.p_point is None
        assert result.sample_size == 0
        assert result.method == METHOD_NONE


# ── 3. Laplace math ───────────────────────────────────────────────────────────

class TestLaplaceHitRate:
    def test_8w_2l(self):
        assert laplace_hit_rate(8, 2) == pytest.approx(9 / 12)

    def test_1w_0l_dampened_from_certainty(self):
        assert laplace_hit_rate(1, 0) == pytest.approx(2 / 3)

    def test_0w_0l_is_neutral_prior(self):
        assert laplace_hit_rate(0, 0) == pytest.approx(0.5)

    def test_18w_2l(self):
        assert laplace_hit_rate(18, 2) == pytest.approx(19 / 22)

    def test_negative_counts_rejected(self):
        with pytest.raises(ValueError):
            laplace_hit_rate(-1, 2)
        with pytest.raises(ValueError):
            laplace_hit_rate(2, -1)


# ── 4. Side adjustment ────────────────────────────────────────────────────────

class TestSideProbability:
    def test_yes_is_identity(self):
        assert side_probability(0.62, "YES") == pytest.approx(0.62)

    def test_no_is_complement(self):
        assert side_probability(0.62, "NO") == pytest.approx(0.38)

    def test_case_insensitive(self):
        assert side_probability(0.62, "yes") == pytest.approx(0.62)
        assert side_probability(0.62, "no") == pytest.approx(0.38)

    def test_invalid_side_raises(self):
        with pytest.raises(ValueError):
            side_probability(0.62, "MAYBE")


# ── 5. copy_edge_class_b live estimator ───────────────────────────────────────

class TestCopyEdgeClassBEstimator:
    @pytest.mark.anyio
    async def test_returns_laplace_hitrate_yes_side(self):
        """18W/2L wallet on a YES signal → (18+1)/(18+2+2) = 19/22, n=20."""
        with patch("strategies.estimator.get_client",
                   fake_get_client_factory(make_wallet_rows(18, 2))):
            result = await get_estimate(
                strategy="copy_edge_class_b",
                category="politics",
                market_id="mkt-1",
                market_price=0.50,
                side="YES",
                wallet_address="wal-abc",
            )
        assert result.p_point == pytest.approx(19 / 22)
        assert result.sample_size == 20
        assert result.method == METHOD_COPY_WALLET_HITRATE
        assert _has_data(result)

    @pytest.mark.anyio
    async def test_no_side_maps_to_complement(self):
        """A NO-side copy signal flips the hit rate onto the YES line: 1 − 19/22."""
        with patch("strategies.estimator.get_client",
                   fake_get_client_factory(make_wallet_rows(18, 2))):
            result = await get_estimate(
                strategy="copy_edge_class_b",
                category="politics",
                market_id="mkt-1",
                market_price=0.50,
                side="NO",
                wallet_address="wal-abc",
            )
        assert result.p_point == pytest.approx(1 - 19 / 22)
        assert result.sample_size == 20

    @pytest.mark.anyio
    async def test_missing_wallet_address_fails_closed(self):
        result = await get_estimate(
            strategy="copy_edge_class_b",
            category="politics",
            market_id="mkt-1",
            market_price=0.50,
            side="YES",
            wallet_address=None,
        )
        assert result.p_point is None
        assert result.sample_size == 0

    @pytest.mark.anyio
    async def test_invalid_side_fails_closed(self):
        with patch("strategies.estimator.get_client",
                   fake_get_client_factory(make_wallet_rows(18, 2))):
            result = await get_estimate(
                strategy="copy_edge_class_b",
                category="politics",
                market_id="mkt-1",
                market_price=0.50,
                side="MAYBE",
                wallet_address="wal-abc",
            )
        assert result.p_point is None
        assert result.sample_size == 0

    @pytest.mark.anyio
    async def test_missing_wallet_row_fails_closed(self):
        with patch("strategies.estimator.get_client", fake_get_client_factory([])):
            result = await get_estimate(
                strategy="copy_edge_class_b",
                category="politics",
                market_id="mkt-1",
                market_price=0.50,
                side="YES",
                wallet_address="wal-unknown",
            )
        assert result.p_point is None
        assert result.sample_size == 0

    @pytest.mark.anyio
    async def test_zero_resolved_history_fails_closed(self):
        """A 0W/0L wallet has no evidence — 0.5 prior must never steer capital."""
        with patch("strategies.estimator.get_client",
                   fake_get_client_factory(make_wallet_rows(0, 0))):
            result = await get_estimate(
                strategy="copy_edge_class_b",
                category="politics",
                market_id="mkt-1",
                market_price=0.50,
                side="YES",
                wallet_address="wal-abc",
            )
        assert result.p_point is None
        assert result.sample_size == 0

    @pytest.mark.anyio
    async def test_db_timeout_fails_closed(self):
        """Supabase timeout → no data (fail-closed), NOT a neutral guess."""
        async def slow_get_client():
            # Suspend past the (shortened) wait_for timeout — the same behaviour as a
            # hung Supabase connection observed through asyncio.wait_for.
            await asyncio.sleep(30)
            raise AssertionError("should have timed out")

        original_timeout = config.SUPABASE_TIMEOUT_SECONDS
        try:
            config.SUPABASE_TIMEOUT_SECONDS = 0.05
            with patch("strategies.estimator.get_client", slow_get_client):
                result = await get_estimate(
                    strategy="copy_edge_class_b",
                    category="politics",
                    market_id="mkt-1",
                    market_price=0.50,
                    side="YES",
                    wallet_address="wal-abc",
                )
        finally:
            config.SUPABASE_TIMEOUT_SECONDS = original_timeout
        assert result.p_point is None
        assert result.sample_size == 0

    @pytest.mark.anyio
    async def test_db_error_fails_closed(self):
        """A Supabase error on the wallet read → no data (fail-closed)."""
        def failing_get_client():
            class FailingClient:
                def table(self, name: str):
                    class ExplodingBuilder:
                        def select(self, cols): return self
                        def eq(self, c, v): return self
                        def limit(self, n): return self
                        def execute(self):
                            raise RuntimeError("supabase down")

                    return ExplodingBuilder()

            return FailingClient()

        with patch("strategies.estimator.get_client", failing_get_client):
            result = await get_estimate(
                strategy="copy_edge_class_b",
                category="politics",
                market_id="mkt-1",
                market_price=0.50,
                side="YES",
                wallet_address="wal-abc",
            )
        assert result.p_point is None
        assert result.sample_size == 0
