"""
Part A (shared throttle helper + daily counter) and Part C.3 (guaranteed
CAUTION floor penalty), from the 28 Sep review.

Follows the idiom in tests/test_mirror_review.py: a fresh in-memory SQLite
DB per test, AsyncSessionFactory patched to it, storage modules imported
inside test bodies so the ambient DATABASE_URL from other test modules can
never leak in.
"""
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from analysis.crypto_signal import CryptoSignal
from analysis.crypto_state import CryptoState
from collectors.llm_client import calls_today, should_call_again
from scheduler.runner import AppRunner
from storage.database import Base


async def _fresh_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def _signal(**over):
    base = dict(
        symbol="btcusdt", signal_type="confluence", direction="long",
        trigger_description="Confluence long: RSI + volume", confidence=0.74,
        current_price=60000.0, target_price=61200.0, stop_loss=59400.0,
        edge_pct=2.0, stake_pct=0.015, timeframe="1h", sentiment_score=0.0,
        indicators_summary="", timestamp=datetime.now(UTC))
    base.update(over)
    return CryptoSignal(**base)


def _mock_groq(verdict="APPROVE", delta=0.0, summary="looks fine"):
    g = MagicMock()
    g.is_available = True
    g.postmortem_available = False
    g.last_factors = ""
    g.last_model = "mock-model"
    g.last_latency_ms = 5
    g.review_signal_candidate = AsyncMock(return_value=(delta, summary, verdict))
    return g


# ── should_call_again: the extracted mirror-review gate, in isolation ──────

class TestShouldCallAgain(unittest.TestCase):
    def test_true_when_both_conditions_met(self):
        self.assertTrue(should_call_again(
            elapsed_ratio=0.5, min_elapsed_ratio=0.33,
            current_value=0.70, last_value=0.60, min_delta=0.05))

    def test_false_when_not_enough_time_elapsed(self):
        self.assertFalse(should_call_again(
            elapsed_ratio=0.2, min_elapsed_ratio=0.33,
            current_value=0.70, last_value=0.60, min_delta=0.05))

    def test_false_when_value_has_not_moved_enough(self):
        self.assertFalse(should_call_again(
            elapsed_ratio=0.9, min_elapsed_ratio=0.33,
            current_value=0.61, last_value=0.60, min_delta=0.05))

    def test_movement_is_symmetric_either_direction(self):
        self.assertTrue(should_call_again(
            elapsed_ratio=0.9, min_elapsed_ratio=0.33,
            current_value=0.50, last_value=0.60, min_delta=0.05))

    def test_exactly_at_the_thresholds_counts_as_met(self):
        self.assertTrue(should_call_again(
            elapsed_ratio=0.33, min_elapsed_ratio=0.33,
            current_value=0.65, last_value=0.60, min_delta=0.05))

    def test_wall_clock_gap_usage_via_ratio(self):
        """A caller with a plain 'seconds since last call' cadence — like
        position_review's own rule — can use the same function by passing
        elapsed_seconds / min_gap_seconds as the ratio."""
        elapsed_seconds, min_gap_seconds = 700.0, 600.0
        self.assertTrue(should_call_again(
            elapsed_ratio=elapsed_seconds / min_gap_seconds, min_elapsed_ratio=1.0,
            current_value=1.0, last_value=1.0, min_delta=0.0))
        elapsed_seconds = 300.0
        self.assertFalse(should_call_again(
            elapsed_ratio=elapsed_seconds / min_gap_seconds, min_elapsed_ratio=1.0,
            current_value=1.0, last_value=1.0, min_delta=0.0))


# ── Per-role daily call counter ─────────────────────────────────────────────

class TestCallsToday(unittest.TestCase):
    def setUp(self):
        import collectors.llm_client as m
        self._saved = (dict(m._calls_today), m._calls_today_date)
        m._calls_today.clear()
        m._calls_today_date = None

    def tearDown(self):
        import collectors.llm_client as m
        m._calls_today.clear()
        m._calls_today.update(self._saved[0])
        m._calls_today_date = self._saved[1]

    def test_empty_before_any_call(self):
        self.assertEqual(calls_today(), {})

    def test_records_role_and_counts(self):
        import collectors.llm_client as m
        m._record_call("pre_trade")
        m._record_call("pre_trade")
        m._record_call("post_trade")
        self.assertEqual(calls_today(), {"pre_trade": 2, "post_trade": 1})

    def test_rolls_over_at_a_new_utc_day(self):
        import collectors.llm_client as m
        from datetime import date
        m._record_call("pre_trade")
        self.assertEqual(calls_today(), {"pre_trade": 1})
        # Simulate having recorded that call yesterday: the accessor should
        # read as empty rather than carry stale counts into a new day, and
        # the next call should start a fresh count rather than accumulate.
        m._calls_today_date = date(2000, 1, 1)
        self.assertEqual(calls_today(), {})
        m._record_call("pre_trade")
        self.assertEqual(calls_today(), {"pre_trade": 1})



@pytest.mark.asyncio
async def test_ask_json_increments_the_counter():
    import collectors.llm_client as m
    saved = (dict(m._calls_today), m._calls_today_date)
    m._calls_today.clear()
    m._calls_today_date = None
    try:
        with patch("collectors.llm_client.chain_for", return_value=[]):
            reply = await m.ask_json("pre_trade", "sys", "user")
        assert not reply
        assert calls_today() == {"pre_trade": 1}
    finally:
        m._calls_today.clear()
        m._calls_today.update(saved[0])
        m._calls_today_date = saved[1]


# ── Mirror review still goes through the shared helper unchanged ───────────
# (behavioural proof lives in tests/test_mirror_review.py, which must pass
# unmodified after this refactor — this just pins that the gate now lives in
# collectors.llm_client rather than being reimplemented inline.)

class TestMirrorReviewUsesSharedGate(unittest.TestCase):
    def test_runner_imports_should_call_again_from_llm_client(self):
        import scheduler.runner as runner_mod
        self.assertIs(runner_mod.should_call_again, should_call_again)


# ── Part C.3: guaranteed minimum penalty for a CAUTION verdict ─────────────

def _runner_with_state(price=60000.0):
    runner = AppRunner()
    runner.notifier = AsyncMock()
    st = CryptoState(symbol="btcusdt", base_asset="BTC")
    st.current_price = price
    runner.crypto_store._states["btcusdt"] = st
    return runner, st


@pytest.mark.asyncio
async def test_caution_costs_a_guaranteed_minimum_even_at_delta_zero():
    """The bug: a CAUTION verdict with the model's own confidence_delta at
    0.0 (a diplomatic 'proceed with caution' that forgot to also lower the
    number) used to cost nothing at all. It must now cost at least
    groq_caution_min_penalty."""
    engine, sm = await _fresh_db()
    runner, st = _runner_with_state()
    runner.groq_sentinel = _mock_groq(verdict="CAUTION", delta=0.0)

    sig = _signal(confidence=0.74)
    with patch("scheduler.runner.AsyncSessionFactory", sm), \
         patch("scheduler.runner.settings.groq_signal_review_enabled", True), \
         patch("scheduler.runner.settings.groq_caution_min_penalty", 0.01):
        from storage.repository import Repository
        async with sm() as s:
            scfg = await Repository(s).get_strategy_config()
        verdict, _summary = await runner._ai_review_candidate(sig, st, [st], scfg)

    assert verdict == "CAUTION"
    assert sig.confidence <= 0.74 - 0.01 + 1e-9
    await engine.dispose()


@pytest.mark.asyncio
async def test_caution_never_shrinks_a_larger_self_assessed_penalty():
    """The floor only ever guarantees a MINIMUM cost — it must not override
    a model that already scored CAUTION worse than the floor."""
    engine, sm = await _fresh_db()
    runner, st = _runner_with_state()
    runner.groq_sentinel = _mock_groq(verdict="CAUTION", delta=-0.03)

    sig = _signal(confidence=0.80)
    with patch("scheduler.runner.AsyncSessionFactory", sm), \
         patch("scheduler.runner.settings.groq_signal_review_enabled", True), \
         patch("scheduler.runner.settings.groq_caution_min_penalty", 0.01):
        from storage.repository import Repository
        async with sm() as s:
            scfg = await Repository(s).get_strategy_config()
        await runner._ai_review_candidate(sig, st, [st], scfg)

    # -0.03 is more negative than the -0.01 floor, so the model's own,
    # larger penalty must win.
    assert sig.confidence == round(0.80 - 0.03, 4)
    await engine.dispose()


@pytest.mark.asyncio
async def test_approve_still_costs_nothing():
    """Regression: only CAUTION and REJECT get a guaranteed floor. An
    APPROVE with delta 0.0 must still leave confidence untouched."""
    engine, sm = await _fresh_db()
    runner, st = _runner_with_state()
    runner.groq_sentinel = _mock_groq(verdict="APPROVE", delta=0.0)

    sig = _signal(confidence=0.74)
    with patch("scheduler.runner.AsyncSessionFactory", sm), \
         patch("scheduler.runner.settings.groq_signal_review_enabled", True), \
         patch("scheduler.runner.settings.groq_caution_min_penalty", 0.01):
        from storage.repository import Repository
        async with sm() as s:
            scfg = await Repository(s).get_strategy_config()
        await runner._ai_review_candidate(sig, st, [st], scfg)

    assert sig.confidence == 0.74
    await engine.dispose()


if __name__ == "__main__":
    unittest.main()


import pytest as _pytest_hk
from unittest.mock import patch as _patch_hk


@_pytest_hk.fixture(autouse=True)
def _intraday_signals_may_be_reviewed():
    """These tests exercise AI review of intraday signals. Since the swing book became the trading book,
    shadow-only intraday signals skip AI review unless a caller opts back in, which these tests do."""
    from config.settings import settings
    with _patch_hk.object(settings, "paper_open_families", "all"):
        yield
