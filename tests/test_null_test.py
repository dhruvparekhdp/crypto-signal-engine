"""
Does the signal set beat entering at random?

This module exists because of a mistake it is designed to prevent. A first
run of this comparison skipped the bad-price filter, and gold's 55 snapshots
priced at 4.3e-05 instead of 4350 turned a p of 0.02 into 0.81 — the
difference between "the detectors are doing something" and "the detectors are
worthless". The conclusion was published before the error was found.

So the tests below are mostly about the ways a replay can be quietly wrong
while still returning a number.
"""
import unittest
from datetime import datetime, timedelta

from analysis.null_test import (
    Entry,
    NullResult,
    compare_against_random,
    render,
    replay,
)

T0 = datetime(2026, 9, 12, 0, 0)


def path(moves, start=100.0, step_min=2):
    """A price path from a list of percentage steps."""
    times, prices, p = [], [], start
    for i, m in enumerate(moves):
        p *= (1 + m / 100)
        times.append(T0 + timedelta(minutes=i * step_min))
        prices.append(p)
    return times, prices


class TestTheReplayItself(unittest.TestCase):
    def test_a_long_that_reaches_its_target_books_the_target(self):
        paths = {"x": path([0] + [0.5] * 20)}
        taken, wins, total = replay([Entry("x", "long", T0)], paths, 1.0, 2.0, 24, 0.0)
        self.assertEqual((taken, wins), (1, 1))
        self.assertAlmostEqual(total, 2.0)

    def test_a_long_that_reaches_its_stop_books_the_stop(self):
        paths = {"x": path([0] + [-0.5] * 20)}
        self.assertAlmostEqual(replay([Entry("x", "long", T0)], paths, 1.0, 2.0, 24, 0.0)[2], -1.0)

    def test_a_short_reads_every_level_the_other_way(self):
        paths = {"x": path([0] + [-0.5] * 20)}
        taken, wins, total = replay([Entry("x", "short", T0)], paths, 1.0, 2.0, 24, 0.0)
        self.assertEqual(wins, 1)
        self.assertAlmostEqual(total, 2.0)

    def test_the_stop_is_checked_before_the_target_within_a_bar(self):
        """
        Which level is reached FIRST is the entire question. A path that dips
        to the stop and then runs to the target is a loss, not a win.
        """
        paths = {"x": path([0, -1.2, 5.0, 5.0])}
        self.assertAlmostEqual(
            replay([Entry("x", "long", T0)], paths, 1.0, 2.0, 24, 0.0)[2], -1.0)

    def test_running_out_of_clock_books_the_move_so_far(self):
        paths = {"x": path([0] + [0.01] * 200)}
        total = replay([Entry("x", "long", T0)], paths, 5.0, 9.0, 1, 0.0)[2]
        self.assertGreater(total, 0.0)
        self.assertLess(total, 9.0)

    def test_the_round_trip_is_charged_on_every_entry(self):
        paths = {"x": path([0] + [0.5] * 20)}
        gross = replay([Entry("x", "long", T0)], paths, 1.0, 2.0, 24, 0.0)[2]
        net = replay([Entry("x", "long", T0)], paths, 1.0, 2.0, 24, 0.118)[2]
        self.assertAlmostEqual(gross - net, 0.118)

    def test_an_entry_with_no_price_history_is_skipped_not_counted(self):
        self.assertEqual(replay([Entry("nope", "long", T0)], {"x": path([0, 1])},
                                1.0, 2.0, 24, 0.0)[0], 0)


class TestTheComparison(unittest.TestCase):
    def test_a_perfect_signal_beats_random(self):
        """
        Entries placed only where price is about to rise. If this cannot be
        detected the test is measuring nothing.
        """
        moves = ([0.0] * 30 + [1.0] * 10) * 12
        paths = {"x": path(moves)}
        good = [Entry("x", "long", T0 + timedelta(minutes=(i * 40 + 30) * 2))
                for i in range(10)]
        result = compare_against_random(good, paths, 2.0, 4.0, 2, 0.0, trials=100)
        self.assertLess(result.p_value, 0.10)
        self.assertEqual(result.verdict, "beats random")

    def test_entries_drawn_at_random_do_not_beat_random(self):
        import random

        rng = random.Random(3)
        moves = [rng.gauss(0, 0.3) for _ in range(3000)]
        paths = {"x": path(moves)}
        arbitrary = [Entry("x", rng.choice(("long", "short")),
                           T0 + timedelta(minutes=rng.randrange(0, 2000) * 2))
                     for _ in range(60)]
        result = compare_against_random(arbitrary, paths, 1.0, 2.0, 4, 0.0, trials=100)
        self.assertGreater(result.p_value, 0.05)

    def test_random_entries_leave_room_for_the_full_hold(self):
        """
        Without this the random set is quietly truncated at the end of the
        sample and scores differently for a reason unrelated to entry quality.
        """
        import inspect

        self.assertIn("latest = every[-1] - timedelta(hours=hold_hours)",
                      inspect.getsource(compare_against_random))

    def test_it_is_reproducible(self):
        paths = {"x": path([0.1] * 500)}
        entries = [Entry("x", "long", T0 + timedelta(minutes=i * 20)) for i in range(20)]
        a = compare_against_random(entries, paths, 1.0, 2.0, 4, 0.0, trials=50, seed=1)
        b = compare_against_random(entries, paths, 1.0, 2.0, 4, 0.0, trials=50, seed=1)
        self.assertEqual(a.random_mean, b.random_mean)

    def test_no_entries_is_reported_rather_than_divided_by(self):
        result = compare_against_random([], {"x": path([0, 1])}, 1.0, 2.0, 4, 0.0)
        self.assertEqual(result.entries, 0)
        self.assertEqual(result.p_value, 1.0)
        self.assertIn("Not enough data", render(result, 1.0, 2.0, 4))


class TestBadPricesAreFilteredBeforeReplaying(unittest.TestCase):
    """
    The mistake this module was written after. One implausible print inside a
    replay is a stop-out or a target that never happened.
    """

    def test_the_runner_uses_the_research_pass_filter_rather_than_its_own(self):
        """
        There was already a filter for exactly this, in research_report. The
        first analysis reimplemented the replay without it. One definition.
        """
        from pathlib import Path

        script = (Path(__file__).resolve().parent.parent / "scripts/null_test.py").read_text()
        self.assertIn("from analysis.research_report import _plausible_price_band", script)

    def test_one_bad_print_can_invert_the_verdict(self):
        """Demonstrated, not asserted from memory."""
        clean = [0.0] * 200
        paths = {"x": path(clean)}
        entries = [Entry("x", "long", T0 + timedelta(minutes=i * 20)) for i in range(20)]
        before = replay(entries, paths, 1.0, 2.0, 4, 0.0)[2]

        times, prices = paths["x"]
        prices[50] = prices[50] * 1e-5          # the gold fault, exactly
        after = replay(entries, {"x": (times, prices)}, 1.0, 2.0, 4, 0.0)[2]
        self.assertNotAlmostEqual(before, after)


class TestReading(unittest.TestCase):
    def test_a_high_p_reads_as_no_better_than_random(self):
        self.assertEqual(NullResult(10, 1.0, 5.0, 2.0, 100, 90).verdict,
                         "no better than random")

    def test_a_low_p_reads_as_beating_random(self):
        self.assertEqual(NullResult(10, 9.0, 1.0, 2.0, 100, 2).verdict, "beats random")

    def test_the_middle_is_not_claimed_either_way(self):
        self.assertEqual(NullResult(10, 5.0, 4.0, 2.0, 100, 30).verdict, "inconclusive")

    def test_edge_is_the_difference_over_random_not_the_raw_return(self):
        """
        A strategy can lose money and still beat random, which is exactly what
        the live settings did: -4.9% against random's -22.6%.
        """
        self.assertAlmostEqual(NullResult(213, -4.9, -22.6, 8.6, 200, 4).edge, 17.7)
