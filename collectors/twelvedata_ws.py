from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import structlog
import websockets

from analysis.crypto_state_store import CommodityStateStore
from config.settings import settings

log = structlog.get_logger()


class TwelveDataWSCollector:
    """
    Real-time Twelve Data WebSocket collector for Commodities
    (Gold XAU/USD, Silver XAG/USD, Crude Oil WTI/USD).
    """

    BASE_WS_URL = "wss://ws.twelvedata.com/v1/quotes/price"
    RECONNECT_FLOOR_SECONDS = 5.0

    def __init__(self, store: CommodityStateStore) -> None:
        self.store = store
        self._running = False
        self._consecutive_failures = 0
        # Symbols the account's plan will not serve. Re-subscribing to these
        # on every reconnect is what made one rejected symbol a permanent
        # source of churn rather than a one-off warning.
        self._rejected: set[str] = set()

    async def run_forever(self) -> None:
        if not settings.twelvedata_api_key:
            log.info("twelvedata_ws_skipped_no_api_key")
            return

        self._running = True
        log.info("twelvedata_ws_collector_started")

        while self._running:
            try:
                await self._connect_and_stream()
                # A clean server-side close ends the stream without raising,
                # so this path gets no backoff from the handler below. Without
                # a pause of its own it reconnects instantly — a tighter loop
                # than any error path.
                if self._running:
                    await asyncio.sleep(self.RECONNECT_FLOOR_SECONDS)
            except asyncio.CancelledError:
                log.info("twelvedata_ws_cancelled")
                break
            except Exception as exc:
                self._consecutive_failures += 1
                wait_secs = min(2 ** self._consecutive_failures, 60)
                log.warning(
                    "twelvedata_ws_disconnected",
                    error=str(exc),
                    consecutive_failures=self._consecutive_failures,
                    retry_in_seconds=wait_secs,
                )
                await asyncio.sleep(wait_secs)

    async def _connect_and_stream(self) -> None:
        url = f"{self.BASE_WS_URL}?apikey={settings.twelvedata_api_key}"
        symbols = [s.strip() for s in settings.twelvedata_symbols.split(",")
                   if s.strip() and s.strip() not in self._rejected]

        # Every symbol refused: reconnecting cannot change that, and looping
        # on it buys ~17k handshakes a day against an API that has already
        # said no. Stop, loudly.
        if not symbols:
            self._running = False
            log.error("twelvedata_ws_stopped",
                      reason="every configured symbol was rejected by the API",
                      rejected=sorted(self._rejected),
                      hint="check TWELVEDATA_SYMBOLS against your plan, or unset "
                           "TWELVEDATA_API_KEY to disable this collector")
            return

        async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
            log.info("twelvedata_ws_connected", symbols=symbols)

            # Subscribe to symbols
            subscribe_payload = {
                "action": "subscribe",
                "params": {"symbols": ",".join(symbols)},
            }
            await ws.send(json.dumps(subscribe_payload))

            async for message in ws:
                if not self._running:
                    break

                try:
                    data = json.loads(message)
                    event = data.get("event")
                    if event == "price":
                        # Real data is the only proof the connection works, so
                        # this is where the backoff resets. Resetting it on
                        # connect pinned the retry at its 2s floor forever:
                        # connecting always succeeded, the stream died after.
                        self._consecutive_failures = 0
                        sym = data.get("symbol", "")
                        price = float(data.get("price", 0.0))
                        ts = data.get("timestamp", 0)
                        timestamp = (
                            datetime.fromtimestamp(ts, tz=timezone.utc)
                            if ts > 0
                            else datetime.now(timezone.utc)
                        )
                        await self.store.update_price(sym, price, timestamp)
                    elif event == "subscribe-status" and data.get("status") != "ok":
                        refused = {str(f.get("symbol", "")).strip()
                                   for f in (data.get("fails") or [])}
                        refused = {s for s in refused if s} - self._rejected
                        if refused:
                            self._rejected |= refused
                            log.warning(
                                "twelvedata_symbols_rejected",
                                symbols=sorted(refused),
                                hint="not served on this plan; dropped from "
                                     "future subscriptions")
                except Exception as exc:
                    log.error("twelvedata_message_error", error=str(exc))

    def stop(self) -> None:
        self._running = False
