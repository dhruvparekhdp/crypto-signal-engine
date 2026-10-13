"""Robustness grid for the profit ladder (owner, 13 Oct 2026): does the switch survive dips that rebound?

    python -m scripts.robust_report --out data/lab/robust_results.json

For every exit variant in data/lab/runs/robust_{4h,8h} (live filter applied, BCH/LTC out):
  * edge (cluster-aware) and the owner's wallet replay (Rs5,000, withdrawals, risk 1-3%, 12% cap)
  * shake-out rate: of the trades closed by a moved stop (trail / profit lock), the share where price came back
    within 16 h (two funding cycles) to the exit price plus another 0.5R in the trade's direction.
"""
from __future__ import annotations

import argparse
import json

import numpy as np
import pandas as pd

from analysis.lab.data import load_bars
from scripts.edge_report import load_run, stats, tag_regimes
from scripts.run_lab import EXITS
from scripts.wallet_plan_backtest import VARIANTS, full_run, windows

H16 = 16 * 3_600_000


def shakeout_rate(g: pd.DataFrame) -> tuple[float | None, int]:
    moved = g[g.reason == "trail"]
    if not len(moved):
        return None, 0
    hits = 0
    for sym, gs in moved.groupby("symbol"):
        b = load_bars(sym, "1h")
        for r in gs.itertuples(index=False):
            i0 = np.searchsorted(b.t, r.exit_t, side="left")
            i1 = np.searchsorted(b.t, r.exit_t + H16, side="right")
            if i1 <= i0:
                continue
            target = r.exit + r.side * 0.5 * r.stop_frac * r.entry
            hits += bool((b.h[i0:i1] >= target).any()) if r.side > 0 else bool((b.l[i0:i1] <= target).any())
    return hits / len(moved), len(moved)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/lab/robust_results.json")
    a = ap.parse_args()
    names = {EXITS[n].key(): n for n in EXITS if n == "swing" or n.startswith("rb_")}
    t = pd.concat([load_run("robust_4h", "4h"), load_run("robust_8h", "8h")], ignore_index=True)
    t = tag_regimes(t)
    t = t[t.btc_vol_rank <= 2 / 3]
    t["exit_model"] = t.cfg.str.split("|").str[2].map(names)
    t["days"] = (t.exit_t - t.entry_t) / 86_400_000
    live = {**VARIANTS["F E + Phase 1 guards"], "max_open_risk": 0.12}
    out = {}
    print(f"{'variant':16} {'meanR':>6} {'cl t':>5} {'hold':>5} {'shakeout':>9} | 12m x (worst)  fall med/worst | 24m x | 5y out")
    for e, g in sorted(t.groupby("exit_model"), key=lambda kv: kv[0]):
        s = stats(g)
        so, n_moved = shakeout_rate(g)
        w12, w24, f = windows(g, 12, live), windows(g, 24, live), full_run(g, live)
        out[e] = {"stats": s, "hold_days": float(g.days.median()), "shakeout": so, "moved_exits": n_moved,
                  "w12": w12, "w24": w24, "full": f, "exits": g.reason.value_counts(normalize=True).round(3).to_dict()}
        print(f"{e:16} {s['mean']:+.3f} {s['cluster_t']:>5.1f} {g.days.median():>5.1f} "
              f"{('-' if so is None else f'{so:.0%} of {n_moved}'):>9} | x{w12['median_total_x']:<5}(x{w12['worst_total_x']:<4}) "
              f"{w12['median_max_dd_pct']:>2}%/{w12['worst_max_dd_pct']:>2}% | x{w24['median_total_x']:<5}| Rs{f['withdrawn_inr']:,}")
    with open(a.out, "w") as fh:
        json.dump(out, fh, indent=1, default=str)
    print("saved", a.out)


if __name__ == "__main__":
    main()
