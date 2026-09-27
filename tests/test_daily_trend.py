"""Longs only above the 20-day average, shorts only below it."""
import asyncio
import time
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from analysis.daily_trend import against_daily_trend, sma


class TestDailyTrend(unittest.TestCase):
    def test_shorting_a_coin_above_its_average_is_refused(self):
        # LTC on 23 Sep: rising all week, shorted five times, lost every time.
        self.assertTrue(against_daily_trend("short", 62.0, 58.0))
        self.assertFalse(against_daily_trend("long", 62.0, 58.0))

    def test_buying_a_coin_below_its_average_is_refused(self):
        self.assertTrue(against_daily_trend("long", 83000, 86000))
        self.assertFalse(against_daily_trend("short", 83000, 86000))

    def test_no_data_means_no_opinion(self):
        self.assertFalse(against_daily_trend("short", 62.0, None))

    def test_the_average_needs_twenty_closed_days(self):
        self.assertIsNone(sma([1.0] * 19))
        self.assertEqual(sma([1.0] * 10 + [2.0] * 20), 2.0)

    def test_it_is_wired_into_the_engine_and_refreshed_hourly(self):
        root = Path(__file__).resolve().parent.parent
        engine = root.joinpath("analysis", "crypto_engine.py").read_text()
        self.assertIn("against_daily_trend", engine)
        runner = root.joinpath("scheduler", "runner.py").read_text()
        self.assertIn('id="daily_trend"', runner)
        self.assertIn('interval="1d"', runner)


class TestDailyTrendJobFetchesConcurrently(unittest.IsolatedAsyncioTestCase):
    """
    _daily_trend_job used to await klines.fetch_candles once per watchlist
    coin, one after another — one round trip per coin, back to back. Coins
    are independent, so a bounded fan-out should make the whole hourly sweep
    take about as long as the slowest single coin, not the sum of all of
    them.
    """

    async def test_the_whole_sweep_is_far_faster_than_one_coin_at_a_time(self):
        from scheduler.runner import AppRunner

        n = 6
        delay = 0.08
        states = [types.SimpleNamespace(symbol=f"c{i}usdt", daily_sma20=None,
                                        daily_trend_at=None) for i in range(n)]
        fake = types.SimpleNamespace(
            crypto_store=types.SimpleNamespace(get_all=AsyncMock(return_value=states)))

        async def fake_fetch_candles(symbol, interval="1d", limit=60):
            await asyncio.sleep(delay)
            return [{"close": 1.0 + i} for i in range(25)]

        fake.klines = types.SimpleNamespace(
            fetch_candles=fake_fetch_candles, FETCH_CONCURRENCY=5)

        start = time.monotonic()
        await AppRunner._daily_trend_job(fake)
        elapsed = time.monotonic() - start

        self.assertTrue(all(st.daily_sma20 is not None for st in states))
        self.assertLess(elapsed, n * delay * 0.7,
                        "_daily_trend_job looks sequential, not concurrent")


if __name__ == "__main__":
    unittest.main()
