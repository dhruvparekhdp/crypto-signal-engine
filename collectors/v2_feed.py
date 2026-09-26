"""
Live frames for the v2 setups: closed 5m, 15m, 1h, 4h and 1d bars from Binance.

The paper engine keeps six hours of 1m bars in memory, which is not enough
for a daily SMA20 or 4h structure. This asks Binance's public klines for
what `analysis.v2_setups.generate` needs, drops the bar still forming, and
returns DataFrames in the lake's column layout, so live shadow trading and
the backtest run the very same code on the very same kind of data.

`end` lets the journal fetch the chart as it stood when a past trade was
taken. Funding comes from the futures API and is optional: when it cannot
be reached, the funding filter simply does not apply.
"""
from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pandas as pd
import structlog

from collectors.binance_klines import KLINE_HOSTS, parse_klines, venue_symbol

log = structlog.get_logger()

LIMITS = {"5m": 1000, "15m": 1000, "1h": 500, "4h": 300, "1d": 120}
MINUTES = {"5m": 5, "15m": 15, "1h": 60, "4h": 240, "1d": 1440}
FUNDING_URL = "https://fapi.binance.com/fapi/v1/fundingRate"
FUTURES_KLINES = "https://fapi.binance.com/fapi/v1/klines"


def to_frame(rows: list[dict], minutes: int, now: datetime) -> pd.DataFrame:
    """Parsed klines -> lake-style DataFrame of CLOSED bars only (naive UTC ts)."""
    if not rows:
        return pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume",
                                     "taker_buy_volume"])
    df = pd.DataFrame(rows).rename(columns={"timestamp": "ts"})
    df["ts"] = pd.to_datetime(df["ts"], utc=True).dt.tz_localize(None)
    cutoff = pd.Timestamp(now.astimezone(UTC).replace(tzinfo=None))
    df = df[df["ts"] + pd.Timedelta(minutes=minutes) <= cutoff]
    return df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)


async def _klines(client: httpx.AsyncClient, symbol: str, interval: str,
                  end: datetime | None) -> list[dict]:
    """
    USD-M FUTURES klines first — the backtest's lake is futures data, and
    live shadow must see the same market (volume, wicks and previous-day
    levels all differ from spot). Spot is only a fallback when fapi cannot
    be reached, and is logged, because it makes shadow and backtest differ.
    """
    params = {"symbol": venue_symbol(symbol), "interval": interval,
              "limit": LIMITS[interval]}
    if end is not None:
        if end.tzinfo is None:          # naive means UTC here, never server-local
            end = end.replace(tzinfo=UTC)
        params["endTime"] = int(end.timestamp() * 1000)
    try:
        r = await client.get(FUTURES_KLINES, params=params)
        if r.status_code == 200:
            return parse_klines(r.json())
    except httpx.HTTPError:
        pass
    log.warning("v2_feed_futures_unavailable_using_spot", symbol=symbol, interval=interval)
    for host in KLINE_HOSTS:
        try:
            r = await client.get(f"{host}/api/v3/klines", params=params)
            if r.status_code == 200:
                return parse_klines(r.json())
        except httpx.HTTPError:
            continue
    return []


async def fetch_frames(symbol: str, end: datetime | None = None,
                       intervals=("5m", "15m", "4h", "1d")) -> dict[str, pd.DataFrame]:
    """{interval: closed-bar DataFrame}. Empty frames when Binance cannot be reached."""
    if end is not None and end.tzinfo is None:
        end = end.replace(tzinfo=UTC)
    now = end or datetime.now(UTC)
    out: dict[str, pd.DataFrame] = {}
    async with httpx.AsyncClient(timeout=15.0) as client:
        for iv in intervals:
            out[iv] = to_frame(await _klines(client, symbol, iv, end), MINUTES[iv], now)
    return out


async def fetch_funding(symbol: str, limit: int = 120) -> pd.DataFrame | None:
    """Recent funding prints as a lake-style frame, or None."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(FUNDING_URL, params={"symbol": venue_symbol(symbol),
                                                      "limit": limit})
        if r.status_code != 200:
            return None
        rows = r.json()
        df = pd.DataFrame({
            "ts": pd.to_datetime([int(x["fundingTime"]) for x in rows], unit="ms"),
            "last_funding_rate": [float(x["fundingRate"]) for x in rows]})
        return df.sort_values("ts").reset_index(drop=True)
    except Exception as exc:
        log.debug("v2_funding_unavailable", symbol=symbol, error=str(exc)[:120])
        return None
