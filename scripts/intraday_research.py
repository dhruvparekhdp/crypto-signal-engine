"""Intraday strategy research (owner, 15 Oct 2026): find a 1h/2h strategy with an edge after costs, honestly.

    python -m scripts.intraday_research screen            # rank configs on 2021-2024 (data/lab/runs/intra_screen_*)
    python -m scripts.intraday_research holdout           # re-test the selected ones on 2025-2026 (never seen)
    python -m scripts.intraday_research report            # holdout verdicts

Rules fixed BEFORE looking at results:
  selection (train 2021-10..2024-12): >= 150 trades, mean R >= +0.10 after costs, cluster t >= 3 (trades in the same
  hour are one bet), positive in >= 3 of 4 train years, and above the random-entry control. Ranked by cluster t.
  holdout (2025-01..2026-09, untouched): mean R > 0 with the 95% cluster-bootstrap range above 0, beats random entries
  (100 draws, judged at the ledger's N), and still positive under stress costs.
Both raw and with the live BTC-volatility filter are reported; the filter is not used to select.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from analysis.significance import cluster_stats

RUNS = Path("data/lab/runs")
OUT = Path("data/lab/intraday_research.json")
H1 = 3_600_000
SY = "BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,DOGEUSDT,XRPUSDT,ADAUSDT,AVAXUSDT,LINKUSDT,SUIUSDT"
EXIT_NAMES = {"sl2.0_rr2.0_h1440_beNone_trNone_pNone": "intra_a", "sl1.5_rr3.0_h2880_beNone_trNone_pNone": "intra_b"}


def _load(tf: str) -> pd.DataFrame | None:
    p = RUNS / f"intra_screen_{tf}" / "trades.parquet"
    if not p.exists():
        return None
    t = pd.read_parquet(p)
    t["tf"] = tf
    t["year"] = pd.to_datetime(t.entry_t, unit="ms").dt.year
    return t


def screen() -> list[dict]:
    rows = []
    control = {}
    for tf in ("1h", "2h"):
        t = _load(tf)
        if t is None:
            print(f"no screen run for {tf} yet")
            continue
        for cfg, g in t.groupby("cfg"):
            strat, params, exit_key = cfg.split("|")
            s = cluster_stats(g.r_net.to_numpy(), (g.entry_t // H1).to_numpy())
            years = g.groupby("year").r_net.mean()
            rec = {"tf": tf, "strategy": strat, "params": params, "exit": EXIT_NAMES.get(exit_key, exit_key),
                   "n": int(len(g)), "mean_r": round(float(g.r_net.mean()), 4), "win": round(float((g.r_net > 0).mean()), 3),
                   "cluster_t": round(float(s.get("cluster_t", 0) or 0), 2),
                   "ci_lo": round(float(s.get("ci_lo", 0) or 0), 4), "ci_hi": round(float(s.get("ci_hi", 0) or 0), 4),
                   "years_positive": int((years > 0).sum()), "years": {int(k): round(float(v), 3) for k, v in years.items()}}
            rows.append(rec)
            if strat == "random":
                control[(tf, rec["exit"])] = max(control.get((tf, rec["exit"]), -9), rec["mean_r"])
    for r in rows:
        ctl = control.get((r["tf"], r["exit"]), 0.0)
        r["vs_random"] = round(r["mean_r"] - ctl, 4)
        r["selected"] = (r["strategy"] != "random" and r["n"] >= 150 and r["mean_r"] >= 0.10 and r["cluster_t"] >= 3
                         and r["years_positive"] >= 3 and r["vs_random"] > 0.10)
    rows.sort(key=lambda r: -r["cluster_t"])
    sel = [r for r in rows if r["selected"]]
    print(f"{len(rows)} configurations screened; random control (best per tf/exit): "
          + ", ".join(f"{k[0]} {k[1]} {v:+.3f}R" for k, v in sorted(control.items())))
    print(f"\nTOP 15 by cluster t (train 2021-2024)")
    for r in rows[:15]:
        print(f"  {'*' if r['selected'] else ' '} {r['tf']} {r['strategy']:16} {r['params'][:28]:28} {r['exit']:8} n={r['n']:>5} "
              f"mean {r['mean_r']:+.3f}R t={r['cluster_t']:>5.1f} years+ {r['years_positive']}/4 vs rnd {r['vs_random']:+.3f}")
    print(f"\nSELECTED for holdout: {len(sel)}")
    OUT.write_text(json.dumps({"screen": rows, "selected": sel[:6]}, indent=1))
    return sel[:6]


def holdout() -> None:
    data = json.loads(OUT.read_text())
    for i, r in enumerate(data["selected"]):
        sets = [f"--set {r['strategy']}.{kv}" for kv in r["params"].split(",") if kv]
        for cost in ("india_gst", "stress"):
            name = f"intra_hold_{i}_{cost}"
            cmd = (f"{sys.executable} -m scripts.run_lab --strategies {r['strategy']} {' '.join(sets)} --tf {r['tf']} "
                   f"--exits {r['exit']} --symbols {SY} --since 2025-01-01 --cost {cost} --workers 3 "
                   f"{'--null-trials 100 ' if cost == 'india_gst' else ''}--ledger-family intraday "
                   f"{'--ledger-kind stress ' if cost == 'stress' else ''}--out data/lab/runs/{name}")
            print("running", name)
            subprocess.run(cmd, shell=True, check=False)


def report() -> None:
    data = json.loads(OUT.read_text())
    out = []
    for i, r in enumerate(data["selected"]):
        res = {"candidate": f"{r['tf']} {r['strategy']} {r['params']} {r['exit']}", "train_mean_r": r["mean_r"]}
        for cost in ("india_gst", "stress"):
            p = RUNS / f"intra_hold_{i}_{cost}"
            if not (p / "trades.parquet").exists():
                continue
            t = pd.read_parquet(p / "trades.parquet")
            s = cluster_stats(t.r_net.to_numpy(), (t.entry_t // H1).to_numpy())
            res[cost] = {"n": int(len(t)), "mean_r": round(float(t.r_net.mean()), 4),
                         "ci": [round(float(s.get("ci_lo", 0) or 0), 3), round(float(s.get("ci_hi", 0) or 0), 3)],
                         "cluster_t": round(float(s.get("cluster_t", 0) or 0), 2)}
            if cost == "india_gst" and (p / "summary.csv").exists():
                sm = pd.read_csv(p / "summary.csv")
                res["null_verdict"] = str(sm.get("null_verdict", pd.Series(["-"])).iloc[0])
        h, st = res.get("india_gst", {}), res.get("stress", {})
        res["passes"] = bool(h and h["mean_r"] > 0 and h["ci"][0] > 0 and str(res.get("null_verdict", "")).startswith("beats random")
                             and st and st["mean_r"] > 0)
        out.append(res)
        print(json.dumps(res))
    data["holdout"] = out
    OUT.write_text(json.dumps(data, indent=1))
    print(f"\n{sum(r['passes'] for r in out)} of {len(out)} candidates pass the holdout")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["screen", "holdout", "report"])
    a = ap.parse_args()
    {"screen": screen, "holdout": holdout, "report": report}[a.stage]()
    _ = np
