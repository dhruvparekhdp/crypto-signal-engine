"""
Live 1-minute candles and depth from Binance REST. The default market feed.

Why this replaced the poll-aggregated bars
------------------------------------------
The REST poller built minute bars by sampling a ticker twice a minute. Two
samples cannot see the minute's true high and low, so the bars came out almost
flat and everything derived from range came out too small with them. Measured
on a reproduction of the live path: ATR 0.052% of price against a real
one-minute ATR nearer 0.15%, and a median bar range of 0.023%.

That is not a cosmetic difference, it decides every signal. The target is the
larger of what volatility offers and what cost demands, and with the ATR term
collapsed the cost floor won every single time — which is why every card on
the live board read the same 0.505% move and 3.0x cost regardless of coin,
regardless of hour. A number that never changes is not a prediction.

Real klines fix the input rather than the arithmetic: true high and low, and
per-bar volume, which the ticker never carried at all.

Hosts, in order
---------------
data-api.binance.vision is the public market-data mirror and is generally not
geo-blocked; the main api host returns HTTP 451 from US cloud IPs, which is
what took the WebSocket collector out on Render. Both are tried, plus the
numbered mirrors, and the first that answers wins. If none do, CoinDCX's
candle route is the last resort — worse data, but real bars.
"""
from __future__ import annotations

from datetime import UTC, datetime

import httpx
import structlog

log = structlog.get_logger()

# Ordered by how likely each is to answer from a cloud IP, not by preference.
KLINE_HOSTS = (
    "https://data-api.binance.vision",   # public mirror, usually not geo-blocked
    "https://api.binance.com",           # main host, HTTP 451 from US cloud IPs
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
)
COINDCX_CANDLES = "https://public.coindcx.com/market_data/candles"

# Our symbol to the venue's, where they differ. Binance does not list
# XAUUSDT — gold trades there as PAX Gold, and asking for the name we use
# internally returns nothing, which is why the board showed gold warming up
# on two candles forever.
VENUE_SYMBOL = {
    # PAX Gold tracks one ounce of gold, so it is a fair stand-in for XAU.
    # Silver is NOT gold: xagusdt used to map here too, which would have
    # priced and measured silver off gold's bars.
    "xauusdt": "PAXGUSDT",
}


def venue_symbol(symbol: str) -> str:
    return VENUE_SYMBOL.get(symbol.lower(), symbol.upper())


def parse_klines(rows) -> list[dict]:
    """
    Binance returns positional arrays: [openTime, o, h, l, c, volume, ...].

    The final row is the bar still forming. It is kept — a partly formed bar
    is real data about the current minute — and marked by the caller, so the
    next poll updates it rather than appending beside it.
    """
    if not isinstance(rows, list):
        return []
    out: list[dict] = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            continue
        try:
            ts = float(row[0])
            if ts > 1e11:
                ts /= 1000.0
            taker_buy = float(row[9]) if len(row) > 9 else 0.0
            out.append({
                "timestamp": datetime.fromtimestamp(ts, tz=UTC),
                "open": float(row[1]), "high": float(row[2]),
                "low": float(row[3]), "close": float(row[4]),
                "volume": max(0.0, float(row[5])),
                "taker_buy_volume": max(0.0, taker_buy),
            })
        except (TypeError, ValueError, OSError, OverflowError):
            continue
    out.sort(key=lambda r: r["timestamp"])
    return out


def parse_depth(symbol: str, payload):
    """Binance depth: {"bids": [[price, qty], ...], "asks": [...]}."""
    from analysis.orderbook import OrderBook

    if not isinstance(payload, dict):
        return None

    def side(key):
        rows = payload.get(key, [])
        out = []
        if not isinstance(rows, list):
            return out
        for row in rows:
            if isinstance(row, (list, tuple)) and len(row) >= 2:
                try:
                    price, size = float(row[0]), float(row[1])
                except (TypeError, ValueError):
                    continue
                if price > 0 and size > 0:
                    out.append((price, size))
        return out

    bids = sorted(side("bids"), key=lambda x: -x[0])
    asks = sorted(side("asks"), key=lambda x: x[0])
    if not bids or not asks:
        return None
    return OrderBook(symbol=symbol.lower(), bids=bids, asks=asks)


class BinanceKlines:
    """
    Polls klines and depth for the watchlist. Public endpoints, no key.

    Remembers which host answered and tries it first next time, so a working
    deployment makes one request per symbol rather than walking a list of
    geo-blocked hosts on every poll.
    """

    def __init__(self, store=None, timeout: float = 20.0) -> None:
        self.store = store
        self.timeout = timeout
        self.host: str | None = None
        self.last_error: str = ""
        self.last_books: dict[str, object] = {}
        self.refreshed: set[str] = set()
        # Per-symbol outcome of the last sweep, and when the last one worked.
        # Without this a failing feed is invisible: the store keeps whatever
        # it had, the board renders it, and nothing anywhere says the numbers
        # stopped being current.
        self.status: dict[str, str] = {}
        self.last_success: datetime | None = None

    def _hosts(self) -> tuple[str, ...]:
        if self.host:
            return (self.host, *(h for h in KLINE_HOSTS if h != self.host))
        return KLINE_HOSTS

    async def _get(self, path: str, params: dict):
        """First host that answers 200. Records it so the next call is direct."""
        errors = []
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for host in self._hosts():
                try:
                    r = await client.get(f"{host}{path}", params=params)
                except Exception as exc:
                    errors.append(f"{host}: {type(exc).__name__}")
                    continue
                if r.status_code == 200:
                    self.host = host
                    try:
                        return r.json()
                    except Exception:
                        errors.append(f"{host}: bad json")
                        continue
                errors.append(f"{host}: HTTP {r.status_code}")
                if r.status_code == 451 and host == self.host:
                    # The remembered host has started refusing; stop preferring it.
                    self.host = None
        self.last_error = " · ".join(errors[:4])
        return None

    async def fetch_candles(self, symbol: str, interval: str = "1m",
                            limit: int = 200) -> list[dict]:
        rows = await self._get("/api/v3/klines", {
            "symbol": venue_symbol(symbol), "interval": interval,
            "limit": min(limit, 1000)})
        if rows is not None:
            bars = parse_klines(rows)
            if bars:
                return bars
        return await self._coindcx_fallback(symbol, interval, limit)

    async def _coindcx_fallback(self, symbol: str, interval: str,
                                limit: int) -> list[dict]:
        base = symbol.upper().removesuffix("USDT")
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as c:
                r = await c.get(COINDCX_CANDLES, params={
                    "pair": f"B-{base}_USDT", "interval": interval,
                    "limit": min(limit, 1000)})
            if r.status_code != 200:
                return []
            rows = r.json()
        except Exception:
            return []
        out = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            try:
                ts = float(row["time"])
                if ts > 1e11:
                    ts /= 1000.0
                out.append({
                    "timestamp": datetime.fromtimestamp(ts, tz=UTC),
                    "open": float(row["open"]), "high": float(row["high"]),
                    "low": float(row["low"]), "close": float(row["close"]),
                    "volume": max(0.0, float(row.get("volume") or 0.0)),
                })
            except (KeyError, TypeError, ValueError, OSError, OverflowError):
                continue
        out.sort(key=lambda r: r["timestamp"])
        return out

    async def fetch_depth(self, symbol: str, limit: int = 50):
        payload = await self._get("/api/v3/depth",
                                  {"symbol": venue_symbol(symbol), "limit": limit})
        if payload is None:
            return None
        book = parse_depth(symbol, payload)
        if book is not None:
            self.last_books[symbol.lower()] = book
        return book

    async def fetch(self) -> int:
        """
        Refresh every watchlist symbol. Returns how many got real candles.

        Best effort per symbol: one market failing must not stop the rest, and
        this job failing must not stop the price feed, which comes from a
        different collector entirely.
        """
        if self.store is None:
            return 0
        symbols = await self.store.get_symbols()
        self.refreshed = set()
        self.status = {}
        for sym in symbols:
            try:
                bars = await self.fetch_candles(sym)
                if not bars:
                    self.status[sym] = f"no candles ({self.last_error or 'empty'})"
                    continue
                if len(bars) < 20:
                    self.status[sym] = f"only {len(bars)} bars — too few to install"
                    continue
                await self.store.replace_candles(sym, bars)
                self.refreshed.add(sym)
                self.status[sym] = f"{len(bars)} bars via {self.host or 'fallback'}"
                book = await self.fetch_depth(sym)
                if book is not None:
                    state = await self.store.get(sym)
                    if state is not None:
                        state.order_book = book
            except Exception as exc:
                self.status[sym] = f"{type(exc).__name__}: {exc}"
                log.debug("binance_klines_symbol_failed", symbol=sym, error=str(exc))
        if self.refreshed:
            self.last_success = datetime.now(UTC)
        log.info("binance_klines_done", refreshed=len(self.refreshed),
                 requested=len(symbols), host=self.host, error=self.last_error)
        return len(self.refreshed)
