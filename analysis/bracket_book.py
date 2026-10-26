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
# variations on the same signals (owner, 26 Oct 2026: "keep the bracket and try 2-3 new variation"), fixed before any
# result: "tight" halves the stop to 2.5%; "trail" keeps the 5% stop but, after the first target, trails the stop
# 1.5% behind the best price (never below entry) instead of parking it at entry. The third variation is a filter, not
# a trade: every trade carries BTC's 24 h direction (with / against / flat) and the panel shows "base, with BTC only"
VARIANTS = {"base": (STOP, None), "tight": (0.025, None), "trail": (STOP, 0.015)}
BTC_FLAT_PCT = 0.5
LEVERAGE_SHOWN = 5       # ROI is shown at 5x, the way the channel shows it; nothing is sized


def _naive(ts: datetime) -> datetime:
    from datetime import UTC
    return ts.astimezone(UTC).replace(tzinfo=None) if ts.tzinfo else ts


# the signal log's candidate_role -> the bracket book's role. "mirror_shadow" is NOT a mirror: it is the engine's own
# short pick held back because it runs against the higher-timeframe trend (analysis/crypto_engine.py), and it has a
# real mirror of its own (22 Oct 2026: 23 of them had been counted as mirrors)
ROLE_OF = {"primary": "primary", "mirror": "mirror", "mirror_shadow": "counter_trend"}


def role_of(candidate_role) -> str:
    return ROLE_OF.get(str(candidate_role or "primary"), "primary")


def btc_label(side: int, btc_24h_pct) -> str:
    """BTC's last-24h move relative to the trade: with / against / flat (inside +-0.5%) / unknown."""
    if btc_24h_pct is None:
        return "unknown"
    x = side * float(btc_24h_pct)
    return "with" if x > BTC_FLAT_PCT else "against" if x < -BTC_FLAT_PCT else "flat"


def open_trade(sig_id, symbol: str, side: int, signal_type: str, role: str, source: str, signal_ts: datetime,
               signal_price: float, confidence: float = 0.0, labels: dict | None = None, pair=None,
               variant: str = "base") -> dict:
    """A virtual trade for one signal; it enters at the open of the first 1m bar after the signal. `labels` (setup,
    news, sentiment, ...) are kept on the trade so results can be split by them afterwards; `pair` is a mirror's
    pick (its signal id), so each pick can be compared with its own mirror."""
    stop_frac, trail = VARIANTS[variant]
    return {"id": sig_id if variant == "base" else f"{sig_id}~{variant}", "sig_id": sig_id, "variant": variant,
            "stop_frac": stop_frac, "trail_frac": trail,
            "symbol": symbol.upper(), "side": side, "signal_type": signal_type, "role": role,
            "source": source, "signal_ts": _naive(signal_ts).isoformat(), "signal_price": signal_price,
            "confidence": confidence, "labels": labels or {}, "pair": pair, "entry": None, "entry_ts": None,
            "stop": None, "legs": 0, "pnl": 0.0, "status": "waiting", "last_ts": _naive(signal_ts).isoformat(),
            "best": 0.0, "worst": 0.0}


def mark(tr: dict, price: float) -> dict | None:
    """An open trade as if closed at `price` now (targets already taken stay taken), or None before its entry."""
    if not tr.get("entry") or not price:
        return None
    n = len(TARGETS)
    net = round((tr["pnl"] + (n - tr["legs"]) / n * tr["side"] * (price / tr["entry"] - 1) - COST) * 100, 4)
    return {**tr, "net_pct": net, "roi_5x_pct": round(net * LEVERAGE_SHOWN, 2), "tp1": tr["legs"] >= 1,
            "exit": "open", "marked": True}


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
            tr.update(entry=o, entry_ts=ts.isoformat(), stop=o * (1 - s * tr.get("stop_frac", STOP)), status="open",
                      best_px=o)
        e = tr["entry"]
        tr["best"] = max(tr["best"], ((h if s > 0 else lo) / e - 1) * 100 * s)
        tr["worst"] = min(tr["worst"], ((lo if s > 0 else h) / e - 1) * 100 * s)
        n = len(TARGETS)
        if (lo <= tr["stop"]) if s > 0 else (h >= tr["stop"]):
            tr["pnl"] += (n - tr["legs"]) / n * s * (tr["stop"] / e - 1)
            moved = s * (tr["stop"] - e) > 1e-12
            tr.update(status="closed", exit="stop" if tr["legs"] == 0 else "trailing stop" if moved else "stop at entry",
                      closed_ts=ts.isoformat())
        else:
            while tr["legs"] < n and ((h >= e * (1 + TARGETS[tr["legs"]])) if s > 0 else (lo <= e * (1 - TARGETS[tr["legs"]]))):
                tr["pnl"] += TARGETS[tr["legs"]] / n
                tr["legs"] += 1
                if tr["legs"] == 1:
                    tr["stop"] = e                       # "after the first take profit, move the stop loss to entry"
                    tr["tp1_ts"] = ts.isoformat()
            if tr.get("trail_frac") and tr["legs"] >= 1:     # "trail": after the first target, follow the best price
                tr["best_px"] = max(tr.get("best_px", e), h) if s > 0 else min(tr.get("best_px", e), lo)
                cand = tr["best_px"] * (1 - s * tr["trail_frac"])
                cand = max(cand, e) if s > 0 else min(cand, e)
                tr["stop"] = max(tr["stop"], cand) if s > 0 else min(tr["stop"], cand)
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
    allv = closed
    closed = [r for r in allv if r.get("variant", "base") == "base"]
    groups = {"all": closed}
    for r in allv:
        groups.setdefault(f"variant:{r.get('variant', 'base')}|{r['source']}|{r['role']}", []).append(r)
    for r in closed:
        groups.setdefault(f"btc:{r['source']}|{r['role']}|{(r.get('labels') or {}).get('btc', 'unknown')}", []).append(r)
    for key in ("source", "role", "symbol"):
        for r in closed:
            groups.setdefault(f"{key}:{r[key]}", []).append(r)
    for r in closed:
        groups.setdefault(f"source_role:{r['source']}|{r['role']}", []).append(r)
        groups.setdefault(f"side:{'long' if r['side'] > 0 else 'short'}", []).append(r)
        if r["source"] == "levels":                  # S/R trades: every label, picks and mirrors apart
            for k, v in (r.get("labels") or {}).items():
                groups.setdefault(f"label:{r['role']}|{k}|{v}", []).append(r)
    return {k: stats(v) for k, v in groups.items()}


def pairs(trades: list[dict]) -> dict:
    """Each pick next to its own mirror (same coin, same moment, other side): the one comparison a market-wide move
    cannot fake. Grouped by the pick's source and role; a pair counts once both sides have a result."""
    by_id = {(t.get("variant", "base"), str(t.get("sig_id", t["id"]))): t for t in trades}
    groups: dict[str, list] = {}
    for m in trades:
        if m.get("role") != "mirror":
            continue
        v = m.get("variant", "base")
        sid = str(m.get("sig_id", m["id"]))
        pid = (sid.rsplit(":", 1)[0] + ":primary") if m.get("source") == "levels" else \
            (str(m["pair"]) if m.get("pair") else None)
        p = by_id.get((v, pid)) if pid else None
        if p is None or "net_pct" not in p or "net_pct" not in m:
            continue
        groups.setdefault(f"{p['source']}|{p['role']}" if v == "base" else f"{v}|{p['source']}|{p['role']}",
                          []).append((p["net_pct"], m["net_pct"]))
    return {k: {"n": len(v), "pick_avg": round(sum(a for a, _ in v) / len(v), 3),
                "mirror_avg": round(sum(b for _, b in v) / len(v), 3), "pick_better": sum(a > b for a, b in v)}
            for k, v in groups.items()}


def wallet_view(trades: list[dict], start: float, margin: float, leverage: float, now: datetime) -> dict:
    """The owner's wallet limit without risk rules (21 Oct 2026: "keep wallet limit not risk"): take the trades in
    the order they entered, each with a fixed margin at a fixed leverage, and skip one only when there is not that
    much free margin - or when the coin is already open, since Delta holds one position per contract (22 Oct: the
    engine had logged 10 AVAX shorts in a few hours, one move counted 10 times). P&L = margin x leverage x the
    trade's result after fees."""
    now = _naive(now)
    ev = sorted((t for t in trades if t.get("entry_ts")), key=lambda t: t["entry_ts"])
    wallet, open_, taken, skipped, wins, losses, peak, mdd = start, [], 0, 0, 0, 0, start, 0.0
    same_coin = 0
    for t in ev:
        et = datetime.fromisoformat(t["entry_ts"])
        for o in sorted([o for o in open_ if o["closed_ts"] and datetime.fromisoformat(o["closed_ts"]) <= et],
                        key=lambda o: o["closed_ts"]):
            wallet += margin * leverage * o["net_pct"] / 100
            wins += o["net_pct"] > 0
            losses += o["net_pct"] <= 0
            peak = max(peak, wallet)
            mdd = max(mdd, 1 - wallet / peak if peak > 0 else 0.0)
            open_.remove(o)
        if t.get("symbol") and any(o["symbol"] == t["symbol"] for o in open_):
            same_coin += 1
            continue
        if wallet - margin * len(open_) < margin:
            skipped += 1
            continue
        taken += 1
        open_.append({"closed_ts": t.get("closed_ts") if t.get("status") == "closed" else None,
                      "net_pct": t.get("net_pct", 0.0), "symbol": t.get("symbol")})
    for o in sorted([o for o in open_ if o["closed_ts"] and datetime.fromisoformat(o["closed_ts"]) <= now],
                    key=lambda o: o["closed_ts"]):
        wallet += margin * leverage * o["net_pct"] / 100
        wins += o["net_pct"] > 0
        losses += o["net_pct"] <= 0
        peak = max(peak, wallet)
        mdd = max(mdd, 1 - wallet / peak if peak > 0 else 0.0)
        open_.remove(o)
    return {"start": start, "margin": margin, "leverage": leverage, "wallet": round(wallet, 2), "taken": taken,
            "skipped_no_margin": skipped, "skipped_same_coin": same_coin, "open": len(open_), "won": int(wins),
            "lost": int(losses),
            "max_dd_pct": round(100 * mdd, 1)}
