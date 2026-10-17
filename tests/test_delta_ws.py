"""18 Oct 2026 owner: prices must update in real time. Delta's WebSocket ticks set the price at once; the 15 s candle
poll no longer overwrites a fresher socket price, and 'updated' is a real time again."""
import asyncio
import time
from datetime import UTC, datetime

import pytest

from collectors.delta_ws import delta_to_internal, internal_to_delta, parse_tick


def test_symbol_mapping():
    assert delta_to_internal("MARK:XRPUSD") == "xrpusdt" and delta_to_internal("XAUTUSD") == "xautusdt"
    assert internal_to_delta("tiausdt") == "TIAUSD"


def test_parse_mark_price_and_trade():
    assert parse_tick({"type": "mark_price", "symbol": "MARK:BTCUSD", "price": "81490.5", "timestamp": 1792272199240857}) \
        == ("btcusdt", 81490.5, 1792272199240)
    assert parse_tick({"type": "all_trades", "symbol": "ADAUSD", "price": "0.2214", "timestamp": 1792272199451484})[1] == 0.2214
    assert parse_tick({"type": "l1_orderbook", "symbol": "ADAUSD"}) is None
    assert parse_tick({"type": "mark_price", "symbol": "MARK:X", "price": "0"}) is None


def _bars(close=1.0, n=60):
    now = int(time.time() * 1000) // 60_000 * 60_000
    return [{"timestamp": datetime.fromtimestamp((now - (n - 1 - i) * 60_000) / 1000, UTC), "open": close,
             "high": close, "low": close, "close": close, "volume": 1.0} for i in range(n)]


@pytest.mark.asyncio
async def test_tick_sets_price_timestamp_and_forming_candle():
    from analysis.crypto_state_store import CryptoStateStore
    store = CryptoStateStore()
    await store.add_symbol("xrpusdt")
    await store.replace_candles("xrpusdt", _bars(1.30))
    now = int(time.time() * 1000)
    assert await store.update_tick("xrpusdt", 1.31, now)
    st = await store.get("xrpusdt")
    assert st.current_price == 1.31 and st.last_tick_ms == now and st.candles_1m[-1].close == 1.31
    assert abs(st.timestamp.timestamp() * 1000 - now) < 5


@pytest.mark.asyncio
async def test_bad_print_is_refused():
    from analysis.crypto_state_store import CryptoStateStore
    store = CryptoStateStore()
    await store.add_symbol("xrpusdt")
    await store.replace_candles("xrpusdt", _bars(1.30))
    assert not await store.update_tick("xrpusdt", 2.0, int(time.time() * 1000))
    assert (await store.get("xrpusdt")).current_price == 1.30


@pytest.mark.asyncio
async def test_candle_poll_does_not_overwrite_a_fresh_socket_price(monkeypatch):
    from analysis.crypto_state_store import CryptoStateStore
    from config.settings import settings
    monkeypatch.setattr(settings, "delta_only_mode", True)
    store = CryptoStateStore()
    await store.add_symbol("xrpusdt")
    await store.replace_candles("xrpusdt", _bars(1.30))
    await store.update_tick("xrpusdt", 1.305, int(time.time() * 1000))
    await store.replace_candles("xrpusdt", _bars(1.299))           # a poll with an older close
    assert (await store.get("xrpusdt")).current_price == 1.305


@pytest.mark.asyncio
async def test_candle_poll_owns_the_price_and_timestamp_when_the_socket_is_quiet(monkeypatch):
    from analysis.crypto_state_store import CryptoStateStore
    from config.settings import settings
    monkeypatch.setattr(settings, "delta_only_mode", True)
    store = CryptoStateStore()
    await store.add_symbol("xrpusdt")
    await store.replace_candles("xrpusdt", _bars(1.30))
    await store.update_tick("xrpusdt", 1.305, int(time.time() * 1000) - 60_000)   # socket silent for a minute
    await store.replace_candles("xrpusdt", _bars(1.299))
    st = await store.get("xrpusdt")
    assert st.current_price == 1.299 and (datetime.now(UTC) - st.timestamp).total_seconds() < 5
