"""
tests/test_pipeline_instrumentation.py — Step 2 wiring tests (List A.md A6).

Proves, at pipeline level:
  1. A dormant-dropped signal (estimate:no_data) is STILL instrumented — the
     data clock runs while the main pipeline is fail-closed dormant.
  2. A third+ similar signal for the same market drops as novelty:dup BEFORE
     the News Analyst call (zero token burn).
  3. A repeat (one similar prior) proceeds but counts novelty:repeat.
  4. A fresh signal counts novelty:novel.
  5. A novelty read failure fails OPEN (signal proceeds, failure visible).
  6. The direction update (confirmed_direction + signal_id backfill) is
     spawned once the News Analyst asserts YES/NO.
"""

import asyncio
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import config

import data.forward_sampler as forward_sampler
from data import novelty
from coordinator.pipeline import run_pipeline, get_drop_counters, _drop_counters
from coordinator.market_state import reset_market_locks
from llm.news_analyst import NewsAnalystOutput
from llm.contract_parser import ContractParserOutput
from llm.trade_decision import TradeDecisionOutput
from risk.cost_model import BookSnapshot
from strategies.estimator import EstimateResult


# ── Mock Supabase plumbing (self-contained; supports gte + fault injection) ──

class MockTableBuilder:
    def __init__(self, db_state: dict[str, list[dict[str, Any]]], table_name: str,
                 fail_tables: set[str]) -> None:
        self.db_state = db_state
        self.table_name = table_name
        self.fail_tables = fail_tables
        self.filters: list[tuple[str, Any]] = []

    def select(self, cols: str) -> "MockTableBuilder":
        return self

    def eq(self, col: str, val: Any) -> "MockTableBuilder":
        self.filters.append((col, val))
        return self

    def gte(self, col: str, val: Any) -> "MockTableBuilder":
        self.filters.append(("__gte__" + col, val))
        return self

    def order(self, col: str, desc: bool = False) -> "MockTableBuilder":
        return self

    def limit(self, val: int) -> "MockTableBuilder":
        return self

    def _match(self, row: dict[str, Any]) -> bool:
        for col, val in self.filters:
            if col.startswith("__gte__"):
                real_col = col[len("__gte__"):]
                if (row.get(real_col) or "") < val:
                    return False
            elif row.get(col) != val:
                return False
        return True

    def execute(self) -> Any:
        class Result:
            def __init__(self, data: list[dict[str, Any]]) -> None:
                self.data = data

        if self.table_name in self.fail_tables:
            raise ConnectionError(f"mock outage on {self.table_name}")
        rows = self.db_state.get(self.table_name, [])
        return Result([r.copy() for r in rows if self._match(r)])

    def insert(self, data: Any) -> "MockTableBuilder":
        if self.table_name in self.fail_tables:
            return self
        rows = data if isinstance(data, list) else [data]
        for item in rows:
            self.db_state.setdefault(self.table_name, []).append(item.copy())
        return self

    def update(self, data: dict[str, Any]) -> "MockTableBuilder":
        if self.table_name in self.fail_tables:
            return self
        for row in self.db_state.get(self.table_name, []):
            if self._match(row):
                row.update(data)
        return self

    def delete(self) -> "MockTableBuilder":
        return self


class MockSupabaseClient:
    def __init__(self, db_state: dict[str, list[dict[str, Any]]],
                 fail_tables: set[str] | None = None) -> None:
        self.db_state = db_state
        self.fail_tables = fail_tables or set()

    def table(self, name: str) -> MockTableBuilder:
        return MockTableBuilder(self.db_state, name, self.fail_tables)


# ── Fixtures + helpers ────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def clean_test_environment(monkeypatch):
    reset_market_locks()
    for k in _drop_counters:
        _drop_counters[k] = 0
    monkeypatch.setattr(config, "FORWARD_SAMPLER_HORIZONS_SECONDS", (0.05, 0.1, 0.15, 0.2))
    yield
    reset_market_locks()


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Tests never touch the network: stub the CLOB price read."""
    async def stub_price(token_id: str) -> float:
        return 0.50

    monkeypatch.setattr("data.market_discovery.get_market_price", stub_price)


def patch_db(db_state: dict[str, list[dict[str, Any]]],
             fail_tables: set[str] | None = None):
    """Patch every get_client binding the pipeline flow can reach (the repo's
    established test pattern — module-level imports must each be patched).

    Returns (ExitStack, client); use the stack as a context manager."""
    client = MockSupabaseClient(db_state, fail_tables)

    async def fake_get_client():
        return client

    stack = ExitStack()
    for target in (
        "memory.supabase_client.get_client",
        "coordinator.pipeline.get_client",
        "llm.contract_parser.get_client",
        "strategies.estimator.get_client",
        "copytrade.performance_tracker.get_client",
    ):
        stack.enter_context(patch(target, fake_get_client))
    return stack, client


def default_db_state() -> dict[str, list[dict[str, Any]]]:
    return {
        "open_positions": [],
        "closed_trades": [],
        "market_signals": [],
        "signal_outcomes": [],
        "idempotency_log": [],
        "agent_memory": [],
        "resolution_keyword_cache": [],
        "tracked_wallets": [],
    }


async def wait_for(condition, timeout: float = 3.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if condition():
            return True
        await asyncio.sleep(0.01)
    return False


async def run_default_pipeline(**overrides):
    kwargs: dict[str, Any] = dict(
        headline="Congress passes new spending bill",
        source="Reuters",
        market_id="mkt-instr",
        market_question="Will the bill pass?",
        market_price=0.50,
        portfolio_value=10_000.0,
        starting_balances={"daily": 10000.0, "weekly": 10000.0, "monthly": 10000.0},
        current_balances={"daily": 10000.0, "weekly": 10000.0, "monthly": 10000.0},
    )
    kwargs.update(overrides)
    return await run_pipeline(**kwargs)


def classify_stub(direction: str = "YES", confidence: float = 0.80):
    """News Analyst stub — tests must never reach the real LLM/network."""
    async def _fake(*args, **kwargs):
        return NewsAnalystOutput(
            event_category="politics", direction=direction,
            confidence_score=confidence, reasoning="Valid signal",
        )
    return _fake


def seed_prior_signal(db_state, market_id: str, headline_hash: str,
                      entities: list[str], age_hours: float = 1.0) -> None:
    db_state.setdefault("signal_outcomes", []).append({
        "id": f"prior-{headline_hash[:6]}",
        "market_id": market_id,
        "headline_hash": headline_hash,
        "entities_json": entities,
        "t0": (datetime.now(timezone.utc) - timedelta(hours=age_hours)).isoformat(),
    })


# ── 1. Dormancy does not stop the data clock ─────────────────────────────────

@pytest.mark.anyio
async def test_dormant_signal_still_instrumented():
    """estimate:no_data drop, but the signal_outcomes row is recorded and the
    forward sampler fills horizons — instrumentation precedes the gates."""
    db_state = default_db_state()

    async def dormant_estimate(*args, **kwargs):
        return EstimateResult(
            p_point=None, sample_size=0, method="none",
            computed_at=datetime.now(timezone.utc),
        )

    get_client_patch, _ = patch_db(db_state)
    with get_client_patch, \
         patch("coordinator.pipeline.get_estimate", dormant_estimate), \
         patch("coordinator.pipeline.classify_signal", classify_stub()):
        supervisor = asyncio.create_task(
            forward_sampler.run_forward_sampler_supervisor()
        )
        try:
            result = await run_default_pipeline()

            assert result is not None
            assert result["status"] == "blocked"
            assert result["reason"] == "estimate_no_data"
            assert get_drop_counters()["estimate:no_data"] == 1

            recorded = await wait_for(
                lambda: len(db_state["signal_outcomes"]) == 1
            )
            assert recorded, "dormant signal was never instrumented"
            row = db_state["signal_outcomes"][0]
            assert row["market_id"] == "mkt-instr"
            assert row["price_t0"] == 0.50
            assert row["strategy"] == "news_velocity"
            assert row["headline_hash"] == novelty.compute_headline_hash(
                "Congress passes new spending bill"
            )
            filled = await wait_for(
                lambda: row.get("p_m60") is not None
            )
            assert filled, "forward horizons never recorded"
            assert get_drop_counters()["novelty:novel"] == 1
        finally:
            supervisor.cancel()
            try:
                await supervisor
            except asyncio.CancelledError:
                pass


# ── 2. Dup drops BEFORE the News Analyst (zero token burn) ───────────────────

@pytest.mark.anyio
async def test_dup_signal_dropped_before_news_analyst():
    db_state = default_db_state()
    headline_hash = novelty.compute_headline_hash("Congress passes new spending bill")
    # The identical headline already appeared twice in-window → this third one dups.
    seed_prior_signal(db_state, "mkt-instr", headline_hash, ["Congress"])
    seed_prior_signal(db_state, "mkt-instr", headline_hash, ["Congress", "bill"])

    classify_mock = AsyncMock(
        return_value=NewsAnalystOutput(
            event_category="politics", direction="YES",
            confidence_score=0.80, reasoning="r",
        )
    )

    get_client_patch, _ = patch_db(db_state)
    with get_client_patch, \
         patch("coordinator.pipeline.classify_signal", classify_mock):
        result = await run_default_pipeline()

    assert result is not None
    assert result["status"] == "blocked"
    assert result["reason"] == "novelty_dup"
    assert get_drop_counters()["novelty:dup"] == 1
    classify_mock.assert_not_awaited()  # zero token burn
    # The dup signal itself was not recorded yet: only the two SEEDED priors
    # are in the table (the row insert happens in the sampler supervisor,
    # which is not running in this test).
    assert len(db_state["signal_outcomes"]) == 2


# ── 3. Repeat proceeds, counted ───────────────────────────────────────────────

@pytest.mark.anyio
async def test_repeat_signal_proceeds_to_estimate_gate():
    db_state = default_db_state()
    headline_hash = novelty.compute_headline_hash("Congress passes new spending bill")
    seed_prior_signal(db_state, "mkt-instr", headline_hash, ["Congress"])

    async def dormant_estimate(*args, **kwargs):
        return EstimateResult(
            p_point=None, sample_size=0, method="none",
            computed_at=datetime.now(timezone.utc),
        )

    get_client_patch, _ = patch_db(db_state)
    with get_client_patch, \
         patch("coordinator.pipeline.get_estimate", dormant_estimate), \
         patch("coordinator.pipeline.classify_signal", classify_stub()):
        result = await run_default_pipeline()

    assert result is not None
    assert result["status"] == "blocked"
    assert result["reason"] == "estimate_no_data"
    assert get_drop_counters()["novelty:repeat"] == 1
    assert get_drop_counters()["novelty:dup"] == 0


# ── 4. Fresh signal counts novel ──────────────────────────────────────────────

@pytest.mark.anyio
async def test_novel_signal_counter():
    db_state = default_db_state()

    async def dormant_estimate(*args, **kwargs):
        return EstimateResult(
            p_point=None, sample_size=0, method="none",
            computed_at=datetime.now(timezone.utc),
        )

    get_client_patch, _ = patch_db(db_state)
    with get_client_patch, \
         patch("coordinator.pipeline.get_estimate", dormant_estimate), \
         patch("coordinator.pipeline.classify_signal", classify_stub()):
        await run_default_pipeline()

    assert get_drop_counters()["novelty:novel"] == 1
    assert get_drop_counters()["novelty:repeat"] == 0
    assert get_drop_counters()["novelty:dup"] == 0


# ── 5. Novelty read failure fails OPEN ────────────────────────────────────────

@pytest.mark.anyio
async def test_novelty_check_failure_fails_open():
    db_state = default_db_state()

    async def dormant_estimate(*args, **kwargs):
        return EstimateResult(
            p_point=None, sample_size=0, method="none",
            computed_at=datetime.now(timezone.utc),
        )

    get_client_patch, _ = patch_db(db_state, fail_tables={"signal_outcomes"})
    with get_client_patch, \
         patch("coordinator.pipeline.get_estimate", dormant_estimate), \
         patch("coordinator.pipeline.classify_signal", classify_stub()):
        result = await run_default_pipeline()

    # Fail-open: the signal proceeds past novelty (no dup drop) and reaches
    # the estimator gate, where dormancy drops it as designed.
    assert result is not None
    assert result["reason"] == "estimate_no_data"
    assert get_drop_counters()["novelty:check_failed"] == 1
    assert get_drop_counters()["novelty:dup"] == 0


# ── 6. Direction update spawned after the News Analyst ───────────────────────

@pytest.mark.anyio
async def test_direction_update_spawned_for_yes_signal():
    db_state = default_db_state()

    async def data_estimate(*args, **kwargs):
        return EstimateResult(
            p_point=0.65, sample_size=100, method="recalibration_base_rate",
            computed_at=datetime.now(timezone.utc),
        )

    async def fake_book(token_id: str):
        return BookSnapshot(best_bid=0.48, best_ask=0.52, depth_usd=5000.0)

    async def fake_parse(*args, **kwargs):
        return ContractParserOutput(
            resolution_source="Reuters", resolution_condition="Condition",
            key_entities=["Trump"], resolution_keywords=["trump", "bill", "spending"],
            ambiguity_score=0.1, resolution_type="binary",
        )

    async def fake_decide(*args, **kwargs):
        return TradeDecisionOutput(
            direction="YES", confidence_score=0.80, reasoning="Edge confirmed"
        ), False

    async def fake_classify(*args, **kwargs):
        return NewsAnalystOutput(
            event_category="politics", direction="YES",
            confidence_score=0.80, reasoning="Valid signal",
        )

    async def fake_classify(*args, **kwargs):
        return NewsAnalystOutput(
            event_category="politics", direction="YES",
            confidence_score=0.80, reasoning="Valid signal",
        )

    get_client_patch, _ = patch_db(db_state)
    with get_client_patch, \
         patch("coordinator.pipeline.get_estimate", data_estimate), \
         patch("coordinator.pipeline.get_market_book", fake_book), \
         patch("coordinator.pipeline.parse_contract", fake_parse), \
         patch("coordinator.pipeline.decide_trade", fake_decide), \
         patch("coordinator.pipeline.classify_signal", fake_classify):
        result = await run_default_pipeline()

    assert result is not None and result["status"] == "success"
    # The hook queues [SamplerJob, DirectionUpdate] — inspect both messages.
    job = forward_sampler.get_sampler_queue().get_nowait()
    assert job.market_id == "mkt-instr"
    assert job.price_t0 == 0.50
    assert job.strategy == "news_velocity"
    assert job.entities, "entities must ride along for novelty lookups"

    update = forward_sampler.get_sampler_queue().get_nowait()
    assert isinstance(update, forward_sampler.DirectionUpdate)
    assert update.outcome_id == job.outcome_id
    assert update.headline == "Congress passes new spending bill"
    assert update.source == "Reuters"
    assert update.confirmed_yes is True  # YES → confirmed_direction True
