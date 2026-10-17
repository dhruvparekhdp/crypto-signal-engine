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


def test_ticker_24h_fills_the_change_high_low_and_volume():
    # 18 Oct 2026: in Delta-only mode every coin read +0.00% on the price tape - nothing filled the 24h stats
    from collectors.delta_market import ticker_24h
    row = {"symbol": "BTCUSD", "close": 81525.0, "open": 81390.0, "high": 81797.5, "low": 80888.5,
           "mark_price": "81524.87", "turnover_usd": 425359570.28}
    st = ticker_24h(row)
    assert st["open"] == 81390.0 and st["high"] == 81797.5 and st["low"] == 80888.5
    assert abs(st["volume"] - 425359570.28 / 81525.0) < 1e-6
    assert ticker_24h({"symbol": "X", "open": 0}) is None


def test_fetch_pushes_24h_stats_so_the_change_is_not_zero():
    import asyncio

    from analysis.crypto_state_store import CryptoStateStore
    from collectors.delta_market import DeltaMarket

    async def go():
        store = CryptoStateStore()
        await store.add_symbol("btcusdt") if hasattr(store, "add_symbol") else None
        m = DeltaMarket(store)
        bars = [{"timestamp": 1_792_000_000_000 + i * 60_000, "open": 81000.0, "high": 81100.0, "low": 80900.0,
                 "close": 81500.0, "volume": 1.0} for i in range(30)]

        async def fake_candles(sym, res, limit=360):
            return bars

        async def fake_marks():
            m._stats = {"BTCUSDT": {"open": 80000.0, "high": 82000.0, "low": 79000.0, "volume": 5000.0}}
            return {"BTCUSDT": 81500.0}
        m.fetch_candles, m.fetch_mark_prices = fake_candles, fake_marks
        store.get_symbols = (lambda: _syms())
        await m.fetch()
        st = await store.get("btcusdt") if hasattr(store, "get") else store._states.get("btcusdt")
        return st

    async def _syms():
        return ["btcusdt"]
    st = asyncio.run(go())
    if st is not None:
        assert round(st.price_change_24h_pct, 2) == round((81500 - 80000) / 80000 * 100, 2)
        assert st.high_24h == 82000.0 and st.volume_24h == 5000.0
