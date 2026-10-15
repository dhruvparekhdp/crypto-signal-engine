"""15 Oct 2026 audit: the web briefing reported US PPI '0.6% vs 0.4% forecast' at 08:37 UTC for a 12:30 UTC release,
and future-dated items counted in the sentiment score at full weight. A future-dated item is never news."""
import unittest
from datetime import UTC, datetime, timedelta

from analysis.news_sentiment import aggregate
from collectors.market_briefing import as_news_items
from tests.test_news_sentiment import NOW, row


class TestFutureDated(unittest.TestCase):
    def test_future_headline_does_not_move_the_score(self):
        self.assertEqual(aggregate([row(-0.9, age_h=-4.0)], NOW).score, 0.0)
        self.assertEqual(aggregate([row(-0.9, age_h=-4.0)], NOW).items, 0)

    def test_small_clock_skew_still_counts(self):
        self.assertLess(aggregate([row(-0.9, age_h=-0.1)], NOW).score, 0.0)

    def test_briefing_drops_events_dated_later_than_now(self):
        later = (datetime.now(UTC) + timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M:%SZ")
        earlier = (datetime.now(UTC) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        ev = [{"headline": "US PPI (Sep) 0.6% vs 0.4% forecast", "event_type": "inflation_data", "score": -0.4,
               "confidence": 0.85, "when": later, "source": "Investing.com"},
              {"headline": "Bitcoin ETFs shed $261M", "event_type": "macro_other", "score": -0.6,
               "confidence": 0.8, "when": earlier, "source": "x"},
              {"headline": "No time given", "event_type": "macro_other", "score": 0.1,
               "confidence": 0.5, "when": "", "source": "x"}]
        items = as_news_items(ev, "groq/x")
        self.assertEqual([i["headline"] for i in items], ["Bitcoin ETFs shed $261M", "No time given"])


if __name__ == "__main__":
    unittest.main()
