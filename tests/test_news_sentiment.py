"""
Stored headlines become the sentiment score, and scheduled releases pause
new trades. Both were missing: sentiment_score was 0.0 on every snapshot,
and the engine traded straight through the 16 Sep FOMC decision.
"""
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from analysis.event_calendar import (
    EVENTS_2026,
    Event,
    active_blackout,
    news_events,
    upcoming,
)
from analysis.news_sentiment import aggregate, combine, per_symbol

NOW = datetime(2026, 10, 1, 12, 0)


def row(score, conf=0.9, age_h=0.0, symbol="all", source="coindesk", event="macro_other"):
    return SimpleNamespace(score=score, confidence=conf, source=source, event_type=event,
                           symbol=symbol, headline="h", published_at=NOW - timedelta(hours=age_h))


class TestAggregate(unittest.TestCase):
    def test_no_news_is_zero(self):
        self.assertEqual(aggregate([], NOW).score, 0.0)

    def test_one_weak_story_barely_moves_it(self):
        """The prior keeps a single low-confidence headline from firing the 0.35 detector."""
        self.assertLess(abs(aggregate([row(-1.0, conf=0.3)], NOW).score), 0.25)

    def test_several_confident_stories_in_agreement_move_it_a_lot(self):
        r = aggregate([row(-0.8) for _ in range(5)], NOW)
        self.assertLess(r.score, -0.6)
        self.assertEqual(r.items, 5)

    def test_old_news_fades(self):
        fresh = aggregate([row(0.8)], NOW).score
        stale = aggregate([row(0.8, age_h=9)], NOW).score
        self.assertGreater(fresh, 2 * stale)

    def test_news_older_than_the_window_is_gone(self):
        self.assertEqual(aggregate([row(0.9, age_h=13)], NOW).score, 0.0)

    def test_noise_counts_for_nothing(self):
        self.assertEqual(aggregate([row(0.9, event="noise")], NOW).score, 0.0)

    def test_a_nan_score_is_ignored_not_clamped(self):
        self.assertEqual(aggregate([row(float("nan"))], NOW).score, 0.0)

    def test_the_fed_outweighs_commentary(self):
        fed = aggregate([row(-0.7, source="federal_reserve")], NOW).score
        press = aggregate([row(-0.7)], NOW).score
        self.assertLess(fed, press)


class TestPerSymbol(unittest.TestCase):
    def test_a_coin_gets_its_own_news_plus_half_the_macro(self):
        rows = [row(0.6, symbol="btcusdt"), row(-0.6, symbol="all")]
        got = per_symbol(rows, ["btcusdt", "ethusdt"], NOW)
        coin = aggregate([rows[0]], NOW)
        macro = aggregate([rows[1]], NOW)
        self.assertAlmostEqual(got["btcusdt"][0], combine(coin, macro))
        self.assertAlmostEqual(got["ethusdt"][0], 0.5 * macro.score)

    def test_every_followed_symbol_gets_a_value_even_with_no_news(self):
        self.assertEqual(per_symbol([], ["paxgusdt"], NOW)["paxgusdt"], (0.0, 0))


class TestBlackout(unittest.TestCase):
    FOMC = Event(datetime(2026, 10, 28, 18, 0), "fomc", "FOMC")

    def test_fomc_pauses_an_hour_before_and_ninety_minutes_after(self):
        ev = (self.FOMC,)
        self.assertIsNone(active_blackout(datetime(2026, 10, 28, 16, 59), events=ev))
        self.assertIsNotNone(active_blackout(datetime(2026, 10, 28, 17, 0), events=ev))
        self.assertIsNotNone(active_blackout(datetime(2026, 10, 28, 19, 30), events=ev))
        self.assertIsNone(active_blackout(datetime(2026, 10, 28, 19, 31), events=ev))

    def test_the_calendar_has_the_remaining_fomc_meetings(self):
        fomc = [e.at for e in EVENTS_2026 if e.kind == "fomc"]
        self.assertEqual(fomc, [datetime(2026, 10, 28, 18, 0), datetime(2026, 12, 9, 19, 0)])

    def test_releases_move_an_hour_later_in_utc_after_us_clocks_change(self):
        cpi = [e.at.hour for e in EVENTS_2026 if e.kind == "cpi"]
        self.assertEqual(cpi, [12, 13, 13])

    def test_confident_high_impact_news_opens_a_window(self):
        rows = [row(-0.8, conf=0.9, age_h=0.25, event="war"),
                row(-0.8, conf=0.4, age_h=0.25, event="tariff"),      # not confident
                row(-0.8, conf=0.9, age_h=2.0, event="war"),          # too old
                row(0.5, conf=0.9, age_h=0.1, event="adoption")]      # not high impact
        found = news_events(rows, NOW, {"war", "tariff", "rate_decision"})
        self.assertEqual(len(found), 1)
        self.assertIsNotNone(active_blackout(NOW, extra=found, events=()))

    def test_upcoming_lists_only_the_next_fortnight(self):
        got = upcoming(datetime(2026, 10, 1))
        self.assertTrue(got)
        self.assertTrue(all(e.at <= datetime(2026, 10, 15) for e in got))


class TestWiring(unittest.TestCase):
    """The runner imports the Telegram stack, so its wiring is checked in the source."""

    SRC = Path(__file__).resolve().parent.parent.joinpath("scheduler", "runner.py").read_text()

    def test_the_job_is_scheduled(self):
        self.assertIn("self._news_sentiment_job,", self.SRC)
        self.assertIn('id="news_sentiment"', self.SRC)

    def test_the_blackout_is_checked_before_any_trade_opens(self):
        tick = self.SRC[self.SRC.index("blackout = self._blackout(now)"):]
        self.assertLess(tick.index("pending = []"), tick.index("should_open("))


class TestIngestHygiene(unittest.TestCase):
    def test_zero_confidence_stays_zero_and_nan_is_not_bullish(self):
        from storage.repository import _bounded
        self.assertEqual(_bounded(0.0, 0.0, 1.0, 0.5), 0.0)
        self.assertEqual(_bounded(None, 0.0, 1.0, 0.5), 0.5)
        self.assertEqual(_bounded(float("nan"), -1.0, 1.0, 0.0), 0.0)
        self.assertEqual(_bounded("inf", -1.0, 1.0, 0.0), 0.0)
        self.assertEqual(_bounded(3, -1.0, 1.0, 0.0), 1.0)


if __name__ == "__main__":
    unittest.main()
