"""
Reward:risk above 1 and a trailing stop are one decision, not two.

On its own a 2R target caps the winner that has to pay for the losers. On its
own a trail behind a 1R target never arms, because the position closes at the
target first — `trailing_can_activate` has said so all along and nothing was
listening. These tests pin the pair.
"""
import math
import unittest
from datetime import UTC, datetime

from analysis.paper_trading import (
    CycleConfig,
    FeeModel,
    Position,
    Side,
    TrailingStop,
)
from analysis.scalp_levels import (
    NoTrade,
    ScalpConfig,
    ScalpLevels,
    scalp_levels,
    trailing_plan,
)


class TestTargetIsFurtherThanTheStop(unittest.TestCase):
    def setUp(self):
        self.cfg = ScalpConfig()

    def test_the_default_ratio_is_no_longer_a_coin_flip(self):
        self.assertGreater(self.cfg.target_reward_risk, 1.0)

    def test_levels_deliver_the_configured_ratio(self):
        for atr in (0.0015, 0.0022, 0.004):
            got = scalp_levels(2522.0, True, atr, self.cfg.for_symbol("ethusdt"),
                               symbol="ethusdt", horizon_minutes=15)
            with self.subTest(atr=atr):
                self.assertIsInstance(got, ScalpLevels)
                self.assertAlmostEqual(got.reward_risk,
                                       self.cfg.target_reward_risk, delta=0.05)

    def test_the_ratio_comes_from_config_not_from_five_literals(self):
        """Passing nothing must use the cost frame's own aim."""
        auto = scalp_levels(2522.0, True, 0.0022, self.cfg, symbol="ethusdt")
        pinned = scalp_levels(2522.0, True, 0.0022, self.cfg, symbol="ethusdt",
                              reward_risk=self.cfg.target_reward_risk)
        self.assertEqual(auto, pinned)

    def test_an_explicit_ratio_is_honoured_as_a_floor(self):
        """
        Asking for 1.0 gets at least 1.0, and sometimes a little more: the
        target can never sit below the cost floor, so on a stop that is itself
        near the floor the delivered ratio is nudged up rather than the target
        being cut into a losing one.
        """
        got = scalp_levels(2522.0, True, 0.0022, self.cfg, symbol="ethusdt",
                           reward_risk=1.0)
        self.assertGreaterEqual(got.reward_risk, 1.0)
        self.assertLess(got.reward_risk, 1.2)
        self.assertGreaterEqual(got.target_pct, self.cfg.min_target_pct)

    def test_shorts_are_mirror_images_at_the_new_ratio(self):
        lo = scalp_levels(2522.0, True, 0.0022, self.cfg, symbol="ethusdt")
        sh = scalp_levels(2522.0, False, 0.0022, self.cfg, symbol="ethusdt")
        self.assertAlmostEqual(lo.reward_risk, sh.reward_risk, delta=0.02)
        self.assertGreater(lo.target - lo.entry, lo.entry - lo.stop)
        self.assertGreater(sh.entry - sh.target, sh.stop - sh.entry)

    def test_the_target_still_has_to_clear_the_cost_floor(self):
        """A wider ratio must not become a way to smuggle a small target in."""
        for atr in (0.0008, 0.0015, 0.003, 0.006):
            got = scalp_levels(2522.0, True, atr, self.cfg.for_symbol("ethusdt"),
                               symbol="ethusdt")
            if isinstance(got, ScalpLevels):
                with self.subTest(atr=atr):
                    self.assertGreaterEqual(got.target_pct, self.cfg.min_target_pct)


class TestStopIsAnchoredNotDerived(unittest.TestCase):
    """
    The stop used to be target/R, so raising the ratio tightened it until the
    fixed round trip was most of the money at risk — 66% of it on the live
    board. It is now anchored: outside one bar's range, and wide enough that
    the fee is a minor share of it. The target follows from the stop.
    """

    def test_the_fee_can_never_dominate_the_risk(self):
        cfg = ScalpConfig().for_symbol("ethusdt")
        for atr in (0.0004, 0.001, 0.002, 0.004):
            got = scalp_levels(2451.0, True, atr, cfg, symbol="ethusdt")
            if isinstance(got, ScalpLevels):
                with self.subTest(atr=atr):
                    self.assertLessEqual(got.cost_pct / got.stop_pct,
                                         cfg.max_cost_share_of_risk + 1e-6)

    def test_the_stop_clears_one_bar_of_noise(self):
        cfg = ScalpConfig().for_symbol("ethusdt")
        for atr in (0.001, 0.002, 0.004):
            got = scalp_levels(2451.0, True, atr, cfg, symbol="ethusdt")
            if isinstance(got, ScalpLevels):
                with self.subTest(atr=atr):
                    self.assertGreaterEqual(got.stop_pct, atr)

    def test_raising_the_ratio_widens_the_target_not_the_risk(self):
        """The inversion. At R:R 3 the stop is unchanged and the target moves."""
        cfg = ScalpConfig().for_symbol("ethusdt")
        two = scalp_levels(2451.0, True, 0.002, cfg, symbol="ethusdt",
                           reward_risk=2.0)
        three = scalp_levels(2451.0, True, 0.002, cfg, symbol="ethusdt",
                             reward_risk=3.0)
        self.assertAlmostEqual(two.stop_pct, three.stop_pct, places=4)
        self.assertGreater(three.target_pct, two.target_pct)

    def test_the_refusal_has_words_a_person_can_read(self):
        from analysis.scalp_levels import REASON_TEXT
        self.assertIn(NoTrade.STOP_INSIDE_NOISE, REASON_TEXT)
        self.assertIn(NoTrade.TOO_SLOW, REASON_TEXT)


class TestTheTrailCanNowActuallyFire(unittest.TestCase):
    def test_a_one_to_one_target_kills_the_default_trail(self):
        """
        The old configuration, kept as the counter-example it is. The default
        preset wakes at 1.0R and the target sat at 1.0R, so the position closed
        at the target first and the trail was dead code.
        """
        cfg = CycleConfig(reward_risk=1.0,
                          trailing=TrailingStop(enabled=True))
        self.assertEqual(TrailingStop().activate_at_r, 1.0)
        self.assertFalse(cfg.trailing_can_activate())

    def test_a_one_to_one_target_never_lets_the_runner_arm(self):
        """
        The runner arms at 1.25R. Against a 1.0R target the target closes the
        trade first, so the trail would be dead code; at 2.0R there is room.
        """
        cfg = CycleConfig(reward_risk=1.0, trailing=TrailingStop.runner())
        self.assertFalse(cfg.trailing_can_activate())
        widened = CycleConfig(reward_risk=2.0, trailing=TrailingStop.runner())
        self.assertTrue(widened.trailing_can_activate())

    def test_the_new_ratio_lets_it_arm(self):
        cfg = CycleConfig(reward_risk=2.0, trailing=TrailingStop.runner())
        self.assertTrue(cfg.trailing_can_activate())

    def test_the_runner_preset_arms_before_the_target(self):
        t = TrailingStop.runner()
        self.assertTrue(t.enabled)
        self.assertLess(t.activate_at_r, 2.0)
        self.assertTrue(t.release_target)
        # 9 of 42 paper trades were closed at exactly entry + fees by the
        # early breakeven jump; the runner now rides 1R behind from 1.25R.
        self.assertFalse(t.lock_breakeven)
        self.assertGreaterEqual(t.activate_at_r, 1.25)

    def test_the_shipped_settings_turn_both_on_together(self):
        from config.settings import settings
        self.assertTrue(settings.paper_trailing_enabled)
        self.assertGreater(settings.paper_reward_risk, 1.0)
        self.assertTrue(
            CycleConfig(reward_risk=settings.paper_reward_risk,
                        trailing=TrailingStop.runner()).trailing_can_activate())


class TestTrailDistanceScalesWithTheSetup(unittest.TestCase):
    """
    The margin-denominated trail is a function of the leverage dial, not of the
    setup. Against an ATR-derived stop it sits so far out it can never ratchet.
    """

    def _pos(self, stop=2511.25):
        p = Position(symbol="ethusdt", side=Side.LONG, entry_price=2522.0,
                     margin=543.88, leverage=35.0, stop_price=stop,
                     target_price=2543.49, liq_price=2462.5,
                     opened_at=datetime.now(UTC), entry_fee=0.0)
        p.initial_stop_price = stop
        return p

    def _trailed_stop(self, trail, leverage, stop=2511.25):
        p, fees = self._pos(stop=stop), FeeModel()
        p.leverage = leverage
        for high in (2534, 2548, 2575):
            p.update_trail(high, high - 2, trail, fees)
        return p.stop_price

    def test_a_margin_trail_still_moves_with_the_leverage_dial(self):
        """
        The setup is identical at both leverages — same entry, same stop, same
        bars. Only the dial changed, and the exit moves with it. That is the
        defect: 0.20 of margin is 2.0% of price at 10x and 0.57% at 35x, so the
        same trade is trailed loosely or tightly for a reason that has nothing
        to do with the trade.

        Shown here on a WIDE stop, because a tight one now hides it: the trail
        is capped at 1R, and with a signal-derived stop the margin form exceeds
        that at every leverage, so the cap — not the dial — decides the exit.
        The defect is bounded now, not repaired. R-denominated is still the
        right setting.
        """
        margin_trail = TrailingStop(enabled=True, activate_at_r=0.75,
                                    trail_pct_of_margin=0.20)
        # 57 of risk: wide enough that the margin trail (50.4 at 10x, 14.4 at
        # 35x) stays under the 1R cap, tight enough that a 53-point move still
        # clears the 0.75R activation.
        wide = 2465.0
        at10 = self._trailed_stop(margin_trail, 10.0, stop=wide)
        at35 = self._trailed_stop(margin_trail, 35.0, stop=wide)
        self.assertNotAlmostEqual(at10, at35, delta=1.0)

    def test_the_trail_never_rides_further_away_than_the_stop_it_replaces(self):
        """
        The cap that bounds the defect above, and the reason it exists.

        Leverage now falls as the stop widens, to hold the loss per trade at
        the risk budget. The margin form divides by leverage, so a 2x position
        produced a trail five times wider than its own stop — and since the
        ratchet only moves the stop toward price, it could not move at all.
        Trailing activated and then silently did nothing.
        """
        margin_trail = TrailingStop(enabled=True, activate_at_r=0.75,
                                    trail_pct_of_margin=0.20)
        at2 = self._trailed_stop(margin_trail, 2.0)
        at35 = self._trailed_stop(margin_trail, 35.0)
        self.assertAlmostEqual(at2, at35, delta=1e-9)
        # 1R behind the last high the trail saw, not five R.
        self.assertGreater(at2, self._pos().stop_price)

    def test_an_r_trail_is_the_same_exit_at_any_leverage(self):
        t = TrailingStop.runner()
        self.assertAlmostEqual(self._trailed_stop(t, 10.0),
                               self._trailed_stop(t, 35.0), delta=1e-9)

    def test_an_r_trail_rides_one_r_behind_the_high(self):
        p, fees = self._pos(), FeeModel()
        t = TrailingStop.runner()
        for high in (2534, 2548, 2575):
            p.update_trail(high, high - 2, t, fees)
        self.assertAlmostEqual(p.stop_price, 2575 - p.risk_per_unit, delta=0.01)

    def test_the_stop_only_ever_moves_forward(self):
        p, fees = self._pos(), FeeModel()
        t = TrailingStop.runner()
        seen = [p.stop_price]
        for high in (2534, 2560, 2540, 2530, 2590, 2545):
            p.update_trail(high, high - 2, t, fees)
            seen.append(p.stop_price)
        self.assertEqual(seen, sorted(seen))

    def test_arming_locks_a_profit_not_a_loss(self):
        p, fees = self._pos(), FeeModel()
        t = TrailingStop.runner()
        p.update_trail(2537, 2536, t, fees)          # 1.4R, past the 1.25R arm
        self.assertTrue(p.trail_active)
        self.assertGreater(p.stop_price, 2522.0)

    def test_a_wiggle_below_the_arm_no_longer_scratches_the_trade(self):
        p, fees = self._pos(), FeeModel()
        p.update_trail(2534, 2532, TrailingStop.runner(), fees)   # 1.1R
        self.assertFalse(p.trail_active)
        self.assertEqual(p.stop_price, 2511.25)

    def test_the_target_is_released_so_a_winner_can_run(self):
        p, fees = self._pos(), FeeModel()
        p.update_trail(2537, 2536, TrailingStop.runner(), fees)
        self.assertTrue(math.isinf(p.target_price))

    def test_it_does_not_arm_before_the_threshold(self):
        p, fees = self._pos(), FeeModel()
        p.update_trail(2527, 2525, TrailingStop.runner(), fees)   # 0.47R
        self.assertFalse(p.trail_active)
        self.assertEqual(p.stop_price, 2511.25)


class TestPublishedTrailPlan(unittest.TestCase):
    """The plan has to be followable by hand — the venue's box takes prices."""

    def setUp(self):
        self.cfg = ScalpConfig().for_symbol("ethusdt")
        self.plan = trailing_plan(2522.0, 2511.25, self.cfg)

    def test_one_r_is_the_initial_stop_distance(self):
        self.assertAlmostEqual(self.plan.risk, 10.75, places=6)

    def test_it_arms_short_of_the_target(self):
        self.assertLess(self.plan.arm_price, 2543.49)
        self.assertGreater(self.plan.arm_price, 2522.0)

    def test_the_breakeven_stop_covers_the_brokerage(self):
        self.assertGreater(self.plan.breakeven_stop, 2522.0)

    def test_the_trailed_stop_never_goes_below_breakeven(self):
        self.assertGreaterEqual(self.plan.stop_at(2528.0, True),
                                self.plan.breakeven_stop)

    def test_a_short_mirrors_the_long(self):
        short = trailing_plan(2522.0, 2532.75, self.cfg)
        self.assertLess(short.arm_price, 2522.0)
        self.assertLess(short.breakeven_stop, 2522.0)
        self.assertLessEqual(short.stop_at(2516.0, False), short.breakeven_stop)

    def test_a_degenerate_setup_returns_nothing_rather_than_dividing_by_zero(self):
        self.assertIsNone(trailing_plan(2522.0, 2522.0, self.cfg))
        self.assertIsNone(trailing_plan(0.0, 10.0, self.cfg))

    def test_the_alert_carries_the_plan(self):
        from analysis.crypto_signal import CryptoSignal
        from notifications.crypto_formatter import format_crypto_signal

        msg = format_crypto_signal(CryptoSignal(
            symbol="ethusdt", signal_type="confluence", direction="long",
            trigger_description="x", confidence=0.72, current_price=2522.0,
            target_price=2543.49, stop_loss=2511.25, edge_pct=0.684,
            stake_pct=0.005, timeframe="15m", sentiment_score=0.0,
            indicators_summary="", timestamp=datetime.now(UTC)))
        self.assertIn("Trail", msg)
        self.assertIn("2,530", msg)          # the arm price
        self.assertIn("reward:risk", msg)


if __name__ == "__main__":
    unittest.main()
