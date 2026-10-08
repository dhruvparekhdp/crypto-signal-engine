"""
Live candles + mark prices from Delta Exchange India.

Internal symbols stay Binance-shaped (btcusdt) so the rest of the engine is
unchanged; we only change the venue that fills the candle store and prices.

  BTCUSDT -> BTCUSD on api.india.delta.exchange
  GET /v2/history/candles  (1m for the board)
  GET /v2/tickers          (mark prices for swing / paper marks)

No API key required for these public market-data routes.
"""
from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime

import httpx
import structlog

from execution.delta_india import INDIA_URL, USER_AGENT, binance_to_delta_symbol

log = structlog.get_logger()

# Delta resolutions we use. Full set: 5s,1m,3m,5m,15m,30m,1h,2h,4h,6h,1d,1w
RES_1M = "1m"
TF_TO_RES = {
    "1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m",
    "1h": "1h", "2h": "2h", "4h": "4h", "6h": "6h", "1d": "1d", "1w": "1w",
}
TF_SECONDS = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600, "1d": 86400, "1w": 604800,
}


def parse_delta_candles(rows: list) -> list[dict]:
    """Delta history/candles rows -> store-shaped bars (oldest first)."""
    out: list[dict] = []
    if not isinstance(rows, list):
        return out
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            ts = float(row["time"])
            if ts > 1e12:
                ts /= 1000.0
            out.append({
                "timestamp": datetime.fromtimestamp(ts, tz=UTC),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": max(0.0, float(row.get("volume") or 0.0)),
            })
        except (KeyError, TypeError, ValueError, OSError, OverflowError):
            continue
    out.sort(key=lambda r: r["timestamp"])
    return out


class DeltaMarket:
    """Polls Delta India for 1m candles (watchlist) and mark prices."""

    FETCH_CONCURRENCY = 5

    def __init__(self, store=None, base_url: str = INDIA_URL, timeout: float = 20.0) -> None:
        self.store = store
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self.last_error: str = ""
        self.refreshed: set[str] = set()
        self.status: dict[str, str] = {}
        self.last_success: datetime | None = None
        self._price_cache: dict[str, float] = {}
        self._price_cache_at = 0.0

    def _headers(self) -> dict:
        return {"User-Agent": USER_AGENT, "Accept": "application/json"}

    async def fetch_candles(self, symbol: str, resolution: str = RES_1M,
                            limit: int = 360) -> list[dict]:
        res = TF_TO_RES.get(resolution, resolution)
        secs = TF_SECONDS.get(res, 60)
        end = int(time.time())
        start = end - secs * min(limit, 2000)
        delta_sym = binance_to_delta_symbol(symbol)
        url = f"{self.base}/v2/history/candles"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as c:
                r = await c.get(url, params={
                    "symbol": delta_sym, "resolution": res,
                    "start": start, "end": end,
                }, headers=self._headers())
            if r.status_code != 200:
                self.last_error = f"{delta_sym}: HTTP {r.status_code}"
                return []
            body = r.json()
            if not body.get("success", True):
                self.last_error = f"{delta_sym}: {body.get('error')}"
                return []
            bars = parse_delta_candles(body.get("result") or [])
            return bars[-limit:]
        except Exception as e:  # noqa: BLE001
            self.last_error = f"{delta_sym}: {type(e).__name__}: {e}"
            return []

    async def fetch_mark_prices(self) -> dict[str, float]:
        """Binance-shaped symbol -> mark price (BTCUSDT -> float). Cached 10s."""
        if time.time() - self._price_cache_at < 10 and self._price_cache:
            return self._price_cache
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as c:
                r = await c.get(f"{self.base}/v2/tickers", headers=self._headers())
            r.raise_for_status()
            rows = r.json().get("result") or []
        except Exception as e:  # noqa: BLE001
            self.last_error = f"tickers: {type(e).__name__}: {e}"
            return self._price_cache
        out: dict[str, float] = {}
        for row in rows:
            sym = (row.get("symbol") or "").upper()
            if not sym.endswith("USD") or sym.endswith("USDT"):
                continue
            # perpetual futures like BTCUSD; skip options (usually have expiry in symbol)
            if len(sym) > 12:
                continue
            try:
                px = float(row.get("mark_price") or row.get("close") or 0)
            except (TypeError, ValueError):
                continue
            if px <= 0:
                continue
            # BTCUSD -> BTCUSDT for internal lookups
            out[sym[:-3] + "USDT"] = px
        if out:
            self._price_cache = out
            self._price_cache_at = time.time()
        return self._price_cache

    async def fetch(self) -> int:
        """Refresh 1m candles for every watchlist symbol. Returns how many succeeded."""
        if self.store is None:
            return 0
        symbols = await self.store.get_symbols()
        self.refreshed = set()
        self.status = {}
        sem = asyncio.Semaphore(self.FETCH_CONCURRENCY)

        async def _one(sym: str) -> None:
            async with sem:
                try:
                    bars = await self.fetch_candles(sym, RES_1M, limit=360)
                    if not bars:
                        self.status[sym] = f"no candles ({self.last_error or 'empty'})"
                        return
                    if len(bars) < 20:
                        self.status[sym] = f"only {len(bars)} bars — too few"
                        return
                    await self.store.replace_candles(sym, bars)
                    self.refreshed.add(sym)
                    self.status[sym] = f"{len(bars)} bars via delta:{binance_to_delta_symbol(sym)}"
                except Exception as exc:  # noqa: BLE001
                    self.status[sym] = f"{type(exc).__name__}: {exc}"
                    log.debug("delta_klines_symbol_failed", symbol=sym, error=str(exc))

        await asyncio.gather(*(_one(sym) for sym in symbols))
        # Push mark prices into the store so current_price tracks Delta
        try:
            marks = await self.fetch_mark_prices()
            now = datetime.now(UTC)
            for sym in list(self.refreshed):
                px = marks.get(sym.upper())
                if px and px > 0:
                    await self.store.update_price(sym, px, now)
        except Exception as e:  # noqa: BLE001
            log.debug("delta_mark_price_push_failed", error=str(e)[:120])
        if self.refreshed:
            self.last_success = datetime.now(UTC)
        log.info("delta_klines_done", refreshed=len(self.refreshed),
                 requested=len(symbols), error=self.last_error)
        return len(self.refreshed)
