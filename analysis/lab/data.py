"""Real market data from the local lake. Never synthesises candles."""
from __future__ import annotations

import glob
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

LAKE = Path("data/lake")
INTERVAL_MS = {"1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
               "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000, "1d": 86_400_000}
RULE = {"1m": "1min", "3m": "3min", "5m": "5min", "15m": "15min", "30m": "30min",
        "1h": "1h", "2h": "2h", "4h": "4h", "1d": "1D"}


class MissingData(RuntimeError):
    pass


@dataclass
class Bars:
    """Column arrays for one symbol and interval. `t` is the bar open time in ms."""
    symbol: str
    interval: str
    t: np.ndarray
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray
    c: np.ndarray
    v: np.ndarray
    tb: np.ndarray                      # taker buy volume
    _lists: dict = field(default_factory=dict, repr=False)

    def __len__(self) -> int:
        return len(self.t)

    @property
    def close_t(self) -> np.ndarray:
        return self.t + INTERVAL_MS[self.interval]

    def lists(self):
        """Python lists for the per-bar trade loop, built once."""
        if not self._lists:
            self._lists = {k: getattr(self, k).tolist() for k in ("t", "o", "h", "l", "c")}
        return self._lists

    def frame(self) -> pd.DataFrame:
        return pd.DataFrame({"open": self.o, "high": self.h, "low": self.l, "close": self.c,
                             "volume": self.v, "taker_buy": self.tb},
                            index=pd.to_datetime(self.t, unit="ms", utc=True))


def _read_dir(root: Path, market: str, symbol: str, interval: str) -> pd.DataFrame:
    files = sorted(glob.glob(str(root / market / "klines" / symbol / interval / "*.parquet")))
    if not files:
        raise MissingData(f"no {interval} klines for {symbol} under {root}")
    cols = ["open_time", "open", "high", "low", "close", "volume", "taker_buy_volume"]
    df = pd.concat([pd.read_parquet(f, columns=cols) for f in files], ignore_index=True)
    df = df.drop_duplicates("open_time").sort_values("open_time").reset_index(drop=True)
    return df


def _to_bars(symbol: str, interval: str, df: pd.DataFrame) -> Bars:
    return Bars(symbol, interval, df["open_time"].to_numpy(np.int64), df["open"].to_numpy(float),
                df["high"].to_numpy(float), df["low"].to_numpy(float), df["close"].to_numpy(float),
                df["volume"].to_numpy(float), df["taker_buy_volume"].to_numpy(float))


@lru_cache(maxsize=64)
def load_bars(symbol: str, interval: str = "1m", root: str = str(LAKE), market: str = "um",
              start_ms: int | None = None, end_ms: int | None = None) -> Bars:
    """Bars for a symbol. Intervals above the stored ones are built from complete
    lower-interval bars only, so a partial bar can never leak in."""
    root_p = Path(root)
    first_month = {}
    for i in ("1m", "5m", "15m", "1h", "4h", "1d"):
        files = sorted(glob.glob(str(root_p / market / "klines" / symbol / i / "*.parquet")))
        if files:
            first_month[i] = Path(files[0]).stem            # "YYYY-MM": cheap and sortable
    have = list(first_month)
    if not have:
        raise MissingData(f"no klines for {symbol} under {root}")
    # Use the stored interval only when it reaches back as far as any finer one; otherwise a
    # short 15m folder would silently cut a 5-year test down to two years.
    finer = [i for i in have if INTERVAL_MS[i] < INTERVAL_MS[interval]
             and INTERVAL_MS[interval] % INTERVAL_MS[i] == 0]
    earliest_finer = min((first_month[i] for i in finer), default=None)
    if interval in have and (earliest_finer is None or first_month[interval] <= earliest_finer):
        df = _read_dir(root_p, market, symbol, interval)
    else:
        if not finer:
            raise MissingData(f"cannot build {interval} for {symbol}; have {have}")
        best = [i for i in finer if first_month[i] == earliest_finer]
        src = max(best, key=lambda i: INTERVAL_MS[i])
        df = _resample(_read_dir(root_p, market, symbol, src), INTERVAL_MS[src], INTERVAL_MS[interval])
    if start_ms is not None:
        df = df[df.open_time >= start_ms]
    if end_ms is not None:
        df = df[df.open_time < end_ms]
    return _to_bars(symbol, interval, df.reset_index(drop=True))


def _resample(df: pd.DataFrame, src_ms: int, dst_ms: int) -> pd.DataFrame:
    need = dst_ms // src_ms
    g = (df.open_time // dst_ms) * dst_ms
    agg = df.groupby(g).agg(open=("open", "first"), high=("high", "max"), low=("low", "min"),
                            close=("close", "last"), volume=("volume", "sum"),
                            taker_buy_volume=("taker_buy_volume", "sum"), n=("open", "size"))
    agg = agg[agg.n == need].drop(columns="n")
    agg.index.name = "open_time"
    return agg.reset_index()


@lru_cache(maxsize=32)
def load_funding(symbol: str, root: str = str(LAKE), market: str = "um") -> tuple[np.ndarray, np.ndarray]:
    """(timestamps ms, rate) sorted. Empty arrays when the symbol has no funding."""
    files = sorted(glob.glob(str(Path(root) / market / "fundingRate" / symbol / "**" / "*.parquet"),
                             recursive=True))
    if not files:
        return np.array([], np.int64), np.array([], float)
    df = pd.concat([pd.read_parquet(f, columns=["calc_time", "last_funding_rate"]) for f in files])
    df = df.drop_duplicates("calc_time").sort_values("calc_time")
    return df["calc_time"].to_numpy(np.int64), df["last_funding_rate"].to_numpy(float)


def available_symbols(root: str = str(LAKE), market: str = "um") -> list[str]:
    return sorted(p.name for p in (Path(root) / market / "klines").glob("*") if p.is_dir())


def coverage(symbol: str, interval: str = "1m", root: str = str(LAKE)) -> dict:
    b = load_bars(symbol, interval, root)
    if not len(b):
        return {"symbol": symbol, "bars": 0}
    return {"symbol": symbol, "interval": interval, "bars": len(b),
            "start": str(pd.to_datetime(b.t[0], unit="ms")), "end": str(pd.to_datetime(b.t[-1], unit="ms")),
            "gaps": int((np.diff(b.t) > INTERVAL_MS[interval]).sum())}
