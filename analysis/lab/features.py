"""Indicators. Every function uses only data up to and including each bar."""
from __future__ import annotations

import numpy as np
import pandas as pd

from analysis.lab.data import INTERVAL_MS, Bars, load_bars


def ema(x: np.ndarray, n: int) -> np.ndarray:
    return pd.Series(x).ewm(span=n, adjust=False).mean().to_numpy()


def sma(x: np.ndarray, n: int) -> np.ndarray:
    return pd.Series(x).rolling(n).mean().to_numpy()


def std(x: np.ndarray, n: int) -> np.ndarray:
    return pd.Series(x).rolling(n).std(ddof=0).to_numpy()


def rsi(c: np.ndarray, n: int = 14) -> np.ndarray:
    d = pd.Series(c).diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return (100 - 100 / (1 + up / dn.replace(0, np.nan))).fillna(50).to_numpy()


def true_range(h, l, c):
    pc = np.r_[c[0], c[:-1]]
    return np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))


def atr(h, l, c, n: int = 14) -> np.ndarray:
    return pd.Series(true_range(h, l, c)).ewm(alpha=1 / n, adjust=False).mean().to_numpy()


def bollinger(c, n=20, k=2.0):
    m = sma(c, n)
    s = std(c, n)
    return m, m + k * s, m - k * s


def macd(c, fast=12, slow=26, sig=9):
    line = ema(c, fast) - ema(c, slow)
    signal = ema(line, sig)
    return line, signal, line - signal


def rolling_max(x, n):
    return pd.Series(x).rolling(n).max().to_numpy()


def rolling_min(x, n):
    return pd.Series(x).rolling(n).min().to_numpy()


def adx(h, l, c, n: int = 14):
    up = np.diff(h, prepend=h[0])
    dn = -np.diff(l, prepend=l[0])
    pdm = np.where((up > dn) & (up > 0), up, 0.0)
    mdm = np.where((dn > up) & (dn > 0), dn, 0.0)
    a = atr(h, l, c, n)
    pdi = 100 * pd.Series(pdm).ewm(alpha=1 / n, adjust=False).mean().to_numpy() / a
    mdi = 100 * pd.Series(mdm).ewm(alpha=1 / n, adjust=False).mean().to_numpy() / a
    dx = 100 * np.abs(pdi - mdi) / np.where(pdi + mdi == 0, np.nan, pdi + mdi)
    return pd.Series(dx).ewm(alpha=1 / n, adjust=False).mean().fillna(0).to_numpy(), pdi, mdi


def zscore(x, n):
    return (x - sma(x, n)) / np.where(std(x, n) == 0, np.nan, std(x, n))


def htf_series(b: Bars, rule: str, fn) -> np.ndarray:
    """Evaluate `fn(close_array) -> array` on a higher timeframe and align it to `b`
    using only HTF bars that have fully closed by each bar's own close. No look-ahead."""
    htf = load_bars(b.symbol, rule)
    vals = fn(htf)
    # value becomes known at the HTF bar's close; a base bar can read it once its own
    # close time has reached that moment.
    known_at = htf.close_t
    idx = np.searchsorted(known_at, b.close_t, side="right") - 1
    out = np.full(len(b), np.nan)
    ok = idx >= 0
    out[ok] = vals[idx[ok]]
    return out


def daily_vwap(b: Bars) -> np.ndarray:
    tp = (b.h + b.l + b.c) / 3
    day = b.t // 86_400_000
    pv = pd.Series(tp * b.v).groupby(day).cumsum().to_numpy()
    vv = pd.Series(b.v).groupby(day).cumsum().to_numpy()
    return pv / np.where(vv == 0, np.nan, vv)


def heikin_ashi(o, h, l, c):
    hc = (o + h + l + c) / 4
    ho = np.empty_like(hc)
    ho[0] = (o[0] + c[0]) / 2
    for i in range(1, len(hc)):
        ho[i] = (ho[i - 1] + hc[i - 1]) / 2
    return ho, hc


def supertrend(h, l, c, n=10, mult=3.0):
    a = atr(h, l, c, n)
    mid = (h + l) / 2
    ub, lb = mid + mult * a, mid - mult * a
    fub, flb = ub.copy(), lb.copy()
    d = np.ones(len(c), np.int8)
    for i in range(1, len(c)):
        fub[i] = ub[i] if (ub[i] < fub[i - 1] or c[i - 1] > fub[i - 1]) else fub[i - 1]
        flb[i] = lb[i] if (lb[i] > flb[i - 1] or c[i - 1] < flb[i - 1]) else flb[i - 1]
        if d[i - 1] == 1 and c[i] < flb[i]:
            d[i] = -1
        elif d[i - 1] == -1 and c[i] > fub[i]:
            d[i] = 1
        else:
            d[i] = d[i - 1]
    return d


def cross_up(a, b):
    return (a > b) & (np.r_[False, a[:-1] <= b[:-1]])


def cross_dn(a, b):
    return (a < b) & (np.r_[False, a[:-1] >= b[:-1]])


def kama(c: np.ndarray, n: int = 10, fast: int = 2, slow: int = 30) -> np.ndarray:
    """Kaufman adaptive moving average (Kaufman 1995): moves fast in trends, slowly in chop. Causal."""
    c = np.asarray(c, float)
    out = np.full(len(c), np.nan)
    if len(c) <= n:
        return out
    fsc, ssc = 2 / (fast + 1), 2 / (slow + 1)
    out[n] = c[n]
    for i in range(n + 1, len(c)):
        change = abs(c[i] - c[i - n])
        vol = np.abs(np.diff(c[i - n:i + 1])).sum()
        er = change / vol if vol > 0 else 0.0
        sc = (er * (fsc - ssc) + ssc) ** 2
        out[i] = out[i - 1] + sc * (c[i] - out[i - 1])
    return out


def mcginley(c: np.ndarray, n: int = 14) -> np.ndarray:
    """McGinley Dynamic (McGinley 1990): an average that speeds up when price runs away from it. Causal."""
    c = np.asarray(c, float)
    out = np.full(len(c), np.nan)
    if not len(c):
        return out
    out[0] = c[0]
    for i in range(1, len(c)):
        prev = out[i - 1]
        ratio = c[i] / prev if prev > 0 else 1.0
        out[i] = prev + (c[i] - prev) / (n * ratio ** 4) if prev > 0 else c[i]
    return out


def garman_klass(o, h, l, c, n: int = 20) -> np.ndarray:
    """Garman-Klass volatility (1980) from OHLC, averaged over n bars (per-bar, not annualised). Causal."""
    o, h, l, c = (np.asarray(x, float) for x in (o, h, l, c))
    with np.errstate(divide="ignore", invalid="ignore"):
        v = 0.5 * np.log(h / l) ** 2 - (2 * np.log(2) - 1) * np.log(c / o) ** 2
    v = np.where(np.isfinite(v), v, np.nan)
    return np.sqrt(pd.Series(v).rolling(n, min_periods=n).mean().to_numpy())
