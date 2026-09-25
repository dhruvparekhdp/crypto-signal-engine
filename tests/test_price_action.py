"""The candle reading handed to the reviewers."""

import unittest
from datetime import datetime, timedelta

from analysis.crypto_state import OHLCVCandle
from analysis.price_action import candle_word, describe, levels, structure, swings


def bars(points):
    """Zig-zag candles through the given closes, with a small wick either side."""
    t0 = datetime(2026, 9, 25)
    out, prev = [], points[0]
    for i, p in enumerate(points):
        hi, lo = max(prev, p) * 0.5 + p * 0.502, min(prev, p) * 0.5 + p * 0.498
        out.append(OHLCVCandle(open=prev, high=hi, low=lo, close=p, volume=1.0,
                               timestamp=t0 + timedelta(minutes=15 * i)))
        prev = p
    return out


UP = [100, 102, 104, 102, 101, 103, 106, 104, 103, 105, 108, 106, 105, 107, 110, 108, 107]
DOWN = [2 * 110 - p for p in UP]


class PriceActionTest(unittest.TestCase):
    def test_structure_follows_swings(self):
        self.assertEqual(structure(bars(UP)), "up")
        self.assertEqual(structure(bars(DOWN)), "down")
        self.assertEqual(structure(bars([100, 101])), "unclear")

    def test_swings_never_use_the_last_bars(self):
        sw = swings(bars(UP), width=2)
        self.assertTrue(all(s.index <= len(UP) - 3 for s in sw))

    def test_levels_bracket_price(self):
        sup, res = levels(bars(UP), 107.5)
        self.assertIsNotNone(sup)
        self.assertLess(sup, 107.5)
        if res is not None:
            self.assertGreater(res, 107.5)

    def test_candle_words(self):
        t = datetime(2026, 9, 25)
        self.assertEqual(candle_word(OHLCVCandle(100, 101, 95, 100.5, 1, t)), "hammer")
        self.assertEqual(candle_word(OHLCVCandle(100, 105, 99.5, 100.3, 1, t)), "shooting-star")
        self.assertEqual(candle_word(OHLCVCandle(100, 104.1, 99.9, 104, 1, t)), "big-green")

    def test_describe_flags_a_trade_against_structure(self):
        text = describe(bars(UP), bars(UP), 107.0, is_long=False)
        self.assertIn("15m structure: up", text)
        self.assertIn("against the 15m structure", text)
        self.assertNotIn("against", describe(bars(UP), bars(UP), 107.0, is_long=True))

    def test_describe_is_empty_without_history(self):
        self.assertEqual(describe([], [], 100.0), "")


if __name__ == "__main__":
    unittest.main()
