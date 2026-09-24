"""
Widening the stop without touching leverage doubles the loss per trade.

The first live cycle exited thirteen of fourteen trades on the stop and
reached no targets. Measured from the fills, every stop sat at 0.408% of
price — 2x a one-minute ATR — while the positions it guarded stayed open for
a median of twenty-seven minutes and sometimes a full day. A one-minute
measurement was guarding an hours-long exposure.

The fix has two halves and only one of them is obvious. Moving the stop from
0.408% to 0.918% is the visible half. The other is that a stop-out costs
leverage times the price distance, so the same change at a fixed 10x would
take the cost of a failed trade from 4.1% of margin to 9.2% — a change made
to survive noise would have doubled every loss. These tests exist because
that half is easy to leave out and expensive to forget.

What none of this does is create edge. For a market with no drift the chance
of reaching the target before the stop is stop/(stop+target), which depends
on the ratio and not the distance — 33.3% at 2:1, at any width. That is
asserted below too, so nobody reads a wider stop as a better trade.
"""
import unittest

from analysis.paper_trading import CycleConfig
from analysis.scalp_levels import ScalpConfig

ENTRY = 100.0


def _stop_at(pct: float) -> float:
    return ENTRY * (1 - pct / 100)


class TestTheRiskBudgetHolds(unittest.TestCase):
    """The amount at stake is configured; the leverage is whatever fits it."""

    def setUp(self):
        self.cfg = CycleConfig()

    def test_a_stop_out_never_costs_more_than_the_budget(self):
        """
        A ceiling, not a quota. A stop tight enough that 10x already fits
        inside the budget is left at 10x and simply risks less — the rule only
        ever removes leverage, it never adds it to spend the allowance.
        """
        for stop_pct in (0.2, 0.408, 0.918, 1.5, 3.0):
            with self.subTest(stop_pct=stop_pct):
                lev = self.cfg.leverage_for_stop(10.0, ENTRY, _stop_at(stop_pct))
                loss = lev * stop_pct / 100
                self.assertLessEqual(loss, self.cfg.max_loss_pct_of_margin + 1e-9)

    def test_a_stop_wide_enough_to_bind_spends_exactly_the_budget(self):
        """Where the cap actually bites, it lands on the number, not near it."""
        for stop_pct in (0.918, 1.5, 3.0):
            with self.subTest(stop_pct=stop_pct):
                lev = self.cfg.leverage_for_stop(10.0, ENTRY, _stop_at(stop_pct))
                self.assertAlmostEqual(lev * stop_pct / 100,
                                       self.cfg.max_loss_pct_of_margin, places=9)

    def test_widening_the_stop_lowers_the_leverage(self):
        """The whole point. Without this, widening doubles the loss."""
        tight = self.cfg.leverage_for_stop(10.0, ENTRY, _stop_at(0.408))
        wide = self.cfg.leverage_for_stop(10.0, ENTRY, _stop_at(0.918))
        self.assertLess(wide, tight)
        self.assertAlmostEqual(wide, tight * 0.408 / 0.918, places=4)

    def test_it_never_raises_leverage_above_what_was_asked_for(self):
        """A generous stop is not a licence to take more leverage."""
        for stop_pct in (0.05, 0.1, 0.2):
            with self.subTest(stop_pct=stop_pct):
                self.assertLessEqual(
                    self.cfg.leverage_for_stop(5.0, ENTRY, _stop_at(stop_pct)), 5.0)

    def test_an_enormous_stop_does_not_drive_leverage_to_zero(self):
        """Below 1x the position can be too small to clear a single lot."""
        self.assertGreaterEqual(
            self.cfg.leverage_for_stop(10.0, ENTRY, _stop_at(50.0)),
            self.cfg.min_leverage)

    def test_the_budget_is_what_the_first_cycle_actually_risked(self):
        """
        The live cycle ran 10x against a measured 0.408% stop, so it was
        risking 4.08% of margin per trade. The budget is set to that, so this
        change moves where the exit sits without moving what a failure costs.
        """
        measured = 10.0 * 0.408 / 100
        self.assertAlmostEqual(self.cfg.max_loss_pct_of_margin, measured, delta=0.002)

    def test_nonsense_inputs_return_the_leverage_unchanged(self):
        for entry, stop in ((0.0, 99.0), (100.0, 0.0), (100.0, 100.0)):
            with self.subTest(entry=entry, stop=stop):
                self.assertEqual(self.cfg.leverage_for_stop(7.0, entry, stop), 7.0)


class TestTheStopIsSizedToTheHold(unittest.TestCase):
    def test_the_multiple_grew(self):
        """2.0 x a one-minute ATR was guarding a hold measured in hours."""
        self.assertGreater(ScalpConfig().stop_atr_multiple, 4.0)

    def test_the_stop_now_clears_one_hours_noise(self):
        """
        Measured on real history: the 1-hour move spread is 0.5921%, and the
        live ATR implied by the fills is 0.204%. The old stop sat at 0.69 of a
        standard deviation — inside the range price covers by doing nothing.
        """
        atr_pct, hourly_sd = 0.204, 0.5921
        stop = atr_pct * ScalpConfig().stop_atr_multiple
        self.assertGreater(stop / hourly_sd, 1.4)

    def test_the_fee_share_of_risk_falls(self):
        """
        The round trip is a fixed 0.118% of notional whatever the stop, so a
        tight stop hands most of what is risked to the exchange.
        """
        round_trip = 0.118
        old = round_trip / 0.408
        new = round_trip / (0.204 * ScalpConfig().stop_atr_multiple)
        self.assertGreater(old, 0.28)
        self.assertLess(new, 0.15)


class TestItIsNotSoldAsEdge(unittest.TestCase):
    """
    Guards against the reading that a wider stop is a better trade. For a
    driftless market P(target first) = stop/(stop+target): the ratio decides
    it, not the distance. Simulated over 60,000 paths this came out 34.8% at
    the old width and 33.7% at the new one, against the 33.3% predicted.
    """

    def test_the_odds_depend_on_the_ratio_and_not_the_width(self):
        for stop in (0.408, 0.918, 2.5):
            with self.subTest(stop=stop):
                target = stop * ScalpConfig().target_reward_risk
                self.assertAlmostEqual(stop / (stop + target), 1 / 3, places=6)

    def test_break_even_still_has_to_be_beaten(self):
        """At 2:1 the system needs better than 33.3%. It measured 0 of 14."""
        rr = ScalpConfig().target_reward_risk
        self.assertAlmostEqual(1 / (1 + rr), 1 / 3, places=6)
