"""
Years of candles from Binance, without an API key and without 4,000 requests.

Two sources, because they cover different parts of the timeline
---------------------------------------------------------------
Binance publishes its own market data as ZIP archives at data.binance.vision:
one file per symbol per month, holding every bar of that month as CSV. A year
of one-minute candles for one symbol is twelve HTTP GETs instead of 526 calls
to /api/v3/klines. For seven symbols that is 84 downloads against 3,682 API
calls — not a marginal saving, a different order of operation, and it is the
difference between a backfill that takes twenty minutes and one that trips a
rate limit somewhere in the middle and leaves a hole.

The archives lag. A month's file appears a day or two after the month ends,
and the current month has daily files at best. So the REST endpoint covers
the tail: the recent window the archives have not caught up with. Same data,
same schema, different transport.

Neither needs an API key. Market data on Binance is public, and every key the
operator is about to add is for something else.

What it does not do
-------------------
Fill gaps by inventing bars. If Binance has no candle for a minute — a halt,
an outage, a symbol that did not exist yet — this returns nothing for that
minute rather than carrying the last close forward. A synthesised flat bar
is indistinguishable downstream from a real one where nothing traded, and it
would quietly teach every model that those minutes were calm.
"""
from __future__ import annotations

import csv
import io
import zipfile
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta

import httpx
import structlog

from collectors.binance_klines import KLINE_HOSTS, venue_symbol

log = structlog.get_logger()

class BinanceUnreachable(RuntimeError):
    """
    Nothing could be fetched, as distinct from nothing existing.

    These two look identical to a caller that only sees an empty list, and
    they mean opposite things: "Binance has no bars for 2019 because the pair
    was not listed" is a finished answer, and "every host refused the
    connection" is a run that did no work. Without the distinction a backfill
    behind a blocked network prints `fetched 0, new 0` and reads as a
    successful no-op — which is how you discover a week later that the table
    is empty.
    """


ARCHIVE_ROOT = "https://data.binance.vision/data/spot"

# Binance caps a klines response at 1000 bars regardless of what you ask for.
REST_PAGE = 1000

INTERVAL_SECONDS = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600, "12h": 43200,
    "1d": 86400,
}


def interval_delta(interval: str) -> timedelta:
    if interval not in INTERVAL_SECONDS:
        raise ValueError(f"unsupported interval {interval!r}; "
                         f"known: {', '.join(sorted(INTERVAL_SECONDS))}")
    return timedelta(seconds=INTERVAL_SECONDS[interval])


def expected_bars(start: datetime, end: datetime, interval: str) -> int:
    """How many bars a range should hold if nothing is missing."""
    span = (end - start).total_seconds()
    return max(0, int(span // INTERVAL_SECONDS[interval]))


def _row_to_candle(row: list, symbol: str, interval: str, source: str) -> dict | None:
    """
    One CSV line or one REST array to a candle dict. Both are the same twelve
    positional fields, which is why one parser serves both transports.

    Rejects rather than repairs. A bar with a zero or negative price is not a
    bar that needs fixing — gold arrived priced at 4.3e-05 for 55 snapshots
    once, and a row like that in a denominator turns a 0.04% move into a
    forward return of fifteen million percent.
    """
    try:
        ms = int(float(row[0]))
        # Archives switched from millisecond to microsecond stamps in 2025.
        while ms > 1e13:
            ms //= 1000
        o, h, low, c = (float(row[1]), float(row[2]), float(row[3]), float(row[4]))
        if min(o, h, low, c) <= 0 or h < low:
            return None
        return {
            "symbol": symbol.lower(),
            "interval": interval,
            "open_time": datetime.fromtimestamp(ms / 1000, tz=UTC).replace(tzinfo=None),
            "open": o, "high": h, "low": low, "close": c,
            "volume": float(row[5]),
            "quote_volume": float(row[7]) if len(row) > 7 else 0.0,
            "trades": int(float(row[8])) if len(row) > 8 else 0,
            "taker_buy_volume": float(row[9]) if len(row) > 9 else 0.0,
            "source": source,
        }
    except (ValueError, IndexError, TypeError, OSError):
        return None


def _months(start: date, end: date) -> list[tuple[int, int]]:
    out, year, month = [], start.year, start.month
    while (year, month) <= (end.year, end.month):
        out.append((year, month))
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return out


class BinanceHistory:
    """Pulls candles for a date range. Stateless; one instance is reusable."""

    def __init__(self, timeout: float = 60.0) -> None:
        self.timeout = timeout
        # Counted, not just logged. fetch_range uses this to tell a range with
        # no bars from a range it could not reach.
        self.transport_failures = 0
        self.responses_seen = 0

    # ── Bulk archives ────────────────────────────────────────────────────

    async def _archive_month(self, client: httpx.AsyncClient, symbol: str,
                             interval: str, year: int, month: int) -> list[dict]:
        venue = venue_symbol(symbol)
        name = f"{venue}-{interval}-{year:04d}-{month:02d}"
        url = f"{ARCHIVE_ROOT}/monthly/klines/{venue}/{interval}/{name}.zip"

        try:
            resp = await client.get(url)
        except Exception as exc:
            log.warning("archive_fetch_failed", url=url, error=str(exc)[:160])
            self.transport_failures += 1
            return []

        if resp.status_code == 404:
            # Expected at both ends, and NOT a transport failure: before the
            # pair was listed, and for the current month, which has no
            # monthly file yet. A 404 is the archive answering.
            log.debug("archive_absent", symbol=symbol, month=f"{year}-{month:02d}")
            return []
        if resp.status_code != 200:
            log.warning("archive_status", url=url, status=resp.status_code)
            self.transport_failures += 1
            return []

        try:
            with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
                inner = zf.namelist()[0]
                text = zf.read(inner).decode("utf-8", errors="replace")
        except Exception as exc:
            log.warning("archive_unreadable", url=url, error=str(exc)[:160])
            return []

        candles = []
        for row in csv.reader(io.StringIO(text)):
            # 2025 archives gained a header line; older ones have none.
            if not row or row[0][:1].isalpha():
                continue
            candle = _row_to_candle(row, symbol, interval, "binance-archive")
            if candle:
                candles.append(candle)
        log.info("archive_month", symbol=symbol, interval=interval,
                 month=f"{year}-{month:02d}", bars=len(candles))
        return candles

    # ── REST tail ────────────────────────────────────────────────────────

    async def _rest_range(self, client: httpx.AsyncClient, symbol: str,
                          interval: str, start: datetime, end: datetime) -> list[dict]:
        """
        Page through /api/v3/klines. Used only for the window the archives
        have not published yet, which is days rather than years.
        """
        venue = venue_symbol(symbol)
        step = interval_delta(interval)
        out: list[dict] = []
        cursor = start

        while cursor < end:
            params = {
                "symbol": venue, "interval": interval,
                "startTime": int(cursor.replace(tzinfo=UTC).timestamp() * 1000),
                "endTime": int(end.replace(tzinfo=UTC).timestamp() * 1000),
                "limit": REST_PAGE,
            }
            rows = None
            reached = False
            for host in KLINE_HOSTS:
                try:
                    resp = await client.get(f"{host}/api/v3/klines", params=params)
                    if resp.status_code == 200:
                        reached = True
                        rows = resp.json()
                        break
                    # A 4xx from Binance is Binance answering — a bad symbol
                    # or an out-of-range window. That is data, not an outage.
                    if 400 <= resp.status_code < 500:
                        reached = True
                        break
                except Exception:
                    continue

            if reached:
                self.responses_seen += 1
            else:
                self.transport_failures += 1

            if not rows:
                break

            page = [c for c in (_row_to_candle(r, symbol, interval, "binance-rest")
                                for r in rows) if c]
            if not page:
                break
            out.extend(page)

            advanced = page[-1]["open_time"] + step
            if advanced <= cursor:
                break  # no forward progress; stop rather than spin
            cursor = advanced
            if len(rows) < REST_PAGE:
                break

        return out

    # ── The thing callers use ────────────────────────────────────────────

    async def fetch_range(self, symbol: str, interval: str,
                          start: datetime, end: datetime) -> AsyncIterator[list[dict]]:
        """
        Yield candles a month at a time, oldest first.

        A generator rather than one list because a year of one-minute bars for
        one symbol is half a million dicts, and seven symbols of that at once
        is a memory problem on a small box for no benefit — the caller writes
        each batch and moves on.
        """
        start = start.replace(tzinfo=None) if start.tzinfo else start
        end = end.replace(tzinfo=None) if end.tzinfo else end
        yielded_any = False

        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            archive_end = start
            for year, month in _months(start.date(), end.date()):
                batch = await self._archive_month(client, symbol, interval, year, month)
                kept = [c for c in batch if start <= c["open_time"] < end]
                if batch:
                    self.responses_seen += 1
                if kept:
                    archive_end = max(archive_end, kept[-1]["open_time"] + interval_delta(interval))
                    yielded_any = True
                    yield kept

            # Whatever the archives did not reach, from the live endpoint.
            if archive_end < end:
                tail = await self._rest_range(client, symbol, interval, archive_end, end)
                tail = [c for c in tail if start <= c["open_time"] < end]
                if tail:
                    log.info("rest_tail", symbol=symbol, interval=interval,
                             bars=len(tail), since=archive_end.isoformat())
                    yielded_any = True
                    yield tail

        # Nothing came back AND nothing ever answered. Saying so is the whole
        # point: a silent empty result here is indistinguishable from a range
        # that genuinely holds no bars, and the two need opposite responses.
        if not yielded_any and self.transport_failures and not self.responses_seen:
            raise BinanceUnreachable(
                f"no host answered for {symbol} {interval} "
                f"({self.transport_failures} failed attempts). "
                "Binance may be blocked from this network.")
