"""Market-neutral (pairs / relative strength) research on Delta-style perpetuals (owner, 26 Oct 2026: "yes test pairs in
lab"). Research only. Every paper result in the week before was market direction (shorts won, longs lost in every
group), so the question is whether being long one coin and short another, sized so BTC's move cancels, earns anything
after fees and funding.

    python -m scripts.pairs_lab            # all families, both periods, null baskets -> data/lab/pairs/report.json

Rules fixed before any result (no tuning after):
  A momentum   rank the 11 crypto coins by their last L-day return (L = 7, 14, 28); long the top 2, short the bottom 2,
               each leg sized by 1 / its 30-day volatility, half the gross on each side; hold 7 days, then re-rank
  B reversal   long the 2 biggest losers, short the 2 biggest winners over L = 1 or 3 days; hold H = 1 or 3 days
  C pairs      every one of the 55 coin pairs (no picking): z = (log price ratio - its 30-day mean) / 30-day std on
               daily closes; |z| >= 2 -> long the cheap coin, short the rich one (vol-sized, half the gross each);
               out when z crosses 0, after 10 days, or at |z| >= 4 (stop)
Costs on every leg in and out: Delta taker 0.05% + 18% GST + 2 bps slippage (0.158% of gross a round trip), plus real
Binance funding (long pays, short receives). Daily closes; a decision at a close trades at that close.
Gate (all must hold): average after costs > 0 in 2021-24 AND 2025-26; better than 95% of 200 random baskets with the
same timing and sizing (A, B); |correlation with BTC| < 0.3 (really market-neutral). Every variant goes in the trial
ledger (family "pairs").
"""
from __future__ import annotations

import itertools
import json
from pathlib import Path

import numpy as np
import pandas as pd

from analysis.lab.data import load_bars, load_funding

COINS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT", "ADAUSDT", "DOGEUSDT", "AVAXUSDT", "LINKUSDT",
         "SUIUSDT", "TIAUSDT"]
COST = 0.00158
SPLIT = pd.Timestamp("2025-01-01")
K = 2
VOL_N = 30
OUT = Path("data/lab/pairs")


def panel() -> tuple[pd.DataFrame, pd.DataFrame]:
    """Daily closes (index = UTC day of the close) and daily funding sums per coin."""
    closes, fund = {}, {}
    for s in COINS:
        b = load_bars(s, "1d")
        closes[s] = pd.Series(b.c, index=pd.to_datetime(b.t + 86_400_000, unit="ms"))   # the day the bar closed
        ts, rate = load_funding(s)
        if len(ts):
            f = pd.Series(rate, index=pd.to_datetime(ts, unit="ms"))
            fund[s] = f.groupby(f.index.floor("D")).sum()
    c = pd.DataFrame(closes).sort_index()
    f = pd.DataFrame(fund).reindex(c.index).fillna(0.0)
    return c, f


class Arrays:
    """numpy views of the panel: closes, cumulative funding and 30-day volatility, T x coins."""

    def __init__(self, c: pd.DataFrame, f: pd.DataFrame):
        self.idx, self.names = c.index, list(c.columns)
        self.c = c.to_numpy(float)
        self.fc = np.vstack([np.zeros(len(self.names)), np.cumsum(f.to_numpy(float), axis=0)])   # fc[i+1] = sum to i
        self.vol = c.pct_change().rolling(VOL_N).std().to_numpy(float)
        self.btc = self.names.index("BTCUSDT")


def _weights(longs, shorts, vol_row) -> dict:
    """Half the gross on each side, each leg 1 / its volatility within its side. Keys: column numbers."""
    w = {}
    for cols, sign in ((longs, 1.0), (shorts, -1.0)):
        inv = np.array([1.0 / max(vol_row[j], 1e-6) for j in cols])
        for j, x in zip(cols, inv / inv.sum() * 0.5):
            w[j] = sign * x
    return w


def _hold_pnl(w: dict, A: Arrays, i0: int, i1: int) -> float:
    """Return on gross over closes i0 -> i1 after costs and funding (long pays positive funding, short receives)."""
    pnl = 0.0
    for j, x in w.items():
        pnl += x * (A.c[i1, j] / A.c[i0, j] - 1)
        pnl -= x * (A.fc[i1 + 1, j] - A.fc[i0 + 1, j])
    return pnl - COST * sum(abs(x) for x in w.values())


def cross_section(A: Arrays, lookback: int, hold: int, reverse: bool, rng=None) -> pd.DataFrame:
    """One rebalance every `hold` days. rng: random baskets with the same timing and sizing (the null)."""
    rows = []
    for i0 in range(max(lookback, VOL_N) + 1, len(A.idx) - hold, hold):
        past = A.c[i0] / A.c[i0 - lookback] - 1
        ok = np.flatnonzero(~np.isnan(past) & ~np.isnan(A.vol[i0]))
        if len(ok) < 2 * K + 1:
            continue
        if rng is not None:
            pick = list(rng.permutation(ok))
            longs, shorts = pick[:K], pick[K:2 * K]
        else:
            order = ok[np.argsort(past[ok])]                       # ascending: losers first
            longs, shorts = (list(order[:K]), list(order[-K:])) if reverse else (list(order[-K:]), list(order[:K]))
        w = _weights(longs, shorts, A.vol[i0])
        rows.append({"t": A.idx[i0], "ret": _hold_pnl(w, A, i0, i0 + hold),
                     "btc": A.c[i0 + hold, A.btc] / A.c[i0, A.btc] - 1})
    return pd.DataFrame(rows)


def pairs_mr(A: Arrays, window: int = 30, entry: float = 2.0, stop: float = 4.0, max_days: int = 10) -> pd.DataFrame:
    trades = []
    n = len(A.idx)
    for a, b in itertools.combinations(range(len(A.names)), 2):
        x = pd.Series(np.log(A.c[:, a] / A.c[:, b]))
        z = ((x - x.rolling(window).mean()) / x.rolling(window).std()).to_numpy()
        i = 0
        while i < n - 1:
            zi = z[i]
            if np.isnan(zi) or abs(zi) < entry or np.isnan(A.vol[i, a]) or np.isnan(A.vol[i, b]):
                i += 1
                continue
            rich, cheap = (a, b) if zi > 0 else (b, a)
            w = _weights([cheap], [rich], A.vol[i])
            j = i + 1
            while j < n - 1 and j - i < max_days:
                zj = z[j]
                if np.isnan(zj) or np.sign(zj) != np.sign(zi) or abs(zj) >= stop:
                    break
                j += 1
            trades.append({"t": A.idx[i], "pair": f"{A.names[a][:-4]}/{A.names[b][:-4]}", "days": j - i,
                           "ret": _hold_pnl(w, A, i, j), "btc": A.c[j, A.btc] / A.c[i, A.btc] - 1})
            i = j + 1
    return pd.DataFrame(trades)


def stats(d: pd.DataFrame, per_year: float | None = None) -> dict:
    if d.empty:
        return {"n": 0}
    out = {"n": len(d), "avg_pct": round(100 * d.ret.mean(), 3), "win_pct": round(100 * (d.ret > 0).mean(), 1),
           "total_pct": round(100 * d.ret.sum(), 1), "corr_btc": round(float(d.ret.corr(d.btc)), 2)}
    cum = d.ret.cumsum()
    out["max_dd_pct"] = round(100 * float((cum.cummax() - cum).max()), 1)
    if per_year:
        sd = d.ret.std()
        out["sharpe"] = round(float(d.ret.mean() / sd * np.sqrt(per_year)), 2) if sd > 0 else None
        out["per_year_pct"] = round(100 * d.ret.mean() * per_year, 1)
    return out


def main() -> None:
    from analysis.lab import ledger
    c, f = panel()
    A = Arrays(c, f)
    OUT.mkdir(parents=True, exist_ok=True)
    window = (str(c.index[0].date()), str(c.index[-1].date()))
    report, rows = {"window": window, "coins": COINS, "variants": {}}, []
    variants = [("A_momentum", L, 7, False) for L in (7, 14, 28)] + \
               [("B_reversal", L, H, True) for L in (1, 3) for H in (1, 3)]
    for fam, L, H, rev in variants:
        name = f"{fam}_L{L}_H{H}"
        d = cross_section(A, L, H, rev)
        rng = np.random.default_rng(7)
        nulls = [cross_section(A, L, H, rev, rng).ret.mean() for _ in range(200)]
        rep = {"all": stats(d, 365 / H), "null_p": round(float(np.mean([x >= d.ret.mean() for x in nulls])), 3)}
        for lab, part in (("2021-24", d[d.t < SPLIT]), ("2025-26", d[d.t >= SPLIT])):
            rep[lab] = stats(part, 365 / H)
        rep["by_year"] = {str(y): round(100 * g.ret.mean(), 3) for y, g in d.groupby(d.t.dt.year)}
        rep["passes"] = bool(rep["2021-24"].get("avg_pct", -1) > 0 and rep["2025-26"].get("avg_pct", -1) > 0
                             and rep["null_p"] <= 0.05 and abs(rep["all"].get("corr_btc", 1)) < 0.3)
        report["variants"][name] = rep
        rows.append({"digest": ledger.trial_digest(name, {"L": L, "H": H, "k": K}, "1d", f"hold{H}", window, "delta_gst",
                                                   "pairs_lab_v1", code="pairs_lab_v1"),
                     "cfg": name, "avg_pct": rep["all"].get("avg_pct"), "null_p": rep["null_p"], "passes": rep["passes"]})
    p = pairs_mr(A)
    rep = {"all": stats(p), "by_year": {str(y): round(100 * g.ret.mean(), 3) for y, g in p.groupby(p.t.dt.year)},
           "trades_per_pair": round(len(p) / 55, 1), "exits_days": p.days.describe().round(1).to_dict()}
    for lab, part in (("2021-24", p[p.t < SPLIT]), ("2025-26", p[p.t >= SPLIT])):
        rep[lab] = stats(part)
    by_pair = p.groupby("pair").ret.agg(["mean", "size"])
    rep["pairs_positive_both_halves"] = int(sum(
        1 for pr in by_pair.index
        if p[(p.pair == pr) & (p.t < SPLIT)].ret.mean() > 0 and p[(p.pair == pr) & (p.t >= SPLIT)].ret.mean() > 0))
    rep["passes"] = bool(rep["2021-24"].get("avg_pct", -1) > 0 and rep["2025-26"].get("avg_pct", -1) > 0
                         and abs(rep["all"].get("corr_btc", 1)) < 0.3)
    report["variants"]["C_pairs_z2_w30"] = rep
    rows.append({"digest": ledger.trial_digest("C_pairs_z2_w30", {"window": 30, "entry": 2, "stop": 4, "max_days": 10},
                                               "1d", "z0", window, "delta_gst", "pairs_lab_v1", code="pairs_lab_v1"),
                 "cfg": "C_pairs_z2_w30 (55 pairs)", "avg_pct": rep["all"].get("avg_pct"), "passes": rep["passes"]})
    led = ledger.record(rows, family="pairs", kind="search",
                        hypothesis="owner 26 Oct: market-neutral pairs / relative strength on perps", proposed_by="owner")
    report["ledger"] = led
    (OUT / "report.json").write_text(json.dumps(report, indent=1, default=str))
    print(json.dumps(report, indent=1, default=str))


if __name__ == "__main__":
    main()
