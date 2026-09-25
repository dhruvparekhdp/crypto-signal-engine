"""
What Binance actually lists, so the watchlist only ever holds real pairs.

The watchlist is the one list the engine reads. Everything else — which feeds
to open, which candles to fetch, which headlines to attribute — follows from
it. A symbol typed from memory ("xauusdt") that Binance does not list gets no
candles, and the stand-ins that were bolted on to cover for that are how gold
ended up priced at 4.3e-05. Searching Binance's own symbol list before adding
means the pair on the watchlist is the pair that trades.

Everything here is public market data: no key, no account.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass

import httpx
import structlog

from collectors.binance_klines import KLINE_HOSTS

log = structlog.get_logger()

FUTURES_EXCHANGE_INFO = "https://fapi.binance.com/fapi/v1/exchangeInfo"

# exchangeInfo carries tickers, not names, so a search for "gold" finds
# nothing on its own. These are the names people actually type. A name maps
# to base assets; the search still only returns pairs Binance lists.
NAME_HINTS: dict[str, tuple[str, ...]] = {
    "gold": ("PAXG", "XAUT"),
    "bitcoin": ("BTC",),
    "ethereum": ("ETH",),
    "ether": ("ETH",),
    "solana": ("SOL",),
    "ripple": ("XRP",),
    "dogecoin": ("DOGE",),
    "cardano": ("ADA",),
    "litecoin": ("LTC",),
    "bitcoin cash": ("BCH",),
    "binance coin": ("BNB",),
    "chainlink": ("LINK",),
    "polkadot": ("DOT",),
    "avalanche": ("AVAX",),
    "polygon": ("POL", "MATIC"),
    "tron": ("TRX",),
    "toncoin": ("TON",),
    "shiba": ("SHIB",),
    "pepe": ("PEPE",),
    "sui": ("SUI",),
    "near": ("NEAR",),
    "aptos": ("APT",),
    "arbitrum": ("ARB",),
    "optimism": ("OP",),
    "uniswap": ("UNI",),
    "stellar": ("XLM",),
}

CACHE_SECONDS = 6 * 3600


@dataclass
class _Cache:
    spot: dict[str, str]          # symbol -> base asset, USDT pairs that are TRADING
    futures: set[str] | None      # perpetual USDT futures, None if unreachable
    host: str
    fetched_at: float


_cache: _Cache | None = None


async def _get_json(client: httpx.AsyncClient, url: str):
    resp = await client.get(url)
    resp.raise_for_status()
    return resp.json()


async def load_symbols(force: bool = False, timeout: float = 15.0) -> _Cache | None:
    """
    Spot USDT pairs that are trading, plus which of them have a perpetual.

    Cached for six hours: listings change a few times a month, and the full
    exchangeInfo is a few megabytes. Returns None only when no host answered,
    which callers must treat as "unknown", never as "not listed".
    """
    global _cache
    if _cache and not force and time.time() - _cache.fetched_at < CACHE_SECONDS:
        return _cache

    async with httpx.AsyncClient(timeout=timeout) as client:
        spot: dict[str, str] | None = None
        used = ""
        for host in KLINE_HOSTS:
            try:
                data = await _get_json(client, f"{host}/api/v3/exchangeInfo")
            except Exception as exc:
                log.warning("binance_symbols_host_failed", host=host, error=str(exc)[:120])
                continue
            spot = {
                s["symbol"]: s.get("baseAsset", "")
                for s in data.get("symbols", [])
                if s.get("status") == "TRADING" and s.get("quoteAsset") == "USDT"
            }
            used = host
            break
        if spot is None:
            return _cache  # a stale list beats none; None if never loaded

        futures: set[str] | None
        try:
            fdata = await _get_json(client, FUTURES_EXCHANGE_INFO)
            futures = {
                s["symbol"] for s in fdata.get("symbols", [])
                if s.get("status") == "TRADING" and s.get("contractType") == "PERPETUAL"
                and s.get("quoteAsset") == "USDT"
            }
        except Exception as exc:
            log.info("binance_futures_symbols_unavailable", error=str(exc)[:120])
            futures = None

    _cache = _Cache(spot=spot, futures=futures, host=used, fetched_at=time.time())
    log.info("binance_symbols_loaded", spot=len(spot),
             futures=None if futures is None else len(futures), host=used)
    return _cache


def rank(query: str, spot: dict[str, str], limit: int = 20) -> list[str]:
    """
    Pairs matching a query, best first.

    Exact base asset beats an exact name, which beats part of a name, then a
    prefix, then a substring — so "sol" puts SOLUSDT first rather than whatever happens to
    contain those letters.
    """
    q = query.strip().lower()
    if not q:
        return []
    qu = q.upper().removesuffix("USDT") or q.upper()
    exact = {b for name, bases in NAME_HINTS.items() if q == name for b in bases}
    hinted = {b for name, bases in NAME_HINTS.items() if q in name for b in bases}

    scored: list[tuple[int, str]] = []
    for symbol, base in spot.items():
        if base == qu or symbol == q.upper():
            score = 0
        elif base in exact:      # "bitcoin" is BTC before it is Bitcoin Cash
            score = 1
        elif base in hinted:
            score = 2
        elif base.startswith(qu):
            score = 3
        elif qu in symbol:
            score = 4
        else:
            continue
        scored.append((score, symbol))
    scored.sort(key=lambda t: (t[0], len(t[1]), t[1]))
    return [s for _, s in scored[:limit]]


async def ticker_24h(symbols: list[str], timeout: float = 10.0) -> dict[str, dict]:
    """Last price, 24h change and 24h quote volume, for judging a pair before adding it."""
    if not symbols:
        return {}
    param = json.dumps(symbols, separators=(",", ":"))
    async with httpx.AsyncClient(timeout=timeout) as client:
        for host in KLINE_HOSTS:
            try:
                rows = await _get_json(client, f"{host}/api/v3/ticker/24hr?symbols={param}")
            except Exception:
                continue
            return {
                r["symbol"]: {
                    "price": float(r.get("lastPrice") or 0),
                    "change_pct": float(r.get("priceChangePercent") or 0),
                    "quote_volume_24h": float(r.get("quoteVolume") or 0),
                }
                for r in rows
            }
    return {}


async def search(query: str, watchlist: set[str], limit: int = 20) -> dict:
    """The payload behind the watchlist page's search box."""
    cache = await load_symbols()
    if cache is None:
        return {"results": [], "available": False,
                "error": "Binance did not answer from this server; try again shortly."}
    matches = rank(query, cache.spot, limit)
    stats = await ticker_24h(matches)
    results = []
    for symbol in matches:
        s = stats.get(symbol, {})
        results.append({
            "symbol": symbol.lower(),
            "pair": f"{cache.spot[symbol]}/USDT",
            "base": cache.spot[symbol],
            "price": s.get("price"),
            "change_pct": s.get("change_pct"),
            "quote_volume_24h": s.get("quote_volume_24h"),
            "futures": None if cache.futures is None else symbol in cache.futures,
            "on_watchlist": symbol.lower() in watchlist,
        })
    return {"results": results, "available": True, "host": cache.host}


async def is_listed(symbol: str) -> bool | None:
    """True/False when Binance's list is known, None when it could not be fetched."""
    cache = await load_symbols()
    if cache is None:
        return None
    return symbol.upper() in cache.spot
