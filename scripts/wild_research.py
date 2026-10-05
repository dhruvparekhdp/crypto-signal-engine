"""Wild-market book research: which strategies make money when Bitcoin is in its wildest third of volatility?

The trend book loses in those markets (scripts/mirror_analysis.py), and the 4h grid hinted that mean-reversion
and fade strategies do the opposite. This checks that properly, without hindsight:

  1. tag every grid trade with Bitcoin's volatility rank at the signal (point in time, analysis.regime_gate)
  2. keep trades taken in wild markets (rank > 0.67)
  3. CHOOSE settings on 2021-2023 only (t > 2.5 on at least 40 trades), then
  4. TEST the chosen ones on 2024-2026 wild markets they never saw, next to random entries in the same markets
     (the grid's `random` strategy: same exits, same costs)
  5. walk-forward: choose on 24 months, trade the next 6, roll; report the unseen trades only

    python -m scripts.wild_research --out data/lab/wild_research.json
"""
from __future__ import annotations

import argparse
import json

import numpy as np
import pandas as pd

from scripts.regime_filter import tag

RUNS = {"4h": "data/lab/runs/full2_4h/trades.parquet", "8h": "data/lab/runs/full2_8h/trades.parquet",
        "2h": "data/lab/runs/full2_2h/trades.parquet", "12h": "data/lab/runs/full2_12h/trades.parquet"}
SPLIT = pd.Timestamp("2024-01-01").value // 10**6
COLS = ["symbol", "sig_t", "r_net", "cfg", "strategy"]


def load(tfs) -> pd.DataFrame:
    out = []
    for tf in tfs:
        try:
            t = pd.read_parquet(RUNS[tf], columns=COLS)
        except (FileNotFoundError, OSError):
            continue
        out.append(t.assign(tf=tf))
    t = pd.concat(out, ignore_index=True)
    t["key"] = t.tf + " · " + t.cfg
    return t


def stats(r: pd.Series) -> dict:
    n = len(r)
    if n < 2:
        return {"n": n, "r": float(r.mean()) if n else None, "t": None, "win": None, "total": float(r.sum())}
    return {"n": n, "r": float(r.mean()), "t": float(r.mean() / (r.std(ddof=1) / np.sqrt(n))), "win": float((r > 0).mean()),
            "total": float(r.sum())}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tfs", default="2h,4h,8h,12h")
    ap.add_argument("--min-n", type=int, default=40)
    ap.add_argument("--min-t", type=float, default=2.5)
    ap.add_argument("--out", default="data/lab/wild_research.json")
    a = ap.parse_args()
    t = load(a.tfs.split(","))
    print(f"{len(t):,} grid trades on {sorted(t.tf.unique())}; tagging market volatility...", flush=True)
    t = tag(t.drop(columns=[c for c in ("coin_adx",) if c in t]))
    t["wild"] = t.btc_vol_rank > 0.67
    w = t[t.wild]
    print(f"{len(w):,} trades in wild markets ({len(w) / len(t):.0%})\n", flush=True)
    rnd = w[w.strategy == "random"]
    train, test = w[w.sig_t < SPLIT], w[w.sig_t >= SPLIT]

    # 3. choose on 2021-2023
    picks = []
    for k, x in train.groupby("key"):
        if x.strategy.iloc[0] == "random" or len(x) < a.min_n:
            continue
        s = stats(x.r_net)
        if s["t"] is not None and s["t"] > a.min_t:
            picks.append((k, s))
    picks.sort(key=lambda p: -p[1]["t"])
    tested = sum(1 for k, x in train.groupby("key") if len(x) >= a.min_n)
    print(f"chosen on 2021-2023: {len(picks)} of {tested} settings (t > {a.min_t}); expected by luck at this bar: "
          f"~{tested * 0.006:.0f}\n")
    rows = []
    for k, s in picks[:40]:
        te = stats(test[test.key == k].r_net)
        rows.append({"key": k, "train": s, "test": te})
        print(f"  {k[:62]:62} train {s['r']:+.3f} (n {s['n']:4}, t {s['t']:.1f})  ->  test {te['r'] if te['r'] is None else round(te['r'], 3):>7} (n {te['n']})")
    chosen_test = test[test.key.isin([k for k, _ in picks])]
    summary = {"chosen_test": stats(chosen_test.r_net), "random_test": stats(rnd[rnd.sig_t >= SPLIT].r_net),
               "random_train": stats(rnd[rnd.sig_t < SPLIT].r_net), "all_wild_test": stats(test.r_net)}
    print(f"\nunseen 2024-2026 wild markets: chosen settings {summary['chosen_test']['r']:+.3f} R/trade "
          f"(n {summary['chosen_test']['n']}, t {summary['chosen_test']['t'] or 0:.1f}) vs random entries "
          f"{summary['random_test']['r']:+.3f} (n {summary['random_test']['n']})")

    # 5. rolling walk-forward
    w = w.sort_values("sig_t").copy()
    w["ym"] = pd.to_datetime(w.sig_t, unit="ms").dt.to_period("M")
    months = sorted(w.ym.unique())
    oos, folds, i = [], [], 24
    while i < len(months):
        tr = w[w.ym.isin(months[i - 24:i])]
        te = w[w.ym.isin(months[i:i + 6])]
        keep = [k for k, x in tr.groupby("key") if x.strategy.iloc[0] != "random" and len(x) >= a.min_n
                and (st := stats(x.r_net))["t"] is not None and st["t"] > a.min_t]
        part = te[te.key.isin(keep)]
        oos.append(part.r_net)
        folds.append({"test": f"{months[i]}..{months[min(i + 6, len(months)) - 1]}", "settings": len(keep), **stats(part.r_net)})
        i += 6
    wf = stats(pd.concat(oos)) if oos else {}
    print(f"walk-forward (choose 24 months, trade next 6): {wf.get('n', 0)} unseen trades, "
          f"{(wf.get('r') or 0):+.3f} R/trade (t {wf.get('t') or 0:.1f})")
    for f in folds:
        print(f"   {f['test']}: {f['settings']} settings, n {f['n']}, R {f['r'] if f['r'] is None else round(f['r'], 3)}")
    # by strategy family in wild markets, whole sample (descriptive)
    fam = [{"strategy": s, **stats(x.r_net)} for s, x in w.groupby("strategy") if len(x) >= 200]
    fam.sort(key=lambda r: -(r["r"] or 0))
    with open(a.out, "w") as f:
        json.dump({"summary": summary, "picks": rows, "walk_forward": wf, "folds": folds, "by_strategy": fam,
                   "settings_tested": tested}, f, default=float)
    print("saved", a.out)


if __name__ == "__main__":
    main()
