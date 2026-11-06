"""
Binance Futures Open Interest collector.

Fetches current open interest and recent 5-minute historical trends from
Binance Futures public endpoints (no API key required).
Calculates 1-hour Open Interest delta (oi_change_1h_pct) to detect
aggressive leverage buildup or liquidation flushes.
"""
from __future__ import annotations

import httpx
import structlog

from analysis.crypto_state_store import CryptoStateStore

log = structlog.get_logger()

BINANCE_FAPI_BASE = "https://fapi.binance.com"


class BinanceFuturesOICollector:
    """Polls open interest for perpetual contracts from Binance public FAPI."""

    def __init__(self, store: CryptoStateStore | None = None, timeout: float = 10.0) -> None:
        self.store = store
        self.timeout = timeout
        self.last_fetch_success = False

    async def fetch_symbol_oi(self, client: httpx.AsyncClient, symbol: str) -> tuple[float, float] | None:
        """
        Returns (current_oi, oi_change_1h_pct) or None if unavailable/unlisted.
        """
        sym = symbol.upper()
        # Non-crypto or spot-only tokens might not have USDT perps
        if sym in ("XAUUSDT", "PAXGUSDT"):
            return None

        try:
            # 1. Fetch 5m history over last hour (12 periods x 5m = 60m)
            hist_url = f"{BINANCE_FAPI_BASE}/futures/data/openInterestHist"
            hist_res = await client.get(hist_url, params={"symbol": sym, "period": "5m", "limit": 12})
            if hist_res.status_code != 200:
                return None

            data = hist_res.json()
            if not isinstance(data, list) or not data:
                return None

            current_oi = float(data[-1].get("sumOpenInterest", 0.0))
            old_oi = float(data[0].get("sumOpenInterest", 0.0))

            change_1h_pct = 0.0
            if old_oi > 0:
                change_1h_pct = ((current_oi - old_oi) / old_oi) * 100.0

            return round(current_oi, 4), round(change_1h_pct, 2)
        except Exception as exc:
            log.debug("binance_oi_fetch_failed", symbol=symbol, error=str(exc))
            return None

    async def fetch_all(self) -> int:
        if self.store is None:
            return 0
        from collectors.source_health import watching

        async with watching("binance_oi") as outcome:
            states = await self.store.get_all()
            outcome["attempted"] = len(states)
            if not states:
                outcome["ok"] = True
                return 0

            updated = 0
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                for state in states:
                    res = await self.fetch_symbol_oi(client, state.symbol)
                    if res is not None:
                        curr_oi, change_1h = res
                        state.open_interest = curr_oi
                        state.oi_change_1h_pct = change_1h
                        updated += 1

            self.last_fetch_success = updated > 0
            log.info("binance_futures_oi_poll_done", updated=updated, total=len(states))
            outcome["failed"] = max(0, len(states) - updated)
            outcome["ok"] = updated > 0
            if not outcome["ok"]:
                outcome["error"] = "no open interest"
            return updated
