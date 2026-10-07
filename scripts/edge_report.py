"""Edge report (roadmap Q-1, Q-5, Q-6, Q-8, Q-4, Q-12): is the swing book's edge real, where does it come from, and
do the proposed filters / exits / higher costs change it?

    python -m scripts.edge_report --out data/lab/edge_report.json

Significance is cluster-aware: trades opened in the same 4h window are one market bet (block bootstrap).
Filters use only what was known at the signal bar's close (sig_t). In-sample: the strategies were chosen on this
history; treat every number as an upper bound.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from analysis import regime_gate
from analysis.lab import features as F
from analysis.lab.data import INTERVAL_MS, load_bars
from analysis.significance import cluster_stats

RUNS = Path("data/lab/runs")
EXCLUDE = {"BCHUSDT", "LTCUSDT"}
H4 = INTERVAL_MS["4h"]
DAY = INTERVAL_MS["1d"]


def stats(df: pd.DataFrame) -> dict:
    if len(df) < 3:
        return {"n": len(df)}
    s = cluster_stats(df.r_net.to_numpy(), (df.entry_t // H4).to_numpy())
    s["win"] = float((df.r_net > 0).mean())
    return {k: (round(v, 3) if isinstance(v, float) else v) for k, v in s.items()}


def load_run(name: str, tf: str) -> pd.DataFrame | None:
    p = RUNS / name / "trades.parquet"
    if not p.exists():
        return None
    t = pd.read_parquet(p)
    t = t[~t.symbol.isin(EXCLUDE)].copy()
    t["tf"] = tf
    t["spec"] = tf + "@" + t.strategy
    return t


def tag_regimes(t: pd.DataFrame) -> pd.DataFrame:
    b = load_bars("BTCUSDT", "1d")
    known = b.t + DAY                                     # a daily bar is known at its close
    rank = regime_gate.vol_rank_series(b.c)
    sma200 = F.sma(b.c, 200)
    i = np.searchsorted(known, t.sig_t.to_numpy(), side="right") - 1
    ok = i >= 0
    t["btc_vol_rank"] = np.where(ok, rank[np.clip(i, 0, None)], np.nan)
    t["btc_trend"] = np.where(ok, np.where(b.c[np.clip(i, 0, None)] > sma200[np.clip(i, 0, None)], "up", "down"), "na")
    t["vol_bucket"] = pd.cut(t.btc_vol_rank, [-0.01, 1 / 3, 2 / 3, 1.01], labels=["calm", "normal", "wild"])
    t["year"] = pd.to_datetime(t.entry_t, unit="ms").dt.year
    return t


def tag_filters(t: pd.DataFrame) -> pd.DataFrame:
    """Per trade, at its signal bar: KAMA and McGinley trend agreement, Garman-Klass volatility percentile."""
    cols = {"kama_ok": [], "mcg_ok": [], "gk_pct": []}
    idx = []
    for (sym, tf), g in t.groupby(["symbol", "tf"]):
        b = load_bars(sym, tf)
        close_t = b.t + INTERVAL_MS[tf]
        k, m = F.kama(b.c), F.mcginley(b.c)
        gk = F.garman_klass(b.o, b.h, b.l, b.c)
        bars_year = int(365 * DAY / INTERVAL_MS[tf])
        gk_pct = pd.Series(gk).rolling(bars_year, min_periods=bars_year // 2).rank(pct=True).to_numpy()
        j = np.searchsorted(close_t, g.sig_t.to_numpy(), side="left")
        j = np.clip(j, 1, len(b.c) - 1)
        side = g.side.to_numpy()
        kama_up, mcg_up = k[j] > k[j - 1], m[j] > m[j - 1]
        cols["kama_ok"] += list(np.where(side > 0, (b.c[j] > k[j]) & kama_up, (b.c[j] < k[j]) & ~kama_up))
        cols["mcg_ok"] += list(np.where(side > 0, (b.c[j] > m[j]) & mcg_up, (b.c[j] < m[j]) & ~mcg_up))
        cols["gk_pct"] += list(gk_pct[j])
        idx += list(g.index)
    for c, v in cols.items():
        t[c] = pd.Series(v, index=idx, dtype=float if c == "gk_pct" else bool).reindex(t.index)
    return t


def by(t: pd.DataFrame, col: str) -> dict:
    return {str(k): stats(g) for k, g in t.groupby(col, observed=True)}


def filter_test(t: pd.DataFrame, mask: pd.Series, name: str) -> dict:
    kept, dropped = t[mask], t[~mask]
    return {"filter": name, "kept_share": round(len(kept) / max(len(t), 1), 3),
            "kept": stats(kept), "dropped": stats(dropped)}


def exits_section() -> dict:
    out = {}
    for tf in ("4h", "8h"):
        t = load_run(f"exits_{tf}", tf)
        if t is None:
            continue
        t["exit_model"] = t.cfg.str.split("|").str[2]
        out[tf] = {str(e): stats(g) for e, g in t.groupby("exit_model")}
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/lab/edge_report.json")
    a = ap.parse_args()
    live = pd.concat([x for x in (load_run("filt_vol_4h", "4h"), load_run("filt_vol_8h", "8h")) if x is not None],
                     ignore_index=True)
    raw = pd.concat([x for x in (load_run("swing_null_4h", "4h"), load_run("swing_null_8h", "8h")) if x is not None],
                    ignore_index=True)
    live, raw = tag_regimes(live), tag_regimes(raw)
    raw = tag_filters(raw)
    rep = {
        "live_book": {"all": stats(live), "by_spec": by(live, "spec"), "by_year": by(live, "year"),
                      "by_side": by(live.assign(side_name=np.where(live.side > 0, "long", "short")), "side_name"),
                      "by_coin": by(live, "symbol"), "by_btc_trend": by(live, "btc_trend")},
        "unfiltered": {"all": stats(raw), "by_vol_regime": by(raw, "vol_bucket"), "by_btc_trend": by(raw, "btc_trend"),
                       "by_spec": by(raw, "spec")},
        "filters_on_unfiltered": [
            filter_test(raw, raw.btc_vol_rank <= 2 / 3, "live: skip wild BTC volatility (rank > 0.67)"),
            filter_test(raw, raw.kama_ok.astype(bool), "Q-5 KAMA(10,2,30) agrees with direction"),
            filter_test(raw, raw.mcg_ok.astype(bool), "Q-5 McGinley(14) agrees with direction"),
            filter_test(raw, raw.gk_pct >= 1 / 3, "Q-6 skip dead markets (Garman-Klass vol bottom third)"),
            filter_test(raw, (raw.btc_vol_rank <= 2 / 3) & raw.kama_ok.astype(bool), "live filter + KAMA"),
            filter_test(raw, (raw.btc_vol_rank <= 2 / 3) & (raw.gk_pct >= 1 / 3), "live filter + GK gate"),
        ],
        "exits": exits_section(),
    }
    st = load_run("stress_swing_4h", "4h")
    if st is not None:
        base = raw[raw.tf == "4h"]
        rep["cost_stress_4h"] = {"normal_costs": by(base, "spec"), "stress_costs": by(st, "spec")}
    Path(a.out).write_text(json.dumps(rep, indent=1, default=str))

    def line(name, s):
        if s.get("n", 0) < 3:
            return f"  {name:44} n={s.get('n')}"
        return (f"  {name:44} n={s['n']:>5} mean {s['mean']:+.3f}R  win {s['win']:.0%}  naive t {s['naive_t']:>5.1f}  "
                f"cluster t {s['cluster_t']:>5.1f}  95% [{s['ci_lo']:+.3f}, {s['ci_hi']:+.3f}]")
    print("LIVE BOOK (filter on, BCH/LTC out)"); print(line("all", rep["live_book"]["all"]))
    for k, s in rep["live_book"]["by_spec"].items():
        print(line(k, s))
    print("by year");  [print(line(k, s)) for k, s in rep["live_book"]["by_year"].items()]
    print("by side");  [print(line(k, s)) for k, s in rep["live_book"]["by_side"].items()]
    print("by BTC trend (200d)"); [print(line(k, s)) for k, s in rep["live_book"]["by_btc_trend"].items()]
    print("by coin");  [print(line(k, s)) for k, s in rep["live_book"]["by_coin"].items()]
    print("\nUNFILTERED by BTC volatility regime"); [print(line(k, s)) for k, s in rep["unfiltered"]["by_vol_regime"].items()]
    print("\nFILTER EXPERIMENTS (on unfiltered trades)")
    for f in rep["filters_on_unfiltered"]:
        print(f" {f['filter']} (keeps {f['kept_share']:.0%})"); print(line("kept", f["kept"])); print(line("dropped", f["dropped"]))
    for tf, d in rep["exits"].items():
        print(f"\nEXIT VARIANTS {tf}"); [print(line(k, s)) for k, s in d.items()]
    if "cost_stress_4h" in rep:
        print("\nCOST STRESS 4h (normal vs stress)")
        for k in rep["cost_stress_4h"]["normal_costs"]:
            print(line(k + " normal", rep["cost_stress_4h"]["normal_costs"][k]))
            print(line(k + " stress", rep["cost_stress_4h"]["stress_costs"].get(k, {"n": 0})))
    print("\nsaved", a.out)


if __name__ == "__main__":
    main()
