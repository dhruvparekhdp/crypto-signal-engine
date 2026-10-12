"""Profit lock for open swing trades (owner, 12 Oct 2026)."""
import pytest

from analysis.swing_book import swing_lock

# short at 100, initial stop 106 (1R = 6 = 3 ATR, so 1 ATR = 2)


def test_off_changes_nothing_but_tracks_the_best_price():
    assert swing_lock(-1, 100, 106, 106, 0, 95, "off", 2.0, 3.0) == (106, 95)


def test_breakeven_after_2r():
    assert swing_lock(-1, 100, 106, 106, 0, 90, "be", 2.0, 3.0)[0] == 106          # 1.67R: not yet
    assert swing_lock(-1, 100, 106, 106, 90, 88, "be", 2.0, 3.0)[0] == 100         # 2R reached: stop to entry


def test_trail_follows_the_best_price_and_never_loosens():
    stop, best = swing_lock(-1, 100, 106, 106, 0, 88, "trail", 2.0, 3.0)        # best 88 -> stop 88 + 6 = 94
    assert (stop, best) == (94, 88)
    stop2, best2 = swing_lock(-1, 100, 106, stop, best, 93, "trail", 2.0, 3.0)  # price bounced: stop stays 94
    assert (stop2, best2) == (94, 88)
    stop3, _ = swing_lock(+1, 100, 94, 94, 0, 112, "trail", 2.0, 1.5)           # long, 2R, half-R trail
    assert stop3 == pytest.approx(109)


@pytest.mark.asyncio
async def test_paper_tick_locks_profit_on_a_swing_trade_that_ran_and_came_back():
    from datetime import UTC, datetime
    from unittest.mock import AsyncMock, patch

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from analysis import swing_book as sb
    from analysis.crypto_state import CryptoState
    from analysis.regime_gate import Verdict
    from scheduler.runner import AppRunner
    from storage.models import Base
    from storage.repository import Repository
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    runner = AppRunner()
    runner.notifier = AsyncMock()
    with patch("scheduler.runner.settings.paper_trading_enabled", True), \
         patch("scheduler.runner.settings.session_filter_enabled", False), \
         patch("scheduler.runner.settings.swing_lock_mode", "trail"), \
         patch("scheduler.runner.settings.swing_lock_after_r", 2.0), \
         patch("scheduler.runner.settings.swing_lock_trail_atr", 3.0), \
         patch("scheduler.runner.AsyncSessionFactory", sm):
        st = CryptoState(symbol="btcusdt", base_asset="BTC")
        st.current_price, st.atr_14 = 60000.0, 300.0
        runner.crypto_store._states["btcusdt"] = st
        sig = sb.to_signal(sb.SwingSetup("BTCUSDT", "keltner_break", +1, 0, 1000.0, 60000.0), 60000.0, datetime.now(UTC))
        sig.regime = Verdict(0.2, 15.0, [])
        runner._pending_swing.append(sig)
        await runner._paper_trading_job()                       # open: stop 57,000 (1R = 3,000)
        st.current_price = 67000.0                              # +2.33R: lock arms, stop = 67,000 - 3,000
        await runner._paper_trading_job()
        async with sm() as s:
            cycle = await Repository(s).get_running_cycle()
            row = (await Repository(s).get_open_positions(cycle.id))[0]
            assert row.stop_price == pytest.approx(64000.0, rel=1e-3) and row.peak_price == pytest.approx(67000.0)
        st.current_price = 63900.0                              # came back: closes at the locked stop, in profit
        await runner._paper_trading_job()
        async with sm() as s:
            t = (await Repository(s).get_cycle_trades(cycle.id))[0]
            assert t.net_pnl > 0


@pytest.mark.asyncio
async def test_a_fourth_same_side_trade_fits_under_the_12pct_cap():
    # 12 Oct 2026: at 9%, three 3% shorts blocked every later short for 4 days
    from tests.test_swing_selection import run
    sigs = [("SOLUSDT", "vol_breakout", -1), ("BTCUSDT", "donchian", -1), ("DOGEUSDT", "keltner_break", -1),
            ("XRPUSDT", "vol_breakout", -1)]
    rows, logs = await run(sigs, cap=0)
    assert len(rows) == 4, [(lg.symbol, lg.skip_reason) for lg in logs]
