"""Where is the swing book losing, and what would improve it? (owner, 21 Oct 2026: "in all the tests we rarely reach
50% win ... deep dive in everything"). Research only.

    python -m scripts.edge_research features     # data/lab/edge_features.parquet: entry context for every live signal
    python -m scripts.edge_research filters      # each pre-set filter, 2021-24 vs 2025-26 (both must agree)

Every filter here was written down before looking at its result. Each is judged on the trades it would REMOVE: a
filter only helps if the removed trades lose money in both periods (the strategies were chosen on 2021-24, so
2025-26 is the honest check). Features use only data closed before the signal bar's close.
"""
from __future__ import annotations

import glob
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from analysis.lab import features as F
from analysis.lab.data import INTERVAL_MS, load_bars

DAY = 86_400_000
OUT = Path("data/lab/edge_features.parquet")


def _daily_ctx(sym: str) -> pd.DataFrame:
    d = load_bars(sym, "1d")
    c = pd.Series(d.c)
    return pd.DataFrame({"known": d.t + DAY, "c": d.c, "sma50": c.rolling(50).mean().to_numpy(),
                         "sma200": c.rolling(200).mean().to_numpy(), "ret28": (c / c.shift(28) - 1).to_numpy() * 100,
                         "ret7": (c / c.shift(7) - 1).to_numpy() * 100})


def _funding(sym: str) -> pd.DataFrame:
    fs = sorted(glob.glob(f"data/lake/um/fundingRate/{sym}/**/*.parquet", recursive=True))
    if not fs:
        return pd.DataFrame(columns=["t", "rate"])
    f = pd.concat([pd.read_parquet(x) for x in fs], ignore_index=True)
    return pd.DataFrame({"t": f.calc_time.astype("int64"), "rate": f.last_funding_rate.astype(float)}).sort_values("t")


def features() -> pd.DataFrame:
    t = pd.read_pickle("data/lab/live_sim_trades.pkl")
    t = t[t.filter_ok].copy().reset_index(drop=True)
    sym_of = lambda c: "PAXGUSDT" if c == "XAUT" else c + "USDT"   # noqa: E731
    btc = _daily_ctx("BTCUSDT")
    rows = []
    cache_d, cache_b, cache_f = {}, {}, {}
    for r in t.itertuples():
        sym = sym_of(r.coin)
        if sym not in cache_d:
            cache_d[sym], cache_f[sym] = _daily_ctx(sym), _funding(sym)
        if (sym, r.tf) not in cache_b:
            b = load_bars(sym, r.tf)
            cache_b[(sym, r.tf)] = (b, F.ema(b.c, 20), F.atr(b.h, b.l, b.c), F.adx(b.h, b.l, b.c, 14)[0]
                                    if hasattr(F, "adx") else None)
        d, fr = cache_d[sym], cache_f[sym]
        b, e20, atr, adx = cache_b[(sym, r.tf)]
        j = int(np.searchsorted(d.known.to_numpy(), r.sig_t, side="right")) - 1
        jb = int(np.searchsorted(btc.known.to_numpy(), r.sig_t, side="right")) - 1
        i = int(np.searchsorted(b.t, r.sig_t - INTERVAL_MS[r.tf], side="left"))       # the signal bar
        k = int(np.searchsorted(fr.t.to_numpy(), r.sig_t, side="right")) - 1 if len(fr) else -1
        s = r.side
        row = {"idx": r.Index}
        if j >= 200:
            row.update(coin_vs_sma200=s * np.sign(d.c[j] - d.sma200[j]), coin_vs_sma50=s * np.sign(d.c[j] - d.sma50[j]),
                       coin_ret28=s * d.ret28[j], coin_ret7=s * d.ret7[j])
        if jb >= 200:
            row.update(btc_vs_sma200=s * np.sign(btc.c[jb] - btc.sma200[jb]), btc_ret28=s * btc.ret28[jb],
                       btc_ret7=s * btc.ret7[jb])
        if 0 <= i < len(b.t) and atr[i] > 0:
            row.update(extension_atr=s * (b.c[i] - e20[i]) / atr[i])
            if adx is not None:
                row.update(adx=adx[i])
        if k >= 0:
            row.update(funding_bps=s * fr.rate.iloc[k] * 1e4)       # + = the trade's side is paying (crowded)
        rows.append(row)
    f = pd.DataFrame(rows).set_index("idx")
    t = t.join(f)
    # how many other live-coin signals fired the same side on the same bar (one market move, many bets)
    t["same_side_cluster"] = t.groupby(["sig_t", "side"]).coin.transform("nunique")
    t["weekday"] = pd.to_datetime(t.entry_t, unit="ms").dt.dayofweek
    t["period"] = np.where(t.entry_t < pd.Timestamp("2025-01-01", tz="UTC").value // 10**6, "2021-24", "2025-26")
    t.to_parquet(OUT)
    print(f"{len(t)} signals with features -> {OUT}")
    return t


FILTERS = {   # name: (rule that REMOVES the trade, plain-English description) - fixed before looking at results
    "against_coin_200d_trend": (lambda t: t.coin_vs_sma200 < 0, "trade against the coin's 200-day trend"),
    "against_coin_50d_trend": (lambda t: t.coin_vs_sma50 < 0, "trade against the coin's 50-day trend"),
    "against_btc_200d_trend": (lambda t: t.btc_vs_sma200 < 0, "trade against BTC's 200-day trend"),
    "against_btc_month": (lambda t: t.btc_ret28 < -5, "BTC moved >5% against the trade over 28 days"),
    "btc_week_against": (lambda t: t.btc_ret7 < -3, "BTC moved >3% against the trade over 7 days"),
    "coin_month_against": (lambda t: t.coin_ret28 < 0, "coin's last 28 days went against the trade"),
    "overextended_3atr": (lambda t: t.extension_atr > 3, "entry more than 3 ATR beyond the 20-bar average"),
    "weak_trend_adx20": (lambda t: t.adx < 20, "ADX below 20 (no trend)"),
    "crowded_funding": (lambda t: t.funding_bps > 1.0, "funding > 0.01%/8h paid by our side (crowded)"),
    "cluster_4plus": (lambda t: t.same_side_cluster >= 4, "4+ coins fired the same side on the same bar"),
    "weekend": (lambda t: t.weekday >= 5, "signal on a weekend"),
}


def filters() -> None:
    t = pd.read_parquet(OUT)
    base = t.groupby("period").r_net.agg(n="size", avg="mean")
    print("baseline:", {p: f"n={int(r.n)} avg {r.avg:+.3f}R" for p, r in base.iterrows()})
    rows = []
    for name, (rule, desc) in FILTERS.items():
        mask = rule(t).fillna(False)
        rec = {"filter": name, "removes": desc}
        ok = True
        for p in ("2021-24", "2025-26"):
            g, keep = t[(t.period == p) & mask], t[(t.period == p) & ~mask]
            rec[f"removed {p}"] = len(g)
            rec[f"removed avg {p}"] = round(g.r_net.mean(), 3) if len(g) else None
            rec[f"kept avg {p}"] = round(keep.r_net.mean(), 3) if len(keep) else None
            rec[f"total R kept-all {p}"] = round(keep.r_net.sum() - t[t.period == p].r_net.sum(), 1)
            ok &= bool(len(g) >= 30 and g.r_net.mean() < 0)
        rec["helps in both"] = ok
        rows.append(rec)
    out = pd.DataFrame(rows)
    pd.set_option("display.width", 250)
    print(out.to_string(index=False))
    out.to_csv("data/lab/edge_filters.csv", index=False)


if __name__ == "__main__":
    {"features": features, "filters": filters}[sys.argv[1]]()
