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
        # The stop that fired here was profit-lock's own tightened stop, not
        # the original 119.0 the trade was risked against — it gets its own
        # exit reason so a dashboard "N stops" figure does not fold small
        # protected wins in with real losses. See ExitReason.PROFIT_LOCK.
        self.assertIs(trade.reason, ExitReason.PROFIT_LOCK)
        self.assertGreater(trade.net_pnl, 0)

    def test_original_stop_still_books_as_stop(self):
        """A loss through the UNTOUCHED original stop keeps the plain reason."""
        p = sol()
        now, wallet = T0, 3000.0
        # Price never moves far enough in favour to arm the lock at all, then
        # falls straight through the original stop (119.0).
        trade = resolve_at_price(p, 118.5, now + timedelta(seconds=30), cfg(), wallet, lock=LOCK)
        self.assertIsNotNone(trade)
        self.assertIs(trade.reason, ExitReason.STOP)
        self.assertFalse(p.stop_moved_by_profit_lock)

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


class TestProfitLockDefersToTrail(unittest.TestCase):
    """
    Off by default (settings.profit_lock_defers_to_trail_enabled=False):
    apply_profit_lock behaves exactly as before, proven by the whole
    TestProfitLock class above running unchanged with defers_to_trail unset
    (it defaults to False on ProfitLock itself). This class only covers the
    NEW on-path.
    """

    def test_default_off_matches_todays_behaviour_exactly(self):
        p = sol()
        runner_trail = TrailingStop.runner()
        # Same LOCK as every other test in this file: defers_to_trail unset.
        self.assertFalse(LOCK.defers_to_trail)
        self.assertTrue(p.apply_profit_lock(121.31, LOCK, FEES, trail=runner_trail))
        self.assertAlmostEqual(p.stop_price, 121.31 * (1 - 0.0015), places=4)

    def test_on_defers_to_the_trails_own_activation_distance(self):
        """
        risk_per_unit = 120.69 - 119.0 = 1.69. The runner trail activates at
        1.25R = 2.1125, which is (2.1125 / 120.69) * 100 = 1.75% of price.
        1.3x that is ~2.28% — well past LOCK's flat 0.5%, so a +0.51% move
        that used to arm the lock immediately must now do nothing.
        """
        p = sol()
        deferring = ProfitLock(enabled=True, at_pct=0.5, to_pct=0.35,
                               trail_pct=0.15, defers_to_trail=True)
        runner_trail = TrailingStop.runner()
        self.assertFalse(p.apply_profit_lock(121.31, deferring, FEES, trail=runner_trail))
        self.assertEqual(p.stop_price, 119.0)          # untouched
        self.assertFalse(p.stop_moved_by_profit_lock)

        # Past the deferred threshold (~2.28%, i.e. above ~123.44), it arms.
        self.assertTrue(p.apply_profit_lock(123.6, deferring, FEES, trail=runner_trail))
        self.assertTrue(p.stop_moved_by_profit_lock)

    def test_on_but_trail_disabled_falls_back_to_at_pct(self):
        """No trail to defer to (trailing off) -> behaves like defers_to_trail
        was never set, using the plain at_pct."""
        p = sol()
        deferring = ProfitLock(enabled=True, at_pct=0.5, to_pct=0.35,
                               trail_pct=0.15, defers_to_trail=True)
        self.assertTrue(p.apply_profit_lock(121.31, deferring, FEES,
                                            trail=TrailingStop(enabled=False)))

    def test_resolve_at_price_passes_the_cycles_own_trail_through(self):
        """The live paper cycle wires cfg.trailing into apply_profit_lock,
        so turning the flag on actually changes what closes the trade —
        not just what apply_profit_lock does in isolation."""
        p = sol()
        deferring = ProfitLock(enabled=True, at_pct=0.5, to_pct=0.35,
                               trail_pct=0.15, defers_to_trail=True)
        live_cfg = CycleConfig(trailing=TrailingStop.runner())
        now, wallet = T0, 3000.0
        # A move that would have armed the plain LOCK (and closed the trade
        # near entry) must instead survive untouched, since the trail has
        # not activated yet either.
        self.assertIsNone(resolve_at_price(p, 121.31, now, live_cfg, wallet, lock=deferring))
        self.assertEqual(p.stop_price, 119.0)


if __name__ == "__main__":
    unittest.main()
