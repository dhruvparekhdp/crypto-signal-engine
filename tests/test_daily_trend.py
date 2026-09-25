"""Longs only above the 20-day average, shorts only below it."""
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
