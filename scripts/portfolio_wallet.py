"""Without AI: the candidate strategies traded TOGETHER through the 25 -> 100 USDT wallet, started from many dates.

One cycle can take months, so a single replay proves little. This starts a fresh 25 USDT wallet at the first
of every month, follows it for up to N months until it hits 100 (target) or busts, and reports how often each
happened. Windows overlap, so read the rates as a history of 'what if I had started then', not as independent trials.

    python -m scripts.portfolio_wallet --trades data/lab/runs/cand_4h/trades.parquet --months 12
"""
from __future__ import annotations

import argparse
import dataclasses

import numpy as np
import pandas as pd

from analysis.lab.wallet import WalletConfig, run_wallet

MONTH_MS = 30 * 86_400_000


def one_start(trades: pd.DataFrame, t0: int, months: int, cfg: WalletConfig):
    win = trades[(trades.entry_t >= t0) & (trades.entry_t < t0 + months * MONTH_MS)]
    if len(win) < 5:
        return None
    res = run_wallet(win, dataclasses.replace(cfg, reset=False))
    c = res.cycles[0]
    return {"start": pd.to_datetime(t0, unit="ms").strftime("%Y-%m"), "status": c["status"], "end": c["end"],
            "trades": c["trades"], "days": (c["t1"] - c["t0"]) / 86_400_000, "dd": c["dd"]}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--trades", required=True)
    ap.add_argument("--months", type=int, default=12)
    ap.add_argument("--risk", default="0.01,0.02,0.03,0.05")
    ap.add_argument("--leverage", default="5")
    ap.add_argument("--concurrent", default="1,3")
    ap.add_argument("--loss-streak", default="0")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    tr = pd.read_parquet(a.trades)
    tr = tr[tr.cfg.str.contains("sl3.0_rr3.0")].sort_values("entry_t").reset_index(drop=True)
    first, last = int(tr.entry_t.min()), int(tr.entry_t.max())
    starts = [int(t.timestamp() * 1000) for t in pd.date_range(pd.to_datetime(first, unit="ms").normalize().replace(day=1) + pd.offsets.MonthBegin(1),
                                                              pd.to_datetime(last - a.months * MONTH_MS, unit="ms"), freq="MS", tz="UTC")]
    print(f"{len(tr)} trades, {tr.strategy.nunique()} strategies ({', '.join(sorted(tr.strategy.unique()))}); {len(starts)} start dates, {a.months}-month horizon\n")
    rows = []
    for risk in map(float, a.risk.split(",")):
        for lev in map(float, a.leverage.split(",")):
            for conc in map(int, a.concurrent.split(",")):
                for ls in map(int, a.loss_streak.split(",")):
                    cfg = WalletConfig(risk_pct=risk, leverage=lev, max_concurrent=conc, loss_streak_pause=ls or None)
                    outs = [o for o in (one_start(tr, t, a.months, cfg) for t in starts) if o]
                    d = pd.DataFrame(outs)
                    hit, bust, run = (d.status == "TARGET").mean(), (d.status == "BUST").mean(), (d.status == "IN_PROGRESS").mean()
                    rows.append({"risk": risk, "lev": lev, "concurrent": conc, "loss_streak_pause": ls, "starts": len(d),
                                 "hit_100": hit, "bust": bust, "still_running": run,
                                 "median_days_to_100": d.loc[d.status == "TARGET", "days"].median(),
                                 "worst_dd_pct": 100 * d.dd.max(), "median_end_balance": d.end.median(),
                                 "start_balance_lost_pct_median": 100 * (1 - d.end.median() / cfg.start)})
    out = pd.DataFrame(rows)
    pd.set_option("display.width", 200, "display.float_format", "{:.2f}".format)
    print(out.to_string(index=False))
    if a.out:
        out.to_csv(a.out, index=False)
        print("\nsaved", a.out)


if __name__ == "__main__":
    main()
