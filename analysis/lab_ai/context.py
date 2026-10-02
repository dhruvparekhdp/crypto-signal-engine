"""Turn one finished trade into the facts a reviewer needs. Computed from real bars, in code."""
from __future__ import annotations

import numpy as np
import pandas as pd

from analysis.lab import features as F
from analysis.lab.data import load_bars
from analysis.lab.strategies import REGISTRY

MIN = 60_000


def _ret(c, i, bars):
    return float(c[i] / c[max(0, i - bars)] - 1) * 100 if i > 0 else 0.0


def build(trade: pd.Series, root: str = "data/lake", extended: bool = False) -> dict:
    """Facts at the moment of entry plus what the trade then did. `trade` is a row of trades.parquet."""
    sym = trade["symbol"]
    b = load_bars(sym, "1m", root)
    ei = int(np.searchsorted(b.t, trade["entry_t"], side="left"))
    xi = int(np.searchsorted(b.t, trade["exit_t"], side="left"))
    ei = min(max(ei, 61), len(b) - 1)
    c15 = load_bars(sym, "15m", root)
    j = int(np.searchsorted(c15.close_t, trade["entry_t"], side="right")) - 1
    rsi = float(F.rsi(c15.c[: j + 1])[-1]) if j > 20 else float("nan")
    atr_pct = float(F.atr(c15.h[: j + 1], c15.l[: j + 1], c15.c[: j + 1])[-1] / c15.c[j] * 100) if j > 20 else float("nan")
    lo24, hi24 = b.l[max(0, ei - 1440):ei].min(), b.h[max(0, ei - 1440):ei].max()
    vol1h = b.v[ei - 60:ei].sum()
    vol_avg = b.v[max(0, ei - 1440 * 3):ei].sum() / max(1, min(ei, 1440 * 3)) * 60
    btc = load_bars("BTCUSDT", "1m", root)
    bi = int(np.searchsorted(btc.t, trade["entry_t"], side="left"))
    entry = float(trade["entry"])
    sf = float(trade["stop_frac"])
    st = REGISTRY.get(trade.get("strategy", ""))
    out = {
        "symbol": sym, "side": "LONG" if trade["side"] > 0 else "SHORT",
        "strategy": f"{st.name} ({st.family})" if st else trade.get("strategy", ""),
        "rule": st.desc if st else "",
        "entry_utc": str(pd.to_datetime(int(trade["entry_t"]), unit="ms"))[:16],
        "stop_pct": round(sf * 100, 3), "round_trip_cost_pct": round(float(trade["fee_frac"]) * 100, 3),
        "at_entry": {
            "ret_1h_pct": round(_ret(b.c, ei, 60), 2), "ret_4h_pct": round(_ret(b.c, ei, 240), 2),
            "ret_24h_pct": round(_ret(b.c, ei, 1440), 2), "rsi14_15m": round(rsi, 1),
            "atr_15m_pct": round(atr_pct, 3),
            "pos_in_24h_range_pct": round(float((entry - lo24) / max(hi24 - lo24, 1e-12)) * 100, 0),
            "volume_1h_vs_avg": round(float(vol1h / max(vol_avg, 1e-9)), 2),
            "btc_24h_ret_pct": round(_ret(btc.c, bi, 1440), 2),
        },
        "outcome": {
            "exit_reason": str(trade["reason"]), "held_min": int(trade["bars"]),
            "r_net": round(float(trade["r_net"]), 2), "r_gross": round(float(trade["r_gross"]), 2),
            "cost_r": round(float(trade["r_gross"] - trade["r_net"]), 2),
            "mae_r": round(float(trade["mae"] / sf), 2), "mfe_r": round(float(trade["mfe"] / sf), 2),
            "move_after_15m_r": round(float(((b.c[min(len(b) - 1, ei + 15)] / entry - 1) * trade["side"]) / sf), 2),
            "move_after_60m_r": round(float(((b.c[min(len(b) - 1, ei + 60)] / entry - 1) * trade["side"]) / sf), 2),
        },
    }
    if extended:
        c1 = c15.c
        pre = [round(float(c1[k] / entry - 1) * 100, 2) for k in range(max(0, j - 15), j + 1)]
        post = [round(float(c1[k] / entry - 1) * 100 * (1 if trade["side"] > 0 else -1), 2)
                for k in range(j + 1, min(len(c1), j + 17))]
        out["last_16_closes_15m_pct_vs_entry"] = pre
        out["next_16_closes_15m_pct_in_trade_direction"] = post
        c4 = load_bars(sym, "4h", root)
        k = int(np.searchsorted(c4.close_t, trade["entry_t"], side="right")) - 1
        if k > 50:
            out["trend_4h"] = {"close_vs_ema50_pct": round(float(c4.c[k] / F.ema(c4.c[: k + 1], 50)[-1] - 1) * 100, 2),
                               "ret_4h_bars_6_pct": round(float(c4.c[k] / c4.c[k - 6] - 1) * 100, 2)}
    return out

