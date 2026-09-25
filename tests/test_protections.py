"""Entry protections: session, daily loss, streaks, cooldown, correlation, stops, liquidation."""

import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace

from analysis.protections import (
    ProtectionConfig,
    RecentTrade,
    check_entry,
    in_session,
    losing_streak,
    max_leverage_for_liquidation,
    stop_too_tight,
)

CFG = ProtectionConfig()
WED_NOON = datetime(2026, 9, 23, 12, 0)


def ok(now=WED_NOON, trades=(), wallet=3000.0, positions=(), symbol="solusdt",
       direction="long", cfg=CFG):
    return check_entry(now, symbol, direction, list(trades), wallet, list(positions), cfg)


class TestProtections(unittest.TestCase):
    def test_session(self):
        self.assertTrue(in_session(WED_NOON, CFG))
        self.assertFalse(in_session(WED_NOON.replace(hour=3), CFG))
        self.assertFalse(in_session(datetime(2026, 9, 26, 12), CFG))   # Saturday
        self.assertEqual(ok(now=WED_NOON.replace(hour=20))[1], "outside_session")
        self.assertTrue(ok(now=WED_NOON.replace(hour=20),
                           cfg=ProtectionConfig(session_filter=False))[0])

    def test_daily_loss_limit(self):
        t = [RecentTrade("btcusdt", WED_NOON - timedelta(hours=1), -80.0)]
        self.assertTrue(ok(trades=t)[0])
        t.append(RecentTrade("ethusdt", WED_NOON - timedelta(minutes=50), 50.0))
        t.append(RecentTrade("xrpusdt", WED_NOON - timedelta(minutes=40), -60.0))
        self.assertEqual(ok(trades=t)[1], "daily_loss_limit")        # 90 >= 3% of 3000
        yesterday = [RecentTrade("btcusdt", WED_NOON - timedelta(days=1), -500.0)]
        self.assertTrue(ok(trades=yesterday)[0])

    def test_losing_streak(self):
        t = [RecentTrade("a", WED_NOON - timedelta(minutes=m), -1.0) for m in (90, 60, 30)]
        self.assertEqual(losing_streak(t)[0], 3)
        self.assertEqual(ok(trades=t)[1], "losing_streak_pause")
        self.assertTrue(ok(now=WED_NOON + timedelta(minutes=95), trades=t)[0])
        t += [RecentTrade("a", WED_NOON - timedelta(minutes=m), -1.0) for m in (120, 150)]
        self.assertEqual(ok(now=WED_NOON + timedelta(hours=4), trades=t)[1],
                         "losing_streak_day_over")
        t.append(RecentTrade("a", WED_NOON - timedelta(minutes=5), 2.0))  # a win resets
        self.assertEqual(losing_streak(t)[0], 0)

    def test_pair_cooldown_and_correlation(self):
        t = [RecentTrade("solusdt", WED_NOON - timedelta(minutes=5), 1.0)]
        self.assertEqual(ok(trades=t)[1], "pair_cooldown")
        self.assertTrue(ok(trades=t, symbol="ethusdt")[0])
        longs = [SimpleNamespace(symbol="btcusdt", side="long"),
                 SimpleNamespace(symbol="ethusdt", side="long")]
        self.assertEqual(ok(positions=longs)[1], "correlated_exposure")
        self.assertTrue(ok(positions=longs, direction="short")[0])
        gold = [SimpleNamespace(symbol="xauusdt", side="long"), longs[0]]
        self.assertTrue(ok(positions=gold)[0])

    def test_stop_floor_and_liquidation_cap(self):
        self.assertTrue(stop_too_tight(100.0, 99.9, 0.00118, CFG))     # 0.1% < 0.177%
        self.assertFalse(stop_too_tight(100.0, 99.5, 0.00118, CFG))
        lev = max_leverage_for_liquidation(100.0, 99.5, 0.0053, CFG)
        self.assertAlmostEqual(lev, 1 / (3 * 0.005 + 0.0053), places=6)  # ~49x
        # at that leverage liquidation is ~3x the stop distance away
        self.assertAlmostEqual((1 / lev - 0.0053) / 0.005, 3.0, places=6)
        self.assertIsNone(max_leverage_for_liquidation(100.0, 0.0, 0.0053, CFG))
        self.assertLess(max_leverage_for_liquidation(100.0, 97.0, 0.0053, CFG), 11)


if __name__ == "__main__":
    unittest.main()
