"""
Freqtrade community strategies, ported, as benchmarks for v2.

Six strategies from github.com/freqtrade/freqtrade-strategies, with their
published parameters (entry/exit rules, timeframe, minimal_roi, stoploss,
trailing), run on the same Binance data and costs as v2. If a well-known
public strategy beats our own, that is worth knowing before anything else.

Porting notes, so the numbers can be trusted as far as they go:
  * Indicators are re-implemented in pandas (the originals use TA-Lib and
    qtpylib, which are not installed here). RSI, ADX and DI use Wilder
    smoothing like TA-Lib; EMA is a plain exponential average (TA-Lib seeds
    it with an SMA, which only differs in the first bars). Bollinger bands
    use qtpylib's rolling std (ddof=1) on typical price.
  * Execution follows Freqtrade's backtester: signals on a candle's close,
    entry at the NEXT candle's open, stoploss checked before ROI within a
    candle, ROI thresholds by minutes since entry, exit signals filled at the
    next open (and only in profit where the strategy says exit_profit_only).
  * All are long-only spot strategies at 1x, as published. Results are
    percent of the position per trade, after taker fees with GST both ways.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

# ── Indicators ───────────────────────────────────────────────────────────────


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def rsi(s: pd.Series, n: int = 14) -> pd.Series:
    d = s.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def typical(df: pd.DataFrame) -> pd.Series:
    return (df["high"] + df["low"] + df["close"]) / 3


def bbands(s: pd.Series, n: int = 20, k: float = 2.0):
    mid = s.rolling(n).mean()
    sd = s.rolling(n).std()
    return mid - k * sd, mid, mid + k * sd


def _wilder(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def dmi(df: pd.DataFrame, n: int = 14):
    """(plus_di, minus_di, adx), Wilder-smoothed like TA-Lib."""
    up = df["high"].diff()
    dn = -df["low"].diff()
    plus = np.where((up > dn) & (up > 0), up, 0.0)
    minus = np.where((dn > up) & (dn > 0), dn, 0.0)
    prev = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - prev).abs(),
                    (df["low"] - prev).abs()], axis=1).max(axis=1)
    atr_ = _wilder(tr, n)
    pdi = 100 * _wilder(pd.Series(plus, index=df.index), n) / atr_
    mdi = 100 * _wilder(pd.Series(minus, index=df.index), n) / atr_
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return pdi, mdi, _wilder(dx, n)


def macd(s: pd.Series, fast=12, slow=26, sig=9):
    m = ema(s, fast) - ema(s, slow)
    return m, ema(m, sig)


def stochf(df: pd.DataFrame, k: int = 5, d: int = 3):
    lo, hi = df["low"].rolling(k).min(), df["high"].rolling(k).max()
    fastk = 100 * (df["close"] - lo) / (hi - lo).replace(0, np.nan)
    return fastk, fastk.rolling(d).mean()


def tema(s: pd.Series, n: int) -> pd.Series:
    e1 = ema(s, n)
    e2 = ema(e1, n)
    return 3 * e1 - 3 * e2 + ema(e2, n)


def supertrend_dir(df: pd.DataFrame, period: int, mult: float) -> np.ndarray:
    """+1 up / -1 down, the usual final-band construction (ftt.supertrend)."""
    prev = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - prev).abs(),
                    (df["low"] - prev).abs()], axis=1).max(axis=1)
    atr_ = tr.rolling(period).mean().to_numpy()
    hl2 = ((df["high"] + df["low"]) / 2).to_numpy()
    c = df["close"].to_numpy()
    ub, lb = hl2 + mult * atr_, hl2 - mult * atr_
    fub, flb = ub.copy(), lb.copy()
    out = np.zeros(len(df))
    for i in range(1, len(df)):
        if np.isnan(atr_[i]):
            continue
        fub[i] = ub[i] if (np.isnan(fub[i - 1]) or ub[i] < fub[i - 1] or c[i - 1] > fub[i - 1]) \
            else fub[i - 1]
        flb[i] = lb[i] if (np.isnan(flb[i - 1]) or lb[i] > flb[i - 1] or c[i - 1] < flb[i - 1]) \
            else flb[i - 1]
        prev_dir = out[i - 1] if out[i - 1] != 0 else 1
        if prev_dir == 1 and c[i] < flb[i]:
            out[i] = -1
        elif prev_dir == -1 and c[i] > fub[i]:
            out[i] = 1
        else:
            out[i] = prev_dir
    return out


# ── Strategies (entry, exit) as published ────────────────────────────────────

@dataclass(frozen=True)
class Strat:
    name: str
    timeframe: str
    minimal_roi: dict           # minutes -> profit fraction
    stoploss: float
    signals: object             # df -> (enter: bool array, exit: bool array)
    exit_profit_only: bool = False
    trailing_positive: float | None = None
    trailing_offset: float = 0.0
    source: str = ""
    notes: dict = field(default_factory=dict)


def _bbandrsi(df):
    lo, mid, up = bbands(typical(df), 20, 2)
    r = rsi(df["close"], 14)
    return (r < 30) & (df["close"] < lo), r > 70


def _cluc(df):
    lo, mid, up = bbands(typical(df), 20, 2)
    ema100 = ema(df["close"], 50)          # named ema100 in the original, period 50
    enter = ((df["close"] < ema100) & (df["close"] < 0.985 * lo)
             & (df["volume"] < df["volume"].rolling(30).mean().shift(1) * 20))
    return enter, df["close"] > mid


def _binh_cluc(df):
    mid40 = df["close"].rolling(40).mean().fillna(0)
    lower40 = (df["close"].rolling(40).mean() - 2 * df["close"].rolling(40).std()).fillna(0)
    bbdelta = (mid40 - lower40).abs()
    closedelta = (df["close"] - df["close"].shift()).abs()
    tail = (df["close"] - df["low"]).abs()
    lo, mid, up = bbands(typical(df), 20, 2)
    binh = (lower40.shift().gt(0) & bbdelta.gt(df["close"] * 0.008)
            & closedelta.gt(df["close"] * 0.0175) & tail.lt(bbdelta * 0.25)
            & df["close"].lt(lower40.shift()) & df["close"].le(df["close"].shift()))
    cluc = ((df["close"] < ema(df["close"], 50)) & (df["close"] < 0.985 * lo)
            & (df["volume"] < df["volume"].rolling(30).mean().shift(1) * 20))
    return binh | cluc, df["close"] > mid


def _s005(df):
    r = rsi(df["close"], 14)
    fisher = np.tanh(0.1 * (r - 50))                # (e^2x - 1)/(e^2x + 1)
    fisher_norma = 50 * (fisher + 1)
    fastk, fastd = stochf(df)
    m, _ = macd(df["close"])
    _, mdi, _ = dmi(df)
    enter = ((df["volume"] > df["volume"].rolling(150).mean() * 4)
             & (df["close"] < df["close"].rolling(40).mean()) & (fastd > fastk)
             & (r > 26) & (fastd > 1) & (fisher_norma < 5))
    crossed = (r > 74) & (r.shift(1) <= 74)
    return enter, crossed & (m < 0) & (mdi > 4)


def _supertrend(df):
    up = ((supertrend_dir(df, 8, 4) == 1) & (supertrend_dir(df, 9, 7) == 1)
          & (supertrend_dir(df, 8, 1) == 1) & (df["volume"] > 0))
    down = ((supertrend_dir(df, 16, 1) == -1) & (supertrend_dir(df, 18, 3) == -1)
            & (supertrend_dir(df, 18, 6) == -1) & (df["volume"] > 0))
    return pd.Series(up, index=df.index), pd.Series(down, index=df.index)


def _multima(df):
    def cond(count, gap, op):
        keys = [k * gap for k in range(count)]
        conds = []
        for k in range(count):
            key, past = keys[k], (k - 1) * gap
            if past > 1 and key > 1:
                a, b = tema(df["close"], key), tema(df["close"], past)
                conds.append(a < b if op == "lt" else a > b)
        return conds
    buy = cond(4, 15, "lt")
    sell = cond(12, 68, "gt")
    enter = np.logical_and.reduce(buy) if buy else np.zeros(len(df), bool)
    exit_ = np.logical_or.reduce(sell) if sell else np.zeros(len(df), bool)
    return pd.Series(enter, index=df.index), pd.Series(exit_, index=df.index)


REPO = "github.com/freqtrade/freqtrade-strategies"
STRATEGIES = [
    Strat("BbandRsi", "1h", {0: 0.10}, -0.25, _bbandrsi, source=REPO),
    Strat("ClucMay72018", "5m", {0: 0.01}, -0.05, _cluc, source=REPO),
    Strat("CombinedBinHAndCluc", "5m", {0: 0.05}, -0.05, _binh_cluc,
          exit_profit_only=True, source=REPO),
    Strat("Strategy005", "5m", {0: 0.05, 20: 0.04, 40: 0.03, 80: 0.02, 1440: 0.01}, -0.10,
          _s005, exit_profit_only=True, source=REPO),
    Strat("Supertrend", "1h", {0: 0.087, 372: 0.058, 861: 0.029, 2221: 0.0}, -0.265,
          _supertrend, trailing_positive=0.05, trailing_offset=0.144, source=REPO),
    Strat("MultiMa", "4h", {0: 0.523, 1553: 0.123, 2332: 0.076, 3169: 0.0}, -0.345,
          _multima, source=REPO),
]


# ── Freqtrade-style backtest ─────────────────────────────────────────────────

MINUTES = {"5m": 5, "15m": 15, "1h": 60, "4h": 240}


def _roi_at(roi: dict, minutes: float) -> float | None:
    keys = [k for k in sorted(roi) if k <= minutes]
    return roi[keys[-1]] if keys else None


def run_strategy(st: Strat, df: pd.DataFrame, fee: float = 0.0005 * 1.18,
                 stop_slip: float = 0.0005) -> list[dict]:
    """Trades of one long-only strategy on one symbol's bars of its timeframe."""
    if len(df) < 200:
        return []
    df = df.reset_index(drop=True)
    enter, exit_ = st.signals(df)
    enter = np.asarray(pd.Series(enter).fillna(False), bool)
    exit_ = np.asarray(pd.Series(exit_).fillna(False), bool)
    o, h, lo, c = (df[x].to_numpy(float) for x in ("open", "high", "low", "close"))
    ts = df["ts"].to_numpy()
    step = MINUTES[st.timeframe]
    trades, i, n = [], 1, len(df)
    while i < n:
        if not enter[i - 1]:
            i += 1
            continue
        entry, t0 = o[i], i                     # next candle's open
        stop = entry * (1 + st.stoploss)
        peak = entry
        exit_px, reason, j = None, "", i
        while j < n:
            held = (j - t0) * step
            # Trailing (Supertrend): once the offset is reached, trail below the high.
            if st.trailing_positive is not None and peak >= entry * (1 + st.trailing_offset):
                stop = max(stop, peak * (1 - st.trailing_positive))
            if lo[j] <= stop:                    # stoploss first, as Freqtrade does
                exit_px, reason = min(o[j], stop) * (1 - stop_slip), "stoploss"
                break
            roi = _roi_at(st.minimal_roi, held)
            if roi is not None and h[j] >= entry * (1 + roi):
                exit_px, reason = max(o[j], entry * (1 + roi)), "roi"
                break
            peak = max(peak, h[j])
            if exit_[j] and j + 1 < n and (not st.exit_profit_only or c[j] > entry * (1 + 2 * fee)):
                exit_px, reason, j = o[j + 1], "exit_signal", j + 1
                break
            j += 1
        if exit_px is None:
            break
        pct = (exit_px / entry - 1) - 2 * fee
        trades.append({"entry_at": str(pd.Timestamp(ts[t0])), "exit_at": str(pd.Timestamp(ts[j])),
                       "pct": round(float(pct) * 100, 4), "reason": reason})
        i = j + 1
    return trades


def summarise(trades: list[dict]) -> dict:
    if not trades:
        return {"trades": 0}
    p = np.array([t["pct"] for t in trades]) / 100
    wins, losses = p[p > 0], p[p <= 0]
    return {"trades": len(p), "win_rate": round(float(len(wins) / len(p)), 3),
            "avg_pct": round(float(p.mean() * 100), 3),
            "avg_win_pct": round(float(wins.mean() * 100), 3) if len(wins) else 0.0,
            "avg_loss_pct": round(float(losses.mean() * 100), 3) if len(losses) else 0.0,
            "profit_factor": (round(float(wins.sum() / -losses.sum()), 3)
                              if len(losses) and losses.sum() < 0 else None),
            "compounded_pct": round(float((np.prod(1 + p) - 1) * 100), 2)}


def resample(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    return (df.set_index("ts").resample(rule).agg({"open": "first", "high": "max", "low": "min",
                                                   "close": "last", "volume": "sum"})
            .dropna().reset_index())


def run_all(frames_by_symbol: dict, start) -> dict:
    """{strategy: summary} over all symbols; frames need 5m, 15m and 4h."""
    out = {}
    for st in STRATEGIES:
        all_trades = []
        for frames in frames_by_symbol.values():
            if st.timeframe == "1h":
                df = resample(frames["15m"], "1h")
            else:
                df = frames[st.timeframe]
            trades = [t for t in run_strategy(st, df) if pd.Timestamp(t["entry_at"]) >= start]
            all_trades += trades
        out[st.name] = dict(summarise(all_trades), timeframe=st.timeframe, source=st.source)
    return out
