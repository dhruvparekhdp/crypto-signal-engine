"""
settings.ai_review_can_block_trade (28 Sep) — "no auto kill, let's trade
go through."

The owner's instruction: the Groq Sentinel still runs and its verdict,
summary and confidence_delta are still saved via save_review every time.
What changes is only whether that delta is allowed to touch sig.confidence
before the crypto_min_confidence / paper_min_confidence floor check.

Off (the new default) must reproduce "no auto kill": a REJECT that would
have sunk a signal below the floor no longer can, and the signal still
becomes a paper-trade candidate. On must reproduce exactly today's older
behaviour, unchanged.

Follows the idiom in tests/test_mirror_review.py and
tests/test_ai_throttle_and_caution.py: a fresh in-memory SQLite DB per test.
"""
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from analysis.crypto_signal import CryptoSignal
from analysis.crypto_state import CryptoState
from scheduler.runner import AppRunner
from storage.database import Base
from storage.models import SignalReview


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


def _mock_groq(verdict="REJECT", delta=0.0, summary="looks weak"):
    g = MagicMock()
    g.is_available = True
    g.postmortem_available = False
    g.last_factors = ""
    g.last_model = "mock-model"
    g.last_latency_ms = 5
    g.review_signal_candidate = AsyncMock(return_value=(delta, summary, verdict))
    return g


def _runner_with_state(price=60000.0):
    runner = AppRunner()
    runner.notifier = AsyncMock()
    st = CryptoState(symbol="btcusdt", base_asset="BTC")
    st.current_price = price
    runner.crypto_store._states["btcusdt"] = st
    return runner, st


def test_default_is_false():
    """The owner's new default: no auto kill unless explicitly flipped back
    on."""
    from config.settings import settings
    assert settings.ai_review_can_block_trade is False


# ── _ai_review_candidate: the mirror-review call site ───────────────────────

@pytest.mark.asyncio
async def test_flag_off_does_not_move_confidence_but_still_saves_the_real_delta():
    engine, sm = await _fresh_db()
    runner, st = _runner_with_state()
    runner.groq_sentinel = _mock_groq(verdict="REJECT", summary="mathematically unsound")

    sig = _signal(confidence=0.74)
    with patch("scheduler.runner.AsyncSessionFactory", sm), \
         patch("scheduler.runner.settings.groq_signal_review_enabled", True), \
         patch("scheduler.runner.settings.groq_reject_penalty", 0.08), \
         patch("scheduler.runner.settings.ai_review_can_block_trade", False):
        from storage.repository import Repository
        async with sm() as s:
            scfg = await Repository(s).get_strategy_config()
        verdict, summary = await runner._ai_review_candidate(sig, st, [st], scfg)

        assert verdict == "REJECT"
        # The floor-facing number is completely untouched by the AI's delta.
        assert sig.confidence == 0.74

        async with sm() as s:
            rows = list((await s.execute(select(SignalReview))).scalars())
    assert len(rows) == 1
    # The real delta the AI wanted is still recorded, even though it was
    # never applied to sig.confidence.
    assert rows[0].verdict == "REJECT"
    assert rows[0].confidence_delta == pytest.approx(-0.08)
    await engine.dispose()


@pytest.mark.asyncio
async def test_flag_on_reproduces_todays_older_behaviour():
    engine, sm = await _fresh_db()
    runner, st = _runner_with_state()
    runner.groq_sentinel = _mock_groq(verdict="REJECT", summary="mathematically unsound")

    sig = _signal(confidence=0.74)
    with patch("scheduler.runner.AsyncSessionFactory", sm), \
         patch("scheduler.runner.settings.groq_signal_review_enabled", True), \
         patch("scheduler.runner.settings.groq_reject_penalty", 0.08), \
         patch("scheduler.runner.settings.ai_review_can_block_trade", True):
        from storage.repository import Repository
        async with sm() as s:
            scfg = await Repository(s).get_strategy_config()
        await runner._ai_review_candidate(sig, st, [st], scfg)

    assert sig.confidence == pytest.approx(0.74 - 0.08)
    await engine.dispose()


# ── _analyse_states: the single-signal path — proves the floor check itself ─

@pytest.mark.asyncio
async def test_off_a_reject_no_longer_sinks_the_signal_below_the_floor():
    """The owner's own framing: 'no auto kill, let's trade go through'. A
    signal that starts at 0.74 and would drop to 0.66 on REJECT must still
    clear a 0.70 floor when the flag is off, and must reach the paper-trade
    queue instead of being suppressed as 'ai_review'."""
    engine, sm = await _fresh_db()
    runner, st = _runner_with_state()
    runner.groq_sentinel = _mock_groq(verdict="REJECT", summary="too risky")

    fired = _signal(confidence=0.74)
    runner.crypto_engine.process = MagicMock(return_value=[fired])
    runner.crypto_engine.forget = MagicMock()

    with patch("scheduler.runner.settings.mirror_review_enabled", False), \
         patch("scheduler.runner.settings.crypto_min_confidence", 0.70), \
         patch("scheduler.runner.settings.groq_signal_review_enabled", True), \
         patch("scheduler.runner.settings.groq_reject_penalty", 0.08), \
         patch("scheduler.runner.settings.ai_review_can_block_trade", False), \
         patch("scheduler.runner.settings.paper_trading_enabled", True), \
         patch("scheduler.runner.AsyncSessionFactory", sm):
        from storage.repository import Repository
        async with sm() as s:
            repo = Repository(s)
            scfg = await repo.get_strategy_config()
            pcfg = await repo.get_paper_config()

        await runner._analyse_states([st], scfg, pcfg)

        async with sm() as s:
            rows = await Repository(s).get_recent_crypto_signals(hours=24)

    assert len(rows) == 1
    # Never suppressed by the AI review, and its confidence in the log is
    # the pre-AI-review number, not sunk by the REJECT delta.
    assert rows[0].suppressed_by == ""
    assert rows[0].confidence == pytest.approx(0.74)
    assert len(runner._pending_paper_signals) == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_on_a_reject_still_sinks_the_signal_below_the_floor():
    """Same setup with the flag flipped back on: today's older behaviour,
    unchanged — the REJECT penalty pushes confidence below the floor and
    the signal is suppressed as 'ai_review', never reaching the paper
    queue."""
    engine, sm = await _fresh_db()
    runner, st = _runner_with_state()
    runner.groq_sentinel = _mock_groq(verdict="REJECT", summary="too risky")

    fired = _signal(confidence=0.74)
    runner.crypto_engine.process = MagicMock(return_value=[fired])
    runner.crypto_engine.forget = MagicMock()

    with patch("scheduler.runner.settings.mirror_review_enabled", False), \
         patch("scheduler.runner.settings.crypto_min_confidence", 0.70), \
         patch("scheduler.runner.settings.groq_signal_review_enabled", True), \
         patch("scheduler.runner.settings.groq_reject_penalty", 0.08), \
         patch("scheduler.runner.settings.ai_review_can_block_trade", True), \
         patch("scheduler.runner.settings.paper_trading_enabled", True), \
         patch("scheduler.runner.AsyncSessionFactory", sm):
        from storage.repository import Repository
        async with sm() as s:
            repo = Repository(s)
            scfg = await repo.get_strategy_config()
            pcfg = await repo.get_paper_config()

        await runner._analyse_states([st], scfg, pcfg)

        async with sm() as s:
            rows = await Repository(s).get_recent_crypto_signals(hours=24)

    assert len(rows) == 1
    assert rows[0].suppressed_by == "ai_review"
    assert rows[0].confidence == pytest.approx(0.74 - 0.08)
    assert runner._pending_paper_signals == []
    await engine.dispose()
