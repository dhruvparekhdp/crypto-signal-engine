"""
BinanceKlines.fetch() used to fetch the watchlist one symbol at a time:
klines, then depth, then the next symbol. Sequentially, N symbols paid N
round trips back to back — the ~7s the job was taking with a handful of
watchlist coins. Symbols are independent, so nothing here needs the old
order preserved; a bounded fan-out (FETCH_CONCURRENCY) should make the
whole sweep take roughly as long as the slowest single symbol, not the sum
of all of them.
"""
from __future__ import annotations

import asyncio
import time
import types
import unittest
from unittest.mock import AsyncMock

from collectors.binance_klines import BinanceKlines

SYMBOLS = [f"c{i}usdt" for i in range(6)]
PER_CALL_DELAY = 0.08


def _fake_store():
    state = types.SimpleNamespace(order_book=None)
    return types.SimpleNamespace(
        get_symbols=AsyncMock(return_value=list(SYMBOLS)),
        replace_candles=AsyncMock(return_value=None),
        get=AsyncMock(return_value=state),
    )


class TestFetchRunsSymbolsConcurrently(unittest.IsolatedAsyncioTestCase):
    async def test_the_whole_sweep_is_far_faster_than_one_symbol_at_a_time(self):
        klines = BinanceKlines(store=_fake_store())

        async def fake_candles(symbol, interval="1m", limit=360):
            await asyncio.sleep(PER_CALL_DELAY)
            return [{"close": 1.0}] * 25

        async def fake_depth(symbol, limit=50):
            await asyncio.sleep(PER_CALL_DELAY)
            return None

        klines.fetch_candles = fake_candles
        klines.fetch_depth = fake_depth

        start = time.monotonic()
        refreshed = await klines.fetch()
        elapsed = time.monotonic() - start

        self.assertEqual(refreshed, len(SYMBOLS))
        # One symbol at a time: len(SYMBOLS) * 2 calls * PER_CALL_DELAY each.
        serial_estimate = len(SYMBOLS) * 2 * PER_CALL_DELAY
        self.assertLess(elapsed, serial_estimate * 0.7,
                        "fetch() looks sequential, not concurrent")

    async def test_a_bad_symbol_does_not_stop_the_rest(self):
        klines = BinanceKlines(store=_fake_store())

        async def fake_candles(symbol, interval="1m", limit=360):
            if symbol == "c0usdt":
                raise RuntimeError("boom")
            return [{"close": 1.0}] * 25

        async def fake_depth(symbol, limit=50):
            return None

        klines.fetch_candles = fake_candles
        klines.fetch_depth = fake_depth

        refreshed = await klines.fetch()
        self.assertEqual(refreshed, len(SYMBOLS) - 1)
        self.assertIn("RuntimeError", klines.status["c0usdt"])

    async def test_concurrency_is_capped_not_unbounded(self):
        """
        Stays polite to Binance rather than firing every symbol at once:
        never more than FETCH_CONCURRENCY requests in flight together.
        """
        klines = BinanceKlines(store=_fake_store())
        in_flight = 0
        peak = 0

        async def fake_candles(symbol, interval="1m", limit=360):
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.02)
            in_flight -= 1
            return [{"close": 1.0}] * 25

        async def fake_depth(symbol, limit=50):
            return None

        klines.fetch_candles = fake_candles
        klines.fetch_depth = fake_depth
        await klines.fetch()
        self.assertLessEqual(peak, BinanceKlines.FETCH_CONCURRENCY)
        self.assertGreater(peak, 1, "not accidentally still sequential")


if __name__ == "__main__":
    unittest.main()
