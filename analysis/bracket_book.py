"""
Channel-style bracket, paper research only (owner, 21 Oct 2026: "add this types of trading in paper for now, let's
review it for 2-5 days, if we can find some pattern"). Never places an order and never touches the paper wallet.

The bracket copies a Telegram channel's calls: stop 5% away, four targets at +1.01 / +2.76 / +4.60 / +10.68% with a
quarter of the position closed at each, the stop moved to the entry once the first target is hit, and (the owner's
"1-2 days") anything still open closed at market after 48 hours. Every logged signal gets one - the engine's pick, its
mirror (the same setup on the other side: the control), and swing signals - so the question "do our entries beat the
bracket's built-in ~81% first-target rate, after fees?" is answered on live data. A 2022-26 replay (21 Oct): random
entries hit the first target 81% of the time and lost 0.15% a trade after fees; our swing entries hit it 85% and made
+0.27% (all of it from the later targets).
"""
from __future__ import annotations

from datetime import datetime, timedelta

STOP = 0.05
TARGETS = (0.0101, 0.0276, 0.0460, 0.1068)
MAX_HOLD = timedelta(hours=48)
COST = 0.0015            # Delta taker 0.05% + 18% GST each side, plus slippage: round trip, share of price
LEVERAGE_SHOWN = 5       # ROI is shown at 5x, the way the channel shows it; nothing is sized


def _naive(ts: datetime) -> datetime:
    from datetime import UTC
    return ts.astimezone(UTC).replace(tzinfo=None) if ts.tzinfo else ts


def open_trade(sig_id: int, symbol: str, side: int, signal_type: str, role: str, source: str, signal_ts: datetime,
               signal_price: float, confidence: float = 0.0) -> dict:
    """A virtual trade for one logged signal; it enters at the open of the first 1m bar after the signal."""
    return {"id": sig_id, "symbol": symbol.upper(), "side": side, "signal_type": signal_type, "role": role,
            "source": source, "signal_ts": _naive(signal_ts).isoformat(), "signal_price": signal_price,
            "confidence": confidence, "entry": None, "entry_ts": None, "stop": None, "legs": 0, "pnl": 0.0,
            "status": "waiting", "last_ts": _naive(signal_ts).isoformat(), "best": 0.0, "worst": 0.0}


def advance(tr: dict, bars) -> dict:
    """Walk 1m bars (dicts or candles with timestamp/open/high/low/close, open-time stamped) that open after the trade's
    last processed bar. Stop first on a bar that touches both; a quarter of the position at each target; stop to entry
    after the first; at 48 h the rest closes at that bar's close. Returns the same dict, updated."""
    if tr["status"] in ("closed",):
        return tr
    last = datetime.fromisoformat(tr["last_ts"])
    seq = []
    for b in bars:
        get = (lambda k: b[k]) if isinstance(b, dict) else (lambda k: getattr(b, k))
        ts = _naive(get("timestamp"))
        if ts > last:
            seq.append((ts, float(get("open")), float(get("high")), float(get("low")), float(get("close"))))
    seq.sort(key=lambda x: x[0])
    s = tr["side"]
    for ts, o, h, lo, c in seq:
        if tr["entry"] is None:
            tr.update(entry=o, entry_ts=ts.isoformat(), stop=o * (1 - s * STOP), status="open")
        e = tr["entry"]
        tr["best"] = max(tr["best"], ((h if s > 0 else lo) / e - 1) * 100 * s)
        tr["worst"] = min(tr["worst"], ((lo if s > 0 else h) / e - 1) * 100 * s)
        n = len(TARGETS)
        if (lo <= tr["stop"]) if s > 0 else (h >= tr["stop"]):
            tr["pnl"] += (n - tr["legs"]) / n * s * (tr["stop"] / e - 1)
            tr.update(status="closed", exit="stop" if tr["legs"] == 0 else "stop at entry", closed_ts=ts.isoformat())
        else:
            while tr["legs"] < n and ((h >= e * (1 + TARGETS[tr["legs"]])) if s > 0 else (lo <= e * (1 - TARGETS[tr["legs"]]))):
                tr["pnl"] += TARGETS[tr["legs"]] / n
                tr["legs"] += 1
                if tr["legs"] == 1:
                    tr["stop"] = e                       # "after the first take profit, move the stop loss to entry"
                    tr["tp1_ts"] = ts.isoformat()
            if tr["legs"] == n:
                tr.update(status="closed", exit="all targets", closed_ts=ts.isoformat())
            elif ts + timedelta(minutes=1) - datetime.fromisoformat(tr["entry_ts"]) >= MAX_HOLD:
                tr["pnl"] += (n - tr["legs"]) / n * s * (c / e - 1)
                tr.update(status="closed", exit="48h limit", closed_ts=ts.isoformat())
        tr["last_ts"] = ts.isoformat()
        if tr["status"] == "closed":
            tr["net_pct"] = round((tr["pnl"] - COST) * 100, 4)
            tr["roi_5x_pct"] = round(tr["net_pct"] * LEVERAGE_SHOWN, 2)
            tr["tp1"] = tr["legs"] >= 1
            break
    return tr


def summary(closed: list[dict]) -> dict:
    """Results per group: first-target rate, share ending in profit, average after fees, ROI at 5x."""
    def stats(rows):
        if not rows:
            return None
        net = [r["net_pct"] for r in rows]
        return {"n": len(rows), "tp1_pct": round(100 * sum(r["tp1"] for r in rows) / len(rows), 1),
                "profit_pct": round(100 * sum(x > 0 for x in net) / len(rows), 1),
                "avg_net_pct": round(sum(net) / len(net), 3), "avg_roi_5x": round(5 * sum(net) / len(net), 2),
                "full_stops": sum(1 for r in rows if r.get("exit") == "stop")}
    groups = {"all": closed}
    for key in ("source", "role", "symbol"):
        for r in closed:
            groups.setdefault(f"{key}:{r[key]}", []).append(r)
    return {k: stats(v) for k, v in groups.items()}
