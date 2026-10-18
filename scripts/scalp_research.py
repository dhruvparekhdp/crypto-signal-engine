"""Scalping research (owner, 18 Oct 2026: "ETH moved 2360 -> 2430 -> 2368 in a day - why can't we scalp these moves?
develop and test in paper only"). Paper only: nothing here can reach the live book.

    python -m scripts.scalp_research screen     # rank configs on 2021-10..2024-12 (data/lab/runs/scalp_screen)
    python -m scripts.scalp_research holdout    # re-test the selected ones on 2025-01..2026-09 (never seen)
    python -m scripts.scalp_research report     # holdout verdicts

5m signals on BTC, ETH, SOL, BNB, XRP with 1m execution and Delta India fees (maker 0.02% / taker 0.05%, + 18% GST):
a market entry at the next open (taker) or a limit at the signal close resting 15 minutes (maker, filled only when
price trades THROUGH it); targets are limits (maker), stops are market orders (taker + slippage). Exits: stop 2 x
ATR(5m) between 0.3% and 2%, target 1R or 1.5R, 4-hour limit.

Rules fixed BEFORE looking at results:
  selection (train): >= 300 trades, mean R >= +0.10 after costs, cluster t >= 3 (trades in the same hour are one bet),
  positive in >= 3 of the 4 calendar years, and >= 0.10R above random entries with the same exit. Top 6 by cluster t.
  holdout (2025-01..2026-09, untouched): mean R > 0 with the 95% cluster-bootstrap range above 0, beats random
  entries (100 draws), and still positive with stress costs (wider slippage on market fills).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import pandas as pd

from analysis.significance import cluster_stats

RUNS = Path("data/lab/runs")
OUT = Path("data/lab/scalp_research.json")
H1 = 3_600_000
SY = "BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT"
STRATS = ("wick_reclaim,range_fade,vwap_stretch,vwap_revert,sweep_reclaim,climax_fade,bb_reversion,rsi2,z_revert,"
          "streak_revert,random")
EXIT_LIST = "scalp_1r_mkt,scalp_1r_lim,scalp_15r_mkt,scalp_15r_lim"


DAY_EXITS = "day_1r_mkt,day_1r_lim,day_15r_mkt,day_15r_lim"


def _exit_names() -> dict:
    from scripts.run_lab import EXITS
    return {EXITS[n].key(): n for n in (EXIT_LIST + "," + DAY_EXITS).split(",")}


def screen_day(tf: str) -> list[dict]:
    """The 15m / 1h iteration (18 Oct 2026: 5m scalps were fee-bound), judged by the same pre-set rules."""
    global OUT
    t = pd.read_parquet(RUNS / f"day_screen_{tf}" / "trades.parquet")
    OUT = Path(f"data/lab/day_research_{tf}.json")
    return _rank(t, f"day scalps {tf}")


def _lab(args: str, name: str) -> None:
    cmd = f"{sys.executable} -m scripts.run_lab {args} --out data/lab/runs/{name}"
    print("running", cmd, flush=True)
    subprocess.run(cmd, shell=True, check=False)


def run_screen() -> None:
    _lab(f"--strategies {STRATS} --tf 5m --grid --grid-limit 4 --exits {EXIT_LIST} --symbols {SY} "
         f"--until 2025-01-01 --cost delta_maker --workers 4 --ledger-family scalp "
         f"--hypothesis 'scalp research 18 Oct: 5m reversal scalps with maker limit entries (train 2021-2024)'",
         "scalp_screen")


def screen() -> list[dict]:
    p = RUNS / "scalp_screen" / "trades.parquet"
    if not p.exists():
        run_screen()
    # the volume-direction screen (owner's idea, same rules, same period) is judged together with the first one
    t = pd.concat([pd.read_parquet(x) for x in (p, RUNS / "scalp_screen_flow" / "trades.parquet") if x.exists()],
                  ignore_index=True)
    return _rank(t, "5m scalps")


def _rank(t: pd.DataFrame, label: str) -> list[dict]:
    t = t.copy()
    t["year"] = pd.to_datetime(t.entry_t, unit="ms").dt.year
    names = _exit_names()
    rows, control = [], {}
    for cfg, g in t.groupby("cfg"):
        strat, params, exit_key = cfg.split("|")
        s = cluster_stats(g.r_net.to_numpy(), (g.entry_t // H1).to_numpy())
        years = g.groupby("year").r_net.mean()
        rec = {"strategy": strat, "params": params, "exit": names.get(exit_key, exit_key), "n": int(len(g)),
               "mean_r": round(float(g.r_net.mean()), 4), "win": round(float((g.r_net > 0).mean()), 3),
               "cluster_t": round(float(s.get("cluster_t", 0) or 0), 2), "years_positive": int((years > 0).sum()),
               "years": {int(k): round(float(v), 3) for k, v in years.items()},
               "fee_r": round(float((g.fee_frac / g.stop_frac).mean()), 3)}
        rows.append(rec)
        if strat == "random":
            control[rec["exit"]] = max(control.get(rec["exit"], -9), rec["mean_r"])
    for r in rows:
        r["vs_random"] = round(r["mean_r"] - control.get(r["exit"], 0.0), 4)
        r["selected"] = (r["strategy"] != "random" and r["n"] >= 300 and r["mean_r"] >= 0.10 and r["cluster_t"] >= 3
                         and r["years_positive"] >= 3 and r["vs_random"] >= 0.10)
    rows.sort(key=lambda r: -r["cluster_t"])
    sel = [r for r in rows if r["selected"]][:6]
    print(f"{len(rows)} configurations; random control by exit: "
          + ", ".join(f"{k} {v:+.3f}R" for k, v in sorted(control.items())))
    print(f"\nTOP 25 by cluster t - {label} (train 2021-10..2024-12, Delta maker costs)")
    for r in rows[:25]:
        print(f"  {'*' if r['selected'] else ' '} {r['strategy']:14} {r['params'][:24]:24} {r['exit']:14} n={r['n']:>6} "
              f"mean {r['mean_r']:+.3f}R win {r['win']:.0%} t={r['cluster_t']:>5.1f} yrs+ {r['years_positive']}/4 "
              f"vs rnd {r['vs_random']:+.3f} fees {r['fee_r']:.2f}R")
    by_exit = pd.DataFrame(rows).groupby("exit").mean_r.describe()[["count", "mean", "max"]]
    print("\nall configs by exit (mean R):\n" + by_exit.round(3).to_string())
    print(f"\nSELECTED for holdout: {len(sel)}")
    OUT.write_text(json.dumps({"screen": rows, "selected": sel}, indent=1))
    return sel


def holdout(tf: str = "5m") -> None:
    data = json.loads(_out(tf).read_text())
    for i, r in enumerate(data["selected"]):
        sets = " ".join(f"--set {r['strategy']}.{kv}" for kv in r["params"].split(",") if kv)
        base = (f"--strategies {r['strategy']} {sets} --tf {tf} --exits {r['exit']} --symbols {SY} --since 2025-01-01 "
                f"--workers 4 --ledger-family scalp")
        _lab(base + " --cost delta_maker --null-trials 100", f"scalp_hold_{tf}_{i}_base")
        _lab(base + " --cost delta_maker_stress --ledger-kind stress", f"scalp_hold_{tf}_{i}_stress")


def _out(tf: str) -> Path:
    return OUT if tf == "5m" else Path(f"data/lab/day_research_{tf}.json")


def report(tf: str = "5m") -> None:
    data = json.loads(_out(tf).read_text())
    out = []
    for i, r in enumerate(data["selected"]):
        res = {"candidate": f"{r['strategy']} {r['params']} {r['exit']}", "train_mean_r": r["mean_r"]}
        for kind in ("base", "stress"):
            p = RUNS / f"scalp_hold_{tf}_{i}_{kind}"
            if not (p / "trades.parquet").exists():
                continue
            t = pd.read_parquet(p / "trades.parquet")
            s = cluster_stats(t.r_net.to_numpy(), (t.entry_t // H1).to_numpy())
            res[kind] = {"n": int(len(t)), "mean_r": round(float(t.r_net.mean()), 4),
                         "ci": [round(float(s.get("ci_lo", 0) or 0), 3), round(float(s.get("ci_hi", 0) or 0), 3)],
                         "cluster_t": round(float(s.get("cluster_t", 0) or 0), 2)}
            if kind == "base" and (p / "summary.csv").exists():
                sm = pd.read_csv(p / "summary.csv")
                res["null_verdict"] = str(sm.get("null_verdict", pd.Series(["-"])).iloc[0])
        b, st = res.get("base", {}), res.get("stress", {})
        res["passes"] = bool(b and b["mean_r"] > 0 and b["ci"][0] > 0 and str(res.get("null_verdict", "")).startswith(
            "beats random") and st and st["mean_r"] > 0)
        out.append(res)
        print(json.dumps(res))
    data["holdout"] = out
    _out(tf).write_text(json.dumps(data, indent=1))
    print(f"\n{sum(r['passes'] for r in out)} of {len(out)} candidates pass the holdout")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["screen", "holdout", "report"])
    ap.add_argument("--tf", default="5m", choices=["5m", "15m", "1h"])
    a = ap.parse_args()
    if a.stage == "screen":
        screen() if a.tf == "5m" else screen_day(a.tf)
    else:
        {"holdout": holdout, "report": report}[a.stage](a.tf)
