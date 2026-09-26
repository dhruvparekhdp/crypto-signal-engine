"""
The v2 setups: 15m structure decides, 5m triggers, fees decide what is allowed.

From the research's recommended design (section 7.2). Four setups, each
judged on its own, all long/short mirrored:

  A  trend pullback     15m trend (HH/HL) intact, price pulls back to the last
                        higher low (or session VWAP), a 5m bullish trigger
                        (engulfing, pin, or 5m change of character up)
  B  sweep & reclaim    a 5m bar wicks through the previous day's low or the
                        last 15m swing low and closes back above it on volume
  C  range fade         15m has made no break for 24+ bars; a rejection at the
                        range edge, targeting the middle
  D  breakout-retest    15m broke a swing high in the last 8 bars; price comes
                        back to the level, holds it, and closes above

Hard filters on every candidate:
  * higher-timeframe bias: no longs when the daily close is under its SMA20
    AND 4h structure is down; no shorts in the mirror case
  * reward >= 1.5R to the first target, target >= 3x the round-trip cost,
    stop >= 1.5x the round-trip cost
  * weekday 07:00-17:00 UTC only (analysis.protections.in_session)
  * funding not crowded against the trade (z-score < 2 over ~30 days)

Nothing here uses a bar that had not closed at decision time. A 15m bar is
visible to a 5m bar only once the 15m bar's close time has passed.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from analysis.structure import atr, structure_states

SETUPS = ("A", "B", "C", "D")
SETUP_NAMES = {"A": "trend_pullback", "B": "sweep_reclaim", "C": "range_fade",
               "D": "breakout_retest"}


@dataclass(frozen=True)
class V2Config:
    setups: tuple = SETUPS
    round_trip: float = 0.00118        # taker both ways with GST, as a fraction
    min_rr: float = 1.5
    min_target_cost_multiple: float = 3.0
    min_stop_cost_multiple: float = 1.5
    session_filter: bool = True
    session_start_utc: int = 7
    session_end_utc: int = 17
    weekdays_only: bool = True
    funding_z_max: float = 2.0
    trigger_vol_mult: float = 1.2
    sweep_vol_mult: float = 1.5
    range_min_bars: int = 24
    retest_max_bars: int = 8
    stop_atr_buffer: float = 0.25
    # Research variants (26 Sep), all off by default and measured side by
    # side in the report. Thresholds are round numbers, not tuned.
    premium_discount: bool = False   # A/D: long only in the lower half of the 15m swing
                                     # range, short only in the upper half
    htf_strict: bool = False         # the 4h structure must AGREE (or be a range with
                                     # the daily on side), not merely not oppose
    news_blackout: bool = False      # no entries around FOMC and US jobs reports
    swing_alternate: bool = False    # alternating swings on 15m ...
    min_swing_atr: float = 0.0       # ... at least this many ATR apart (0.75 in the variant)
    displacement_atr: float = 0.0    # D: the 15m break bar's body >= this x ATR
    entry_depth: float = 0.0         # limit this fraction of the trigger bar deeper
    regime_routing: bool = False     # A/D only when 15m choppiness < 50, C only when > 50;
                                     # nothing when 15m ATR% is above its 95th percentile


@dataclass
class Candidate:
    ts: pd.Timestamp        # decision time: close of the 5m trigger bar
    symbol: str
    setup: str
    side: str               # "long" | "short"
    entry: float
    stop: float
    target: float
    notes: dict = field(default_factory=dict)

    @property
    def rr(self) -> float:
        risk = abs(self.entry - self.stop)
        return abs(self.target - self.entry) / risk if risk > 0 else 0.0


# ── Aligning timeframes without peeking ──────────────────────────────────────

def _close_times(df: pd.DataFrame, minutes: float) -> np.ndarray:
    return (df["ts"] + pd.Timedelta(minutes=minutes)).to_numpy()


def align(lower_close: np.ndarray, higher: pd.DataFrame, higher_minutes: float) -> np.ndarray:
    """Per lower-TF close time: the last higher-TF bar closed by then (-1 = none)."""
    hc = _close_times(higher, higher_minutes)
    return np.searchsorted(hc, lower_close, side="right") - 1


def daily_bias(k1d: pd.DataFrame) -> pd.DataFrame:
    """Per daily bar: close and SMA20 of closes, both known at that day's close."""
    d = k1d[["ts", "close"]].copy()
    d["sma20"] = d["close"].rolling(20, min_periods=20).mean()
    return d


FUNDING_STD_FLOOR = 0.00005


def funding_z(funding: pd.DataFrame | None, window: int = 90) -> pd.DataFrame | None:
    """Z-score of each funding print against the previous `window` prints (~30 days)."""
    if funding is None or funding.empty:
        return None
    f = funding[["ts", "last_funding_rate"]].sort_values("ts").reset_index(drop=True)
    past = f["last_funding_rate"].shift(1).rolling(window, min_periods=window // 3)
    # Most pairs sit at exactly 0.01% for weeks, so the spread is 0 and a
    # one-tick change scored +-infinity, blocking a whole side. The floor
    # (0.005% per 8h) means only a real move in funding counts as crowding.
    f["z"] = (f["last_funding_rate"] - past.mean()) / past.std().clip(lower=FUNDING_STD_FLOOR)
    return f


# ── Candidate generation ─────────────────────────────────────────────────────

def choppiness(df: pd.DataFrame, n: int = 14) -> pd.Series:
    """Choppiness index: ~100 = sideways, ~0 = one-way. 50 splits trend from range."""
    prev = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - prev).abs(),
                    (df["low"] - prev).abs()], axis=1).max(axis=1)
    span = df["high"].rolling(n).max() - df["low"].rolling(n).min()
    return (100 * np.log10(tr.rolling(n).sum() / span.replace(0, np.nan))
            / np.log10(n)).reset_index(drop=True)


def _in_session(ts: pd.Timestamp, cfg: V2Config) -> bool:
    if cfg.weekdays_only and ts.weekday() >= 5:
        return False
    return cfg.session_start_utc <= ts.hour < cfg.session_end_utc


def _passes_costs(side: str, entry: float, stop: float, target: float,
                  cfg: V2Config) -> bool:
    if entry <= 0 or any(np.isnan(x) for x in (stop, target)):
        return False
    sign = 1.0 if side == "long" else -1.0
    risk, reward = sign * (entry - stop), sign * (target - entry)
    if risk <= 0 or reward <= 0:
        return False
    return (reward / risk >= cfg.min_rr
            and reward / entry >= cfg.min_target_cost_multiple * cfg.round_trip
            and risk / entry >= cfg.min_stop_cost_multiple * cfg.round_trip)


def _vol_ratio(v: np.ndarray, i: int) -> float:
    past = v[max(0, i - 20):i]
    if len(past) < 5:
        return 0.0
    med = float(np.median(past))
    return v[i] / med if med > 0 else 0.0


def generate(symbol: str, k5: pd.DataFrame, k15: pd.DataFrame, k4h: pd.DataFrame,
             k1d: pd.DataFrame, funding: pd.DataFrame | None = None,
             cfg: V2Config = V2Config()) -> list[Candidate]:
    """Every candidate the rules produce over the data, in time order."""
    if len(k5) < 50 or len(k15) < 50:
        return []
    k5 = k5.reset_index(drop=True)
    k15 = k15.reset_index(drop=True)
    o, h, lo, c, v = (k5[x].to_numpy(dtype=float) for x in ("open", "high", "low",
                                                            "close", "volume"))
    ts5 = k5["ts"]
    close5 = _close_times(k5, 5)
    a5 = atr(k5).to_numpy()
    s5 = structure_states(k5)
    ev5 = s5["event"].to_numpy()

    s15 = structure_states(k15, alternate=cfg.swing_alternate,
                           min_swing_atr=cfg.min_swing_atr)
    a15 = atr(k15).to_numpy()
    j15 = align(close5, k15, 15)
    st15 = s15["state"].to_numpy()
    ev15 = s15["event"].to_numpy()
    sh15, sl15 = s15["last_sh"].to_numpy(), s15["last_sl"].to_numpy()
    hl15, lh15 = s15["last_hl"].to_numpy(), s15["last_lh"].to_numpy()
    since15 = s15["bars_since_event"].to_numpy()
    h15, l15 = k15["high"].to_numpy(), k15["low"].to_numpy()

    st4 = structure_states(k4h)["state"].to_numpy() if len(k4h) >= 20 else None
    j4 = align(close5, k4h, 240) if st4 is not None else None
    db = daily_bias(k1d) if len(k1d) >= 21 else None
    jd = align(close5, k1d, 1440) if db is not None else None
    fz = funding_z(funding)
    jf = (np.searchsorted(fz["ts"].to_numpy(), close5, side="right") - 1
          if fz is not None else None)

    # Previous UTC day high/low, from the 5m bars themselves.
    day = ts5.dt.floor("D")
    daily = k5.groupby(day).agg(pdh=("high", "max"), pdl=("low", "min")).shift(1)
    pdh = daily["pdh"].reindex(day.to_numpy()).to_numpy()
    pdl = daily["pdl"].reindex(day.to_numpy()).to_numpy()

    tp = (k5["high"] + k5["low"] + k5["close"]) / 3
    vwap = ((tp * k5["volume"]).groupby(day).cumsum()
            / k5["volume"].groupby(day).cumsum().replace(0, np.nan)).to_numpy()

    o15, c15 = k15["open"].to_numpy(), k15["close"].to_numpy()
    chop = choppiness(k15).to_numpy() if cfg.regime_routing else None
    hot = None
    if cfg.regime_routing:
        atr_pct = pd.Series(a15) / k15["close"].reset_index(drop=True)
        hot = (atr_pct > atr_pct.rolling(2880, min_periods=500).quantile(0.95)).to_numpy()
    blackout = None
    if cfg.news_blackout and len(k5):
        from analysis.event_calendar import blackout_windows
        wins = blackout_windows(pd.Timestamp(close5[0]).to_pydatetime(),
                                pd.Timestamp(close5[-1]).to_pydatetime())
        blackout = (np.array([np.datetime64(a) for a, _ in wins], dtype="datetime64[ns]"),
                    np.array([np.datetime64(b) for _, b in wins], dtype="datetime64[ns]"))

    out: list[Candidate] = []
    for i in range(30, len(k5)):
        j = j15[i]
        if j < 30 or np.isnan(a15[j]) or np.isnan(a5[i]):
            continue
        t = pd.Timestamp(close5[i])
        if cfg.session_filter and not _in_session(t, cfg):
            continue
        if blackout is not None and len(blackout[0]):
            w = np.searchsorted(blackout[0], close5[i], side="right") - 1
            if w >= 0 and close5[i] <= blackout[1][w]:
                continue
        if hot is not None and hot[j]:
            continue
        trend_ok = chop is None or (not np.isnan(chop[j]) and chop[j] < 50)
        range_ok = chop is None or (not np.isnan(chop[j]) and chop[j] > 50)

        # Higher-timeframe bias.
        allow_long = allow_short = True
        if db is not None and jd[i] >= 0 and not np.isnan(db["sma20"].iloc[jd[i]]):
            below = db["close"].iloc[jd[i]] < db["sma20"].iloc[jd[i]]
            four = st4[j4[i]] if st4 is not None and j4[i] >= 0 else "range"
            if below and four == "down":
                allow_long = False
            if not below and four == "up":
                allow_short = False
            if cfg.htf_strict:
                allow_long = allow_long and (four == "up" or (four == "range" and not below))
                allow_short = allow_short and (four == "down" or (four == "range" and below))
        if fz is not None and jf[i] >= 0:
            z = fz["z"].iloc[jf[i]]
            if not np.isnan(z):
                if z >= cfg.funding_z_max:
                    allow_long = False
                if z <= -cfg.funding_z_max:
                    allow_short = False

        atr15 = a15[j]
        rng = h[i] - lo[i]
        body = abs(c[i] - o[i])
        green, red = c[i] > o[i], c[i] < o[i]
        prev_red = c[i - 1] < o[i - 1]
        prev_green = c[i - 1] > o[i - 1]
        bull_engulf = green and prev_red and c[i] >= o[i - 1] and o[i] <= c[i - 1]
        bear_engulf = red and prev_green and c[i] <= o[i - 1] and o[i] >= c[i - 1]
        lower_wick = min(o[i], c[i]) - lo[i]
        upper_wick = h[i] - max(o[i], c[i])
        bull_pin = rng > 0 and lower_wick >= 2 * body and lower_wick >= 0.6 * rng
        bear_pin = rng > 0 and upper_wick >= 2 * body and upper_wick >= 0.6 * rng
        vr = _vol_ratio(v, i)
        vol_ok = vr >= cfg.trigger_vol_mult
        bull_trigger = (bull_engulf or bull_pin or ev5[i] == "choch_up") and vol_ok
        bear_trigger = (bear_engulf or bear_pin or ev5[i] == "choch_down") and vol_ok
        entry = c[i]
        swing_range = sh15[j] - sl15[j] if not (np.isnan(sh15[j]) or np.isnan(sl15[j])) else 0

        def add(setup, side, stop, target, **notes):
            if setup not in cfg.setups:
                return
            px = entry
            if cfg.entry_depth > 0:
                px = entry - cfg.entry_depth * rng if side == "long" \
                    else entry + cfg.entry_depth * rng
            if cfg.premium_discount and setup in ("A", "D") and swing_range > 0:
                where = (px - sl15[j]) / swing_range
                if (side == "long" and where > 0.5) or (side == "short" and where < 0.5):
                    return
            if cfg.regime_routing and ((setup in ("A", "D") and not trend_ok)
                                       or (setup == "C" and not range_ok)):
                return
            if _passes_costs(side, px, stop, target, cfg):
                out.append(Candidate(t, symbol, setup, side, float(px), float(stop),
                                     float(target), notes))

        # A. trend pullback to the last higher low / lower high, or VWAP.
        if st15[j] == "up" and allow_long and bull_trigger and not np.isnan(hl15[j]):
            zone_ok = (lo[i] <= hl15[j] + 0.5 * atr15 and lo[i] >= hl15[j] - 0.5 * atr15) \
                or (not np.isnan(vwap[i]) and lo[i] <= vwap[i] <= h[i])
            if zone_ok and not np.isnan(sh15[j]):
                stop = min(hl15[j], lo[i]) - cfg.stop_atr_buffer * atr15
                add("A", "long", stop, sh15[j], level=float(hl15[j]))
        if st15[j] == "down" and allow_short and bear_trigger and not np.isnan(lh15[j]):
            zone_ok = (h[i] >= lh15[j] - 0.5 * atr15 and h[i] <= lh15[j] + 0.5 * atr15) \
                or (not np.isnan(vwap[i]) and lo[i] <= vwap[i] <= h[i])
            if zone_ok and not np.isnan(sl15[j]):
                stop = max(lh15[j], h[i]) + cfg.stop_atr_buffer * atr15
                add("A", "short", stop, sl15[j], level=float(lh15[j]))

        # B. sweep & reclaim of the previous day's extreme or the last 15m swing.
        if vr >= cfg.sweep_vol_mult:
            for level in (pdl[i], sl15[j]):
                if allow_long and not np.isnan(level) and lo[i] < level \
                        and lo[i] >= level - atr15 and c[i] > level:
                    stop = lo[i] - 0.2 * atr15
                    add("B", "long", stop, entry + 2 * (entry - stop), level=float(level))
                    break
            for level in (pdh[i], sh15[j]):
                if allow_short and not np.isnan(level) and h[i] > level \
                        and h[i] <= level + atr15 and c[i] < level:
                    stop = h[i] + 0.2 * atr15
                    add("B", "short", stop, entry - 2 * (stop - entry), level=float(level))
                    break

        # C. range fade at the edge of a quiet 15m range, target the middle.
        if st15[j] == "range" or since15[j] >= cfg.range_min_bars:
            w0 = max(0, j - cfg.range_min_bars + 1)
            top, bot = h15[w0:j + 1].max(), l15[w0:j + 1].min()
            mid = (top + bot) / 2
            if since15[j] >= cfg.range_min_bars and top > bot:
                if allow_long and lo[i] <= bot + 0.1 * atr15 and (bull_pin or bull_engulf):
                    add("C", "long", bot - cfg.stop_atr_buffer * atr15, mid,
                        range_low=float(bot), range_high=float(top))
                if allow_short and h[i] >= top - 0.1 * atr15 and (bear_pin or bear_engulf):
                    add("C", "short", top + cfg.stop_atr_buffer * atr15, mid,
                        range_low=float(bot), range_high=float(top))

        # D. retest of a level the 15m just broke.
        if since15[j] <= cfg.retest_max_bars:
            k = j - int(since15[j])
            strong = cfg.displacement_atr <= 0 or (
                not np.isnan(a15[k]) and abs(c15[k] - o15[k]) >= cfg.displacement_atr * a15[k])
            if ev15[k] in ("bos_up", "choch_up") and allow_long and strong:
                level = sh15[k]
                if not np.isnan(level) and lo[i] <= level + 0.1 * atr15 \
                        and lo[i] >= level - 0.5 * atr15 and c[i] > level and green:
                    stop = lo[i] - 0.1 * atr15
                    add("D", "long", stop, entry + 2 * (entry - stop), level=float(level))
            if ev15[k] in ("bos_down", "choch_down") and allow_short and strong:
                level = sl15[k]
                if not np.isnan(level) and h[i] >= level - 0.1 * atr15 \
                        and h[i] <= level + 0.5 * atr15 and c[i] < level and red:
                    stop = h[i] + 0.1 * atr15
                    add("D", "short", stop, entry - 2 * (stop - entry), level=float(level))
    return out
