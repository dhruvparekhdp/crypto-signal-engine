"""
Who owns state.current_price: the live WebSocket, or the 15s REST poll.

The owner's phone showed BCHUSDT diverging from Binance's own order book —
traced to replace_candles() unconditionally overwriting current_price with
its own REST close on every poll, discarding whatever fresher price the
WebSocket had just set. That capped "live" at the poll interval and is why
turning the socket on in Settings didn't visibly change anything.
"""
import asyncio
import unittest
from datetime import UTC, datetime
from unittest.mock import patch

from analysis.crypto_state_store import CryptoStateStore


def _bars(base: float, n: int = 25) -> list[dict]:
    now = datetime.now(UTC)
    return [{"open": base + i, "high": base + i + 5, "low": base + i - 5,
             "close": base + i, "volume": 10.0, "timestamp": now}
            for i in range(n)]


class TestPriceOwnership(unittest.TestCase):
    def test_rest_seeds_price_on_cold_start_regardless_of_mode(self):
        async def run():
            store = CryptoStateStore()
            with patch("analysis.crypto_state_store.settings") as s:
                s.binance_only_mode = True
                s.binance_ws_enabled = True
                s.delta_only_mode = False
                await store.replace_candles("btcusdt", _bars(50000.0))
            state = await store.get("btcusdt")
            self.assertEqual(state.current_price, 50000.0 + 24)
        asyncio.run(run())

    def test_socket_keeps_owning_price_once_it_has_one(self):
        """
        binance_only_mode + the live stream on (the owner's actual settings):
        once the socket has set a live price, the REST poll must not stomp
        it with its own (older, or simply different) close.
        """
        async def run():
            store = CryptoStateStore()
            with patch("analysis.crypto_state_store.settings") as s:
                s.binance_only_mode = True
                s.binance_ws_enabled = True
                s.delta_only_mode = False
                now = datetime.now(UTC)
                await store.update_kline(symbol="bchusdt", open_=336.70, high=336.80,
                                         low=336.60, close=336.74, volume=1.0,
                                         timestamp=now, is_closed=False)
                await store.replace_candles("bchusdt", _bars(336.90))
            state = await store.get("bchusdt")
            self.assertEqual(state.current_price, 336.74,
                             "the socket's price must survive the REST poll")
        asyncio.run(run())

    def test_rest_still_owns_price_when_the_socket_is_off(self):
        """binance_only_mode with the socket OFF: REST is the only source,
        same as before this fix — it must keep updating the price."""
        async def run():
            store = CryptoStateStore()
            with patch("analysis.crypto_state_store.settings") as s:
                s.binance_only_mode = True
                s.binance_ws_enabled = False
                s.delta_only_mode = False
                await store.replace_candles("ethusdt", _bars(2600.0))
                await store.replace_candles("ethusdt", _bars(2650.0))
            state = await store.get("ethusdt")
            self.assertEqual(state.current_price, 2650.0 + 24)
        asyncio.run(run())

    def test_outside_binance_only_mode_rest_never_overrides_the_ticker(self):
        """CoinDCX/CoinGecko mode: unchanged from before — REST candles seed
        a cold start only, never override a ticker price that already exists."""
        async def run():
            store = CryptoStateStore()
            with patch("analysis.crypto_state_store.settings") as s:
                s.binance_only_mode = False
                s.binance_ws_enabled = False
                s.delta_only_mode = False
                await store.update_from_rest(
                    symbol="solusdt", price=121.16, high_24h=125.0, low_24h=118.0,
                    volume_24h=1000.0, change_24h_pct=1.0,
                    timestamp=datetime.now(UTC))
                await store.replace_candles("solusdt", _bars(999.0))
            state = await store.get("solusdt")
            self.assertEqual(state.current_price, 121.16)
        asyncio.run(run())
