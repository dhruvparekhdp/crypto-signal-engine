"""Owner's tight trail (18 Oct 2026) vs the live ladder: same trades, live volatility filter, BCH/LTC out.

    python -m scripts.owngap_report
"""
from __future__ import annotations

import pandas as pd

from scripts.edge_report import load_run, stats, tag_regimes
from scripts.robust_report import shakeout_rate
from scripts.run_lab import EXITS
from scripts.wallet_plan_backtest import VARIANTS, full_run, windows


def main() -> None:
    names = {EXITS[n].key(): n for n in ("rb_std_c60_b25", "own_gap50_c60", "own_gap30_c60")}
    t = pd.concat([load_run("owngap_4h", "4h"), load_run("owngap_8h", "8h")], ignore_index=True)
    t = tag_regimes(t)
    t = t[t.btc_vol_rank <= 2 / 3]
    t["exit_model"] = t.cfg.str.split("|").str[2].map(names)
    live = {**VARIANTS["F E + Phase 1 guards"], "max_open_risk": 0.12}
    label = {"rb_std_c60_b25": "LIVE ladder (1.5R->+0.5R, 0.25R buffer)", "own_gap50_c60": "lock = best - 0.5R from 1R",
             "own_gap30_c60": "lock = best - 0.3R from 1R"}
    for e in ("rb_std_c60_b25", "own_gap50_c60", "own_gap30_c60"):
        g = t[t.exit_model == e]
        s = stats(g)
        so, n_moved = shakeout_rate(g)
        w12, f = windows(g, 12, live), full_run(g, live)
        reached = g[g.mfe / g.stop_frac >= 1.25] if "mfe" in g else g
        print(f"{label[e]:42} n {s['n']} | avg {s['mean']:+.3f}R (t {s['cluster_t']:.1f}) | win {s['win']:.0%} | "
              f"shaken out & came back {('-' if so is None else f'{so:.0%}')} of {n_moved} | 12m x{w12['median_total_x']} "
              f"(worst x{w12['worst_total_x']}, dd {w12['median_max_dd_pct']}%/{w12['worst_max_dd_pct']}%) | "
              f"5y withdrawn Rs{f['withdrawn_inr']:,} | trades that reached 1.25R: avg {reached.r_net.mean():+.2f}R (n {len(reached)})")
        print("   exits:", g.reason.value_counts(normalize=True).round(2).to_dict())


if __name__ == "__main__":
    main()
