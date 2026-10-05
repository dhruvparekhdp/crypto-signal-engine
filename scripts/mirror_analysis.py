"""Every live trade next to its mirror: same coin, same entry bar, opposite side, same exit rules and costs.

Where the original wins and the mirror loses, the strategy read the market right. Where the mirror wins, it read it
backwards. Splitting by year, coin, side and the market regime at entry shows WHEN each happens, and a walk-forward
check tests whether "flip to the mirror in regime X" would have helped on months the rule had not seen.

    python -m scripts.mirror_analysis --out data/lab/mirror_analysis.json
"""
from __future__ import annotations

import argparse
import json

import numpy as np
import pandas as pd

from analysis.lab import features as F
from analysis.lab.data import load_bars

PAIRS = [("data/lab/runs/cand_4h/trades.parquet", "data/lab/runs/mirror_4h/trades.parquet", "4h"),
         ("data/lab/runs/null_8h/trades.parquet", "data/lab/runs/mirror_8h/trades.parquet", "8h")]
DAY = 86_400_000


def _live(df: pd.DataFrame) -> pd.DataFrame:
    return df[df.cfg.str.contains("sl3.0_rr3.0") & df.cfg.str.contains("h10080")]


def paired() -> pd.DataFrame:
    out = []
    for orig_f, mir_f, tf in PAIRS:
        o = _live(pd.read_parquet(orig_f))
        try:
            m = _live(pd.read_parquet(mir_f))
        except (FileNotFoundError, OSError):
            continue
        m = m.assign(strategy=m.strategy.str.replace("mirror_", "", regex=False))
        j = o.merge(m[["symbol", "sig_t", "strategy", "r_net", "reason", "side"]], on=["symbol", "sig_t", "strategy"],
                    suffixes=("", "_m"))
        out.append(j.assign(tf=tf))
    return pd.concat(out, ignore_index=True)


def regimes(df: pd.DataFrame) -> pd.DataFrame:
    """Market state at the signal, from daily bars that had CLOSED before it (no look-ahead)."""
    btc = load_bars("BTCUSDT", "1d")
    bt, bc = btc.t + DAY, btc.c                                     # a daily bar is known once it has closed
    b_ema = F.ema(bc, 50)
    b_vol = pd.Series(np.diff(np.log(bc), prepend=np.nan)).rolling(30).std().to_numpy() * np.sqrt(365)
    b_ret30 = bc / np.roll(bc, 30) - 1
    cols = {"btc_trend": [], "btc_vol": [], "btc_ret30": [], "coin_adx": [], "coin_trend": []}
    coin = {}
    for s in df.symbol.unique():
        b = load_bars(s, "1d")
        coin[s] = (b.t + DAY, F.adx(b.h, b.l, b.c)[0],
                   b.c, F.ema(b.c, 50))
    for r in df.itertuples(index=False):
        i = np.searchsorted(bt, r.sig_t, side="right") - 1
        cols["btc_trend"].append("up" if i >= 50 and bc[i] > b_ema[i] else "down")
        cols["btc_vol"].append(b_vol[i] if i >= 30 else np.nan)
        cols["btc_ret30"].append(b_ret30[i] if i >= 30 else np.nan)
        ct, adx, cc, ce = coin[r.symbol]
        k = np.searchsorted(ct, r.sig_t, side="right") - 1
        cols["coin_adx"].append(adx[k] if k >= 30 else np.nan)
        cols["coin_trend"].append("up" if k >= 50 and cc[k] > ce[k] else "down")
    df = df.assign(**cols)
    # terciles from history only would be stricter; these are labels for describing, the walk-forward below is strict
    df["vol_regime"] = pd.qcut(df.btc_vol, 3, labels=["calm", "normal", "wild"])
    df["adx_regime"] = pd.cut(df.coin_adx, [0, 20, 30, 200], labels=["no trend (<20)", "weak (20-30)", "strong (>30)"])
    df["with_btc"] = np.where((df.side > 0) == (df.btc_trend == "up"), "with BTC trend", "against BTC trend")
    return df


def table(df: pd.DataFrame, by) -> list[dict]:
    g = df.groupby(by, observed=True)
    rows = []
    for k, x in g:
        n = len(x)
        diff = x.r_net - x.r_net_m
        se = diff.std(ddof=1) / np.sqrt(n) if n > 1 else np.nan
        rows.append({"key": k if not isinstance(k, tuple) else " · ".join(map(str, k)), "n": n,
                     "orig_r": float(x.r_net.mean()), "mirror_r": float(x.r_net_m.mean()),
                     "orig_win": float((x.r_net > 0).mean()), "mirror_win": float((x.r_net_m > 0).mean()),
                     "orig_total": float(x.r_net.sum()), "mirror_total": float(x.r_net_m.sum()),
                     "edge_t": float(diff.mean() / se) if se and np.isfinite(se) and se > 0 else None,
                     "both_lost": float(((x.r_net <= 0) & (x.r_net_m <= 0)).mean())})
    return rows


def walk_forward_flip(df: pd.DataFrame, keys=("strategy", "tf", "adx_regime", "with_btc"), train_months=24, test_months=6,
                      min_n=30) -> dict:
    """In each fold, learn on the past which buckets the mirror beat (t < -2 on the difference), flip those on the
    next unseen months, and compare with always trading the original."""
    df = df.sort_values("sig_t").copy()
    df["ym"] = pd.to_datetime(df.sig_t, unit="ms").dt.to_period("M")
    months = sorted(df.ym.unique())
    folds, base, flip = [], [], []
    i = train_months
    while i < len(months):
        tr = df[df.ym.isin(months[i - train_months:i])]
        te = df[df.ym.isin(months[i:i + test_months])]
        bad = set()
        for k, x in tr.groupby(list(keys), observed=True):
            if len(x) >= min_n:
                d = x.r_net - x.r_net_m
                t = d.mean() / (d.std(ddof=1) / np.sqrt(len(x)))
                if t < -2:
                    bad.add(k)
        key = te[list(keys)].apply(tuple, axis=1)
        flipped = key.isin(bad)
        r = np.where(flipped, te.r_net_m, te.r_net)
        base.extend(te.r_net.tolist())
        flip.extend(r.tolist())
        folds.append({"test": f"{months[i]}..{months[min(i + test_months, len(months)) - 1]}", "flipped_buckets": len(bad),
                      "flipped_trades": int(flipped.sum()), "orig_r": float(te.r_net.mean()), "with_flip_r": float(np.mean(r))})
        i += test_months
    base, flip = np.array(base), np.array(flip)
    d = flip - base
    return {"keys": list(keys), "folds": folds, "oos_trades": len(base), "orig_r": float(base.mean()),
            "with_flip_r": float(flip.mean()), "diff_r": float(d.mean()),
            "diff_t": float(d.mean() / (d.std(ddof=1) / np.sqrt(len(d)))) if d.std() > 0 else 0.0}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="data/lab/mirror_analysis.json")
    a = ap.parse_args()
    df = regimes(paired())
    df["year"] = pd.to_datetime(df.sig_t, unit="ms").dt.year
    df["side_name"] = np.where(df.side > 0, "long", "short")
    df["coin"] = df.symbol.str.replace("USDT", "", regex=False)
    df["exit_pair"] = df.reason + " / mirror " + df.reason_m
    print(f"{len(df)} paired trades (original + mirror on the same bar)\n")
    res = {"n": len(df), "orig_r": float(df.r_net.mean()), "mirror_r": float(df.r_net_m.mean()),
           "both_lost": float(((df.r_net <= 0) & (df.r_net_m <= 0)).mean()),
           "by": {}}
    for name, by in [("strategy", ["strategy", "tf"]), ("year", "year"), ("coin", "coin"), ("side", "side_name"),
                     ("btc_trend", "btc_trend"), ("vol_regime", "vol_regime"), ("adx_regime", "adx_regime"),
                     ("with_btc", "with_btc"), ("strategy_adx", ["strategy", "adx_regime"]),
                     ("strategy_with_btc", ["strategy", "with_btc"]), ("year_side", ["year", "side_name"]),
                     ("exit_pair", "exit_pair")]:
        res["by"][name] = table(df, by)
        print(f"-- by {name}")
        for r in res["by"][name]:
            print(f"   {str(r['key']):42} n={r['n']:5}  orig {r['orig_r']:+.3f}  mirror {r['mirror_r']:+.3f}  "
                  f"both lost {r['both_lost']:.0%}  t={r['edge_t'] if r['edge_t'] is None else round(r['edge_t'], 1)}")
    res["walk_forward_flip"] = walk_forward_flip(df)
    w = res["walk_forward_flip"]
    print(f"\nwalk-forward flip test on {w['oos_trades']} unseen trades: original {w['orig_r']:+.3f} R/trade, "
          f"with flips {w['with_flip_r']:+.3f} (t={w['diff_t']:.1f})")
    res["monthly"] = [{"m": str(k), "orig": float(x.r_net.sum()), "mirror": float(x.r_net_m.sum()), "n": len(x)}
                      for k, x in df.groupby(pd.to_datetime(df.sig_t, unit="ms").dt.to_period("M"))]
    with open(a.out, "w") as f:
        json.dump(res, f, default=lambda o: None if isinstance(o, float) and not np.isfinite(o) else str(o))
    print("saved", a.out)


if __name__ == "__main__":
    main()
