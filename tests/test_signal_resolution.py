"""Signal labels (owner, 19 Oct 2026: "fix the resolver labels"): judged on the signal's own window, stop first on a
bar that touches both, expiry only after the whole window, and a hole in the bars is a gap - not a label."""
from datetime import datetime, timedelta

from analysis.crypto_state import OHLCVCandle
from analysis.signal_resolution import resolve, window_minutes

T0 = datetime(2026, 10, 19, 1, 36, 25)


def bars(rows, start=T0.replace(second=0) + timedelta(minutes=1)):
    return [OHLCVCandle(open=r[0], high=r[1], low=r[2], close=r[3], volume=1, timestamp=start + timedelta(minutes=i))
            for i, r in enumerate(rows)]


FLAT = (100, 100.1, 99.9, 100)


def test_a_bar_touching_both_is_a_loss_even_if_it_closes_green():
    r = resolve("long", 100, 102, 99, T0, bars([FLAT, (100, 102.5, 98.5, 102.4)]), 60)
    assert r.status == "lost" and r.pnl_pct == -1.0


def test_target_first_is_a_win_and_shorts_mirror_it():
    assert resolve("long", 100, 102, 99, T0, bars([FLAT, (100, 102.1, 99.8, 102)]), 60).status == "won"
    r = resolve("short", 100, 98, 101, T0, bars([FLAT, (100, 100.2, 97.9, 98)]), 60)
    assert r.status == "won" and r.pnl_pct == 2.0 and r.minutes == 2


def test_judged_on_the_signals_own_window_not_24_hours():
    late_hit = [FLAT] * 90 + [(100, 102.5, 99.9, 102)]          # target on minute 91 of an 81-minute signal
    r = resolve("long", 100, 102, 99, T0, bars(late_hit), window_minutes("81m"))
    assert r.status == "expired" and r.minutes == 81
    assert resolve("long", 100, 102, 99, T0, bars(late_hit), window_minutes("4h")).status == "won"   # same bars, 4h claim


def test_expiry_waits_for_the_whole_window():
    assert resolve("long", 100, 102, 99, T0, bars([FLAT] * 30), 60).status == "running"
    r = resolve("long", 100, 102, 99, T0, bars([FLAT] * 59 + [(100, 100.6, 99.9, 100.5)]), 60)
    assert r.status == "expired" and r.pnl_pct == 0.5


def test_a_hole_in_the_bars_is_a_gap_not_a_label():
    late_start = bars([FLAT] * 70, start=T0 + timedelta(minutes=20))     # memory starts 20 min after the signal
    assert resolve("long", 100, 102, 99, T0, late_start, 60).status == "gap"
    holed = bars([FLAT] * 10) + bars([FLAT] * 60, start=T0 + timedelta(minutes=30))
    assert resolve("long", 100, 102, 99, T0, holed, 60).status == "gap"


def test_store_shaped_dicts_and_window_parsing():
    rows = [{"timestamp": T0 + timedelta(seconds=35), "open": 100, "high": 102.2, "low": 99.95, "close": 102}]
    assert resolve("long", 100, 102, 99, T0, rows, 60).status == "won"
    assert (window_minutes("81m"), window_minutes("1h"), window_minutes("4h"), window_minutes("x")) == (81, 60, 240, 240)


async def _job_outcome(candle_rows, timeframe="1h", minutes_ago=30):
    """Run the real resolver job on one logged signal (long at 100, target 102, stop 99) with these 1m candles."""
    from datetime import UTC
    from unittest.mock import patch

    from sqlalchemy import update
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from analysis.crypto_state import CryptoState
    from scheduler.runner import AppRunner
    from storage.models import Base, CryptoSignalLog
    from storage.repository import Repository
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    sig_ts = (datetime.now(UTC) - timedelta(minutes=minutes_ago)).replace(tzinfo=None)
    async with sm() as s:
        await Repository(s).log_crypto_signal(symbol="solusdt", signal_type="confluence", direction="long",
                                              trigger_description="x", confidence=0.6, current_price=100.0,
                                              target_price=102.0, stop_loss=99.0, edge_pct=0, stake_pct=0,
                                              timeframe=timeframe, trade_mode="intraday")
        await s.execute(update(CryptoSignalLog).values(timestamp=sig_ts))
        await s.commit()
    runner = AppRunner()
    st = CryptoState(symbol="solusdt", base_asset="SOL")
    st.candles_1m = bars(candle_rows, start=sig_ts.replace(second=0, microsecond=0) + timedelta(minutes=1))
    runner.crypto_store._states["solusdt"] = st
    with patch("scheduler.runner.AsyncSessionFactory", sm):
        await runner._resolve_signal_outcomes_job()
    async with sm() as s:
        return (await Repository(s).crypto_signals_between(1, 0))[0].outcome


import pytest  # noqa: E402


@pytest.mark.asyncio
async def test_the_job_books_a_two_touch_bar_as_a_loss():
    # the old job booked this 'won': the bar touched 98.5 and 102.5 and closed green
    assert await _job_outcome([FLAT, (100, 102.5, 98.5, 102.4)] + [FLAT] * 20) == "lost"


@pytest.mark.asyncio
async def test_the_job_expires_at_the_signals_own_window():
    # 15-minute signal, flat for 25 minutes: expired (the old job kept it open for the paper engine's 24 hours)
    assert await _job_outcome([FLAT] * 25, timeframe="15m") == "expired"
