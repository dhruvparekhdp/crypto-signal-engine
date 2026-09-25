"""
Binance's public archive, as a local Parquet lake.

data.binance.vision publishes every market's history as zipped CSV, one file
per symbol per day or month, free and without an API key. This module knows
the URL layout and the CSV columns for each kind of file, and turns them
into Parquet under one folder per kind:

    data/lake/{market}/{kind}/{SYMBOL}/{interval or "-"}/{YYYY-MM}.parquet

Parquet on disk, not Postgres, on purpose. One symbol's 1-second klines are
31 million rows a year and BTC's futures trades are billions; the database is
for what the engine decides, the lake is for what the market did. Analyses
read the months they need with pandas (`read`), and anything worth keeping
from them — labelled events, reviews — goes to the database.

Kinds and where they exist (checked against Binance's public-data README):

    klines                spot + um, 1s (spot only) .. 1mo, monthly + daily
    aggTrades, trades     spot + um, monthly + daily           (very large)
    markPriceKlines, premiumIndexKlines, indexPriceKlines      um only
    fundingRate           um, monthly
    metrics               um, daily only: open interest, top-trader and
                          global long/short ratios, taker buy/sell ratio, 5m
    bookDepth             um, daily only: depth at +-1..5% from mid
    bookTicker            um, daily; Binance stopped publishing it in 2024

Spot timestamps switched from milliseconds to microseconds on 1 Jan 2025;
`_to_datetime` accepts either.
"""
from __future__ import annotations

import calendar
import hashlib
import io
import zipfile
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

ROOT = "https://data.binance.vision/data"
LAKE = Path("data/lake")

KLINE_COLS = ["open_time", "open", "high", "low", "close", "volume", "close_time",
              "quote_volume", "trades", "taker_buy_volume", "taker_buy_quote_volume",
              "ignore"]
COLUMNS = {
    "klines": KLINE_COLS,
    "markPriceKlines": KLINE_COLS,
    "premiumIndexKlines": KLINE_COLS,
    "indexPriceKlines": KLINE_COLS,
    "aggTrades": ["agg_trade_id", "price", "quantity", "first_trade_id", "last_trade_id",
                  "transact_time", "is_buyer_maker"],
    "trades": ["id", "price", "qty", "quote_qty", "time", "is_buyer_maker"],
    "fundingRate": ["calc_time", "funding_interval_hours", "last_funding_rate"],
    "metrics": ["create_time", "symbol", "sum_open_interest", "sum_open_interest_value",
                "count_toptrader_long_short_ratio", "sum_toptrader_long_short_ratio",
                "count_long_short_ratio", "sum_taker_long_short_vol_ratio"],
    "bookDepth": ["timestamp", "percentage", "depth", "notional"],
    "bookTicker": ["update_id", "best_bid_price", "best_bid_qty", "best_ask_price",
                   "best_ask_qty", "transaction_time", "event_time"],
}
# The column that holds each row's time.
TIME_COL = {
    "klines": "open_time", "markPriceKlines": "open_time", "premiumIndexKlines": "open_time",
    "indexPriceKlines": "open_time", "aggTrades": "transact_time", "trades": "time",
    "fundingRate": "calc_time", "metrics": "create_time", "bookDepth": "timestamp",
    "bookTicker": "transaction_time",
}
INTERVAL_KINDS = {"klines", "markPriceKlines", "premiumIndexKlines", "indexPriceKlines"}
DAILY_ONLY = {"metrics", "bookDepth", "bookTicker"}
MONTHLY_ONLY = {"fundingRate"}
FUTURES_ONLY = {"markPriceKlines", "premiumIndexKlines", "indexPriceKlines", "fundingRate",
                "metrics", "bookDepth", "bookTicker"}
INTERVALS = ["1s", "1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h",
             "1d", "3d", "1w", "1mo"]

# Rough compressed-Parquet size per symbol per year, for the dry run. Order of
# magnitude only: BTC trades far more than LTC.
MB_PER_YEAR = {
    ("klines", "1s"): 700, ("klines", "1m"): 20, ("klines", "3m"): 7, ("klines", "5m"): 4,
    ("klines", "15m"): 1.5, ("klines", "30m"): 0.8, ("klines", "1h"): 0.4,
    "aggTrades": 15000, "trades": 30000, "metrics": 6, "bookDepth": 25, "fundingRate": 0.05,
    "markPriceKlines": 20, "premiumIndexKlines": 20, "indexPriceKlines": 20, "bookTicker": 40000,
}


@dataclass(frozen=True)
class Part:
    """One archive file: a symbol, a kind, an interval, a day or a month."""

    market: str          # "spot" | "um"
    kind: str
    symbol: str          # upper case, e.g. BTCUSDT
    interval: str        # "" for kinds without one
    period: str          # "YYYY-MM" (monthly) or "YYYY-MM-DD" (daily)

    @property
    def daily(self) -> bool:
        return len(self.period) == 10

    @property
    def url(self) -> str:
        base = "spot" if self.market == "spot" else f"futures/{self.market}"
        freq = "daily" if self.daily else "monthly"
        folder = f"{self.symbol}/{self.interval}" if self.interval else self.symbol
        stem = (f"{self.symbol}-{self.interval}-{self.period}" if self.interval
                else f"{self.symbol}-{self.kind}-{self.period}")
        return f"{ROOT}/{base}/{freq}/{self.kind}/{folder}/{stem}.zip"

    @property
    def month(self) -> str:
        return self.period[:7]


def lake_path(root: Path, market: str, kind: str, symbol: str, interval: str,
              month: str) -> Path:
    return Path(root) / market / kind / symbol.upper() / (interval or "-") / f"{month}.parquet"


def _months(start: date, end: date) -> list[date]:
    out, d = [], date(start.year, start.month, 1)
    while d <= end:
        out.append(d)
        d = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
    return out


def plan(market: str, kind: str, symbol: str, interval: str, start: date,
         end: date) -> list[Part]:
    """
    Every file needed to cover [start, end], newest data included.

    Complete months use the monthly file; the current month (whose monthly
    file does not exist yet) uses daily files up to yesterday. Kinds that
    Binance only publishes daily are daily throughout.
    """
    if market == "spot" and kind in FUTURES_ONLY:
        raise ValueError(f"{kind} exists for futures only")
    if kind in INTERVAL_KINDS and interval not in INTERVALS:
        raise ValueError(f"unknown interval {interval!r}")
    if market != "spot" and kind in INTERVAL_KINDS and interval == "1s":
        raise ValueError("1s klines exist for spot only")
    iv = interval if kind in INTERVAL_KINDS else ""
    sym = symbol.upper()
    yesterday = date.today() - timedelta(days=1)
    end = min(end, yesterday)
    parts: list[Part] = []
    for m in _months(start, end):
        last = date(m.year, m.month, calendar.monthrange(m.year, m.month)[1])
        whole_month_past = last < date.today().replace(day=1)
        if kind in DAILY_ONLY or (not whole_month_past and kind not in MONTHLY_ONLY):
            d = max(m, start)
            while d <= min(last, end):
                parts.append(Part(market, kind, sym, iv, d.isoformat()))
                d += timedelta(days=1)
        elif whole_month_past:
            parts.append(Part(market, kind, sym, iv, m.strftime("%Y-%m")))
    return parts


def _to_datetime(series: pd.Series) -> pd.Series:
    """
    Epoch ms, epoch us (spot since 2025) or a date string -> naive UTC.

    metrics and bookDepth write "2023-01-01 00:05:00"; everything else writes
    epoch numbers.
    """
    v = pd.to_numeric(series, errors="coerce")
    if v.notna().mean() < 0.5:
        return pd.to_datetime(series, errors="coerce")
    return pd.to_datetime(v.where(v <= 10**14, v // 1000), unit="ms", errors="coerce")


def _is_header(cell) -> bool:
    text = str(cell)
    return any(ch.isalpha() for ch in text) and not any(ch.isdigit() for ch in text)


def parse_csv(kind: str, raw: bytes) -> pd.DataFrame:
    """
    One archive CSV into typed columns plus a `ts` datetime column.

    Futures files carry a header row, older spot files do not; both are read
    headerless and a header row, if present, is dropped.
    """
    cols = COLUMNS[kind]
    df = pd.read_csv(io.BytesIO(raw), header=None, dtype=str)
    if not df.empty and _is_header(df.iloc[0, 0]):
        df = df.iloc[1:]
    if df.empty:
        return pd.DataFrame(columns=[c for c in cols if c != "ignore"] + ["ts"])
    df = df.iloc[:, :len(cols)]
    df.columns = cols[:df.shape[1]]
    df["ts"] = _to_datetime(df[TIME_COL[kind]])
    for c in list(df.columns):
        if c in ("ts", "symbol"):
            continue
        if c == "is_buyer_maker":
            df[c] = df[c].astype(str).str.lower().eq("true")
        elif c == TIME_COL[kind] and kind in ("metrics", "bookDepth"):
            df = df.drop(columns=[c])
        else:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.drop(columns=[c for c in ("ignore", "symbol") if c in df.columns])
    return df.dropna(subset=["ts"]).reset_index(drop=True)


def unzip_verified(blob: bytes, checksum_text: str | None) -> bytes:
    """The CSV inside a zip, after checking Binance's published SHA-256."""
    if checksum_text:
        want = checksum_text.strip().split()[0].lower()
        got = hashlib.sha256(blob).hexdigest()
        if want and got != want:
            raise ValueError(f"checksum mismatch: {got[:12]} != {want[:12]}")
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        return z.read(z.namelist()[0])


def write_month(root: Path, part: Part, frames: list[pd.DataFrame]) -> Path | None:
    """Merge a month's frames (existing file included), dedupe on time, write."""
    frames = [f for f in frames if f is not None and not f.empty]
    if not frames:
        return None
    path = lake_path(root, part.market, part.kind, part.symbol, part.interval, part.month)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        frames.insert(0, pd.read_parquet(path))
    df = pd.concat(frames, ignore_index=True)
    key = ["ts", "percentage"] if part.kind == "bookDepth" else ["ts"]
    if part.kind in ("aggTrades", "trades"):
        key = [df.columns[0]]
    df = df.drop_duplicates(subset=key, keep="last").sort_values(key).reset_index(drop=True)
    df.to_parquet(path, index=False, compression="zstd")
    return path


def read(kind: str, symbol: str, start, end, interval: str = "", market: str = "um",
         root: Path = LAKE, columns: list[str] | None = None) -> pd.DataFrame:
    """Everything the lake holds for [start, end), read month by month."""
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    frames = []
    for m in _months(start.date(), end.date()):
        p = lake_path(root, market, kind, symbol, interval, m.strftime("%Y-%m"))
        if p.exists():
            frames.append(pd.read_parquet(p, columns=columns))
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    return df[(df["ts"] >= start) & (df["ts"] < end)].reset_index(drop=True)


def estimate_mb(kind: str, interval: str, years: float, symbols: int) -> float:
    per = MB_PER_YEAR.get((kind, interval), MB_PER_YEAR.get(kind, 1.0))
    if kind in INTERVAL_KINDS and (kind, interval) not in MB_PER_YEAR:
        per = MB_PER_YEAR.get(kind, 20) if kind != "klines" else 0.5
    return per * years * symbols
