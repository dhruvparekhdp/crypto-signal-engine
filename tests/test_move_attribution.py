"""
Hourly "why did it move": the numbers are ours, the explanation is the
model's, and both are stored side by side.
"""
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace

from analysis.move_attribution import (
    build_prompt,
    parse_attribution,
    signals_digest,
    summarise_moves,
)

NOW = datetime(2026, 10, 1, 12, 0)


def series(start: float, end: float, hours: int = 12, step_min: int = 2):
    n = hours * 60 // step_min
    return [(NOW - timedelta(minutes=step_min * (n - i)), start + (end - start) * i / n)
            for i in range(n + 1)]


class TestMoves(unittest.TestCase):
    def test_changes_are_measured_from_real_prices(self):
        m = summarise_moves({"btcusdt": series(100.0, 112.0)}, NOW)[0]
        self.assertAlmostEqual(m.change_12h, 12.0, places=1)
        self.assertAlmostEqual(m.change_1h, (112 - 111) / 111 * 100, places=1)
        self.assertAlmostEqual(m.range_12h, 12.0, places=1)

    def test_a_gap_in_the_data_is_unknown_not_zero(self):
        pts = [(NOW - timedelta(minutes=5), 100.0), (NOW, 101.0)]
        m = summarise_moves({"ethusdt": pts}, NOW)[0]
        self.assertIsNone(m.change_4h)
        self.assertIsNone(m.change_12h)

    def test_bad_prices_are_ignored(self):
        pts = series(100.0, 100.0) + [(NOW - timedelta(minutes=1), 0.0),
                                      (NOW, float("nan"))]
        m = summarise_moves({"paxgusdt": pts}, NOW)[0]
        self.assertAlmostEqual(m.change_12h, 0.0, places=3)


class TestParse(unittest.TestCase):
    def test_coins_off_the_watchlist_and_made_up_causes_are_cleaned(self):
        got = parse_attribution({
            "overall": "Fed hold lifted risk.",
            "drivers": [{"event": "Fed holds", "coins": ["btcusdt", "dogeusdt"],
                         "direction": "up", "confidence": 0.8}],
            "coins": [{"symbol": "BTCUSDT", "cause_type": "news", "cause": "Fed",
                       "confidence": "0.9", "reasoning": "r"},
                      {"symbol": "dogeusdt", "cause_type": "news"},
                      {"symbol": "ethusdt", "cause_type": "vibes", "confidence": "nan"}],
        }, {"btcusdt", "ethusdt"})
        self.assertEqual([c["symbol"] for c in got["coins"]], ["btcusdt", "ethusdt"])
        self.assertEqual(got["coins"][1]["cause_type"], "no_clear_cause")
        self.assertEqual(got["coins"][1]["confidence"], 0.5)
        self.assertEqual(got["drivers"][0]["coins"], ["btcusdt"])


class TestPrompt(unittest.TestCase):
    def test_it_carries_moves_signals_and_news(self):
        moves = summarise_moves({"btcusdt": series(100.0, 105.0)}, NOW)
        sig = SimpleNamespace(symbol="btcusdt", timestamp=NOW - timedelta(hours=2),
                              signal_type="confluence", direction="short", confidence=0.72,
                              outcome="lost", pnl_pct=-0.41, suppressed_by="")
        text = build_prompt(moves, signals_digest([sig]), "Fed held rates.", 12)
        self.assertIn("BTCUSDT", text)
        self.assertIn("short confluence", text)
        self.assertIn("Fed held rates.", text)


class TestPage(unittest.TestCase):
    def test_the_page_is_linked_and_themed(self):
        import scheduler.health as h
        self.assertIn('href="/moves"', h._HTML)
        self.assertIn("fmtStamp", h._MOVES_HTML)
        self.assertIn("<title>Market Moves</title>", h._MOVES_HTML)


if __name__ == "__main__":
    unittest.main()
