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
            assert t.net_pnl > 0 and t.exit_reason == "profit_lock"


@pytest.mark.asyncio
async def test_a_fourth_same_side_trade_fits_under_the_12pct_cap():
    # 12 Oct 2026: at 9%, three 3% shorts blocked every later short for 4 days
    from tests.test_swing_selection import run
    sigs = [("SOLUSDT", "vol_breakout", -1), ("BTCUSDT", "donchian", -1), ("DOGEUSDT", "keltner_break", -1),
            ("XRPUSDT", "vol_breakout", -1)]
    rows, logs = await run(sigs, cap=0)
    assert len(rows) == 4, [(lg.symbol, lg.skip_reason) for lg in logs]


def test_ladder_steps_match_the_backtest_s1():
    from analysis.swing_book import BE_COST_FRAC
    # long entry 100, initial stop 94 (1R = 6)
    assert swing_lock(+1, 100, 94, 94, 0, 105, "ladder", 0, 0)[0] == 94                  # 0.83R: nothing yet
    assert swing_lock(+1, 100, 94, 94, 0, 106, "ladder", 0, 0)[0] == pytest.approx(100 * (1 + BE_COST_FRAC))  # 1R
    assert swing_lock(+1, 100, 94, 94, 0, 109, "ladder", 0, 0)[0] == pytest.approx(103)  # 1.5R -> lock 0.5R
    assert swing_lock(+1, 100, 94, 94, 0, 115, "ladder", 0, 0)[0] == pytest.approx(109)  # 2.5R -> lock 1.5R
    s, b = swing_lock(-1, 100, 106, 106, 0, 91, "ladder", 0, 0)                          # short 1.5R
    assert (s, b) == (pytest.approx(97), 91)
    assert swing_lock(-1, 100, 106, 97, 91, 99, "ladder", 0, 0)[0] == pytest.approx(97)  # never loosens


def test_ladder_is_the_live_default():
    from config.settings import Settings
    assert Settings.model_fields["swing_lock_mode"].default == "ladder"


@pytest.mark.asyncio
async def test_best_price_since_open_is_rebuilt_from_candles():
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from scheduler.runner import AppRunner
    runner = AppRunner()
    now = datetime.now(UTC)
    bars = [{"timestamp": now - timedelta(hours=h), "high": 100 + h, "low": 95 - h} for h in (1, 2, 3, 30)]
    runner.delta_market = SimpleNamespace(fetch_candles=AsyncMock(return_value=bars))
    row = SimpleNamespace(symbol="solusdt", opened_at=(now - timedelta(hours=5)).replace(tzinfo=None))
    assert await runner._best_price_since(row, -1) == 92        # lowest low since open (the 30h-old bar excluded)
    assert await runner._best_price_since(row, +1) == 103


def test_ladder_keeps_locking_however_far_the_trade_runs():
    # long entry 100, 1R = 6: at 10R (+60%) the lock is 9R; at 100R (+600%) it is 99R
    assert swing_lock(+1, 100, 94, 94, 0, 160, "ladder", 0, 0)[0] == pytest.approx(100 + 9 * 6)
    assert swing_lock(+1, 100, 94, 94, 0, 700, "ladder", 0, 0)[0] == pytest.approx(100 + 99 * 6)
    assert swing_lock(-1, 100, 106, 106, 0, 82, "ladder", 0, 0)[0] == pytest.approx(100 - 2.0 * 6)   # 3R short
    assert swing_lock(+1, 100, 94, 94, 0, 97, "ladder", 0, 0)[0] == 94     # -0.5R (in loss): original stop stays


def test_ladder_buffer_leaves_room_below_each_lock():
    # long entry 100, 1R = 6, buffer 0.25R: 1R -> -0.25R (98.5), 1.5R -> +0.25R (101.5)
    assert swing_lock(+1, 100, 94, 94, 0, 106, "ladder", 0, 0, buffer_r=0.25)[0] == pytest.approx(98.5)
    assert swing_lock(+1, 100, 94, 94, 0, 109, "ladder", 0, 0, buffer_r=0.25)[0] == pytest.approx(101.5)


async def _tick_setup(monkeypatch, closed_beyond):
    from datetime import UTC, datetime
    from unittest.mock import AsyncMock

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from analysis import swing_book as sb
    from analysis.crypto_state import CryptoState
    from analysis.regime_gate import Verdict
    from config.settings import settings
    from scheduler.runner import AppRunner
    from storage.models import Base
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    for k, v in dict(paper_trading_enabled=True, session_filter_enabled=False, swing_lock_mode="ladder",
                     swing_lock_buffer_r=0.25, swing_lock_confirm_min=60).items():
        monkeypatch.setattr(settings, k, v)
    monkeypatch.setattr("scheduler.runner.AsyncSessionFactory", sm)
    runner = AppRunner()
    runner.notifier = AsyncMock()
    runner._closed_beyond = AsyncMock(return_value=closed_beyond)
    st = CryptoState(symbol="btcusdt", base_asset="BTC")
    st.current_price, st.atr_14 = 60000.0, 300.0
    runner.crypto_store._states["btcusdt"] = st
    sig = sb.to_signal(sb.SwingSetup("BTCUSDT", "keltner_break", +1, 0, 1000.0, 60000.0), 60000.0, datetime.now(UTC))
    sig.regime = Verdict(0.2, 15.0, [])
    runner._pending_swing.append(sig)
    await runner._paper_trading_job()                       # open: stop 57,000, 1R = 3,000
    st.current_price = 64600.0                              # +1.53R: lock at +0.25R (~60,750)
    await runner._paper_trading_job()
    return runner, st, sm


@pytest.mark.asyncio
async def test_a_wick_below_the_locked_stop_does_not_close_without_a_confirmed_close(monkeypatch):
    from storage.repository import Repository
    runner, st, sm = await _tick_setup(monkeypatch, closed_beyond=False)
    st.current_price = 60500.0                              # wick below the ~60,750 lock, no 1h close beyond it
    await runner._paper_trading_job()
    async with sm() as s:
        cycle = await Repository(s).get_running_cycle()
        rows = await Repository(s).get_open_positions(cycle.id)
        assert len(rows) == 1 and rows[0].stop_price > 60000          # still open, lock kept


@pytest.mark.asyncio
async def test_a_confirmed_close_beyond_the_lock_books_the_profit(monkeypatch):
    from storage.repository import Repository
    runner, st, sm = await _tick_setup(monkeypatch, closed_beyond=True)
    st.current_price = 60600.0
    await runner._paper_trading_job()
    async with sm() as s:
        cycle = await Repository(s).get_running_cycle()
        assert await Repository(s).get_open_positions(cycle.id) == []
        t = (await Repository(s).get_cycle_trades(cycle.id))[0]
        assert t.exit_reason == "profit_lock"


@pytest.mark.asyncio
async def test_the_original_stop_still_fires_instantly_while_locked(monkeypatch):
    from storage.repository import Repository
    runner, st, sm = await _tick_setup(monkeypatch, closed_beyond=False)
    st.current_price = 56000.0                              # crash through the original 57,000 stop
    await runner._paper_trading_job()
    async with sm() as s:
        cycle = await Repository(s).get_running_cycle()
        assert await Repository(s).get_open_positions(cycle.id) == []


@pytest.mark.asyncio
async def test_closed_beyond_reads_the_last_closed_candle_only():
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from scheduler.runner import AppRunner
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    bars = [{"timestamp": now - timedelta(hours=2), "close": 99.0},
            {"timestamp": now - timedelta(hours=1), "close": 101.0},     # last CLOSED 1h candle
            {"timestamp": now, "close": 90.0}]                           # still forming: ignored
    r = AppRunner()
    r.delta_market = SimpleNamespace(fetch_candles=AsyncMock(return_value=bars))
    assert await r._closed_beyond("solusdt", +1, 100.0, 60) is False     # long stop 100: closed at 101, above
    r._close_cache = {}
    assert await r._closed_beyond("solusdt", -1, 100.0, 60) is True      # short stop 100: closed at 101, beyond
