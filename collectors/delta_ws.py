"""
Real-time prices from Delta India's WebSocket (owner, 18 Oct 2026: "prices should update every second or with a socket,
no delay - we are live"). Delta is the venue the real money trades on, so its own prices are the ones that matter.

One connection, two public channels for every watchlist coin:
  mark_price  (MARK:XRPUSD)  ~every 2 s - the price Delta uses for liquidation and unrealised P&L
  all_trades  (XRPUSD)       every trade as it prints
Each message sets the coin's current price and timestamp at once (CryptoStateStore.update_tick). The 15-second REST
candle job keeps the 1m history and indicators, and owns the price only when this socket has gone quiet.

Reconnects with backoff (1 s .. 30 s); re-subscribes when the watchlist changes. No key needed (public data).
"""
from __future__ import annotations

import asyncio
import json
import time

import structlog
import websockets

log = structlog.get_logger()

URL = "wss://socket.india.delta.exchange"


def delta_to_internal(symbol: str) -> str:
    """'MARK:XRPUSD' / 'XRPUSD' -> 'xrpusdt' (the engine's internal name)."""
    s = (symbol or "").upper().removeprefix("MARK:")
    return (s[:-3] + "USDT").lower() if s.endswith("USD") else s.lower()


def internal_to_delta(symbol: str) -> str:
    s = (symbol or "").upper()
    return s[:-4] + "USD" if s.endswith("USDT") else s


def parse_tick(msg: dict) -> tuple[str, float, int] | None:
    """(internal symbol, price, timestamp ms) from a mark_price / all_trades message, else None."""
    kind = msg.get("type")
    if kind not in ("mark_price", "all_trades"):
        return None
    try:
        price = float(msg.get("price"))
    except (TypeError, ValueError):
        return None
    if price <= 0:
        return None
    ts = msg.get("timestamp")
    ts_ms = int(ts) // 1000 if isinstance(ts, (int, float)) and ts > 1e14 else int(time.time() * 1000)
    return delta_to_internal(msg.get("symbol", "")), price, ts_ms


class DeltaWSCollector:
    def __init__(self, store) -> None:
        self.store = store
        self._running = False
        self.connected = False
        self.messages = 0
        self.ticks = 0
        self.reconnects = 0
        self.last_msg_ms = 0
        self.last_error = ""
        self._symbols: tuple[str, ...] = ()

    def status(self) -> dict:
        age = (time.time() * 1000 - self.last_msg_ms) / 1000 if self.last_msg_ms else None
        return {"connected": self.connected, "messages": self.messages, "ticks": self.ticks,
                "reconnects": self.reconnects, "last_message_age_s": round(age, 1) if age is not None else None,
                "symbols": len(self._symbols), "last_error": self.last_error[:160]}

    def stop(self) -> None:
        self._running = False

    async def run_forever(self) -> None:
        self._running = True
        backoff = 1.0
        log.info("delta_ws_started", url=URL)
        while self._running:
            try:
                await self._session()
                backoff = 1.0
            except asyncio.CancelledError:
                break
            except Exception as e:  # noqa: BLE001 - reconnect on anything
                self.last_error = f"{type(e).__name__}: {str(e)[:120]}"
                log.warning("delta_ws_disconnected", error=self.last_error, retry_in_s=backoff)
            self.connected = False
            if not self._running:
                break
            self.reconnects += 1
            await asyncio.sleep(backoff)
            backoff = min(30.0, backoff * 2)
        self.connected = False

    async def _session(self) -> None:
        symbols = tuple(sorted(await self.store.get_symbols()))
        if not symbols:
            await asyncio.sleep(5)
            return
        self._symbols = symbols
        delta = [internal_to_delta(s) for s in symbols]
        async with websockets.connect(URL, ping_interval=20, ping_timeout=20, open_timeout=15,
                                      close_timeout=5, max_queue=4096) as ws:
            await ws.send(json.dumps({"type": "subscribe", "payload": {"channels": [
                {"name": "mark_price", "symbols": ["MARK:" + d for d in delta]},
                {"name": "all_trades", "symbols": delta}]}}))
            self.connected = True
            self.last_error = ""
            log.info("delta_ws_subscribed", symbols=len(delta))
            next_check = time.time() + 60
            while self._running:
                raw = await asyncio.wait_for(ws.recv(), timeout=30)      # Delta sends mark prices every ~2 s
                self.messages += 1
                self.last_msg_ms = int(time.time() * 1000)
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                tick = parse_tick(msg)
                if tick is not None:
                    sym, price, ts_ms = tick
                    if await self.store.update_tick(sym, price, ts_ms):
                        self.ticks += 1
                if time.time() >= next_check:
                    next_check = time.time() + 60
                    if tuple(sorted(await self.store.get_symbols())) != self._symbols:
                        log.info("delta_ws_watchlist_changed")
                        return                                  # reconnect with the new list
