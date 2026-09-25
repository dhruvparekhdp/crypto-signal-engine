"""
Price action as code: the definitions from the research, over OHLCV frames.

Every function takes a pandas DataFrame with columns ts, open, high, low,
close, volume (and optionally taker_buy_volume), one row per CLOSED bar,
oldest first. Nothing here looks at a bar after the one it is judging:

  * a swing at bar i is only known at bar i + k (`confirm`), and every
    consumer uses the confirm index, never i itself. This is the most common
    lookahead bug in structure bots, and a backtest that has it is fiction.
  * per-bar outputs at row i use rows <= i only.

Contents:
  atr                  Wilder ATR
  swings               fractal swing points with their confirm index
  structure_states     per-bar trend state (up/down/range), BOS / CHoCH events,
                       last swing high/low and the last higher-low / lower-high
  sr_zones             swing prices clustered into support/resistance zones
  prev_day_levels      previous UTC day's high, low, close
  session_vwap         VWAP reset at 00:00 UTC
  volume_profile       POC, value-area high and low over a window
  is_sweep_low/high    liquidity sweep + reclaim of a level
  engulfing, pin_bar, inside_bar, candle_momentum   triggers, for use at levels only
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

# ── Volatility ───────────────────────────────────────────────────────────────

def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    """Wilder's average true range. NaN for the first n-1 bars."""
    prev = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - prev).abs(),
                    (df["low"] - prev).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()


# ── Swings ───────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SwingPoint:
    idx: int          # the bar that made the high/low
    confirm: int      # the first bar at which it is known (idx + k)
    price: float
    kind: str         # "high" | "low"


def swings(df: pd.DataFrame, k: int = 2) -> list[SwingPoint]:
    """
    Fractal pivots: bar i is a swing high if its high is above the k bars
    before it and not below the k bars after; lows mirrored. Known at i + k.
    """
    h, lo = df["high"].to_numpy(), df["low"].to_numpy()
    out: list[SwingPoint] = []
    for i in range(k, len(df) - k):
        if h[i] > h[i - k:i].max() and h[i] >= h[i + 1:i + k + 1].max():
            out.append(SwingPoint(i, i + k, float(h[i]), "high"))
        if lo[i] < lo[i - k:i].min() and lo[i] <= lo[i + 1:i + k + 1].min():
            out.append(SwingPoint(i, i + k, float(lo[i]), "low"))
    out.sort(key=lambda s: (s.confirm, s.idx))
    return out


# ── Market structure state machine ───────────────────────────────────────────

def structure_states(df: pd.DataFrame, k: int = 2, buffer_atr: float = 0.0) -> pd.DataFrame:
    """
    Per bar: state ("up" / "down" / "range"), event ("bos_up", "bos_down",
    "choch_up", "choch_down" or ""), last confirmed swing high / low, the
    last higher low (in an uptrend) / lower high (downtrend), and bars since
    the last event.

    BOS    close beyond the last swing in the trend's direction (continuation)
    CHoCH  close beyond the last swing AGAINST the trend (character change)
    From range, the first break sets the state. A swing that has been broken
    is not broken again until a new swing forms.
    """
    n = len(df)
    close = df["close"].to_numpy()
    a = atr(df).to_numpy() if buffer_atr > 0 else np.zeros(n)
    by_confirm: dict[int, list[SwingPoint]] = {}
    for s in swings(df, k):
        by_confirm.setdefault(s.confirm, []).append(s)

    state, last_sh, last_sl = "range", np.nan, np.nan
    sh_broken = sl_broken = True
    prev_sh = prev_sl = np.nan
    hl = lh = np.nan
    since = 0
    rows = []
    for i in range(n):
        for s in by_confirm.get(i, []):
            if s.kind == "high":
                prev_sh, last_sh, sh_broken = last_sh, s.price, False
                if not np.isnan(prev_sh) and s.price < prev_sh:
                    lh = s.price
            else:
                prev_sl, last_sl, sl_broken = last_sl, s.price, False
                if not np.isnan(prev_sl) and s.price > prev_sl:
                    hl = s.price
        buf = (a[i] * buffer_atr) if buffer_atr > 0 and not np.isnan(a[i]) else 0.0
        event = ""
        if not sh_broken and not np.isnan(last_sh) and close[i] > last_sh + buf:
            event = "choch_up" if state == "down" else "bos_up"
            state, sh_broken = "up", True
        elif not sl_broken and not np.isnan(last_sl) and close[i] < last_sl - buf:
            event = "choch_down" if state == "up" else "bos_down"
            state, sl_broken = "down", True
        since = 0 if event else since + 1
        rows.append((state, event, last_sh, last_sl, hl, lh, since))
    return pd.DataFrame(rows, columns=["state", "event", "last_sh", "last_sl",
                                       "last_hl", "last_lh", "bars_since_event"],
                        index=df.index)


# ── Levels ───────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Zone:
    low: float
    high: float
    touches: int
    last_idx: int

    @property
    def mid(self) -> float:
        return (self.low + self.high) / 2


def sr_zones(points: list[SwingPoint], tolerance: float, upto: int | None = None,
             lookback: int = 80, min_touches: int = 2) -> list[Zone]:
    """
    Cluster the last `lookback` confirmed swing prices (confirm <= upto) into
    zones: a price joins a zone within `tolerance` (typically 0.25 x 1h ATR)
    of the zone's running mean. Zones with fewer than `min_touches` drop out.
    """
    pts = [p for p in points if upto is None or p.confirm <= upto][-lookback:]
    pts.sort(key=lambda p: p.price)
    zones: list[list[SwingPoint]] = []
    for p in pts:
        if zones and abs(p.price - np.mean([q.price for q in zones[-1]])) <= tolerance:
            zones[-1].append(p)
        else:
            zones.append([p])
    return [Zone(min(q.price for q in z), max(q.price for q in z), len(z),
                 max(q.idx for q in z))
            for z in zones if len(z) >= min_touches]


def prev_day_levels(df: pd.DataFrame) -> pd.DataFrame:
    """Per bar: previous UTC day's high, low and close (NaN on the first day)."""
    day = df["ts"].dt.floor("D")
    daily = df.groupby(day).agg(high=("high", "max"), low=("low", "min"),
                                close=("close", "last"))
    prev = daily.shift(1)
    out = prev.reindex(day.to_numpy())
    out.index = df.index
    return out.rename(columns={"high": "pdh", "low": "pdl", "close": "pdc"})


def session_vwap(df: pd.DataFrame) -> pd.Series:
    """VWAP of typical price, reset at 00:00 UTC."""
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    day = df["ts"].dt.floor("D")
    pv = (tp * df["volume"]).groupby(day).cumsum()
    vol = df["volume"].groupby(day).cumsum()
    return pv / vol.replace(0, np.nan)


def volume_profile(df: pd.DataFrame, bins: int = 50,
                   value_area: float = 0.70) -> tuple[float, float, float] | None:
    """
    (POC, VAH, VAL) over the rows given. Each bar's volume is spread evenly
    across the bins its high-low range covers. The value area grows from the
    POC, taking the larger neighbour each step, until it holds 70% of volume.
    """
    if df.empty:
        return None
    lo, hi = float(df["low"].min()), float(df["high"].max())
    if hi <= lo:
        return (lo, lo, lo)
    edges = np.linspace(lo, hi, bins + 1)
    vol = np.zeros(bins)
    for low, high, v in zip(df["low"].to_numpy(), df["high"].to_numpy(),
                            df["volume"].to_numpy(), strict=False):
        a = max(0, min(bins - 1, int(np.searchsorted(edges, low, "right") - 1)))
        b = max(0, min(bins - 1, int(np.searchsorted(edges, high, "right") - 1)))
        vol[a:b + 1] += v / (b - a + 1)
    poc = int(vol.argmax())
    total, inside = vol.sum(), vol[poc]
    left = right = poc
    while total > 0 and inside / total < value_area and (left > 0 or right < bins - 1):
        up = vol[right + 1] if right < bins - 1 else -1
        down = vol[left - 1] if left > 0 else -1
        if up >= down:
            right += 1
            inside += up
        else:
            left -= 1
            inside += down
    centre = (edges[:-1] + edges[1:]) / 2
    return float(centre[poc]), float(edges[right + 1]), float(edges[left])


# ── Sweeps and candle triggers ───────────────────────────────────────────────

def _vol_ok(df: pd.DataFrame, i: int, mult: float) -> bool:
    if mult <= 0:
        return True
    past = df["volume"].iloc[max(0, i - 20):i]
    return len(past) >= 5 and df["volume"].iloc[i] >= mult * float(past.median())


def is_sweep_low(df: pd.DataFrame, i: int, level: float, atr_val: float,
                 vol_mult: float = 1.5) -> bool:
    """Bar i trades below `level` by at most 1 ATR, then closes back above it."""
    r = df.iloc[i]
    return (r.low < level and r.low >= level - atr_val and r.close > level
            and _vol_ok(df, i, vol_mult))


def is_sweep_high(df: pd.DataFrame, i: int, level: float, atr_val: float,
                  vol_mult: float = 1.5) -> bool:
    r = df.iloc[i]
    return (r.high > level and r.high <= level + atr_val and r.close < level
            and _vol_ok(df, i, vol_mult))


def engulfing(df: pd.DataFrame, i: int, atr_val: float | None = None) -> str:
    """'bull' / 'bear' / '' — body covers the previous opposite-colour body."""
    if i < 1:
        return ""
    p, c = df.iloc[i - 1], df.iloc[i]
    if atr_val and (c.high - c.low) < atr_val:
        return ""
    if c.close > c.open and p.close < p.open and c.close >= p.open and c.open <= p.close:
        return "bull"
    if c.close < c.open and p.close > p.open and c.close <= p.open and c.open >= p.close:
        return "bear"
    return ""


def pin_bar(df: pd.DataFrame, i: int) -> str:
    """'bull' (long lower wick) / 'bear' / '' — wick >= 2x body and >= 60% of range."""
    c = df.iloc[i]
    rng = c.high - c.low
    if rng <= 0:
        return ""
    body = abs(c.close - c.open)
    lower = min(c.open, c.close) - c.low
    upper = c.high - max(c.open, c.close)
    if lower >= 2 * body and lower >= 0.6 * rng and c.close >= c.low + rng * 2 / 3:
        return "bull"
    if upper >= 2 * body and upper >= 0.6 * rng and c.close <= c.low + rng / 3:
        return "bear"
    return ""


def inside_bar(df: pd.DataFrame, i: int) -> bool:
    if i < 1:
        return False
    return df["high"].iloc[i] < df["high"].iloc[i - 1] and df["low"].iloc[i] > df["low"].iloc[i - 1]


def candle_momentum(df: pd.DataFrame, i: int, n: int = 3) -> float:
    """Net body of the last n bars over their summed ranges: +1 all-up, -1 all-down."""
    w = df.iloc[max(0, i - n + 1):i + 1]
    rng = float((w["high"] - w["low"]).sum())
    return float((w["close"] - w["open"]).sum()) / rng if rng > 0 else 0.0
