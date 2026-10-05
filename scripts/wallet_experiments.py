"""Why do so few monthly 25 USDT wallets reach 100? Named sizing variants on the live 4h + 8h trades, side by side.

Each variant starts a fresh wallet on the 1st of every month and follows it for N months (scripts.portfolio_wallet's
method), so variants are compared on exactly the same trades and start dates.

    python -m scripts.wallet_experiments --out data/lab/wallet_experiments.json
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict

import numpy as np
import pandas as pd

from analysis.lab.wallet import WalletConfig
from scripts.portfolio_wallet import MONTH_MS, _tf_of, one_start

VARIANTS = {
    "now_5pct_4open": dict(risk_pct=0.05, max_concurrent=4),
    "now_2pct_4open": dict(risk_pct=0.02, max_concurrent=4),
    "5pct_total_risk_10": dict(risk_pct=0.05, max_concurrent=4, max_open_risk=0.10),
    "5pct_two_per_side": dict(risk_pct=0.05, max_concurrent=4, max_same_side=2),
    "3pct_8open_total_12": dict(risk_pct=0.03, max_concurrent=8, max_open_risk=0.12),
    "4pct_6open_total_10_side3": dict(risk_pct=0.04, max_concurrent=6, max_open_risk=0.10, max_same_side=3),
    "6pct_total_12_side2": dict(risk_pct=0.06, max_concurrent=4, max_open_risk=0.12, max_same_side=2),
    "start100_to400_2pct": dict(risk_pct=0.02, max_concurrent=4, start=100.0, target=400.0),
    "1pct_unlimited": dict(risk_pct=0.01, max_concurrent=99),
    "2pct_unlimited": dict(risk_pct=0.02, max_concurrent=99),
    "3pct_unlimited": dict(risk_pct=0.03, max_concurrent=99),
    "5pct_unlimited": dict(risk_pct=0.05, max_concurrent=99),
    "3pct_unlimited_total_15": dict(risk_pct=0.03, max_concurrent=99, max_open_risk=0.15),
}


def run(trades: pd.DataFrame, months: int, variants: dict, leverage: float = 5.0) -> dict:
    first, last = int(trades.entry_t.min()), int(trades.entry_t.max())
    starts = [int(t.timestamp() * 1000) for t in pd.date_range(
        pd.to_datetime(first, unit="ms").normalize().replace(day=1) + pd.offsets.MonthBegin(1),
        pd.to_datetime(last - months * MONTH_MS, unit="ms"), freq="MS", tz="UTC")]
    out = {}
    for name, kw in variants.items():
        cfg = WalletConfig(leverage=leverage, reset=False, **kw)
        res = [o for o in (one_start(trades, t, months, cfg) for t in starts) if o]
        by_year = defaultdict(list)
        for o in res:
            by_year[o["start"][:4]].append(o)
        skipped = defaultdict(int)
        for o in res:
            for k, v in o["skipped"].items():
                skipped[k] += v
        ends = np.array([o["end"] / cfg.start * 25 for o in res])        # in "started with 25" units
        out[name] = {
            "config": kw, "starts": len(res),
            "hit": sum(o["status"] == "TARGET" for o in res), "bust": sum(o["status"] == "BUST" for o in res),
            "median_end": float(np.median(ends)), "p25_end": float(np.percentile(ends, 25)),
            "p75_end": float(np.percentile(ends, 75)), "in_profit": float(np.mean(ends > 25)),
            "median_dd": float(np.median([o["dd"] for o in res])), "worst_dd": float(max(o["dd"] for o in res)),
            "median_trades": float(np.median([o["trades"] for o in res])),
            "median_days_to_target": (float(np.median([o["days"] for o in res if o["status"] == "TARGET"]))
                                      if any(o["status"] == "TARGET" for o in res) else None),
            "skipped_per_start": {k: round(v / len(res)) for k, v in skipped.items()},
            "by_year": {y: {"n": len(L), "hit": sum(o["status"] == "TARGET" for o in L),
                            "median_end": float(np.median([o["end"] / cfg.start * 25 for o in L]))} for y, L in sorted(by_year.items())},
            "per_start": [{"start": o["start"], "status": o["status"], "end": o["end"] / cfg.start * 25, "dd": o["dd"]} for o in res],
        }
        r = out[name]
        print(f"{months:>2}m {name:28} hit {r['hit']:>2}/{r['starts']}  bust {r['bust']:>2}  median {r['median_end']:6.1f}  "
              f"profit {r['in_profit']:.0%}  medDD {r['median_dd']:.0%}  worstDD {r['worst_dd']:.0%}  trades {r['median_trades']:.0f}", flush=True)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--trades", action="append", default=None,
                    help="trade files (default: the live 4h and 8h sets)")
    ap.add_argument("--months", default="12,24")
    ap.add_argument("--leverage", type=float, default=5.0, help="leverage ceiling; each trade uses only what it needs")
    ap.add_argument("--out", default="data/lab/wallet_experiments.json")
    a = ap.parse_args()
    files = a.trades or ["data/lab/runs/cand_4h/trades.parquet", "data/lab/runs/null_8h/trades.parquet"]
    tr = pd.concat([pd.read_parquet(f).assign(tf=_tf_of(f)) for f in files])
    tr = tr[tr.cfg.str.contains("sl3.0_rr3.0") & tr.cfg.str.contains("h10080")].sort_values("entry_t").reset_index(drop=True)
    print(f"{len(tr)} trades from {', '.join(files)}\n", flush=True)
    result = {"trades": len(tr), "files": files, "horizons": {}}
    for m in map(int, a.months.split(",")):
        result["horizons"][str(m)] = run(tr, m, VARIANTS, a.leverage)
    with open(a.out, "w") as f:
        json.dump(result, f, default=float)
    print("\nsaved", a.out)


if __name__ == "__main__":
    main()
