"""Delta India market-data collector: parse + symbol map + bars."""
from __future__ import annotations

import unittest
from datetime import UTC, datetime

from analysis.swing_book import _aggregate_delta_rows, bars_from_delta
from collectors.delta_market import parse_delta_candles
from execution.delta_india import binance_to_delta_symbol


class ParseTests(unittest.TestCase):
    def test_parse_candles_sorted(self):
        rows = [
            {"time": 1_000_002, "open": 2, "high": 3, "low": 1, "close": 2.5, "volume": 10},
            {"time": 1_000_000, "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 5},
        ]
        bars = parse_delta_candles(rows)
        self.assertEqual(len(bars), 2)
        self.assertLess(bars[0]["timestamp"], bars[1]["timestamp"])
        self.assertEqual(bars[0]["close"], 1.5)

    def test_symbol_map(self):
        self.assertEqual(binance_to_delta_symbol("btcusdt"), "BTCUSD")


class SwingBarsTests(unittest.TestCase):
    def test_bars_from_delta_drops_forming(self):
        # 200 closed 4h bars + 1 forming
        now_ms = 1_000_000_000_000
        iv = 4 * 3600 * 1000
        rows = []
        for i in range(200):
            t = (now_ms - (200 - i) * iv) // 1000
            rows.append({"time": t, "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 1})
        # forming bar (open in the future relative to close time)
        rows.append({"time": now_ms // 1000, "open": 9, "high": 9, "low": 9, "close": 9, "volume": 1})
        b = bars_from_delta("BTCUSDT", rows, now_ms, "4h")
        self.assertIsNotNone(b)
        self.assertGreaterEqual(len(b.c), 150)
        self.assertNotIn(9.0, list(b.c[-1:]))  # forming close discarded

    def test_aggregate_4h_to_8h(self):
        # two complete 4h bars -> one 8h; incomplete trailing bucket dropped
        rows = [
            {"time": 0, "open": 1, "high": 3, "low": 0.5, "close": 2, "volume": 10},
            {"time": 4 * 3600, "open": 2, "high": 4, "low": 1.5, "close": 3.5, "volume": 20},
            {"time": 8 * 3600, "open": 3.5, "high": 5, "low": 3, "close": 4, "volume": 5},  # incomplete
        ]
        out = _aggregate_delta_rows(rows, src_secs=4 * 3600, dst_secs=8 * 3600)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["time"], 0)
        self.assertEqual(out[0]["open"], 1)
        self.assertEqual(out[0]["high"], 4)
        self.assertEqual(out[0]["low"], 0.5)
        self.assertEqual(out[0]["close"], 3.5)
        self.assertEqual(out[0]["volume"], 30)


if __name__ == "__main__":
    unittest.main()
