"""Without AI: the candidate strategies traded TOGETHER through the 25 -> 100 USDT wallet, started from many dates.

One cycle can take months, so a single replay proves little. This starts a fresh 25 USDT wallet at the first
of every month, follows it for up to N months until it hits 100 (target) or busts, and reports how often each
happened. Windows overlap, so read the rates as a history of 'what if I had started then', not as independent trials.

    python -m scripts.portfolio_wallet --trades data/lab/runs/cand_4h/trades.parquet --months 12
"""
from __future__ import annotations

import argparse
import dataclasses
from pathlib import Path

import numpy as np
import pandas as pd

from analysis.lab.wallet import WalletConfig, run_wallet

MONTH_MS = 30 * 86_400_000


def _num(x, d):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return round(x, d) if np.isfinite(x) else None


def _stamp(ms) -> str:
    return pd.to_datetime(int(ms), unit="ms").strftime("%Y-%m-%d %H:%M")


def ledger_row(r: dict) -> dict:
    """One passbook line: when the trade opened and closed, what, why, how big, fees, result, balance."""
    return {"t": _stamp(r["t"]), "opened": _stamp(r["entry_t"]), "symbol": r["symbol"],
            "side": "long" if r["side"] > 0 else "short", "strategy": r.get("strategy", ""), "tf": r.get("tf") or "",
            "entry": _num(r.get("entry"), 6), "exit_px": _num(r.get("exit"), 6), "r": _num(r.get("r_net"), 3),
            "ret_pct": _num(100 * r["net_ret"], 3) if r.get("net_ret") is not None else None,
            "stop_pct": _num(100 * r["stop_frac"], 2) if r.get("stop_frac") is not None else None,
            "notional": round(r["notional"], 2), "margin": round(r["margin"], 2), "fee": _num(r.get("fee_usd"), 4),
            "pnl": round(r["pnl"], 3), "before": round(r["balance_before"], 2), "balance": round(r["balance_after"], 2),
            "exit": r.get("reason", ""), "liquidated": bool(r.get("liquidated"))}


def universe(tr: pd.DataFrame) -> list[dict]:
    """Every distinct trade the strategies took (no wallet, no overlap), for per-strategy stats on any date range."""
    return [{"o": _stamp(r.entry_t), "c": _stamp(r.exit_t), "s": r.symbol.replace("USDT", ""), "d": int(r.side),
             "st": r.strategy, "tf": r.tf, "r": _num(r.r_net, 3), "x": r.reason} for r in tr.itertuples(index=False)]


def one_start(trades: pd.DataFrame, t0: int, months: int, cfg: WalletConfig):
    win = trades[(trades.entry_t >= t0) & (trades.entry_t < t0 + months * MONTH_MS)]
    if len(win) < 5:
        return None
    res = run_wallet(win, dataclasses.replace(cfg, reset=False))
    c = res.cycles[0]
    ledger = [ledger_row(r) for r in res.ledger if r.get("cycle", 1) == 1]
    return {"start": pd.to_datetime(t0, unit="ms").strftime("%Y-%m"), "status": c["status"], "end": c["end"],
            "trades": c["trades"], "days": (c["t1"] - c["t0"]) / 86_400_000, "dd": c["dd"], "peak": c["peak"],
            "ledger": ledger, "skipped": dict(res.skipped)}


def _tf_of(path: str) -> str:
    """Bar size of a lab run, from the run's spec.json (sig_tf), else from the folder name."""
    import json
    import re
    from pathlib import Path
    spec = Path(path).parent / "spec.json"
    try:
        tf = json.loads(spec.read_text()).get("sig_tf")
        if tf:
            return tf
    except (OSError, ValueError):
        pass
    m = re.search(r"(\d+[mhd])\b", Path(path).parent.name)
    return m.group(1) if m else ""


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--trades", required=True)
    ap.add_argument("--months", type=int, default=12)
    ap.add_argument("--risk", default="0.01,0.02,0.03,0.05")
    ap.add_argument("--leverage", default="5")
    ap.add_argument("--concurrent", default="1,3")
    ap.add_argument("--loss-streak", default="0")
    ap.add_argument("--risk-mode", default="fixed", help="fixed,adaptive")
    ap.add_argument("--trades-extra", action="append", default=[], help="more trade files to add (e.g. 8h)")
    ap.add_argument("--detail-out", default=None, help="JSON with every start's full history for the --detail-* settings")
    ap.add_argument("--detail-risk", default="0.01,0.02,0.05")
    ap.add_argument("--detail-concurrent", default="4")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    tr = pd.concat([pd.read_parquet(f).assign(tf=_tf_of(f)) for f in [a.trades] + a.trades_extra])
    tr = tr[tr.cfg.str.contains("sl3.0_rr3.0") & tr.cfg.str.contains("h10080")].sort_values("entry_t").reset_index(drop=True)
    first, last = int(tr.entry_t.min()), int(tr.entry_t.max())
    starts = [int(t.timestamp() * 1000) for t in pd.date_range(pd.to_datetime(first, unit="ms").normalize().replace(day=1) + pd.offsets.MonthBegin(1),
                                                              pd.to_datetime(last - a.months * MONTH_MS, unit="ms"), freq="MS", tz="UTC")]
    print(f"{len(tr)} trades, {tr.strategy.nunique()} strategies ({', '.join(sorted(tr.strategy.unique()))}); {len(starts)} start dates, {a.months}-month horizon\n")
    rows, details = [], []
    for risk in map(float, a.risk.split(",")):
        for lev in map(float, a.leverage.split(",")):
            for conc in map(int, a.concurrent.split(",")):
                for ls, mode in [(int(x), m) for x in a.loss_streak.split(",") for m in a.risk_mode.split(",")]:
                    cfg = WalletConfig(risk_pct=risk, leverage=lev, max_concurrent=conc, loss_streak_pause=ls or None, risk_mode=mode)
                    outs = [o for o in (one_start(tr, t, a.months, cfg) for t in starts) if o]
                    if (a.detail_out and str(risk) in a.detail_risk.split(",") and str(conc) in a.detail_concurrent.split(",")
                            and ls == 0 and mode == "fixed"):
                        details.append({"label": f"risk {risk * 100:g}% per trade, up to {conc} open, {lev:g}x max",
                                        "risk": risk, "concurrent": conc, "months": a.months,
                                        "starts": [{k: v for k, v in o.items()} for o in outs]})
                    d = pd.DataFrame([{k: v for k, v in o.items() if k not in ("ledger", "skipped")} for o in outs])
                    hit, bust, run = (d.status == "TARGET").mean(), (d.status == "BUST").mean(), (d.status == "IN_PROGRESS").mean()
                    rows.append({"mode": mode, "risk": risk, "lev": lev, "concurrent": conc, "loss_streak_pause": ls, "starts": len(d),
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
    if a.detail_out:
        import json
        with open(a.detail_out, "w") as f:
            json.dump({"months": a.months, "trades_file": a.trades, "settings": details}, f, default=float)
        print("saved", a.detail_out, f"({len(details)} settings with full histories)")
        uni = Path(a.detail_out).with_name("wallet_trades.json")
        uni.write_text(json.dumps({"trades": universe(tr)}, default=float))
        print("saved", uni, f"({len(tr)} distinct trades)")


if __name__ == "__main__":
    main()
