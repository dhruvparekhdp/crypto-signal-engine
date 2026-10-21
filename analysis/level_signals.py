"""
Support / resistance setups for the channel-style bracket, paper research only (owner, 21 Oct 2026: "for this type
of trade, let's generate signals with resistance and all market sentiment things ... and trade in paper with proper
label to check"; "check for all watchlist coin, don't bring risk").

Rules, fixed before any result was seen (1h bars, closed bars only):
  levels   swing highs and lows of the last 7 days (a bar higher / lower than the 3 on each side), grouped when within
           0.4% of each other; a level needs 2+ touches. A level made of swing HIGHS that price is now above is a
           flipped level (old resistance, now support) - the channel's "settled above the support zone".
  long     the last hour came within 0.2% of a support level or swept below it ("liquidity swept"), closed back above
           it on an up candle, with the level at most 1.5% below the close
  short    the mirror at resistance
Ambiguous bars (both) are skipped. The trade itself is the bracket in analysis/bracket_book.py; sentiment and news
are LABELS, not filters, so their effect can be measured afterwards.
"""
from __future__ import annotations

import numpy as np

LOOKBACK = 168          # 7 days of 1h bars
PIVOT_K = 3
CLUSTER = 0.004
TOUCH = 0.002
MAX_DIST = 0.015


def pivots(h: np.ndarray, lo: np.ndarray, k: int = PIVOT_K) -> tuple[np.ndarray, np.ndarray]:
    """Indexes of swing highs and swing lows (strictly confirmed: k bars each side)."""
    n = len(h)
    ph, pl = [], []
    for i in range(k, n - k):
        if h[i] == h[i - k:i + k + 1].max():
            ph.append(i)
        if lo[i] == lo[i - k:i + k + 1].min():
            pl.append(i)
    return np.array(ph, dtype=int), np.array(pl, dtype=int)


def _clusters(points: list[tuple[float, str]]) -> list[dict]:
    """Group (price, kind) points within CLUSTER of each other -> [{price, touches, highs, lows}]."""
    out: list[dict] = []
    for p, kind in sorted(points):
        if out and p <= out[-1]["last"] * (1 + CLUSTER):
            g = out[-1]
            g["prices"].append(p)
            g["last"] = p
            g[kind] += 1
        else:
            out.append({"prices": [p], "last": p, "highs": int(kind == "highs"), "lows": int(kind == "lows")})
    return [{"price": float(np.mean(g["prices"])), "touches": len(g["prices"]), "highs": g["highs"], "lows": g["lows"]}
            for g in out]


def setup_at(i: int, o, h, lo, c, ph: np.ndarray, pl: np.ndarray) -> dict | None:
    """The setup on bar i (its close is the signal), using pivots confirmed by then, or None."""
    lo_i = max(0, i - LOOKBACK)
    hi_i = i - PIVOT_K                                   # a pivot needs PIVOT_K bars after it
    if hi_i <= lo_i:
        return None
    hs = ph[(ph >= lo_i) & (ph < hi_i)]
    ls = pl[(pl >= lo_i) & (pl < hi_i)]
    levels = [lv for lv in _clusters([(float(h[j]), "highs") for j in hs] + [(float(lo[j]), "lows") for j in ls])
              if lv["touches"] >= 2]
    if not levels:
        return None
    cands = []
    for lv in levels:
        L = lv["price"]
        if c[i] > L and lo[i] <= L * (1 + TOUCH) and c[i] >= o[i] and (c[i] - L) / L <= MAX_DIST:
            cands.append(("long", lv))
        if c[i] < L and h[i] >= L * (1 - TOUCH) and c[i] <= o[i] and (L - c[i]) / L <= MAX_DIST:
            cands.append(("short", lv))
    sides = {s for s, _ in cands}
    if len(sides) != 1:
        return None
    side, lv = max(cands, key=lambda x: x[1]["touches"])
    L = lv["price"]
    flip = (lv["highs"] > lv["lows"]) if side == "long" else (lv["lows"] > lv["highs"])
    swept = (lo[i] < L * (1 - TOUCH)) if side == "long" else (h[i] > L * (1 + TOUCH))
    return {"side": 1 if side == "long" else -1,
            "setup": ("breakout retest" if flip else "support reclaim") if side == "long"
            else ("breakdown retest" if flip else "resistance reject"),
            "level": round(L, 8), "touches": lv["touches"], "swept": bool(swept),
            "level_dist_pct": round(abs(c[i] - L) / L * 100, 3)}


def price_labels(i: int, c: np.ndarray, side: int, ema200: np.ndarray, atr: np.ndarray) -> dict:
    """Trend and volatility at the signal, signed by the trade's side where that matters."""
    out = {}
    if i >= 24:
        chg = (c[i] / c[i - 24] - 1) * 100
        out["move_24h"] = "with" if chg * side > 1 else "against" if chg * side < -1 else "flat"
    if not np.isnan(ema200[i]):
        out["trend_1h_ema200"] = "with" if (c[i] - ema200[i]) * side > 0 else "against"
    if atr[i] > 0:
        a = atr[i] / c[i] * 100
        out["atr_1h"] = "low <0.5%" if a < 0.5 else "mid 0.5-1%" if a < 1 else "high >1%"
    return out
