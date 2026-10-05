"""
7-day price range: a calibrated band for each of the next 7 days, odds of finishing higher, odds of touching
±5% / ±10%. Phase 1 of design/7d_forecast_plan.md: maths only, no AI.

How it works
------------
* Width. Daily volatility is a blend of a fast EWMA and the 30- and 90-day realised volatility of closed daily
  bars. A k-day move is then expressed in "volatility units": z = return_k / (sigma_daily * sqrt(k)).
* Shape. Crypto moves are fat-tailed and skewed, so instead of assuming a bell curve the quantiles of z are taken
  from history: every past k-day move of every coin, using only data before the forecast date.
* Touches. The same idea for the highest high and lowest low inside the 7 days, so "touches +5%" comes from how
  far prices actually travelled intraweek, not from a formula.
* Lean. Off by default. A ridge regression on a few point-in-time features can tilt the centre, capped at
  0.3 volatility units, but it is only switched on if the walk-forward backtest shows it beats no tilt.

Everything a forecast uses is computed from bars that had closed before it (tests/test_forecast7d.py).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from analysis.lab.data import Bars

H = 7
QS = (0.10, 0.25, 0.50, 0.75, 0.90)
TOUCH = (0.05, 0.10)
MAX_LEAN = 0.30
FEATURES = ("ret_7d_z", "ret_30d_z", "ema50_dist", "range30_pos", "vol_ratio")


def daily_sigma(c: np.ndarray) -> np.ndarray:
    """sigma of daily log returns known at the close of each bar (index i uses returns up to and including bar i)."""
    r = np.diff(np.log(c), prepend=np.nan)
    out = np.full(len(c), np.nan)
    ew = np.nan
    for i in range(1, len(c)):
        x = r[i] ** 2
        ew = x if np.isnan(ew) else 0.94 * ew + 0.06 * x
        if i >= 90:
            rv30 = np.nanstd(r[i - 29:i + 1])
            rv90 = np.nanstd(r[i - 89:i + 1])
            out[i] = 0.5 * np.sqrt(ew) + 0.3 * rv30 + 0.2 * rv90
    return out


def features(b: Bars, sig: np.ndarray, i: int) -> dict:
    c = b.c
    s = sig[i]
    ema = _ema(c[:i + 1], 50)[-1]
    lo, hi = c[i - 29:i + 1].min(), c[i - 29:i + 1].max()
    rv7 = np.std(np.diff(np.log(c[i - 7:i + 1])))
    return {"ret_7d_z": np.log(c[i] / c[i - 7]) / (s * np.sqrt(7)),
            "ret_30d_z": np.log(c[i] / c[i - 30]) / (s * np.sqrt(30)),
            "ema50_dist": np.log(c[i] / ema) / (s * np.sqrt(7)),
            "range30_pos": (c[i] - lo) / (hi - lo) if hi > lo else 0.5,
            "vol_ratio": rv7 / s if s > 0 else 1.0}


def _ema(x, n):
    a = 2 / (n + 1)
    out = np.empty(len(x))
    out[0] = x[0]
    for k in range(1, len(x)):
        out[k] = a * x[k] + (1 - a) * out[k - 1]
    return out


@dataclass
class History:
    """Standardised past moves, pooled across coins: the empirical shape the forecast draws its quantiles from."""
    z: dict = field(default_factory=lambda: {k: [] for k in range(1, H + 1)})   # k-day close-to-close moves
    up: list = field(default_factory=list)                                         # 7-day max high, vol units
    dn: list = field(default_factory=list)                                         # 7-day min low, vol units
    X: list = field(default_factory=list)
    y: list = field(default_factory=list)
    t: list = field(default_factory=list)                                          # time each row became known

    def add_coin(self, b: Bars, sig: np.ndarray, start: int = 90):
        for i in range(start, len(b) - H):
            s = sig[i]
            if not s > 0:
                continue
            known = int(b.t[i + H]) + 86_400_000                                    # outcome known once day 7 closes
            for k in range(1, H + 1):
                self.z[k].append((known, np.log(b.c[i + k] / b.c[i]) / (s * np.sqrt(k))))
            sd = s * np.sqrt(H)
            self.up.append((known, np.log(b.h[i + 1:i + H + 1].max() / b.c[i]) / sd))
            self.dn.append((known, np.log(b.l[i + 1:i + H + 1].min() / b.c[i]) / sd))
            f = features(b, sig, i)
            self.X.append([f[k] for k in FEATURES])
            self.y.append(np.log(b.c[i + H] / b.c[i]) / sd)
            self.t.append(known)

    def before(self, t_ms: int) -> "Frozen":
        """Only the moves whose outcome was already known at t_ms."""
        sel = lambda rows: np.array([v for k, v in rows if k <= t_ms])
        mask = np.array(self.t) <= t_ms
        return Frozen({k: sel(v) for k, v in self.z.items()}, sel(self.up), sel(self.dn),
                      np.array(self.X)[mask], np.array(self.y)[mask])


@dataclass
class Frozen:
    z: dict
    up: np.ndarray
    dn: np.ndarray
    X: np.ndarray
    y: np.ndarray

    def lean_model(self, alpha: float = 50.0):
        if len(self.y) < 300:
            return None
        mu, sd = self.X.mean(0), self.X.std(0) + 1e-9
        Xs = (self.X - mu) / sd
        w = np.linalg.solve(Xs.T @ Xs + alpha * np.eye(Xs.shape[1]), Xs.T @ (self.y - self.y.mean()))
        return mu, sd, w, float(self.y.mean())


@dataclass
class Forecast7d:
    symbol: str
    price: float
    sigma_daily: float
    lean_z: float
    days: list            # [{day, q10, q25, q50, q75, q90}] prices
    p_up: float
    touch: dict           # {"+5%": p, "-5%": p, ...}
    exp_high: float
    exp_low: float
    key_values: dict

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


def forecast(symbol: str, b: Bars, sig: np.ndarray, i: int, hist: Frozen, lean: bool = False) -> Forecast7d | None:
    s = sig[i]
    if not s > 0 or len(hist.up) < 200:
        return None
    p = float(b.c[i])
    f = features(b, sig, i)
    lz = 0.0
    if lean:
        m = hist.lean_model()
        if m is not None:
            mu, sd, w, _ = m
            lz = float(np.clip(((np.array([f[k] for k in FEATURES]) - mu) / sd) @ w, -MAX_LEAN, MAX_LEAN))
    days = []
    for k in range(1, H + 1):
        zq = np.quantile(hist.z[k], QS) + lz * np.sqrt(k / H)
        px = p * np.exp(zq * s * np.sqrt(k))
        days.append({"day": k, **{f"q{int(q * 100)}": float(v) for q, v in zip(QS, px)}})
    z7 = hist.z[H] + lz
    sd7 = s * np.sqrt(H)
    touch = {}
    for x in TOUCH:
        touch[f"+{int(x * 100)}%"] = float(np.mean(hist.up + lz > np.log(1 + x) / sd7))
        touch[f"-{int(x * 100)}%"] = float(np.mean(hist.dn + lz < np.log(1 - x) / sd7))
    return Forecast7d(symbol, p, float(s), lz, days, float(np.mean(z7 > 0)), touch,
                      float(p * np.exp(np.median(hist.up + lz) * sd7)), float(p * np.exp(np.median(hist.dn + lz) * sd7)),
                      {**{k: float(v) for k, v in f.items()}, "sigma_daily_pct": float(100 * s),
                       "sigma_7d_pct": float(100 * sd7)})


def baseline(p: float, s: float) -> dict:
    """No-skill reference: a random walk with the same volatility and a bell curve."""
    from scipy.stats import norm
    sd7 = s * np.sqrt(H)
    return {"q": [p * np.exp(norm.ppf(q) * sd7) for q in QS], "p_up": 0.5,
            "touch": {f"{sgn}{int(x * 100)}%": float(min(1.0, 2 * (1 - norm.cdf(abs(np.log(1 + (x if sgn == '+' else -x))) / sd7))))
                      for x in TOUCH for sgn in "+-"}}


def pinball(qs_prices, y) -> float:
    return float(np.mean([max(q * (y - v), (q - 1) * (y - v)) for q, v in zip(QS, qs_prices)]))
