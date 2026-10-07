"""End to end: a swing signal opens through the real paper job, keeps its tested stop, closes at target."""
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from analysis import swing_book as sb
from analysis.crypto_state import CryptoState
from scheduler.runner import AppRunner
from storage.models import Base


@pytest.mark.asyncio
async def test_swing_trade_opens_holds_its_stop_and_closes_at_target():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    runner = AppRunner()
    runner.notifier = AsyncMock()
    with patch("scheduler.runner.settings.paper_trading_enabled", True), \
         patch("scheduler.runner.settings.session_filter_enabled", False), \
         patch("scheduler.runner.settings.position_review_enabled", True), \
         patch("scheduler.runner.AsyncSessionFactory", session_maker):
        st = CryptoState(symbol="btcusdt", base_asset="BTC")
        st.current_price = 60000.0
        st.atr_14 = 300.0
        runner.crypto_store._states["btcusdt"] = st
        setup = sb.SwingSetup("BTCUSDT", "keltner_break", +1, 0, 1000.0, 60000.0)   # 4h ATR 1000 -> 5% stop (tf defaults to 4h)
        _sig = sb.to_signal(setup, 60000.0, datetime.now(UTC))
        from analysis.regime_gate import Verdict
        _sig.regime = Verdict(0.2, 15.0, [])
        runner._pending_swing.append(_sig)
        runner._review_open_position = AsyncMock(side_effect=AssertionError("AI must not review swing positions"))

        await runner._paper_trading_job()                         # tick 1: open
        from storage.repository import Repository
        async with session_maker() as s:
            repo = Repository(s)
            cycle = await repo.get_running_cycle()
            rows = await repo.get_open_positions(cycle.id)
            assert len(rows) == 1
            row = rows[0]
            assert row.trade_mode == "swing" and row.signal_type == "swing_keltner_break"
            assert row.stop_price == pytest.approx(57000.0) and row.target_price == pytest.approx(69000.0)
            assert row.leverage <= 10.0
            # a stop-out costs about 3% of the wallet (plus costs)
            loss_at_stop = row.margin * row.leverage * (3000 / 60000)
            assert 0.025 * cycle.starting_wallet < loss_at_stop < 0.0305 * cycle.starting_wallet
            assert (row.expires_at.replace(tzinfo=UTC) - datetime.now(UTC)).total_seconds() > 6.9 * 86400

        st.current_price = 66000.0                                # tick 2: +10%, below target
        await runner._paper_trading_job()
        async with session_maker() as s:
            row = (await Repository(s).get_open_positions(cycle.id))[0]
            assert row.stop_price == pytest.approx(57000.0)       # no profit lock, trail, ladder or ratchet

        st.current_price = 69500.0                                # tick 3: target
        await runner._paper_trading_job()
        async with session_maker() as s:
            repo = Repository(s)
            assert await repo.get_open_positions(cycle.id) == []
            t = (await repo.get_cycle_trades(cycle.id))[0]
            assert t.exit_reason == "target" and t.trade_mode == "swing" and t.net_pnl > 0


@pytest.mark.asyncio
async def test_intraday_signals_no_longer_open_trades_by_default():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    runner = AppRunner()
    runner.notifier = AsyncMock()
    from analysis.crypto_signal import CryptoSignal
    with patch("scheduler.runner.settings.paper_trading_enabled", True), \
         patch("scheduler.runner.settings.session_filter_enabled", False), \
         patch("scheduler.runner.AsyncSessionFactory", session_maker):
        st = CryptoState(symbol="btcusdt", base_asset="BTC")
        st.current_price = 60000.0
        st.atr_14 = 600.0
        runner.crypto_store._states["btcusdt"] = st
        sig = CryptoSignal(symbol="btcusdt", signal_type="confluence", direction="long", trigger_description="x",
                           confidence=0.8, current_price=60000.0, target_price=61200.0, stop_loss=59400.0, edge_pct=2,
                           stake_pct=0.01, timeframe="15m", sentiment_score=0, indicators_summary="", timestamp=datetime.now(UTC))
        runner._pending_paper_signals.append((sig, st))
        await runner._paper_trading_job()
        from storage.repository import Repository
        async with session_maker() as s:
            repo = Repository(s)
            cycle = await repo.get_running_cycle()
            assert await repo.get_open_positions(cycle.id) == []
