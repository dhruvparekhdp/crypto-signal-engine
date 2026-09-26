"""The owner's profit lock: at +0.5% lock a small win, then trail tight."""

import unittest
from datetime import datetime, timedelta

from analysis.paper_cycle import resolve_at_price
from analysis.paper_trading import (
    CycleConfig,
    ExitReason,
    FeeModel,
    ProfitLock,
    Side,
    TrailingStop,
    open_position,
)

T0 = datetime(2026, 9, 26, 15, 18)
FEES = FeeModel()
LOCK = ProfitLock(enabled=True, at_pct=0.5, to_pct=0.35, trail_pct=0.15)


def sol(side=Side.LONG, entry=120.69, stop=119.0, target=122.13):
    return open_position("solusdt", side, entry, 30.0, 25.0, FEES, 0.2, 2.0, T0,
                         stop_price=stop, target_price=target)


def cfg():
    return CycleConfig(trailing=TrailingStop(enabled=False))


class TestProfitLock(unittest.TestCase):
    def test_nothing_happens_before_the_move(self):
        p = sol()
        self.assertFalse(p.apply_profit_lock(121.2, LOCK, FEES))        # +0.42%
        self.assertEqual(p.stop_price, 119.0)

    def test_the_owners_sol_trade(self):
        """Entry 120.69; at ~121.31 he pulled the stop to 121.15. The rule does the same."""
        p = sol()
        self.assertTrue(p.apply_profit_lock(121.31, LOCK, FEES))        # +0.51%
        # the higher of the lock (+0.35% = 121.112) and the trail (121.31 - 0.15%
        # = 121.128): 121.128, next to the 121.15 he set by hand
        self.assertAlmostEqual(p.stop_price, 121.31 * (1 - 0.0015), places=4)
        self.assertTrue(p.trail_active)
        self.assertEqual(p.target_price, 122.13)                        # target kept
        p.apply_profit_lock(121.80, LOCK, FEES)                         # new high
        self.assertAlmostEqual(p.stop_price, 121.80 * (1 - 0.0015), places=4)
        self.assertFalse(p.apply_profit_lock(121.50, LOCK, FEES))       # never loosens
        self.assertAlmostEqual(p.stop_price, 121.80 * (1 - 0.0015), places=4)

    def test_a_locked_trade_ends_as_a_win_after_fees(self):
        p = sol()
        now, wallet = T0, 3000.0
        for px in (121.0, 121.31, 121.6, 121.5):          # 121.5 stays above 121.418
            now += timedelta(seconds=30)
            self.assertIsNone(resolve_at_price(p, px, now, cfg(), wallet, lock=LOCK))
        trade = resolve_at_price(p, 121.3, now + timedelta(seconds=30), cfg(), wallet, lock=LOCK)
        self.assertIsNotNone(trade)
        self.assertIs(trade.reason, ExitReason.STOP)
        self.assertGreater(trade.net_pnl, 0)

    def test_the_lock_always_covers_costs(self):
        tiny = ProfitLock(enabled=True, at_pct=0.5, to_pct=0.01, trail_pct=0.15)
        p = sol()
        p.apply_profit_lock(121.31, tiny, FEES)
        self.assertGreaterEqual((p.stop_price / 120.69 - 1), FEES.round_trip_pct() - 1e-12)

    def test_the_stop_never_jumps_to_the_price(self):
        wide = ProfitLock(enabled=True, at_pct=0.3, to_pct=0.5, trail_pct=0.15)
        p = sol()
        p.apply_profit_lock(121.1, wide, FEES)                          # +0.34%
        self.assertLess(p.stop_price, 121.1 * (1 - 0.0015) + 1e-9)

    def test_shorts_mirror(self):
        p = sol(Side.SHORT, entry=120.69, stop=122.0, target=119.0)
        self.assertTrue(p.apply_profit_lock(120.07, LOCK, FEES))        # -0.51%
        # lower of the lock (120.268) and the trail (120.07 + 0.15% = 120.250)
        self.assertAlmostEqual(p.stop_price, 120.07 * 1.0015, places=4)
        p.apply_profit_lock(119.5, LOCK, FEES)
        self.assertAlmostEqual(p.stop_price, 119.5 * 1.0015, places=4)

    def test_disabled_does_nothing(self):
        p = sol()
        self.assertFalse(p.apply_profit_lock(122.0, ProfitLock(enabled=False), FEES))
        self.assertEqual(p.stop_price, 119.0)


if __name__ == "__main__":
    unittest.main()
