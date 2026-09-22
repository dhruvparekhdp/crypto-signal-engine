"""
Measuring the history in code, so the model is asked the right question.

The obvious move with months of data and four API keys is to hand the rows to
a model. It does not work: 35,000 snapshots is ~1.5M tokens, and no model
computes a rank correlation over 35,000 rows in its head — asked to, it
produces a number shaped like an answer. So the arithmetic happens here, and
the model is given a two-kilobyte result.

Which means these tests carry the weight. A wrong IC here is not a wrong
number in a log; it is a wrong number a strong model will reason confidently
from, and every proposal downstream inherits it.
"""
import unittest
from datetime import datetime, timedelta

from analysis.research_report import (
    Findings,
    analyse,
    render,
    spearman,
)


class Row:
    def __init__(self, symbol="btcusdt", price=100.0, later=None, rsi=50.0,
                 minutes=0, atr=1.0, macd=0.0, signal=0.0, bb=(90.0, 110.0),
                 volume=1e6):
        self.symbol, self.price = symbol, price
        self.rsi_14, self.atr_14, self.volume_24h = rsi, atr, volume
        self.macd_line, self.macd_signal = macd, signal
        self.bollinger_lower, self.bollinger_upper = bb
        self.timestamp = datetime(2026, 9, 20) + timedelta(minutes=minutes)
        nxt = price if later is None else later
        self.price_30m_later = self.price_1h_later = nxt
        self.price_4h_later = self.price_1d_later = nxt


class TestSpearman(unittest.TestCase):
    def test_a_perfect_ranking_is_one(self):
        xs = list(range(100))
        self.assertAlmostEqual(spearman(xs, [float(x) for x in xs]), 1.0, places=6)

    def test_a_reversed_ranking_is_minus_one(self):
        xs = list(range(100))
        self.assertAlmostEqual(spearman(xs, [float(-x) for x in xs]), -1.0, places=6)

    def test_one_wild_outlier_barely_moves_it(self):
        """
        The reason it is rank correlation and not Pearson. This data has had
        prices off by eight orders of magnitude; a Pearson coefficient over
        one of those is not a measurement.
        """
        xs = [float(i) for i in range(100)]
        ys = [float(i) for i in range(100)]
        clean = spearman(xs, ys)
        ys[50] = 1e12
        self.assertGreater(spearman(xs, ys), clean - 0.05)

    def test_too_few_points_is_zero_rather_than_a_confident_number(self):
        self.assertEqual(spearman([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]), 0.0)


class TestBadPrintsAreDroppedBeforeAnythingIsComputed(unittest.TestCase):
    """
    Gold spent 55 snapshots priced at 4.3e-05 instead of 4350 — a feed
    handing back the wrong unit. Those rows pass a `price > 0` check, which
    is exactly why the collector's guard let them through, and one of them in
    a denominator turns a 0.04% move into a forward return of 15 million
    percent. That figure lands in a mean and takes the report with it.
    """

    def _book(self, n=400):
        return [Row(price=100.0 + i * 0.01, later=100.0 + i * 0.01, minutes=i)
                for i in range(n)]

    def test_a_price_off_by_orders_of_magnitude_is_rejected(self):
        rows = self._book()
        rows.append(Row(price=4.3e-05, later=100.0, minutes=999))
        found = analyse(rows)
        self.assertEqual(found.rejected, 1)

    def test_the_baseline_survives_one_bad_print(self):
        """Without the filter this mean runs to millions of percent."""
        rows = self._book()
        clean = analyse(rows).baseline["1h"][0]
        rows.append(Row(price=4.3e-05, later=100.0, minutes=999))
        self.assertAlmostEqual(analyse(rows).baseline["1h"][0], clean, places=3)

    def test_an_absurd_forward_return_is_dropped_even_if_the_base_survives(self):
        """The belt to the filter's braces: a bad label, not a bad base."""
        rows = self._book()
        rows.append(Row(price=100.0, later=100.0 * 5000, minutes=998))
        self.assertLess(abs(analyse(rows).baseline["1h"][0]), 1.0)

    def test_each_symbol_is_judged_against_its_own_prices(self):
        """XRP at 1.36 is not a bad print merely because BTC is at 77,000."""
        rows = ([Row("btcusdt", 77000.0 + i, 77000.0 + i, minutes=i) for i in range(200)]
                + [Row("xrpusdt", 1.36, 1.36, minutes=i) for i in range(200)])
        self.assertEqual(analyse(rows).rejected, 0)

    def test_the_report_says_how_many_it_dropped(self):
        rows = self._book()
        rows.append(Row(price=4.3e-05, later=100.0, minutes=999))
        self.assertIn("dropped as bad prints", render(analyse(rows)))


class TestItMeasuresWhatItClaims(unittest.TestCase):
    def test_a_planted_mean_reversion_shows_up_negative(self):
        """Low RSI followed by a rise is a negative IC. The sign is the point."""
        rows = [Row(rsi=float(i % 100), price=100.0,
                    later=100.0 * (1 + (50 - (i % 100)) / 10000), minutes=i)
                for i in range(600)]
        self.assertLess(analyse(rows).ic["rsi_14"]["1h"], -0.5)

    def test_a_planted_momentum_effect_shows_up_positive(self):
        rows = [Row(rsi=float(i % 100), price=100.0,
                    later=100.0 * (1 + ((i % 100) - 50) / 10000), minutes=i)
                for i in range(600)]
        self.assertGreater(analyse(rows).ic["rsi_14"]["1h"], 0.5)

    def test_noise_measures_as_noise(self):
        import random
        rng = random.Random(7)
        rows = [Row(rsi=rng.uniform(0, 100), price=100.0,
                    later=100.0 * (1 + rng.uniform(-0.002, 0.002)), minutes=i)
                for i in range(2000)]
        self.assertLess(abs(analyse(rows).ic["rsi_14"]["1h"]), 0.08)

    def test_an_unlabelled_row_is_not_counted(self):
        """Every row in the table held 0.0 labels until the labeller shipped."""
        rows = [Row(price=100.0, minutes=i) for i in range(200)]
        for r in rows:
            r.price_30m_later = r.price_1h_later = 0.0
            r.price_4h_later = r.price_1d_later = 0.0
        self.assertEqual(analyse(rows).rows, 0)


class TestTheReport(unittest.TestCase):
    def test_the_cost_hurdle_is_stated_before_any_figure(self):
        """
        Every number in the report is meaningless without it: a feature that
        predicts a move smaller than its own fees is not a weak edge, it is a
        losing trade with good statistics.
        """
        rows = [Row(price=100.0 + i * 0.01, later=100.0 + i * 0.01, minutes=i)
                for i in range(400)]
        text = render(analyse(rows, "btcusdt"))
        self.assertIn("Round-trip cost", text)
        self.assertLess(text.index("Round-trip cost"), text.index("INFORMATION COEFFICIENT"))

    def test_each_decile_is_marked_against_the_cost(self):
        rows = [Row(rsi=float(i % 100), price=100.0,
                    later=100.0 * (1 + (50 - (i % 100)) / 500), minutes=i)
                for i in range(2000)]
        text = render(analyse(rows, "btcusdt"))
        self.assertTrue("clears" in text or "under" in text)

    def test_an_empty_history_says_so_instead_of_rendering_a_table(self):
        self.assertIn("No labelled snapshots", render(Findings()))

    def test_it_is_small_enough_to_send_to_a_model(self):
        """
        The whole economic argument. Raw rows would be ~1.5M tokens; this is
        the result of the arithmetic, not its input.
        """
        import random
        rng = random.Random(3)
        rows = [Row(symbol=f"sym{i % 7}", rsi=rng.uniform(0, 100), price=100.0,
                    later=100.0 * (1 + rng.uniform(-0.01, 0.01)), minutes=i)
                for i in range(20000)]
        self.assertLess(len(render(analyse(rows))), 8000)


class TestTheQuestionAskedOfTheModel(unittest.TestCase):
    def test_it_is_told_the_cost_hurdle_is_the_thing_that_matters(self):
        from analysis.research_report import RESEARCH_SYSTEM

        self.assertIn("round-trip cost", RESEARCH_SYSTEM)
        self.assertIn("losing trade with good statistics", RESEARCH_SYSTEM)

    def test_it_is_forbidden_from_inventing_data_it_does_not_have(self):
        """It has statistics. It has no news, no book, no macro."""
        from analysis.research_report import RESEARCH_SYSTEM

        self.assertIn("Do not invent numbers", RESEARCH_SYSTEM)

    def test_nothing_found_is_an_allowed_answer(self):
        """Otherwise it will produce a plausible suggestion to fill the slot."""
        from analysis.research_report import RESEARCH_SYSTEM

        self.assertIn("nothing here pays for", RESEARCH_SYSTEM)

    def test_every_proposal_has_to_cite_a_figure_and_a_test(self):
        from analysis.research_report import RESEARCH_SYSTEM

        self.assertIn('"because"', RESEARCH_SYSTEM)
        self.assertIn('"test"', RESEARCH_SYSTEM)


def _source(relative: str) -> str:
    """
    Read the module as text rather than importing it.

    scheduler/runner.py imports python-telegram-bot, which cannot load in
    every environment this suite runs in. A wiring test that only runs where
    Telegram imports is a wiring test that does not run.
    """
    from pathlib import Path

    return (Path(__file__).resolve().parent.parent / relative).read_text()


class TestItIsWiredIn(unittest.TestCase):
    """A report nothing runs and nothing serves is a module, not a feature."""

    def setUp(self):
        self.runner = _source("scheduler/runner.py")
        self.health = _source("scheduler/health.py")

    def test_a_weekly_job_runs_it(self):
        self.assertIn("_research_pass_job", self.runner)
        self.assertIn('id="research_pass"', self.runner)
        self.assertIn("days=7", self.runner)

    def test_the_labeller_runs_far_more_often_than_the_research_pass(self):
        """
        Labels feed the report, so the report can only be as fresh as they
        are. Measuring more often than the data is labelled would mostly
        re-measure the same rows and invite reading noise as a change.
        """
        self.assertIn('id="snapshot_labels"', self.runner)
        self.assertIn("minutes=15", self.runner)

    def test_an_endpoint_serves_it(self):
        self.assertIn('add_get("/api/research"', self.health)

    def test_it_explains_itself_when_nothing_has_run_yet(self):
        """
        A blank panel that means three different things is the failure this
        app spent a commit removing elsewhere; it should not reappear here.
        """
        self.assertIn("No research pass has run yet", self.health)
        self.assertIn("fresh=1", self.health)

    def test_the_weekly_result_is_kept_rather_than_recomputed_per_request(self):
        """
        The measurement walks every labelled snapshot in the retention
        window. That is not work to do on a page load.
        """
        self.assertIn("last_research_text", self.runner)
        self.assertIn("last_research_text", self.health)
