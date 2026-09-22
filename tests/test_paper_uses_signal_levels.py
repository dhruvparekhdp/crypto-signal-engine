"""
The paper book has to trade the signal, not something near it.

Captured 22 Sep, 7:49 PM, one minute apart on the same pair:

  signal      XAUUSDT LONG  entry 4,334.6300  target 4,344.6600  stop 4,329.6100
  paper trade XAUUSDT LONG  entry 4,335.0635  target 4,508.4660  stop 4,248.3622

The published target was 0.231% away. The trade opened against one 4% away —
seventeen times wider — because the levels were rebuilt from a margin-risk
budget (`stop_pct_of_margin / leverage`, then times the reward:risk) and the
signal's own target and stop were never read.

The cost is visible fifteen minutes earlier in the same chat: an XAUUSDT long
opened at 4,341.0741 and closed at 4,338.1561 — a move of -0.067%, nowhere
near either level — as "time expired", paying 2.09 in fees and 0.29 funding
to learn nothing. Gold does not travel 4% in the hold window, so every gold
trade resolved that way.

Two consequences, both fatal to a proof of concept: the paper P&L measured a
strategy the engine never proposed, and its win rate could never agree with
the signal accuracy on /audit, because they were scoring different trades.
"""
import unittest
from datetime import UTC, datetime

from analysis.paper_cycle import _hold_minutes
from analysis.paper_trading import FeeModel, Side, open_position

SIG_ENTRY, SIG_TARGET, SIG_STOP = 4334.63, 4344.66, 4329.61
FEES = FeeModel()


def _open(**kw):
    base = dict(
        symbol="xauusdt", side=Side.LONG, entry_price=SIG_ENTRY, margin=646.0,
        leverage=10.0, fees=FEES, stop_pct_of_margin=0.20, reward_risk=2.0,
        opened_at=datetime.now(UTC), timeframe="16m",
    )
    base.update(kw)
    return open_position(**base)


class TestSignalLevelsAreHonoured(unittest.TestCase):
    def test_the_trade_opens_on_the_published_levels(self):
        pos = _open(stop_price=SIG_STOP, target_price=SIG_TARGET)
        self.assertAlmostEqual(pos.target_price, SIG_TARGET, places=4)
        self.assertAlmostEqual(pos.stop_price, SIG_STOP, places=4)

    def test_the_old_behaviour_is_what_the_screenshot_showed(self):
        """Without explicit levels it still rebuilds them — 4% and 2%."""
        pos = _open()
        target_move = (pos.target_price - pos.entry_price) / pos.entry_price
        stop_move = (pos.entry_price - pos.stop_price) / pos.entry_price
        self.assertAlmostEqual(target_move, 0.04, places=4)
        self.assertAlmostEqual(stop_move, 0.02, places=4)

    def test_honouring_the_signal_preserves_its_reward_to_risk(self):
        pos = _open(stop_price=SIG_STOP, target_price=SIG_TARGET)
        reward = pos.target_price - pos.entry_price
        risk = pos.entry_price - pos.stop_price
        self.assertAlmostEqual(reward / risk, 2.0, places=1)

    def test_the_levels_are_not_seventeen_times_too_wide(self):
        signal_move = (SIG_TARGET - SIG_ENTRY) / SIG_ENTRY
        pos = _open(stop_price=SIG_STOP, target_price=SIG_TARGET)
        paper_move = (pos.target_price - pos.entry_price) / pos.entry_price
        self.assertLess(abs(paper_move / signal_move - 1.0), 0.05)

    def test_a_short_keeps_its_orientation(self):
        pos = _open(side=Side.SHORT, stop_price=4344.66, target_price=4329.61)
        self.assertLess(pos.target_price, pos.entry_price)
        self.assertGreater(pos.stop_price, pos.entry_price)


class TestHoldWindow(unittest.TestCase):
    class Sig:
        def __init__(self, tf):
            self.timeframe = tf

    class Cfg:
        max_hold_minutes = 240

    def test_a_sixteen_minute_setup_is_not_held_for_four_hours(self):
        self.assertEqual(_hold_minutes(self.Sig("16m"), self.Cfg()), 24.0)

    def test_the_configured_maximum_is_still_the_ceiling(self):
        self.assertEqual(_hold_minutes(self.Sig("8h"), self.Cfg()), 240)

    def test_a_very_short_setup_still_gets_room_to_breathe(self):
        self.assertEqual(_hold_minutes(self.Sig("2m"), self.Cfg()), 15.0)

    def test_an_unparseable_timeframe_falls_back_to_the_maximum(self):
        for tf in ("", "soon", None, "abcm"):
            with self.subTest(tf=tf):
                self.assertEqual(_hold_minutes(self.Sig(tf), self.Cfg()), 240)


if __name__ == "__main__":
    unittest.main()
