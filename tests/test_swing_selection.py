"""Swing book selection: at most two trades per direction, strongest signal first, no trades on coins without edge."""
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from analysis import swing_book as sb
from analysis.crypto_state import CryptoState
from storage.models import Base

PRICES = {"BTCUSDT": 60000.0, "SOLUSDT": 150.0, "DOGEUSDT": 0.2, "XRPUSDT": 0.6, "BCHUSDT": 400.0}


async def run(signals):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    from scheduler.runner import AppRunner
    runner = AppRunner()
    runner.notifier = AsyncMock()
    with patch("scheduler.runner.settings.paper_trading_enabled", True), \
         patch("scheduler.runner.settings.session_filter_enabled", False), \
         patch("scheduler.runner.AsyncSessionFactory", session_maker):
        for sym, px in PRICES.items():
            st = CryptoState(symbol=sym.lower(), base_asset=sym[:-4])
            st.current_price = px
            st.atr_14 = px * 0.005
            runner.crypto_store._states[sym.lower()] = st
        now = datetime.now(UTC)
        for sym, strat, side in signals:
            px = PRICES[sym]
            runner._pending_swing.append(sb.to_signal(sb.SwingSetup(sym, strat, side, 0, px * 0.015, px), px, now))
        await runner._paper_trading_job()
        from storage.repository import Repository
        async with session_maker() as s:
            repo = Repository(s)
            cycle = await repo.get_running_cycle()
            rows = await repo.get_open_positions(cycle.id)
            logs = await repo.crypto_signals_between(1, 0)
    return rows, logs


@pytest.mark.asyncio
async def test_three_longs_together_open_only_the_two_strongest():
    rows, logs = await run([("XRPUSDT", "ichimoku", +1), ("SOLUSDT", "vol_breakout", +1), ("BTCUSDT", "donchian", +1)])
    assert sorted(r.symbol for r in rows) == ["btcusdt", "solusdt"]          # XRP + Ichimoku ranks lowest
    skipped = {l.symbol: l.skip_reason for l in logs if l.skip_reason}
    assert skipped == {"xrpusdt": "max_2_longs_open"}


@pytest.mark.asyncio
async def test_the_cap_is_per_direction():
    rows, _ = await run([("SOLUSDT", "vol_breakout", +1), ("BTCUSDT", "donchian", +1), ("DOGEUSDT", "keltner_break", -1)])
    assert len(rows) == 3


@pytest.mark.asyncio
async def test_coins_without_edge_are_not_traded():
    rows, logs = await run([("BCHUSDT", "vol_breakout", +1)])
    assert rows == []
    assert logs[0].skip_reason == "coin_without_edge"


def test_priority_prefers_strong_strategy_and_coin():
    assert sb.priority("swing_vol_breakout", "4h", "solusdt") > sb.priority("swing_ichimoku", "4h", "xrpusdt")
    assert sb.priority("swing_unknown", "1h", "newusdt") == pytest.approx(0.4)
