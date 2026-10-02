"""Without AI: where do the candidates' results come from? By year, coin, side, and how much they overlap.

    python -m scripts.breakdown --trades data/lab/runs/cand_4h/trades.parquet --out data/lab/breakdown
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

pd.set_option("display.width", 200, "display.float_format", "{:.3f}".format, "display.max_rows", 200)


def tbl(df, col):
    g = df.groupby(col).r_net
    return pd.DataFrame({"n": g.size(), "R_per_trade": g.mean(), "win": g.apply(lambda x: (x > 0).mean()),
                         "total_R": g.sum()})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trades", required=True)
    ap.add_argument("--out", default="data/lab/breakdown")
    a = ap.parse_args()
    t = pd.read_parquet(a.trades)
    t = t[t.cfg.str.contains("sl3.0_rr3.0")].copy()
    t["year"] = pd.to_datetime(t.entry_t, unit="ms").dt.year
    t["side_name"] = np.where(t.side > 0, "long", "short")
    Path(a.out).mkdir(parents=True, exist_ok=True)
    for col, name in (("year", "by_year"), ("symbol", "by_symbol"), ("side_name", "by_side"), ("strategy", "by_strategy")):
        r = tbl(t, col)
        print(f"\n== {name}\n{r.to_string()}")
        r.to_csv(Path(a.out) / f"{name}.csv")
    r = t.groupby(["strategy", "year"]).r_net.agg(["size", "mean"]).unstack("year")
    print("\n== R per trade, strategy x year\n" + r["mean"].to_string())
    r["mean"].to_csv(Path(a.out) / "strategy_by_year.csv")
    # overlap: how many positions are open at once (this is what hurts a small wallet)
    ev = np.r_[np.c_[t.entry_t, np.ones(len(t))], np.c_[t.exit_t, -np.ones(len(t))]]
    ev = ev[np.argsort(ev[:, 0], kind="stable")]
    open_n = np.cumsum(ev[:, 1])
    span = np.diff(ev[:, 0], append=ev[-1, 0])
    print(f"\n== exposure: average {np.average(open_n, weights=np.maximum(span, 1)):.1f} positions open at once, maximum {int(open_n.max())}; "
          f"{len(t) / ((t.entry_t.max() - t.entry_t.min()) / 86_400_000):.2f} trades per day across all coins")
    d = t.assign(day=pd.to_datetime(t.entry_t, unit="ms").dt.floor("D")).pivot_table(index="day", columns="strategy", values="r_net", aggfunc="sum").fillna(0)
    print("\n== correlation of daily R between strategies (1.0 = the same bet)\n" + d.corr().to_string())
    # biggest losing streak and the worst stretch, in R, for the whole portfolio taken in time order
    r_ = t.sort_values("entry_t").r_net.to_numpy()
    c = np.cumsum(r_)
    dd = (np.maximum.accumulate(c) - c).max()
    run = best = 0
    for x in r_:
        run = run + 1 if x <= 0 else 0
        best = max(best, run)
    print(f"\n== portfolio path: total {r_.sum():.0f} R over {len(r_)} trades, worst drawdown {dd:.0f} R, longest losing streak {best} trades")
    print(f"   at 1% risk per R that is a worst drawdown of about {dd:.0f}% of the wallet; at 2%: {2 * dd:.0f}%")


if __name__ == "__main__":
    main()
