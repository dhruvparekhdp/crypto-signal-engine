"""
A running paper cycle used to be deaf to Settings changes.

`config_for_cycle()` rebuilt every risk/behaviour knob from the PaperCycle
row written once when the cycle started (16 Sep, in production) - not from
the live, Settings-editable PaperTradingConfig. `max_concurrent`/
`max_hold_minutes` were already patched on top from the live config as a
one-off workaround at the single call site; nothing else was. So lowering
the confidence floor - or any other risk knob - on the Settings page had
zero effect on the cycle actually running, only on whatever cycle starts
next (which, in production, could be weeks away).

29 Sep: the owner's explicit choice was to make every tunable knob live
immediately, no manual "apply" step, and no opt-out banner - only the
cycle's own financial state (starting_wallet/target_wallet: which run this
is, not how it's risk-managed) stays tied to the cycle it belongs to.

This is an end-to-end proof through the real tick, not just a unit test of
config_for_cycle() in isolation: fire the identical signal before and
after a live settings change, same running cycle throughout, and show the
tick's own decision changes without a new cycle ever starting.
"""
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from analysis.crypto_signal import CryptoSignal
from analysis.crypto_state import CryptoState
from scheduler.runner import AppRunner
from storage.database import Base


def _signal(confidence: float) -> CryptoSignal:
    return CryptoSignal(
        symbol="btcusdt", signal_type="confluence", direction="long",
        trigger_description="Confluence long setup", confidence=confidence,
        current_price=60000.0, target_price=61200.0, stop_loss=59400.0,
        edge_pct=2.0, stake_pct=0.015, timeframe="15m", sentiment_score=0.1,
        indicators_summary="", timestamp=datetime.now(UTC))


@pytest.mark.asyncio
async def test_a_live_confidence_floor_change_reaches_the_running_cycle_immediately():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    runner = AppRunner()
    runner.notifier = AsyncMock()
    st = CryptoState(symbol="btcusdt", base_asset="BTC")
    st.current_price = 60000.0
    runner.crypto_store._states["btcusdt"] = st

    with patch("scheduler.runner.settings.paper_trading_enabled", True), \
         patch("scheduler.runner.settings.session_filter_enabled", False), \
         patch("scheduler.runner.AsyncSessionFactory", session_maker):
        from storage.repository import Repository

        # Cycle starts under the default floor (0.70) - this is the one
        # and only cycle for the whole test, never recreated.
        runner._pending_paper_signals.append((_signal(0.60), st))
        await runner._paper_trading_job()

        async with session_maker() as s:
            repo = Repository(s)
            cycle = await repo.get_running_cycle()
            assert cycle.min_confidence == 0.70   # the cycle's own frozen snapshot
            assert len(await repo.get_open_positions(cycle.id)) == 0  # 0.60 < 0.70: skipped

        # Lower the floor on the live, Settings-editable config - nothing
        # about the running cycle's own row changes.
        async with session_maker() as s:
            await Repository(s).update_paper_config(min_confidence=0.55)

        # Same signal, same confidence, same still-running cycle.
        runner._pending_paper_signals.append((_signal(0.60), st))
        await runner._paper_trading_job()

        async with session_maker() as s:
            repo = Repository(s)
            cycle = await repo.get_running_cycle()
            assert cycle.id == 1   # still the same cycle - never recreated
            assert cycle.min_confidence == 0.70   # the row's own snapshot is untouched
            open_positions = await repo.get_open_positions(cycle.id)
            assert len(open_positions) == 1   # but the live floor (0.55) is what actually decided
            assert open_positions[0].symbol == "btcusdt"

    await engine.dispose()


@pytest.mark.asyncio
async def test_the_cycles_own_wallet_and_target_are_never_overridden_by_live_config():
    """The fix is scoped to risk/behaviour knobs only. starting_wallet and
    target_wallet describe which run this is - a live settings change must
    never silently move them out from under a cycle in progress."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    runner = AppRunner()
    runner.notifier = AsyncMock()
    st = CryptoState(symbol="btcusdt", base_asset="BTC")
    st.current_price = 60000.0
    runner.crypto_store._states["btcusdt"] = st

    with patch("scheduler.runner.settings.paper_trading_enabled", True), \
         patch("scheduler.runner.settings.session_filter_enabled", False), \
         patch("scheduler.runner.AsyncSessionFactory", session_maker):
        from storage.repository import Repository

        runner._pending_paper_signals.append((_signal(0.90), st))
        await runner._paper_trading_job()

        async with session_maker() as s:
            repo = Repository(s)
            cycle = await repo.get_running_cycle()
            started_with = cycle.starting_wallet
            target_with = cycle.target_wallet

        async with session_maker() as s:
            await Repository(s).update_paper_config(
                starting_wallet=999999.0, target_wallet=1.0)

        async with session_maker() as s:
            repo = Repository(s)
            cycle = await repo.get_running_cycle()
            assert cycle.starting_wallet == started_with
            assert cycle.target_wallet == target_with

    await engine.dispose()
