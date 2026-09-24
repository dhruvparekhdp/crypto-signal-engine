"""
Whether an open position still deserves to be open.

The rule as given: a trade at a loss is held only on 75% confidence, and a
trade in profit is trailed by confidence rather than held on faith.

Every test here exists to defend one property — this can only ever tighten
risk. A losing position may be closed EARLIER than its stop; nothing can hold
one past an exit that has already fired. By the time a stop is reached the
loss has stopped being bounded, and "it might bounce" is true of every losing
position ever opened.
"""
import unittest
from datetime import UTC, datetime, timedelta

from analysis.position_review import (
    AI_MAX_HARM,
    AI_MAX_HELP,
    HOLD_THRESHOLD,
    _needs_model,
    decide,
    momentum_agrees,
    not_exhausted,
    room_before_stop,
    time_used,
    trail_r_for_confidence,
    trend_confidence,
)

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)


class _Pos:
    def __init__(self, entry=100.0, stop=99.0, long=True, opened=-30, expires=90):
        self.symbol, self.side = "btcusdt", "long" if long else "short"
        self.entry_price, self.stop_price = entry, stop
        self.opened_at = NOW + timedelta(minutes=opened)
        self.expires_at = NOW + timedelta(minutes=expires)
        self.signal_type, self.confidence = "confluence", 0.76


class _State:
    def __init__(self, price=99.6, rsi=50.0, hist=0.0):
        self.current_price, self.rsi_14, self.macd_histogram = price, rsi, hist
        self.cvd_trend = "neutral"


class TestTheLocalRead(unittest.TestCase):
    def test_room_before_stop_runs_from_one_to_zero(self):
        self.assertAlmostEqual(room_before_stop(100, 100, 99, True), 1.0)
        self.assertAlmostEqual(room_before_stop(100, 99.5, 99, True), 0.5)
        self.assertAlmostEqual(room_before_stop(100, 99, 99, True), 0.0)

    def test_room_before_stop_reads_the_other_way_for_a_short(self):
        self.assertAlmostEqual(room_before_stop(100, 100.5, 101, False), 0.5)

    def test_a_position_past_its_stop_scores_zero_not_negative(self):
        self.assertEqual(room_before_stop(100, 98, 99, True), 0.0)

    def test_time_used_runs_down(self):
        opened, expires = NOW - timedelta(minutes=60), NOW + timedelta(minutes=60)
        self.assertAlmostEqual(time_used(opened, expires, NOW), 0.5)
        self.assertAlmostEqual(time_used(opened, expires, expires), 0.0)

    def test_momentum_reads_the_histogram_for_the_side_held(self):
        self.assertGreater(momentum_agrees(_State(hist=0.5), True), 0.5)
        self.assertLess(momentum_agrees(_State(hist=0.5), False), 0.5)
        self.assertEqual(momentum_agrees(_State(hist=0.0), True), 0.5)

    def test_exhaustion_is_about_room_left_in_the_needed_direction(self):
        """A long at RSI 82 needs more of a move the market already resisted."""
        self.assertLess(not_exhausted(_State(rsi=82), True), 0.4)
        self.assertGreater(not_exhausted(_State(rsi=82), False), 0.9)

    def test_a_healthy_position_scores_well_above_a_broken_one(self):
        healthy = trend_confidence(_Pos(), _State(price=99.9, rsi=45, hist=0.4), NOW)
        broken = trend_confidence(_Pos(expires=2),
                                  _State(price=99.05, rsi=80, hist=-0.4), NOW)
        self.assertGreater(healthy, broken + 0.3)

    def test_the_score_never_leaves_zero_to_one(self):
        for pos, state in ((_Pos(), _State(price=200.0)),
                           (_Pos(), _State(price=1.0, rsi=0)),
                           (_Pos(expires=-500), _State(rsi=100, hist=-9))):
            with self.subTest(price=state.current_price):
                self.assertGreaterEqual(trend_confidence(pos, state, NOW), 0.0)
                self.assertLessEqual(trend_confidence(pos, state, NOW), 1.0)


class TestTheModelIsOnlyAskedWhenItMatters(unittest.TestCase):
    """Paying for an opinion that cannot change the outcome is just latency."""

    def test_no_adjustment_could_rescue_a_low_score(self):
        for trend in (0.0, 0.3, 0.64):
            with self.subTest(trend=trend):
                self.assertFalse(_needs_model(trend))
                self.assertLess(trend + AI_MAX_HELP, HOLD_THRESHOLD)

    def test_no_adjustment_could_sink_a_high_score(self):
        for trend in (0.90, 0.95, 1.0):
            with self.subTest(trend=trend):
                self.assertFalse(_needs_model(trend))
                self.assertGreaterEqual(trend - AI_MAX_HARM, HOLD_THRESHOLD)

    def test_the_band_between_is_asked(self):
        for trend in (0.65, 0.70, 0.75, 0.89):
            with self.subTest(trend=trend):
                self.assertTrue(_needs_model(trend))

    def test_the_band_is_derived_from_the_bounds_not_hardcoded(self):
        """Loosening what the model may do must not silently stop it being asked."""
        self.assertFalse(_needs_model(HOLD_THRESHOLD - AI_MAX_HELP - 1e-9))
        self.assertTrue(_needs_model(HOLD_THRESHOLD - AI_MAX_HELP))
        self.assertFalse(_needs_model(HOLD_THRESHOLD + AI_MAX_HARM))


class TestTheDecision(unittest.TestCase):
    def test_the_threshold_is_the_one_that_was_asked_for(self):
        self.assertEqual(HOLD_THRESHOLD, 0.75)

    def test_at_the_threshold_it_holds(self):
        self.assertTrue(decide(0.75).hold)
        self.assertFalse(decide(0.7499).hold)

    def test_the_model_can_only_move_it_within_the_bounds(self):
        self.assertAlmostEqual(decide(0.80, +5.0).confidence, 0.80 + AI_MAX_HELP)
        self.assertAlmostEqual(decide(0.80, -5.0).confidence, 0.80 - AI_MAX_HARM)

    def test_the_model_is_given_more_room_to_close_than_to_hold(self):
        """Cutting early costs a spread; holding on costs the trade."""
        self.assertGreater(AI_MAX_HARM, AI_MAX_HELP)

    def test_no_model_answer_falls_back_to_the_local_read(self):
        """
        Not to closing, which is the one place this does not take the safer
        branch. Closing on a model outage would shut positions for a reason
        with nothing to do with the market — a provider hiccup at 3am would
        liquidate the book. The local read is a measurement; the model is a
        bounded adjustment on top, and losing it should not flip the
        measurement.
        """
        self.assertFalse(decide(0.70, None).hold)   # local read says close
        self.assertTrue(decide(0.84, None).hold)    # local read says hold
        self.assertFalse(decide(0.84, None).asked_model)

    def test_the_model_could_still_have_closed_that_position(self):
        """So the fallback is a real choice, not an equivalent outcome."""
        self.assertTrue(decide(0.84, None).hold)
        self.assertFalse(decide(0.84, -AI_MAX_HARM).hold)


class TestTheTrailForWinners(unittest.TestCase):
    def test_more_confidence_buys_a_looser_trail(self):
        self.assertGreater(trail_r_for_confidence(0.9), trail_r_for_confidence(0.3))

    def test_it_stays_inside_its_bounds_for_any_input(self):
        for c in (-5.0, 0.0, 0.5, 1.0, 5.0):
            with self.subTest(c=c):
                self.assertGreaterEqual(trail_r_for_confidence(c), 0.35)
                self.assertLessEqual(trail_r_for_confidence(c), 1.10)

    def test_even_the_loosest_trail_stays_near_one_r(self):
        """
        The trail is separately capped at 1R in update_trail, so a value far
        above that would be silently clipped and the dial would stop working.
        """
        self.assertLessEqual(trail_r_for_confidence(1.0), 1.25)


class TestEveryTradeGetsTheModelsRead(unittest.TestCase):
    """
    The instruction was that the model's read feeds the confidence on every
    trade, up or down — not only on the losing ones.

    The two sides still differ in one respect, and it is not whether the model
    is consulted. On a loser the score has a threshold to cross, so the model
    is only worth asking inside the band where its adjustment could cross it.
    On a winner the score maps continuously onto how much rope the trail gets,
    so there is no band and every point of it counts.
    """

    def test_a_winner_asks_the_model_even_at_a_score_that_would_skip_it(self):
        import inspect

        from analysis.position_review import review_position

        source = inspect.getsource(review_position)
        self.assertIn("if losing and not _needs_model(trend)", source)

    def test_the_runner_reviews_both_sides(self):
        from pathlib import Path

        runner = (Path(__file__).resolve().parent.parent / "scheduler/runner.py").read_text()
        self.assertIn("review_position(pos, state, now, losing=losing)", runner)
        # And not by returning early for winners before the review runs.
        block = runner[runner.index("async def _review_open_position"):
                       runner.index("async def _review_closed_trade")]
        self.assertLess(block.index("review = await review_position"),
                        block.index("if not losing:"))

    def test_a_winner_is_trailed_on_the_composite_not_the_local_read(self):
        from pathlib import Path

        runner = (Path(__file__).resolve().parent.parent / "scheduler/runner.py").read_text()
        self.assertIn("trail_r_for_confidence(review.confidence)", runner)

    def test_a_winner_is_never_closed_on_a_score(self):
        """
        Profitable trades are managed by the trail, as instructed. A low score
        tightens the trail; it does not book the trade.
        """
        from pathlib import Path

        runner = (Path(__file__).resolve().parent.parent / "scheduler/runner.py").read_text()
        block = runner[runner.index("if not losing:"):
                       runner.index("if review.hold:")]
        self.assertNotIn("close_position", block)
        self.assertIn("return None", block)

    def test_winners_are_reviewed_less_often_than_losers(self):
        from config.settings import settings

        self.assertGreater(settings.position_review_interval_seconds_winning,
                           settings.position_review_interval_seconds)

    def test_the_prompt_tells_the_model_which_question_it_is_answering(self):
        """
        Asked to judge a winner with a prompt written for losers, a model
        reasons about taking profit — which is the trail's job, not its own.
        """
        from analysis.position_review import REVIEW_SYSTEM

        self.assertIn("is the reason it was opened still true", REVIEW_SYSTEM)
        self.assertIn("do not", REVIEW_SYSTEM.lower())
        self.assertIn("further to go", REVIEW_SYSTEM)


class TestTheReviewPaysItsOwnWay(unittest.TestCase):
    """
    Reviewing every open position on the post-mortem chain would cost about
    Rs 2,000 a month on its own — the whole budget, before the post-mortems
    and the research pass it would be sharing with.
    """

    def test_it_does_not_run_on_the_post_mortem_chain(self):
        import inspect

        from analysis.position_review import review_position

        self.assertIn('ask_json("position_review"', inspect.getsource(review_position))

    def test_its_chain_leads_with_something_cheaper_than_the_post_mortems(self):
        from collectors.llm_client import _parse_chain
        from config.settings import settings

        review = _parse_chain(settings.llm_chain_position_review)[0]
        post = _parse_chain(settings.llm_chain_post_trade)[0]
        self.assertNotEqual(review, post)
        self.assertEqual(review[0], "gemini")


class TestItCanOnlyTightenRisk(unittest.TestCase):
    """The safety argument, asserted rather than assumed."""

    def test_the_review_runs_only_on_positions_that_survived_the_exit_check(self):
        from pathlib import Path

        runner = (Path(__file__).resolve().parent.parent / "scheduler/runner.py").read_text()
        resolve = runner.index("trade = resolve_at_price(")
        review = runner.index("trade = await self._review_open_position(")
        self.assertLess(resolve, review, "the review must not precede the exit check")

    def test_nothing_in_the_module_widens_a_stop(self):
        from pathlib import Path

        source = (Path(__file__).resolve().parent.parent
                  / "analysis/position_review.py").read_text()
        for forbidden in ("stop_price =", "expires_at =", "stop_price=", "expires_at="):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)

    def test_closing_early_uses_the_reason_that_says_why(self):
        from pathlib import Path

        from analysis.paper_trading import ExitReason

        runner = (Path(__file__).resolve().parent.parent / "scheduler/runner.py").read_text()
        self.assertIn("ExitReason.CONVICTION_LOST", runner)
        self.assertEqual(ExitReason.CONVICTION_LOST.value, "conviction_lost")
