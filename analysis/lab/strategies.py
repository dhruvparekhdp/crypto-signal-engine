"""Strategy library. A strategy turns bars into +1 / -1 / 0 at each bar CLOSE.

The simulator enters on the next bar's open, so a signal can never use the bar it
trades on. Families: trend, breakout, mean_reversion, volatility, volume, structure,
carry, regime, meta, baseline. Sources: classic (textbook / widely published),
community (popular on TradingView and in trading courses), custom (written for this
engine from what its own data showed).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from itertools import product
from typing import Callable

import numpy as np

from analysis.lab import features as F
from analysis.lab.data import Bars, load_funding


@dataclass(frozen=True)
class Strategy:
    id: str
    name: str
    family: str
    source: str
    desc: str
    fn: Callable[[Bars, dict], np.ndarray]
    defaults: dict = field(default_factory=dict)
    grid: dict = field(default_factory=dict)
    tf: str = "15m"

    def signals(self, b: Bars, params: dict | None = None) -> np.ndarray:
        p = {**self.defaults, **(params or {})}
        s = self.fn(b, p)
        s = np.nan_to_num(s).astype(np.int8)
        s[:max(60, int(p.get("warmup", 0)))] = 0
        return s

    def param_sets(self, limit: int = 12) -> list[dict]:
        if not self.grid:
            return [dict(self.defaults)]
        keys = list(self.grid)
        sets = [dict(zip(keys, v)) for v in product(*[self.grid[k] for k in keys])]
        return [{**self.defaults, **s} for s in sets[:limit]]


REGISTRY: dict[str, Strategy] = {}


def register(id, name, family, source, desc, defaults=None, grid=None, tf="15m"):
    def deco(fn):
        REGISTRY[id] = Strategy(id, name, family, source, desc, fn, defaults or {}, grid or {}, tf)
        return fn
    return deco


def _sig(long, short):
    return np.where(long, 1, np.where(short, -1, 0)).astype(np.int8)


def _htf_trend(b, rule="1h", n=50):
    r = F.htf_series(b, rule, lambda h: np.where(h.c > F.ema(h.c, n), 1.0, -1.0))
    return np.nan_to_num(r)


# ---------------------------------------------------------------- trend
@register("ema_cross", "EMA Cross", "trend", "classic", "Fast EMA crosses slow EMA.",
          {"fast": 20, "slow": 50}, {"fast": [9, 20], "slow": [50, 100]})
def _ema_cross(b, p):
    f, s = F.ema(b.c, p["fast"]), F.ema(b.c, p["slow"])
    return _sig(F.cross_up(f, s), F.cross_dn(f, s))


@register("macd_cross", "MACD Signal Cross", "trend", "classic",
          "MACD crosses its signal line, only with the 200 EMA trend.",
          {"slow_trend": 200}, {"slow_trend": [100, 200]})
def _macd_cross(b, p):
    line, sig, _ = F.macd(b.c)
    t = F.ema(b.c, p["slow_trend"])
    return _sig(F.cross_up(line, sig) & (b.c > t), F.cross_dn(line, sig) & (b.c < t))


@register("supertrend", "Supertrend Flip", "trend", "community",
          "ATR trailing band flips direction.", {"n": 10, "mult": 3.0}, {"n": [10, 14], "mult": [2.0, 3.0]})
def _supertrend(b, p):
    d = F.supertrend(b.h, b.l, b.c, p["n"], p["mult"])
    return _sig((d == 1) & (np.r_[0, d[:-1]] == -1), (d == -1) & (np.r_[0, d[:-1]] == 1))


@register("adx_di", "ADX / DI Trend", "trend", "classic",
          "+DI crosses -DI while ADX shows a real trend.", {"adx_min": 25}, {"adx_min": [20, 25, 30]})
def _adx_di(b, p):
    a, pdi, mdi = F.adx(b.h, b.l, b.c)
    return _sig(F.cross_up(pdi, mdi) & (a > p["adx_min"]), F.cross_dn(pdi, mdi) & (a > p["adx_min"]))


@register("ichimoku", "Ichimoku Cloud", "trend", "classic",
          "Tenkan/Kijun cross on the right side of the cloud.", {})
def _ichimoku(b, p):
    tenkan = (F.rolling_max(b.h, 9) + F.rolling_min(b.l, 9)) / 2
    kijun = (F.rolling_max(b.h, 26) + F.rolling_min(b.l, 26)) / 2
    sa = ((tenkan + kijun) / 2)
    sb = (F.rolling_max(b.h, 52) + F.rolling_min(b.l, 52)) / 2
    # the cloud plotted today was computed 26 bars ago; shift so nothing leaks forward
    sa, sb = np.r_[np.full(26, np.nan), sa[:-26]], np.r_[np.full(26, np.nan), sb[:-26]]
    top, bot = np.fmax(sa, sb), np.fmin(sa, sb)
    return _sig(F.cross_up(tenkan, kijun) & (b.c > top), F.cross_dn(tenkan, kijun) & (b.c < bot))


@register("heikin_trend", "Heikin-Ashi Trend", "trend", "community",
          "Colour flip of Heikin-Ashi candles above/below the 50 EMA.", {})
def _heikin(b, p):
    ho, hc = F.heikin_ashi(b.o, b.h, b.l, b.c)
    up = hc > ho
    t = F.ema(b.c, 50)
    return _sig(up & ~np.r_[False, up[:-1]] & (b.c > t), ~up & np.r_[False, up[:-1]] & (b.c < t))


@register("tsmom", "Time-Series Momentum", "trend", "classic",
          "Go with the sign of the N-bar return, re-checked when it flips (Moskowitz et al.).",
          {"n": 96}, {"n": [48, 96, 192]}, tf="1h")
def _tsmom(b, p):
    r = b.c / np.r_[np.full(p["n"], np.nan), b.c[:-p["n"]]] - 1
    s = np.sign(np.nan_to_num(r))
    flip = s != np.r_[0, s[:-1]]
    return np.where(flip, s, 0)


@register("trend_pullback", "Trend Pullback", "trend", "community",
          "In an hourly trend, buy a dip to the 10 EMA that closes back strong.", {"htf": "1h"})
def _pullback(b, p):
    e = F.ema(b.c, 10)
    tr = _htf_trend(b, p["htf"])
    return _sig((tr > 0) & (b.l <= e * 1.001) & (b.c > e) & (b.c > b.o),
                (tr < 0) & (b.h >= e * 0.999) & (b.c < e) & (b.c < b.o))


@register("nnfx", "NNFX Confluence", "trend", "community",
          "Baseline EMA + MACD + RSI band + volume gate (the 'No Nonsense' recipe).",
          {"base": 50, "vol_mult": 1.1}, {"base": [50, 100], "vol_mult": [1.0, 1.3]})
def _nnfx(b, p):
    base = F.ema(b.c, p["base"])
    line, sig, _ = F.macd(b.c)
    r = F.rsi(b.c)
    vol = b.v > F.sma(b.v, 20) * p["vol_mult"]
    longc = (b.c > base) & (line > sig) & (r > 50) & (r < 70) & vol
    shortc = (b.c < base) & (line < sig) & (r < 50) & (r > 30) & vol
    prev = np.r_[False, longc[:-1]], np.r_[False, shortc[:-1]]
    return _sig(longc & ~prev[0], shortc & ~prev[1])


# ------------------------------------------------------------- breakout
@register("donchian", "Donchian Breakout (Turtle)", "breakout", "classic",
          "Close beyond the N-bar high/low.", {"n": 55}, {"n": [20, 55, 100]})
def _donchian(b, p):
    hi = np.r_[np.nan, F.rolling_max(b.h, p["n"])[:-1]]
    lo = np.r_[np.nan, F.rolling_min(b.l, p["n"])[:-1]]
    return _sig(b.c > hi, b.c < lo)


@register("keltner_break", "Keltner Breakout", "breakout", "classic",
          "Close outside the EMA +/- k*ATR channel.", {"k": 2.0}, {"k": [1.5, 2.0, 2.5]})
def _keltner(b, p):
    m, a = F.ema(b.c, 20), F.atr(b.h, b.l, b.c)
    up, dn = m + p["k"] * a, m - p["k"] * a
    return _sig(F.cross_up(b.c, up), F.cross_dn(b.c, dn))


@register("orb", "Opening Range Breakout", "breakout", "community",
          "Break of the first N minutes of the UTC day.", {"minutes": 60}, {"minutes": [30, 60, 120]}, tf="5m")
def _orb(b, p):
    day = b.t // 86_400_000
    mins = (b.t % 86_400_000) // 60_000
    inr = mins < p["minutes"]
    import pandas as pd
    s = pd.Series
    rh = s(np.where(inr, b.h, np.nan)).groupby(day).transform("max").to_numpy()
    rl = s(np.where(inr, b.l, np.nan)).groupby(day).transform("min").to_numpy()
    after = ~inr
    first_long = F.cross_up(b.c, rh) & after
    first_short = F.cross_dn(b.c, rl) & after
    # one trade per day per side
    fl = s(first_long).groupby(day).cumsum().to_numpy() == 1
    fs = s(first_short).groupby(day).cumsum().to_numpy() == 1
    return _sig(first_long & fl, first_short & fs)


@register("breakout_retest", "Breakout & Retest", "structure", "community",
          "Break of a 20-bar level, then a hold on the retest.", {"n": 20})
def _retest(b, p):
    n = p["n"]
    hi = np.r_[np.nan, F.rolling_max(b.h, n)[:-1]]
    lo = np.r_[np.nan, F.rolling_min(b.l, n)[:-1]]
    hi_old = np.r_[np.full(6, np.nan), hi[:-6]]
    lo_old = np.r_[np.full(6, np.nan), lo[:-6]]
    return _sig((b.c > hi_old) & (b.l <= hi_old * 1.002) & (b.c > b.o),
                (b.c < lo_old) & (b.h >= lo_old * 0.998) & (b.c < b.o))


@register("vol_breakout", "Volume-Confirmed Breakout", "volume", "community",
          "New 30-bar high/low on volume well above normal.", {"z": 2.0}, {"z": [1.5, 2.0, 3.0]})
def _volbreak(b, p):
    z = F.zscore(b.v, 50)
    hi = np.r_[np.nan, F.rolling_max(b.h, 30)[:-1]]
    lo = np.r_[np.nan, F.rolling_min(b.l, 30)[:-1]]
    return _sig((b.c > hi) & (z > p["z"]), (b.c < lo) & (z > p["z"]))


# -------------------------------------------------------- mean reversion
@register("rsi2", "Connors RSI(2)", "mean_reversion", "classic",
          "RSI(2) extreme against a 200 SMA trend filter (Connors & Alvarez).",
          {"lo": 10, "hi": 90}, {"lo": [5, 10, 15]})
def _rsi2(b, p):
    r = F.rsi(b.c, 2)
    t = F.sma(b.c, 200)
    return _sig((r < p["lo"]) & (b.c > t), (r > p["hi"]) & (b.c < t))


@register("rsi_extreme", "RSI 30/70 Reversal", "mean_reversion", "classic",
          "Buy RSI(14) turning up from oversold, sell turning down from overbought.",
          {"lo": 30, "hi": 70}, {"lo": [20, 30], "hi": [70, 80]})
def _rsix(b, p):
    r = F.rsi(b.c, 14)
    return _sig(F.cross_up(r, np.full_like(r, p["lo"])), F.cross_dn(r, np.full_like(r, p["hi"])))


@register("bb_reversion", "Bollinger Reversion", "mean_reversion", "classic",
          "Close back inside the band after an excursion outside.", {"k": 2.0}, {"k": [2.0, 2.5, 3.0]})
def _bbrev(b, p):
    _, up, lo = F.bollinger(b.c, 20, p["k"])
    pc = np.r_[b.c[0], b.c[:-1]]
    pu, pl = np.r_[np.nan, up[:-1]], np.r_[np.nan, lo[:-1]]
    return _sig((pc < pl) & (b.c > lo), (pc > pu) & (b.c < up))


@register("z_revert", "ATR Z-Score Reversion", "mean_reversion", "custom",
          "Fade a stretch of more than k ATRs from the 50 EMA.", {"k": 2.5}, {"k": [2.0, 2.5, 3.5]})
def _zrev(b, p):
    z = (b.c - F.ema(b.c, 50)) / F.atr(b.h, b.l, b.c)
    return _sig(F.cross_up(z, np.full_like(z, -p["k"])), F.cross_dn(z, np.full_like(z, p["k"])))


@register("stoch_cross", "Stochastic Cross", "mean_reversion", "classic",
          "%K crosses %D in the oversold / overbought zones.", {"lo": 20, "hi": 80})
def _stoch(b, p):
    hh, ll = F.rolling_max(b.h, 14), F.rolling_min(b.l, 14)
    k = 100 * (b.c - ll) / np.where(hh == ll, np.nan, hh - ll)
    d = F.sma(np.nan_to_num(k, nan=50), 3)
    return _sig(F.cross_up(k, d) & (k < p["lo"]), F.cross_dn(k, d) & (k > p["hi"]))


@register("vwap_revert", "VWAP Reversion", "mean_reversion", "community",
          "Fade a stretch from the UTC-day VWAP.", {"k": 2.0}, {"k": [1.5, 2.0, 3.0]}, tf="5m")
def _vwap(b, p):
    v = F.daily_vwap(b)
    sd = F.std(b.c - v, 96)
    z = (b.c - v) / np.where(sd == 0, np.nan, sd)
    return _sig(F.cross_up(z, np.full_like(z, -p["k"])), F.cross_dn(z, np.full_like(z, p["k"])))


@register("streak_revert", "Exhaustion Streak", "mean_reversion", "community",
          "After N same-colour candles, fade the streak.", {"n": 5}, {"n": [4, 5, 7]})
def _streak(b, p):
    n = p["n"]
    red = (b.c < b.o).astype(int)
    grn = (b.c > b.o).astype(int)
    import pandas as pd
    rr = pd.Series(red).rolling(n).sum().to_numpy() == n
    gg = pd.Series(grn).rolling(n).sum().to_numpy() == n
    return _sig(rr, gg)


@register("climax_fade", "Volume Climax Fade", "mean_reversion", "custom",
          "A very large candle on a volume spike has run out of buyers or sellers: fade it.",
          {"atr_k": 2.5, "z": 2.5}, {"atr_k": [2.0, 3.0], "z": [2.0, 3.0]})
def _climax(b, p):
    big = (b.h - b.l) > p["atr_k"] * F.atr(b.h, b.l, b.c)
    vz = F.zscore(b.v, 50) > p["z"]
    return _sig(big & vz & (b.c < b.o), big & vz & (b.c > b.o))


# ------------------------------------------------------------ volatility
@register("bb_squeeze", "Squeeze Breakout (TTM)", "volatility", "community",
          "Bollinger inside Keltner (compression), then momentum release.", {"len": 20}, {})
def _squeeze(b, p):
    m, up, lo = F.bollinger(b.c, p["len"], 2.0)
    a = F.atr(b.h, b.l, b.c, p["len"])
    kc_up, kc_lo = F.ema(b.c, p["len"]) + 1.5 * a, F.ema(b.c, p["len"]) - 1.5 * a
    on = (up < kc_up) & (lo > kc_lo)
    release = np.r_[False, on[:-1]] & ~on
    mom = b.c - (F.rolling_max(b.h, p["len"]) + F.rolling_min(b.l, p["len"])) / 2
    return _sig(release & (mom > 0), release & (mom < 0))


@register("vol_compress", "Volatility Compression Break", "volatility", "custom",
          "ATR near a 100-bar low, then a close beyond the last 10 bars.", {"pct": 0.2}, {"pct": [0.1, 0.2, 0.3]})
def _compress(b, p):
    import pandas as pd
    a = F.atr(b.h, b.l, b.c)
    rank = pd.Series(a).rolling(100).rank(pct=True).to_numpy()
    low_vol = np.r_[False, (rank[:-1] <= p["pct"])]
    hi = np.r_[np.nan, F.rolling_max(b.h, 10)[:-1]]
    lo = np.r_[np.nan, F.rolling_min(b.l, 10)[:-1]]
    return _sig(low_vol & (b.c > hi), low_vol & (b.c < lo))


# ---------------------------------------------------------------- volume
@register("vol_spike_follow", "Volume Spike Momentum", "volume", "custom",
          "Spike in volume with a strong close in one direction: follow it.", {"z": 3.0}, {"z": [2.5, 3.0, 4.0]})
def _vsf(b, p):
    z = F.zscore(b.v, 50)
    body = (b.c - b.o) / np.where(b.h == b.l, np.nan, b.h - b.l)
    return _sig((z > p["z"]) & (body > 0.6), (z > p["z"]) & (body < -0.6))


@register("vol_spike_fade", "Volume Spike Fade", "volume", "custom",
          "The same spike, faded: tests whether the live volume_spike signal is simply backwards.",
          {"z": 3.0}, {"z": [2.5, 3.0, 4.0]})
def _vsfade(b, p):
    return -_vsf(b, p)


@register("taker_flow", "Taker Buy Pressure", "volume", "custom",
          "Persistent taker-buy dominance (order-flow imbalance) in the direction of the trend.",
          {"thr": 0.58}, {"thr": [0.55, 0.58, 0.62]})
def _taker(b, p):
    ratio = F.sma(b.tb / np.where(b.v == 0, np.nan, b.v), 6)
    t = F.ema(b.c, 50)
    return _sig(F.cross_up(ratio, np.full_like(ratio, p["thr"])) & (b.c > t),
                F.cross_dn(ratio, np.full_like(ratio, 1 - p["thr"])) & (b.c < t))


# ------------------------------------------------------------- structure
@register("sweep_reclaim", "Liquidity Sweep & Reclaim", "structure", "community",
          "Wick through a 15-bar extreme that closes back inside.", {"n": 15}, {"n": [10, 15, 30]})
def _sweep(b, p):
    lo = np.r_[np.nan, F.rolling_min(b.l, p["n"])[:-1]]
    hi = np.r_[np.nan, F.rolling_max(b.h, p["n"])[:-1]]
    return _sig((b.l < lo) & (b.c > lo) & (b.c > b.o), (b.h > hi) & (b.c < hi) & (b.c < b.o))


# ----------------------------------------------------------------- carry
@register("funding_fade", "Funding-Rate Fade", "carry", "community",
          "Fade crowded positioning: very high funding shorts, very negative funding longs.",
          {"hi": 0.0004, "lo": -0.0002}, {"hi": [0.0003, 0.0005], "lo": [-0.0001, -0.0002]}, tf="1h")
def _funding(b, p):
    ts, rate = load_funding(b.symbol)
    if not len(ts):
        return np.zeros(len(b), np.int8)
    # a funding print is public from its timestamp onward
    idx = np.searchsorted(ts, b.close_t, side="right") - 1
    r = np.where(idx >= 0, rate[np.clip(idx, 0, None)], 0.0)
    new = np.r_[True, idx[1:] != idx[:-1]]
    return _sig(new & (r < p["lo"]), new & (r > p["hi"]))


# ---------------------------------------------------------------- regime
@register("regime_switch", "Regime Switch", "regime", "custom",
          "ADX high: Donchian trend-follow. ADX low: Bollinger reversion. One engine, two behaviours.",
          {"adx_split": 22}, {"adx_split": [18, 22, 28]})
def _regime(b, p):
    a, _, _ = F.adx(b.h, b.l, b.c)
    trend = _donchian(b, {"n": 40})
    rev = _bbrev(b, {"k": 2.5})
    return np.where(a >= p["adx_split"], trend, rev)


@register("pullback_mr", "Trend-Filtered Dip Buy", "regime", "custom",
          "RSI dip only in the direction of the 4h trend (mean reversion with a trend leash).",
          {"lo": 35, "hi": 65}, {"lo": [30, 40], "hi": [60, 70]})
def _pmr(b, p):
    r = F.rsi(b.c, 14)
    tr = _htf_trend(b, "4h", 50)
    return _sig(F.cross_up(r, np.full_like(r, p["lo"])) & (tr > 0),
                F.cross_dn(r, np.full_like(r, p["hi"])) & (tr < 0))


# ------------------------------------------------------------- baseline
@register("random", "Random Entries (control)", "baseline", "classic",
          "Coin-flip entries at a fixed rate. A strategy that cannot beat this has no edge.",
          {"rate": 0.01, "seed": 7}, {})
def _random(b, p):
    # hashed from each bar's own timestamp, so the draw for a bar never depends on how
    # many bars exist after it
    salt = (p["seed"] * 40503 + sum(map(ord, b.symbol)) * 7919) & 0xFFFFFFFF
    x = (b.t.astype(np.uint64) * np.uint64(2654435761) + np.uint64(salt)) & np.uint64(0xFFFFFFFF)
    x ^= x >> np.uint64(15)
    x = (x * np.uint64(2246822519)) & np.uint64(0xFFFFFFFF)
    u = (x & np.uint64(0xFFFF)).astype(float) / 65536.0
    side = np.where(((x >> np.uint64(16)) & np.uint64(1)) == 1, 1, -1)
    return np.where(u < p["rate"], side, 0)


# ----------------------------------------------------------------- meta
def fade(strategy_id: str) -> Callable[[Bars, dict], np.ndarray]:
    base = REGISTRY[strategy_id]
    return lambda b, p: -base.signals(b, p)


def combine(ids: list[str], mode: str = "any", k: int = 2) -> Callable[[Bars, dict], np.ndarray]:
    """`any`: first non-zero wins in listed order. `vote`: at least k agree on a side."""
    def fn(b: Bars, p: dict) -> np.ndarray:
        sigs = np.vstack([REGISTRY[i].signals(b) for i in ids])
        if mode == "vote":
            up, dn = (sigs > 0).sum(0), (sigs < 0).sum(0)
            return np.where(up >= k, 1, np.where(dn >= k, -1, 0))
        out = np.zeros(sigs.shape[1], np.int8)
        for row in sigs[::-1]:
            out = np.where(row != 0, row, out)
        return out
    return fn


# ------------------------------------------------------- mirrors and ensembles
def _register_mirrors_and_votes():
    """Every strategy also runs backwards ("mirror"), and a few ensembles trade only when several agree.
    A strategy and its mirror share costs exactly, so any difference between them is the signal itself."""
    for sid in [k for k in list(REGISTRY) if k != "random"]:
        base = REGISTRY[sid]
        REGISTRY[f"mirror_{sid}"] = Strategy(
            f"mirror_{sid}", f"Mirror: {base.name}", base.family, "custom",
            f"{base.name} traded in the opposite direction (the live engine's 'mirror' idea).",
            (lambda b, p, _f=base.fn: -_f(b, p)), dict(base.defaults), dict(base.grid), base.tf)
    trend = ["donchian", "keltner_break", "vol_breakout", "ichimoku", "ema_cross"]
    for sid, name, ids, mode, k in (
            ("vote2_trend", "Two of five trend strategies agree", trend, "vote", 2),
            ("vote3_trend", "Three of five trend strategies agree", trend, "vote", 3),
            ("first_trend", "First trend strategy to fire", trend, "any", 1)):
        REGISTRY[sid] = Strategy(sid, name, "meta", "custom", f"{name}; each member uses its default settings.",
                                 combine(ids, mode, k), {}, {}, "4h")


def catalog() -> list[dict]:
    return [{"id": s.id, "name": s.name, "family": s.family, "source": s.source, "desc": s.desc,
             "tf": s.tf, "params": s.defaults, "grid": s.grid} for s in REGISTRY.values()]


_register_mirrors_and_votes()
