"""Collect every rules-only run into one JSON: per-timeframe significance, walk-forward, cost anatomy, top configs.

    python -m scripts.noai_report_data --out data/lab/noai_report.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

from analysis.lab.metrics import walk_forward

RUNS = [("15m", "full_5y"), ("1h", "htf_1h"), ("2h", "htf_2h"), ("4h", "htf_4h"), ("4h (wider grid)", "htf_4h_wide"),
        ("8h", "htf_8h"), ("12h", "htf_12h"), ("1d", "cand_1d")]


def analyse(name: str, run: str, root: Path) -> dict | None:
    d = root / run
    if not (d / "trades.parquet").exists():
        return None
    t = pd.read_parquet(d / "trades.parquet", columns=["cfg", "strategy", "side", "entry_t", "exit_t", "r_net", "r_gross", "fee_frac", "stop_frac", "reason", "bars"])
    t = t[t.strategy != "random"]
    g = t.groupby("cfg").agg(n=("r_net", "size"), net=("r_net", "mean"), gross=("r_gross", "mean"), sd=("r_net", "std"), win=("r_net", lambda x: (x > 0).mean())).reset_index()
    g = g[g.n >= 30]
    g["t"] = g.net / (g.sd / np.sqrt(g.n))
    g["p"] = (1 - stats.norm.cdf(g.t))
    m = len(g)
    g["p_bonf"] = (g.p * m).clip(upper=1)
    g["strategy"] = g.cfg.str.split("|").str[0]
    g["params"] = g.cfg.str.split("|").str[1]
    g["exit"] = g.cfg.str.split("|").str[2]
    best = g.sort_values("net", ascending=False).head(8)
    # walk-forward: pick the best config on 12 months, judge it on the next 3 unseen, per strategy
    wf = []
    for sid, d_ in t.groupby("strategy"):
        w = walk_forward(d_, "cfg", is_months=12, oos_months=3, min_is_trades=30)
        o = w.get("oos", {})
        if o.get("n", 0) >= 30:
            wf.append({"strategy": sid, "oos_n": o["n"], "oos_exp_r": o["expectancy_r"], "ci_lo": o["ci_lo"], "ci_hi": o["ci_hi"],
                       "folds_pos": w["folds_positive"], "folds": w["folds_total"]})
    wf.sort(key=lambda r: -r["oos_exp_r"])
    # cost anatomy over all trades of the run, weighted by trades
    return {"tf": name, "run": run, "configs": m, "trades": int(len(t)), "positive": int((g.net > 0).sum()), "t_gt_2": int((g.t > 2).sum()),
            "bonf_sig": int((g.p_bonf < 0.05).sum()), "mean_net": float(t.r_net.mean()), "mean_gross": float(t.r_gross.mean()),
            "cost_r": float((t.r_gross - t.r_net).mean()), "mean_stop_pct": float(t.stop_frac.mean() * 100),
            "best": [{k: (float(r[k]) if k in ("net", "gross", "t", "p_bonf", "win") else (int(r[k]) if k == "n" else r[k])) for k in
                      ("strategy", "params", "exit", "n", "win", "net", "gross", "t", "p_bonf")} for _, r in best.iterrows()],
            "walk_forward": wf[:8], "wf_positive_strategies": sum(1 for r in wf if r["ci_lo"] > 0), "wf_strategies": len(wf)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="data/lab/runs")
    ap.add_argument("--out", default="data/lab/noai_report.json")
    a = ap.parse_args()
    out = []
    for name, run in RUNS:
        r = analyse(name, run, Path(a.root))
        if r:
            out.append(r)
            print(f"{name:16} {r['trades']:>9,} trades {r['configs']:>4} configs | positive {r['positive']:>3} | t>2 {r['t_gt_2']:>3} | Bonferroni-significant {r['bonf_sig']:>3} | "
                  f"gross {r['mean_gross']:+.3f} cost {r['cost_r']:.3f} net {r['mean_net']:+.3f} R | WF-significant strategies {r['wf_positive_strategies']}/{r['wf_strategies']}", flush=True)
    Path(a.out).write_text(json.dumps(out, default=float))
    print("saved", a.out)


if __name__ == "__main__":
    main()
