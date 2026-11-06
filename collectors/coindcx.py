from __future__ import annotations

from datetime import UTC, datetime

import httpx
import structlog

from analysis.crypto_state_store import CryptoStateStore

log = structlog.get_logger()

_API_BASE = "https://api.coindcx.com"
_PUBLIC_BASE = "https://public.coindcx.com"

# /exchange/ticker covers SPOT markets only. Instruments that exist only as
# futures — gold and silver among them — are absent from it, which is why
# XAUUSDT sat at $0.000000 on the dashboard while trading fine in the app.
_FUTURES_PRICE_URLS = (
    f"{_PUBLIC_BASE}/market_data/v3/current_prices/futures/rt",
    f"{_PUBLIC_BASE}/market_data/v3/current_prices/futures",
)

# Real OHLCV bars, which the ticker cannot give. The ticker reports a rolling
# 24-hour volume; a 24-hour total is not a per-minute volume, and using one as
# the other left every volume check reading a constant. This endpoint is the
# only source here that carries volume actually traded inside a bar.
_CANDLES_URL = f"{_PUBLIC_BASE}/market_data/candles"


def _futures_name_variants(base: str, quote: str = "USDT") -> tuple[str, ...]:
    """
    Fallback spellings, kept only for instruments whose record omits "mkt".

    Matching is now done on the "mkt" field the endpoint provides — the probe
    showed B-XAU_USDT carries mkt "XAUUSDT", which is exactly the watchlist
    spelling. Deriving the key from data beats guessing the prefix.
    """
    b = base.upper()
    q = quote.upper()
    return (
        f"B-{b}_{q}", f"F-{b}_{q}", f"{b}_{q}", f"{b}{q}",
        f"I-{b}_{q}", f"B-{b}_{q}_FUT",
    )


def _quote_symbol(pair: str) -> str:
    """Return the quote currency of a pair in uppercase, e.g. 'btcusdt' -> 'USDT', 'xauinr' -> 'INR'."""
    p = pair.strip().lower()
    for quote in ("usdt", "busd", "usdc", "inr"):
        if p.endswith(quote):
            return quote.upper()
    return "USDT"


def _num(node: dict, *keys: str) -> float | None:
    """First key that parses to a finite number, else None."""
    for k in keys:
        v = node.get(k)
        if v is None:
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if f == f and abs(f) != float("inf"):
            return f
    return None


def _extract_price(node) -> float | None:
    """Pull a last price out of whichever shape the endpoint returns."""
    if isinstance(node, (int, float)):
        return float(node) or None
    if isinstance(node, dict):
        for key in ("ls", "last_price", "mark_price", "price", "c", "close"):
            v = node.get(key)
            if v is None:
                continue
            try:
                f = float(v)
            except (TypeError, ValueError):
                continue
            if f > 0:
                return f
    return None


def _parse_candles(payload) -> list[dict]:
    """
    Normalise a candle payload into plain dicts, newest last.

    Written tolerantly on purpose: this venue has published candles as both
    objects and positional arrays, with the timestamp as `time` or `t` and in
    seconds or milliseconds. A parser that insists on one shape turns a
    working feed into a silent outage, so anything unrecognised is skipped
    and the caller falls back to the poll-aggregated bars.
    """
    if not isinstance(payload, list):
        return []
    out: list[dict] = []
    for row in payload:
        try:
            if isinstance(row, dict):
                t = row.get("time", row.get("t", row.get("timestamp")))
                o = row.get("open", row.get("o"))
                h = row.get("high", row.get("h"))
                low = row.get("low", row.get("l"))
                c = row.get("close", row.get("c"))
                v = row.get("volume", row.get("v", 0.0))
            elif isinstance(row, (list, tuple)) and len(row) >= 6:
                t, o, h, low, c, v = row[:6]
            else:
                continue
            if t is None or o is None or c is None:
                continue
            ts = float(t)
            # Seconds or milliseconds — anything past the year 2286 in seconds
            # is milliseconds. Guessing wrong puts every bar in 1970.
            if ts > 1e11:
                ts /= 1000.0
            out.append({
                "timestamp": datetime.fromtimestamp(ts, tz=UTC),
                "open": float(o), "high": float(h), "low": float(low),
                "close": float(c), "volume": max(0.0, float(v or 0.0)),
            })
        except (TypeError, ValueError, OSError, OverflowError):
            continue
    out.sort(key=lambda r: r["timestamp"])
    return out


def _base_symbol(pair: str) -> str:
    """Strip the quote asset from a Binance-style pair, e.g. 'btcusdt' -> 'btc', 'xauinr' -> 'xau'."""
    p = pair.strip().lower()
    for quote in ("usdt", "busd", "usdc", "inr"):
        if p.endswith(quote) and len(p) > len(quote):
            return p[: -len(quote)]
    return p


class CoinDCXCollector:
    """
    REST-polling crypto price collector using CoinDCX's public ticker API.

    Free, no API key, no meaningful rate limit — one GET returns every market
    on the exchange in a single call. Preferred over CoinGecko for symbols
    it covers, since these are the exact prices you'd see trading on CoinDCX
    itself rather than an aggregate. CoinGecko fills in anything CoinDCX
    doesn't list.
    """

    TICKER_URL = f"{_API_BASE}/exchange/ticker"
    _QUOTES = ("USDT", "INR")  # try USDT pairs first, fall back to INR pairs

    def __init__(self, store: CryptoStateStore) -> None:
        self.store = store
        self._consecutive_failures = 0
        self.last_matched_symbols: set[str] = set()
        # Symbols served by the futures venue rather than spot — gold and
        # silver among them. Surfaced so the dashboard can say which is which.
        self.futures_symbols: set[str] = set()

    async def fetch(self) -> None:
        from collectors.source_health import watching

        async with watching("coindcx") as outcome:
            await self._fetch_watched(outcome)

    async def _fetch_watched(self, outcome: dict) -> None:
        symbols = await self.store.get_symbols()
        outcome["attempted"] = len(symbols)
        if not symbols:
            outcome["ok"] = True
            return

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(self.TICKER_URL)

            if resp.status_code != 200:
                self._consecutive_failures += 1
                log.warning("coindcx_bad_status", status=resp.status_code, body=resp.text[:200])
                outcome["error"] = f"HTTP {resp.status_code}"
                outcome["failed"] = len(symbols)
                return

            rows = resp.json()
            by_market = {r.get("market", "").upper(): r for r in rows if r.get("market")}

            now = datetime.now(UTC)
            matched: set[str] = set()
            for sym in symbols:
                base = _base_symbol(sym).upper()
                quote = _quote_symbol(sym)
                row = by_market.get(f"{base}{quote}")
                if not row:
                    continue

                try:
                    price = float(row.get("last_price") or 0.0)
                    if price <= 0:
                        continue
                    high = float(row.get("high") or price)
                    low = float(row.get("low") or price)
                    volume = float(row.get("volume") or 0.0)
                    change_pct = float(row.get("change_24_hour") or 0.0)
                except (TypeError, ValueError):
                    continue

                await self.store.update_from_rest(
                    symbol=sym,
                    price=price,
                    high_24h=high,
                    low_24h=low,
                    volume_24h=volume,
                    change_24h_pct=change_pct,
                    timestamp=now,
                )
                matched.add(sym)

            # Anything the spot ticker did not cover may still be a futures
            # instrument. Gold is the case that prompted this.
            missing = [s for s in symbols if s not in matched]
            if missing:
                matched |= await self._fetch_futures(missing, now)

            self.last_matched_symbols = matched
            unmatched = sorted(set(symbols) - matched)
            if unmatched:
                log.warning("coindcx_symbols_unmatched", symbols=unmatched)
            self._consecutive_failures = 0
            log.info("coindcx_poll_done", matched=len(matched), requested=len(symbols))
            outcome["failed"] = len(unmatched)
            outcome["ok"] = len(matched) > 0
            if not outcome["ok"]:
                outcome["error"] = "no markets matched"
            elif unmatched:
                outcome["note"] = "unmatched: " + ",".join(unmatched[:8])
        except Exception as exc:
            self._consecutive_failures += 1
            log.warning("coindcx_fetch_failed", error=str(exc))
            outcome["error"] = f"{type(exc).__name__}: {exc}"
            outcome["failed"] = len(symbols)

    def _pair_variants(self, symbol: str) -> tuple[str, ...]:
        """
        Candle pairs are named differently from ticker markets: `B-BTC_USDT`
        for the Binance-sourced book, `I-BTC_INR` for the Indian one.
        Quote currency must strictly match the symbol's quote.
        """
        base = _base_symbol(symbol).upper()
        quote = _quote_symbol(symbol)
        if quote == "USDT":
            return (f"B-{base}_USDT", f"B-{base}_USDT_FUT", f"{base}USDT")
        elif quote == "INR":
            return (f"I-{base}_INR", f"B-{base}_INR", f"{base}INR")
        return (f"B-{base}_{quote}", f"{base}{quote}")

    async def fetch_candles_raw(self, symbol: str, interval: str = "1m",
                                limit: int = 120) -> tuple[str, object] | None:
        """
        Return (pair, payload) from the first candle pair name that answers.

        Best effort and never fatal — the collector is a price poller first,
        and a venue that changes its candle route must not take prices down
        with it.
        """
        async with httpx.AsyncClient(timeout=15.0) as client:
            for pair in self._pair_variants(symbol):
                try:
                    r = await client.get(_CANDLES_URL, params={
                        "pair": pair, "interval": interval, "limit": limit})
                except Exception as exc:
                    log.debug("coindcx_candles_unreachable", pair=pair, error=str(exc))
                    continue
                if r.status_code != 200:
                    continue
                try:
                    payload = r.json()
                except Exception:
                    continue
                if isinstance(payload, list) and payload:
                    return pair, payload
        return None

    async def fetch_candles(self, interval: str = "1m", limit: int = 120) -> int:
        """
        Replace the poll-aggregated history with real bars, where they exist.

        Two things improve at once. Volume becomes a genuine per-bar figure,
        so the volume family has something to read instead of abstaining. And
        the high and low become the real ones: aggregating two polls a minute
        understates the true range, which is a floor on volatility rather than
        a measurement of it.

        Returns the number of symbols refreshed, so a caller can tell "the
        venue answered with nothing" from "the venue was never asked".
        """
        symbols = await self.store.get_symbols()
        refreshed = 0
        for sym in symbols:
            got = await self.fetch_candles_raw(sym, interval, limit)
            if not got:
                continue
            pair, payload = got
            bars = _parse_candles(payload)
            if not bars:
                log.debug("coindcx_candles_unparsed", symbol=sym, pair=pair)
                continue
            await self.store.replace_candles(sym, bars)
            refreshed += 1
        log.info("coindcx_candles_done", refreshed=refreshed, requested=len(symbols))
        return refreshed

    async def fetch_futures_raw(self) -> tuple[str, object] | None:
        """
        Return (url, payload) from the first futures endpoint that answers.

        Exists so the shape can be inspected from the running server — the
        futures response format is not publicly documented and could not be
        reached from the environment this was written in.
        """
        async with httpx.AsyncClient(timeout=15.0) as client:
            for url in _FUTURES_PRICE_URLS:
                try:
                    r = await client.get(url)
                except Exception as exc:
                    log.debug("coindcx_futures_unreachable", url=url, error=str(exc))
                    continue
                if r.status_code == 200:
                    try:
                        return url, r.json()
                    except Exception:
                        return url, r.text[:2000]
        return None

    async def _fetch_futures(self, symbols: list[str], now: datetime) -> set[str]:
        """
        Fill in symbols the spot ticker does not list. Best effort, never fatal.

        The record carries a real 24h high, low, volume, change and funding
        rate. The first version of this passed high=low=price and volume=0,
        which is the same flat-candle mistake that collapsed ATR on the REST
        path — and it silenced the volume family for exactly the instruments
        that needed the futures feed.
        """
        matched: set[str] = set()
        got = await self.fetch_futures_raw()
        if not got:
            return matched
        _url, payload = got

        table = payload
        if isinstance(payload, dict):
            for wrapper in ("prices", "data", "result"):
                inner = payload.get(wrapper)
                if isinstance(inner, dict):
                    table = inner
                    break
        if not isinstance(table, dict):
            return matched

        # Index by the market name the venue itself reports, falling back to
        # the raw key for records that omit it.
        by_market: dict[str, dict] = {}
        for key, rec in table.items():
            # Real records are dicts. A bare number is normalised rather than
            # skipped, so a leaner response shape still yields a price instead
            # of the symbol silently staying dead.
            if isinstance(rec, (int, float)):
                rec = {"ls": float(rec)}
            if not isinstance(rec, dict):
                continue
            name = str(rec.get("mkt") or key).upper()
            by_market.setdefault(name, rec)
            by_market.setdefault(str(key).upper(), rec)

        for sym in symbols:
            base = _base_symbol(sym).upper()
            quote = _quote_symbol(sym)
            rec = by_market.get(f"{base}{quote}")
            if rec is None:
                for name in _futures_name_variants(base, quote):
                    rec = by_market.get(name)
                    if rec is not None:
                        break
            if rec is None:
                continue

            price = _num(rec, "ls", "last_price", "mp", "price", "c")
            if price is None or price <= 0:
                continue

            high = _num(rec, "h", "high") or price
            low = _num(rec, "l", "low") or price
            volume = _num(rec, "v", "volume") or 0.0
            change = _num(rec, "pc", "change_24_hour") or 0.0
            funding = _num(rec, "fr", "efr", "funding_rate")

            await self.store.update_from_rest(
                symbol=sym, price=price, high_24h=high, low_24h=low,
                volume_24h=volume, change_24h_pct=change, timestamp=now,
            )
            if funding is not None:
                await self.store.set_funding_rate(sym, funding)
            matched.add(sym)
            self.futures_symbols.add(sym)
            log.info("coindcx_futures_matched", symbol=sym, price=price,
                     change_24h=change, funding=funding)
        return matched
