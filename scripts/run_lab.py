"""Strategy Lab command line. No AI, no network: reads data/lake and writes data/lab/runs/.

    python -m scripts.run_lab --list
    python -m scripts.run_lab --strategies donchian,rsi2,bb_reversion --since 2023-01-01
    python -m scripts.run_lab --family mean_reversion,trend --grid --null-trials 30
    python -m scripts.run_lab --all --exits std,trail --risk 0.01,0.02,0.05 --leverage 3,5,10
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

import pandas as pd

from analysis.lab.data import coverage
from analysis.lab.runner import CRYPTO, RunSpec, run_lab
from analysis.lab.simulate import ExitModel
from analysis.lab.strategies import REGISTRY, catalog
from analysis.lab.wallet import WalletConfig

EXITS = {
    "std": ExitModel(stop_atr=1.5, rr=2.0),
    "tight": ExitModel(stop_atr=1.0, rr=1.5, max_hold_min=480),
    "wide": ExitModel(stop_atr=2.5, rr=2.0, max_hold_min=2880),
    "scalp": ExitModel(stop_atr=1.0, rr=1.0, max_hold_min=240),
    "be": ExitModel(stop_atr=1.5, rr=3.0, be_trigger_r=1.0),
    "trail": ExitModel(stop_atr=2.0, rr=8.0, be_trigger_r=1.0, trail_atr=2.0, max_hold_min=4320),
    "partial": ExitModel(stop_atr=1.5, rr=3.0, partial_r=1.0, partial_frac=0.5, be_trigger_r=1.0),
    "swing": ExitModel(stop_atr=3.0, rr=3.0, max_hold_min=10080),
}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true", help="show every strategy and exit")
    ap.add_argument("--strategies", default="", help="comma list of strategy ids")
    ap.add_argument("--family", default="", help="comma list of families")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--exclude", default="")
    ap.add_argument("--symbols", default=",".join(CRYPTO))
    ap.add_argument("--since", default=None)
    ap.add_argument("--until", default=None)
    ap.add_argument("--exits", default="std", help=",".join(EXITS))
    ap.add_argument("--cost", default="india_gst", choices=["india_gst", "binance_vip0", "optimistic", "stress"])
    ap.add_argument("--exec", default="1m", dest="exec_tf")
    ap.add_argument("--tf", default=None, help="override every strategy's signal timeframe")
    ap.add_argument("--grid", action="store_true", help="sweep each strategy's parameter grid")
    ap.add_argument("--grid-limit", type=int, default=6)
    ap.add_argument("--null-trials", type=int, default=0)
    ap.add_argument("--risk", default="0.02", help="wallet risk per trade, comma list")
    ap.add_argument("--leverage", default="5", help="wallet leverage, comma list")
    ap.add_argument("--concurrent", default="1")
    ap.add_argument("--start-balance", type=float, default=25.0)
    ap.add_argument("--target", type=float, default=100.0)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    if a.list:
        for s in catalog():
            print(f"{s['id']:18} {s['family']:15} {s['source']:10} {s['tf']:4} {s['name']}")
        return
    ids = [i for i in a.strategies.split(",") if i]
    ids += [s.id for s in REGISTRY.values() if s.family in a.family.split(",") and s.id not in ids]
    if a.all:
        ids = list(REGISTRY)
    ids = [i for i in ids if i not in a.exclude.split(",")]
    bad = [i for i in ids if i not in REGISTRY]
    if bad or not ids:
        raise SystemExit(f"unknown or no strategies: {bad or 'none selected'} (use --list)")
    spec = RunSpec(strategies=ids, symbols=a.symbols.split(","), exits=[EXITS[e] for e in a.exits.split(",")],
                   cost=a.cost, grid=a.grid, grid_limit=a.grid_limit, sig_tf=a.tf, exec_tf=a.exec_tf,
                   start=a.since, end=a.until, null_trials=a.null_trials)
    wallets = [WalletConfig(start=a.start_balance, target=a.target, risk_pct=float(r), leverage=float(l),
                            max_concurrent=int(c))
               for r in a.risk.split(",") for l in a.leverage.split(",") for c in a.concurrent.split(",")]
    out = a.out or f"data/lab/runs/{time.strftime('%Y%m%d-%H%M%S')}"
    res = run_lab(spec, workers=a.workers, out_dir=out, wallets=wallets)
    s = res["summary"].sort_values("expectancy_r", ascending=False)
    cols = ["strategy", "params", "n", "win_rate", "expectancy_r", "ci_lo", "ci_hi", "profit_factor", "max_dd_r"]
    if "null_p" in s:
        cols += ["null_mean_r", "null_p"]
    pd.set_option("display.width", 220, "display.max_colwidth", 38, "display.float_format", "{:.3f}".format)
    print(s[cols].head(25).to_string(index=False))
    print(f"\n{len(res['trades']):,} trades in {res['elapsed_s']:.0f}s -> {out}")


if __name__ == "__main__":
    main()
