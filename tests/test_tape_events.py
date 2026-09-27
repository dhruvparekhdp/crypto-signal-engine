"""
Research phases 2-3 (27 Sep): forced liquidations and large single trades
from Binance's free public streams. Observational only — nothing in the
signal engine reads these yet, only /api/pipeline shows them. These tests
cover the in-memory rolling store, the WS payload parsing, and that the
threshold actually keeps ordinary trades from ever reaching the store.
"""
from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, datetime, timedelta


def _run(coro):
    return asyncio.run(coro)


class TestRecordLiquidation(unittest.TestCase):
    def test_a_liquidation_on_a_watchlist_symbol_is_recorded(self):
        from analysis.crypto_state_store import CryptoStateStore

        async def go():
            store = CryptoStateStore()
            await store.seed(["btcusdt"])
            await store.record_liquidation("btcusdt", "sell", 50000.0, 0.1)
            state = await store.get("btcusdt")
            self.assertEqual(len(state.liquidations), 1)
            ev = state.liquidations[0]
            self.assertEqual(ev.side, "sell")
            self.assertEqual(ev.price, 50000.0)
            self.assertEqual(ev.qty, 0.1)
            self.assertAlmostEqual(ev.notional, 5000.0)
        _run(go())

    def test_case_insensitive_symbol_matches_the_watchlist(self):
        from analysis.crypto_state_store import CryptoStateStore

        async def go():
            store = CryptoStateStore()
            await store.seed(["btcusdt"])
            await store.record_liquidation("BTCUSDT", "buy", 50000.0, 0.1)
            state = await store.get("btcusdt")
            self.assertEqual(len(state.liquidations), 1)
        _run(go())

    def test_a_symbol_not_on_the_watchlist_is_silently_ignored(self):
        """A race during a watchlist edit or reconnect must never create a
        phantom state entry just because a stream message arrived for it."""
        from analysis.crypto_state_store import CryptoStateStore

        async def go():
            store = CryptoStateStore()
            await store.seed(["btcusdt"])
            await store.record_liquidation("dogeusdt", "sell", 0.1, 1000.0)
            self.assertIsNone(await store.get("dogeusdt"))
            self.assertEqual(await store.count(), 1)
        _run(go())

    def test_a_non_positive_price_or_qty_is_dropped(self):
        from analysis.crypto_state_store import CryptoStateStore

        async def go():
            store = CryptoStateStore()
            await store.seed(["btcusdt"])
            await store.record_liquidation("btcusdt", "sell", 0.0, 0.1)
            await store.record_liquidation("btcusdt", "sell", 50000.0, 0.0)
            await store.record_liquidation("btcusdt", "sell", -1.0, 0.1)
            state = await store.get("btcusdt")
            self.assertEqual(state.liquidations, [])
        _run(go())

    def test_events_older_than_the_window_are_pruned_on_the_next_write(self):
        from analysis.crypto_state_store import TAPE_WINDOW_MINUTES, CryptoStateStore

        async def go():
            store = CryptoStateStore()
            await store.seed(["btcusdt"])
            old = datetime.now(UTC) - timedelta(minutes=TAPE_WINDOW_MINUTES + 1)
            await store.record_liquidation("btcusdt", "sell", 50000.0, 0.1, timestamp=old)
            await store.record_liquidation("btcusdt", "buy", 51000.0, 0.2)
            state = await store.get("btcusdt")
            self.assertEqual(len(state.liquidations), 1)
            self.assertEqual(state.liquidations[0].side, "buy")
        _run(go())


class TestRecordLargeTrade(unittest.TestCase):
    def test_a_large_trade_is_recorded_the_same_way(self):
        from analysis.crypto_state_store import CryptoStateStore

        async def go():
            store = CryptoStateStore()
            await store.seed(["ethusdt"])
            await store.record_large_trade("ethusdt", "buy", 2700.0, 20.0)
            state = await store.get("ethusdt")
            self.assertEqual(len(state.large_trades), 1)
            self.assertAlmostEqual(state.large_trades[0].notional, 54000.0)
        _run(go())


class TestWSPayloadParsing(unittest.IsolatedAsyncioTestCase):
    """_handle_stream_payload for the two new stream types."""

    async def asyncSetUp(self):
        from analysis.crypto_state_store import CryptoStateStore
        from collectors.binance_ws import BinanceWSCollector

        self.store = CryptoStateStore()
        await self.store.seed(["btcusdt"])
        self.collector = BinanceWSCollector(self.store)

    async def test_force_order_sell_is_a_long_liquidation(self):
        payload = {"stream": "btcusdt@forceOrder", "data": {
            "e": "forceOrder", "o": {"s": "BTCUSDT", "S": "SELL", "o": "LIMIT",
                                     "q": "0.014", "p": "9910", "ap": "9910", "X": "FILLED"}}}
        await self.collector._handle_stream_payload(payload)
        state = await self.store.get("btcusdt")
        self.assertEqual(len(state.liquidations), 1)
        self.assertEqual(state.liquidations[0].side, "sell")
        self.assertEqual(state.liquidations[0].price, 9910.0)

    async def test_force_order_buy_is_a_short_liquidation(self):
        payload = {"stream": "btcusdt@forceOrder", "data": {
            "e": "forceOrder", "o": {"s": "BTCUSDT", "S": "BUY", "o": "LIMIT",
                                     "q": "0.014", "p": "9910", "ap": "9910", "X": "FILLED"}}}
        await self.collector._handle_stream_payload(payload)
        state = await self.store.get("btcusdt")
        self.assertEqual(state.liquidations[0].side, "buy")

    async def test_a_trade_under_the_threshold_never_reaches_the_store(self):
        from unittest.mock import patch

        from config.settings import settings
        payload = {"stream": "btcusdt@aggTrade", "data": {
            "e": "aggTrade", "s": "BTCUSDT", "p": "50000", "q": "0.001", "m": False}}
        with patch.object(settings, "large_trade_notional_usd", 50000.0):
            await self.collector._handle_stream_payload(payload)
        state = await self.store.get("btcusdt")
        self.assertEqual(state.large_trades, [])

    async def test_a_trade_at_or_above_the_threshold_is_recorded_with_aggressor_side(self):
        from unittest.mock import patch

        from config.settings import settings
        # m=true: buyer is the maker, so the trade was SELL-initiated.
        payload = {"stream": "btcusdt@aggTrade", "data": {
            "e": "aggTrade", "s": "BTCUSDT", "p": "50000", "q": "2", "m": True}}
        with patch.object(settings, "large_trade_notional_usd", 50000.0):
            await self.collector._handle_stream_payload(payload)
        state = await self.store.get("btcusdt")
        self.assertEqual(len(state.large_trades), 1)
        self.assertEqual(state.large_trades[0].side, "sell")
        self.assertAlmostEqual(state.large_trades[0].notional, 100000.0)

    async def test_kline_and_miniticker_still_work_unchanged(self):
        """The new branches must not shadow the existing stream handling."""
        payload = {"stream": "btcusdt@miniTicker", "data": {
            "s": "BTCUSDT", "c": "51000", "o": "50000", "h": "51500", "l": "49500", "v": "1000"}}
        await self.collector._handle_stream_payload(payload)
        state = await self.store.get("btcusdt")
        self.assertEqual(state.volume_24h, 1000.0)


class TestStreamSubscriptionListsBothNewStreams(unittest.TestCase):
    def test_the_source_builds_forceorder_and_aggtrade_per_symbol(self):
        """Cheap, robust guard: reading the source rather than driving a
        real connection — the actual subscribe behaviour is exercised by
        the payload-parsing tests above."""
        import inspect

        import collectors.binance_ws as bws
        src = inspect.getsource(bws.BinanceWSCollector._connect_and_stream)
        self.assertIn("@forceOrder", src)
        self.assertIn("@aggTrade", src)
        self.assertIn("binance_liquidation_stream_enabled", src)
        self.assertIn("binance_large_trade_stream_enabled", src)


class TestTapeSummary(unittest.TestCase):
    def test_no_events_is_a_clean_zero_not_a_crash(self):
        from scheduler.settings_page import _tape_summary
        self.assertEqual(_tape_summary([], datetime.now(UTC)),
                         {"count": 0, "buy_notional": 0.0, "sell_notional": 0.0, "last": None})

    def test_events_are_summed_by_side_and_the_last_one_is_named(self):
        from analysis.crypto_state import TapeEvent
        from scheduler.settings_page import _tape_summary
        now = datetime.now(UTC)
        events = [
            TapeEvent(side="sell", price=50000.0, qty=0.1, notional=5000.0,
                     timestamp=now - timedelta(minutes=2)),
            TapeEvent(side="buy", price=51000.0, qty=0.2, notional=10200.0,
                     timestamp=now - timedelta(seconds=30)),
        ]
        got = _tape_summary(events, now)
        self.assertEqual(got["count"], 2)
        self.assertAlmostEqual(got["sell_notional"], 5000.0)
        self.assertAlmostEqual(got["buy_notional"], 10200.0)
        self.assertEqual(got["last"]["side"], "buy")
        self.assertEqual(got["last"]["ago_s"], 30)

    def test_pipeline_api_carries_both_tape_fields_per_symbol(self):
        import asyncio
        import json
        import os
        import tempfile
        from types import SimpleNamespace

        from aiohttp.test_utils import make_mocked_request

        os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tempfile.mktemp(suffix='.db')}"

        async def go():
            from analysis.crypto_state import CryptoState, TapeEvent
            from storage.database import init_db

            await init_db()
            state = CryptoState(symbol="btcusdt", base_asset="BTC", current_price=50000.0)
            state.liquidations = [TapeEvent(side="sell", price=50000.0, qty=0.1,
                                            notional=5000.0, timestamp=datetime.now(UTC))]

            async def get_all():
                return [state]
            runner = SimpleNamespace(crypto_store=SimpleNamespace(get_all=get_all))
            from scheduler.settings_page import pipeline_api
            resp = await pipeline_api(runner)(make_mocked_request("GET", "/api/pipeline"))
            body = json.loads(resp.text)
            feed = body["feeds"][0]
            self.assertEqual(feed["liquidations_15m"]["count"], 1)
            self.assertEqual(feed["large_trades_15m"]["count"], 0)
        asyncio.run(go())


if __name__ == "__main__":
    unittest.main()
