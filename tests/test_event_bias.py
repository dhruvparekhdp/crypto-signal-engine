"""Trade through news with the crowd's bias instead of pausing (owner, 26 Sep)."""

import unittest

from analysis.event_bias import allows, from_sentiment, parse


class TestEventBias(unittest.TestCase):
    def test_parse(self):
        got = parse({"bias": "BEAR", "confidence": "0.8", "reason": "hawkish preview",
                     "sources": ["cme"]})
        self.assertEqual((got["bias"], got["confidence"], got["source"]), ("bear", 0.8, "groq"))
        self.assertIsNone(parse({"bias": "up"}))
        self.assertEqual(parse({"bias": "bull", "confidence": "nan"})["confidence"], 0.5)

    def test_only_trades_against_a_confident_bias_are_skipped(self):
        bear = {"bias": "bear", "confidence": 0.8}
        self.assertTrue(allows("short", bear))
        self.assertFalse(allows("long", bear))
        self.assertTrue(allows("long", {"bias": "bear", "confidence": 0.4}))   # weak
        self.assertTrue(allows("long", {"bias": "neutral", "confidence": 0.9}))
        self.assertTrue(allows("long", None))

    def test_sentiment_fallback(self):
        self.assertEqual(from_sentiment(0.35)["bias"], "bull")
        self.assertEqual(from_sentiment(-0.5)["bias"], "bear")
        self.assertEqual(from_sentiment(0.05)["bias"], "neutral")
        self.assertTrue(allows("short", from_sentiment(0.3)))    # 0.3 < 0.55: advice only


if __name__ == "__main__":
    unittest.main()
