"""Where a scalp rule works and where not, and why (owner, 18 Oct 2026). Reads a lab run's trades.parquet.

    python -m scripts.scalp_diagnose RUN_DIR [--cfg 'strategy|params|exit'] [--json OUT]

Breakdowns (mean R after costs, trades, win rate): coin, side, session (UTC hour), weekday/weekend, BTC trend and
volatility at entry, year, exit reason; plus gross vs net R (do fees kill it?), how much R the fees take, and how far
trades ran for or against (MFE / MAE in R) - a rule whose trades rarely reach +1R needs a nearer target, one that
reaches it often but still loses is losing on fees or stops.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

SESSIONS = [(0, 8, "Asia 00-08 UTC"), (8, 13, "Europe 08-13"), (13, 21, "US 13-21"), (21, 24, "Late 21-24")]


def _btc_context(t: pd.DataFrame) -> pd.DataFrame:
    """BTC's 24h EMA trend (up/down/flat) and daily volatility rank at each trade's signal time."""
    from analysis import regime_gate
    from analysis.lab import features as F
    from analysis.lab.data import load_bars
    b = load_bars("BTCUSDT", "1h")
    e = F.ema(b.c, 24)
    slope = e / np.r_[np.full(24, np.nan), e[:-24]] - 1
    i = np.clip(np.searchsorted(b.t + 3_600_000, t.sig_t.to_numpy(), side="right") - 1, 0, len(b.t) - 1)
    s = slope[i]
    t["btc_trend"] = np.where(np.isnan(s), "na", np.where(s > 0.01, "up >1%/day", np.where(s < -0.01, "down >1%/day", "flat")))
    d = load_bars("BTCUSDT", "1d")
    rank = regime_gate.vol_rank_series(d.c)
    j = np.searchsorted(d.t + 86_400_000, t.sig_t.to_numpy(), side="right") - 1
    vr = np.where(j >= 0, rank[np.clip(j, 0, None)], np.nan)
    t["btc_vol"] = pd.cut(vr, [-0.01, 1 / 3, 2 / 3, 1.01], labels=["calm", "normal", "wild"]).astype(str)
    return t


def _stats(g: pd.DataFrame) -> dict:
    return {"n": int(len(g)), "mean_r": round(float(g.r_net.mean()), 3), "gross_r": round(float(g.r_gross.mean()), 3),
            "win": round(float((g.r_net > 0).mean()), 3), "total_r": round(float(g.r_net.sum()), 1)}


def diagnose(t: pd.DataFrame) -> dict:
    t = t.copy()
    ts = pd.to_datetime(t.entry_t, unit="ms")
    t["hour"], t["year"] = ts.dt.hour, ts.dt.year
    t["session"] = pd.cut(t.hour, [s[0] - 0.5 for s in SESSIONS] + [23.5], labels=[s[2] for s in SESSIONS]).astype(str)
    t["day"] = np.where(ts.dt.dayofweek >= 5, "weekend", "weekday")
    t["side_name"] = np.where(t.side > 0, "long", "short")
    t = _btc_context(t)
    out = {"all": _stats(t),
           "fees_in_r": round(float((t.fee_frac / t.stop_frac).mean()), 3),
           "reached_1r_first": round(float((t.mfe / t.stop_frac >= 1).mean()), 3),
           "mfe_r_median": round(float((t.mfe / t.stop_frac).median()), 2),
           "mae_r_median": round(float((t.mae / t.stop_frac).median()), 2),
           "stop_pct_median": round(float(t.stop_frac.median() * 100), 3)}
    for col in ("symbol", "side_name", "session", "day", "btc_trend", "btc_vol", "year", "reason"):
        out[col] = {str(k): _stats(g) for k, g in t.groupby(col)}
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("run")
    ap.add_argument("--cfg", default=None)
    ap.add_argument("--json", default=None)
    a = ap.parse_args()
    t = pd.read_parquet(Path(a.run) / "trades.parquet")
    if a.cfg:
        t = t[t.cfg == a.cfg]
    res = diagnose(t)
    if a.json:
        Path(a.json).write_text(json.dumps(res, indent=1))
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
