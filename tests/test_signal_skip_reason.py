"""
Why a fired signal never became a paper trade, on the exact signal row the
Signals page reads (27 Sep) — the ETHUSDT Volume Surge screenshot: 50%
confidence after the AI review, published, but paper trading's own
min_confidence (0.70) is a separate, higher bar. The card had no way to
say so; it just looked ignored.

The trap this is guarding against: reprice_signal() does
`sig, why_not = reprice_signal(sig, ...)`, and on several outcomes that
REASSIGNS sig to None — sig.log_id would be gone at the exact moment it is
needed to write the reason back. log_id has to be captured before that
line, not read off sig afterwards.
"""
import asyncio
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from analysis.crypto_signal import CryptoSignal
from analysis.crypto_state import CryptoState
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
        trigger_description="t", confidence=0.55, current_price=60000.0,
        target_price=61200.0, stop_loss=59400.0, edge_pct=2.0, stake_pct=0.015,
        timeframe="15m", sentiment_score=0.0, indicators_summary="",
        timestamp=datetime.now(UTC))
    base.update(over)
    return CryptoSignal(**base)


class TestMarkSignalSkipped(unittest.TestCase):
    def test_sets_the_reason_on_the_right_row(self):
        async def go():
            engine, sm = await _fresh_db()
            from storage.repository import Repository
            async with sm() as s:
                repo = Repository(s)
                log_id = await repo.log_crypto_signal(
                    symbol="btcusdt", signal_type="confluence", direction="long",
                    trigger_description="t", confidence=0.55, current_price=60000.0,
                    target_price=61200.0, stop_loss=59400.0, edge_pct=2.0,
                    stake_pct=0.015, timeframe="15m")
                await repo.mark_signal_skipped(log_id, "confidence_below_floor")
                rows = await repo.crypto_signals_between(1)
            self.assertEqual(rows[0].skip_reason, "confidence_below_floor")
            await engine.dispose()
        asyncio.run(go())

    def test_a_zero_log_id_is_a_no_op_not_an_error(self):
        async def go():
            engine, sm = await _fresh_db()
            from storage.repository import Repository
            async with sm() as s:
                await Repository(s).mark_signal_skipped(0, "confidence_below_floor")
            await engine.dispose()
        asyncio.run(go())   # must not raise


@pytest.mark.asyncio
async def test_a_signal_below_papers_own_confidence_floor_is_explained_on_its_row():
    """Exactly the owner's screenshot: fires (crypto_min_confidence 0.5),
    but paper trading's own floor is higher (0.70 here)."""
    engine, sm = await _fresh_db()
    runner = AppRunner()
    runner.notifier = AsyncMock()
    with patch("scheduler.runner.settings.paper_trading_enabled", True), \
         patch("scheduler.runner.settings.session_filter_enabled", False), \
         patch("scheduler.runner.AsyncSessionFactory", sm):
        from storage.repository import Repository

        st = CryptoState(symbol="btcusdt", base_asset="BTC")
        st.current_price = 60000.0
        st.atr_14 = 600.0
        runner.crypto_store._states["btcusdt"] = st

        async with sm() as s:
            repo = Repository(s)
            await repo.update_paper_config(min_confidence=0.70)
            log_id = await repo.log_crypto_signal(
                symbol="btcusdt", signal_type="confluence", direction="long",
                trigger_description="t", confidence=0.55, current_price=60000.0,
                target_price=61200.0, stop_loss=59400.0, edge_pct=2.0,
                stake_pct=0.015, timeframe="15m")

        sig = _signal(confidence=0.55)
        sig.log_id = log_id
        runner._pending_paper_signals.append((sig, st))

        await runner._paper_trading_job()

        async with sm() as s:
            repo = Repository(s)
            rows = await repo.crypto_signals_between(1)
            cycle = await repo.get_running_cycle()
            open_positions = await repo.get_open_positions(cycle.id) if cycle else []

    assert rows[0].skip_reason == "confidence_below_floor"
    assert open_positions == []
    await engine.dispose()


@pytest.mark.asyncio
async def test_an_opened_trade_leaves_skip_reason_empty():
    engine, sm = await _fresh_db()
    runner = AppRunner()
    runner.notifier = AsyncMock()
    with patch("scheduler.runner.settings.paper_trading_enabled", True), \
         patch("scheduler.runner.settings.session_filter_enabled", False), \
         patch("scheduler.runner.AsyncSessionFactory", sm):
        from storage.repository import Repository

        st = CryptoState(symbol="btcusdt", base_asset="BTC")
        st.current_price = 60000.0
        st.atr_14 = 600.0
        runner.crypto_store._states["btcusdt"] = st

        async with sm() as s:
            repo = Repository(s)
            log_id = await repo.log_crypto_signal(
                symbol="btcusdt", signal_type="confluence", direction="long",
                trigger_description="t", confidence=0.80, current_price=60000.0,
                target_price=61200.0, stop_loss=59400.0, edge_pct=2.0,
                stake_pct=0.015, timeframe="15m")

        sig = _signal(confidence=0.80)
        sig.log_id = log_id
        runner._pending_paper_signals.append((sig, st))

        await runner._paper_trading_job()

        async with sm() as s:
            rows = await Repository(s).crypto_signals_between(1)

    assert rows[0].skip_reason == ""
    await engine.dispose()


@pytest.mark.asyncio
async def test_a_stale_signal_is_still_explained_even_though_reprice_signal_nulls_it():
    """The exact trap this feature could fall into: reprice_signal()
    returns (None, "stale") and reassigns the loop's `sig` to None — the
    reason must still land on the ORIGINAL signal's row."""
    engine, sm = await _fresh_db()
    runner = AppRunner()
    runner.notifier = AsyncMock()
    with patch("scheduler.runner.settings.paper_trading_enabled", True), \
         patch("scheduler.runner.settings.session_filter_enabled", False), \
         patch("scheduler.runner.AsyncSessionFactory", sm):
        from storage.repository import Repository

        st = CryptoState(symbol="btcusdt", base_asset="BTC")
        st.current_price = 60000.0
        st.atr_14 = 600.0
        runner.crypto_store._states["btcusdt"] = st

        async with sm() as s:
            repo = Repository(s)
            log_id = await repo.log_crypto_signal(
                symbol="btcusdt", signal_type="confluence", direction="long",
                trigger_description="t", confidence=0.80, current_price=60000.0,
                target_price=61200.0, stop_loss=59400.0, edge_pct=2.0,
                stake_pct=0.015, timeframe="15m")

        old_ts = datetime.now(UTC) - timedelta(minutes=30)
        sig = _signal(confidence=0.80, timestamp=old_ts)   # older than max_signal_age_seconds
        sig.log_id = log_id
        runner._pending_paper_signals.append((sig, st))

        await runner._paper_trading_job()

        async with sm() as s:
            rows = await Repository(s).crypto_signals_between(1)

    assert rows[0].skip_reason == "stale"
    await engine.dispose()


if __name__ == "__main__":
    unittest.main()
