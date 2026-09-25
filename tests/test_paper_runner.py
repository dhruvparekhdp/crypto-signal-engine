"""
Integration test for paper trading lifecycle inside AppRunner.
"""
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from analysis.crypto_signal import CryptoSignal
from analysis.crypto_state import CryptoState
from config.settings import settings
from scheduler.runner import AppRunner
import storage.models
from storage.database import Base


@pytest.mark.asyncio
async def test_paper_trading_end_to_end():
    # Setup test in-memory SQLite database
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    runner = AppRunner()
    runner.notifier = AsyncMock()

    # Enable paper trading in settings for this test
    with patch("scheduler.runner.settings.paper_trading_enabled", True), \
         patch("scheduler.runner.settings.paper_alert_telegram", True), \
         patch("scheduler.runner.settings.session_filter_enabled", False), \
         patch("scheduler.runner.AsyncSessionFactory", session_maker):

        # 1. Provide a crypto state in crypto_store
        st = CryptoState(symbol="btcusdt", base_asset="BTC")
        st.current_price = 60000.0
        st.atr_14 = 600.0
        runner.crypto_store._states["btcusdt"] = st

        # 2. Queue a candidate signal
        sig = CryptoSignal(
            symbol="btcusdt",
            signal_type="confluence",
            direction="long",
            trigger_description="Confluence long setup",
            confidence=0.80,
            current_price=60000.0,
            target_price=61200.0,
            stop_loss=59400.0,
            edge_pct=2.0,
            stake_pct=0.015,
            timeframe="15m",
            sentiment_score=0.1,
            indicators_summary="Test indicators",
            timestamp=datetime.now(timezone.utc),
            ai_review="Solid setup",
        )
        runner._pending_paper_signals.append((sig, st))

        # 3. Run tick 1: should start cycle, open paper trade
        await runner._paper_trading_job()

        # Verify trade opened in DB
        async with session_maker() as session:
            from storage.repository import Repository
            repo = Repository(session)
            cycle = await repo.get_running_cycle()
            assert cycle is not None
            assert cycle.status == "running"

            open_positions = await repo.get_open_positions(cycle.id)
            assert len(open_positions) == 1
            pos = open_positions[0]
            assert pos.symbol == "btcusdt"
            assert pos.side == "long"
            assert pos.margin > 0
            assert cycle.wallet < cycle.starting_wallet  # Margin deducted

        # Verify telegram open alert was sent
        assert runner.notifier.send_text.called

        # 4. Simulate price move reaching target price (target is 60000 * 1.04 = 62400)
        st.current_price = 63000.0

        # Run tick 2: position should close at target
        await runner._paper_trading_job()

        async with session_maker() as session:
            repo = Repository(session)
            open_positions = await repo.get_open_positions(cycle.id)
            assert len(open_positions) == 0  # Closed and deleted

            trades = await repo.get_cycle_trades(cycle.id)
            assert len(trades) == 1
            trade = trades[0]
            assert trade.symbol == "btcusdt"
            assert trade.exit_reason == "target"
            assert trade.net_pnl > 0  # Winning trade!

    await engine.dispose()
