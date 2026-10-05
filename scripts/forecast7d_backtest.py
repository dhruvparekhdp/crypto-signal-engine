"""Walk-forward backtest of analysis.forecast7d: one forecast per coin per week over 5 years, scored against the
no-skill baseline (random walk, same volatility, bell curve).

Each forecast only sees moves whose outcome was known before its date. Scores:
  pinball   average quantile loss of the day-7 band, as % of price (lower is better)
  cover50/80  how often day-7 price landed inside the 50% / 80% band (should be ~50% / ~80%)
  brier_up  Brier score of P(higher in 7 days) (0.25 = coin flip; lower is better)
  touch     Brier score of the four touch probabilities

    python -m scripts.forecast7d_backtest --coins BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,DOGEUSDT
"""
from __future__ import annotations

import argparse
import json

import numpy as np

from analysis import forecast7d as F7
from analysis.lab.data import load_bars

DAY = 86_400_000
ALL = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT", "ADAUSDT", "DOGEUSDT", "AVAXUSDT", "LINKUSDT",
       "LTCUSDT", "BCHUSDT", "SUIUSDT"]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--coins", default="BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,DOGEUSDT")
    ap.add_argument("--warmup-days", type=int, default=365)
    ap.add_argument("--step-days", type=int, default=7)
    ap.add_argument("--out", default="data/lab/forecast7d_backtest.json")
    a = ap.parse_args()
    coins = a.coins.split(",")
    bars = {s: load_bars(s, "1d") for s in ALL}
    sig = {s: F7.daily_sigma(b.c) for s, b in bars.items()}
    hist = F7.History()
    for s in ALL:                                   # the shape is pooled over all 12 coins; scored on --coins
        hist.add_coin(bars[s], sig[s])
    print(f"history rows {len(hist.y)}; scoring {coins}", flush=True)
    rows = []
    t0 = min(int(b.t[0]) for b in bars.values()) + a.warmup_days * DAY
    for s in coins:
        b = bars[s]
        idx = [i for i in range(90, len(b) - F7.H) if int(b.t[i]) >= t0][::a.step_days]
        for n, i in enumerate(idx):
            known = int(b.t[i]) + DAY
            fz = hist.before(known)
            out = {}
            for name, lean in (("quant", False), ("quant_lean", True)):
                fc = F7.forecast(s, b, sig[s], i, fz, lean=lean)
                if fc is None:
                    break
                out[name] = fc
            if len(out) < 2:
                continue
            p, y = float(b.c[i]), float(b.c[i + F7.H])
            hi, lo = float(b.h[i + 1:i + F7.H + 1].max()), float(b.l[i + 1:i + F7.H + 1].min())
            base = F7.baseline(p, sig[s][i])
            touched = {"+5%": hi >= p * 1.05, "-5%": lo <= p * 0.95, "+10%": hi >= p * 1.10, "-10%": lo <= p * 0.90}
            r = {"symbol": s, "date": str(np.datetime64(int(b.t[i]), "ms"))[:10], "price": p, "y": y,
                 "ret_pct": 100 * (y / p - 1), "up": y > p, "touched": touched}
            for name, fc in out.items():
                d7 = fc.days[-1]
                qs = [d7[f"q{int(q * 100)}"] for q in F7.QS]
                r[name] = {"pinball_pct": 100 * F7.pinball(qs, y) / p, "in50": qs[1] <= y <= qs[3], "in80": qs[0] <= y <= qs[4],
                           "p_up": fc.p_up, "brier_up": (fc.p_up - (y > p)) ** 2,
                           "touch_brier": float(np.mean([(fc.touch[k] - touched[k]) ** 2 for k in touched])),
                           "q": qs, "lean_z": fc.lean_z}
            qs = base["q"]
            r["baseline"] = {"pinball_pct": 100 * F7.pinball(qs, y) / p, "in50": qs[1] <= y <= qs[3], "in80": qs[0] <= y <= qs[4],
                             "p_up": 0.5, "brier_up": 0.25,
                             "touch_brier": float(np.mean([(base["touch"][k] - touched[k]) ** 2 for k in touched]))}
            rows.append(r)
        print(f"  {s}: {sum(1 for r in rows if r['symbol'] == s)} forecasts", flush=True)

    def score(rs, m):
        x = [r[m] for r in rs]
        return {"n": len(x), "pinball_pct": float(np.mean([v["pinball_pct"] for v in x])),
                "cover50": float(np.mean([v["in50"] for v in x])), "cover80": float(np.mean([v["in80"] for v in x])),
                "brier_up": float(np.mean([v["brier_up"] for v in x])), "touch_brier": float(np.mean([v["touch_brier"] for v in x]))}

    summary = {m: score(rows, m) for m in ("baseline", "quant", "quant_lean")}
    per_coin = {s: {m: score([r for r in rows if r["symbol"] == s], m) for m in ("baseline", "quant", "quant_lean")} for s in coins}
    per_year = {y: {m: score([r for r in rows if r["date"][:4] == y], m) for m in ("baseline", "quant", "quant_lean")}
                for y in sorted({r["date"][:4] for r in rows})}
    # does the lean beat no lean? paired difference of pinball loss
    d = np.array([r["quant_lean"]["pinball_pct"] - r["quant"]["pinball_pct"] for r in rows])
    lean_t = float(d.mean() / (d.std(ddof=1) / np.sqrt(len(d)))) if d.std() > 0 else 0.0
    d2 = np.array([r["quant"]["pinball_pct"] - r["baseline"]["pinball_pct"] for r in rows])
    quant_t = float(d2.mean() / (d2.std(ddof=1) / np.sqrt(len(d2)))) if d2.std() > 0 else 0.0
    print("\n            pinball%  cover50  cover80  brier_up  touch_brier")
    for m, v in summary.items():
        print(f"{m:11} {v['pinball_pct']:8.3f}  {v['cover50']:7.0%}  {v['cover80']:7.0%}  {v['brier_up']:8.4f}  {v['touch_brier']:10.4f}")
    print(f"\nquant vs baseline pinball diff t={quant_t:.1f} (negative = quant better); lean vs no lean t={lean_t:.1f}")
    with open(a.out, "w") as f:
        json.dump({"coins": coins, "summary": summary, "per_coin": per_coin, "per_year": per_year,
                   "quant_vs_baseline_t": quant_t, "lean_vs_quant_t": lean_t, "rows": rows}, f, default=float)
    print("saved", a.out)


if __name__ == "__main__":
    main()
