"""
Candidates: every strategy that is NOT live gets virtual trades with live-style exits, so it builds a forward record
before anyone picks it for live (owner, 26 Oct 2026: "only we get some confidence from paper, so we can pick those
strategies in live"; "both, but no changes for live, only paper"). Nothing here places an order, touches the paper
wallet or is copied to Delta. No slot limit and no one-per-coin rule: each signal is scored on its own.

Tracks
  swing                every swing signal (the 8 live specs) with the live exit: the signal's stop distance (3xATR),
                       its 3R target, the profit-lock ladder (analysis/swing_book.swing_lock, 1h-close confirmation)
                       and the 7-day limit. Labelled with what the real paper book did (taken / news_blackout /
                       already_open_in_symbol / ...), so the cost of each skip rule is measured
  swing_ratchet        the same signals with the ratchet exit (lab x_ratchet_g05, 23 Oct: +0.020R / +0.022R a trade in
                       both periods): from 2.8R the target moves out 0.25R per 0.25R gained, the stop 0.5R behind
  swing_mirror         the opposite side of each swing signal, same stop distance and exit: the control
  impulse_follow       live moves (analysis/impulse.py, 30 Oct 2026) traded in their direction: stop 2x the 15-min
                       range (>= 1.5%), 3R, 3 days; impulse_fade is the control; impulse_ai takes the AI's call with the
                       live news and must beat plain following
  swing_2h             paper-only 2h swing specs (settings.swing_candidate_strategies, 28 Oct 2026) with the live exit;
                       swing_2h_mirror is their control
  intraday_pick        intraday primary signals: their own stop and target, 24 h limit
  counter_trend        the engine's held-back counter-trend shorts ("mirror_shadow"): same exit
  intraday_mirror      mirrors of the picks: the control;  counter_trend_mirror: mirrors of the counter-trend shorts

Promotion gate (all must hold): 28+ days running, 100+ closed trades, average R after fees above 0, above its control,
and above 0 in both rising and falling BTC weeks (20+ trades each), so one market direction cannot pass it.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from analysis.swing_book import BE_COST_FRAC, swing_lock

EPOCH = datetime(1970, 1, 1)            # naive UTC times throughout (datetime.timestamp() would read them as local)
COST = BE_COST_FRAC                     # Delta taker 0.05% + 18% GST each side + slippage, round trip, share of price
SWING_HOLD = timedelta(days=7)
INTRADAY_HOLD = timedelta(hours=24)
IMPULSE_HOLD = timedelta(hours=72)        # analysis/impulse.HOLD_H: the lab's best exit for live moves
RATCHET_FROM_R, RATCHET_STEP_R, RATCHET_TARGET_GAP_R, RATCHET_LOCK_GAP_R = 2.8, 0.25, 0.45, 0.5
CONTROL = {"swing": "swing_mirror", "swing_2h": "swing_2h_mirror", "impulse_follow": "impulse_fade",
           "impulse_ai": "impulse_follow", "swing_ratchet": "swing", "intraday_pick": "intraday_mirror",
           "counter_trend": "counter_trend_mirror",
           "stop_reverse_2r": "stop_same_side_2r", "stop_reverse_15r": "stop_same_side_2r",
           "fed_reaction": "fed_fade", "news_event_template": "news_event_template_ctrl",
           "risk_radar_paper": "risk_radar_paper_ctrl"}
GATE = {"days": 28, "n": 100, "n_each_week_kind": 20}
EXIT_OF = {"swing": "swing", "swing_mirror": "swing", "swing_2h": "swing", "swing_2h_mirror": "swing",
           "impulse_follow": "fixed", "impulse_fade": "fixed", "impulse_ai": "fixed", "swing_ratchet": "ratchet", "intraday_pick": "fixed",
           "counter_trend": "fixed", "intraday_mirror": "fixed", "counter_trend_mirror": "fixed",
           "stop_reverse_2r": "fixed", "stop_reverse_15r": "fixed", "stop_same_side_2r": "fixed",
           "fed_reaction": "fixed", "fed_fade": "fixed", "news_event_template": "fixed", "news_event_template_ctrl": "fixed",
           "risk_radar_paper": "fixed", "risk_radar_paper_ctrl": "fixed"}


def _naive(ts: datetime) -> datetime:
    from datetime import UTC
    return ts.astimezone(UTC).replace(tzinfo=None) if ts.tzinfo else ts


def open_trade(track: str, sig_id, symbol: str, side: int, signal_price: float, stop: float, target: float,
               signal_ts: datetime, signal_type: str = "", timeframe: str = "", label: str = "",
               ladder: str = "1:0,1.5:0.5,2:1,2.5:1.5", buffer_r: float = 0.25, confirm_min: int = 60) -> dict | None:
    """A virtual trade; it enters at the open of the first 1m bar after the signal, keeping the signal's stop distance
    and target in R. None when the levels make no sense (no stop, target on the wrong side)."""
    dist = abs(signal_price - stop)
    rr = (target - signal_price) * side / dist if dist > 0 else 0.0
    if not (signal_price > 0 and dist > 0 and rr > 0):
        return None
    hold = (IMPULSE_HOLD if track.startswith("impulse") else
            SWING_HOLD if EXIT_OF[track] in ("swing", "ratchet") else INTRADAY_HOLD)
    return {"id": f"{track}:{sig_id}", "sig_id": sig_id, "track": track, "exit_kind": EXIT_OF[track],
            "symbol": symbol.upper(), "side": side, "signal_type": signal_type, "timeframe": timeframe,
            "label": label or "-", "signal_ts": _naive(signal_ts).isoformat(), "stop_frac": dist / signal_price,
            "rr": round(rr, 4), "hold_s": hold.total_seconds(), "ladder": ladder, "buffer_r": buffer_r,
            "confirm_min": confirm_min, "entry": None, "entry_ts": None, "status": "waiting",
            "last_ts": _naive(signal_ts).isoformat(), "best_r": 0.0, "worst_r": 0.0}


def _r(tr: dict, price: float) -> float:
    return tr["side"] * (price - tr["entry"]) / tr["risk"]


def _close(tr: dict, price: float, ts: datetime, reason: str) -> None:
    tr.update(status="closed", exit_price=price, exit=reason, closed_ts=ts.isoformat())
    tr["r_net"] = round(_r(tr, price) - COST * tr["entry"] / tr["risk"], 4)


def advance(tr: dict, bars) -> dict:
    """Walk closed 1m bars (dicts or candles, open-time stamped) newer than the trade's last processed bar. Stop first
    on a bar that touches both; a stop the lock has moved fires only on a confirmed close beyond it (live rule)."""
    if tr["status"] == "closed":
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
            risk = o * tr["stop_frac"]
            tr.update(entry=o, entry_ts=ts.isoformat(), risk=risk, init_stop=o - s * risk, stop=o - s * risk,
                      target=o + s * tr["rr"] * risk, best=o, status="open")
        fav, adv = (h, lo) if s > 0 else (lo, h)
        tr["best_r"] = max(tr["best_r"], _r(tr, fav))
        tr["worst_r"] = min(tr["worst_r"], _r(tr, adv))
        locked = s * (tr["stop"] - tr["init_stop"]) > 1e-12
        stop_hit = (lo <= tr["stop"]) if s > 0 else (h >= tr["stop"])
        if stop_hit and not (locked and tr["exit_kind"] != "fixed" and tr["confirm_min"]):
            gap = (o <= tr["stop"]) if s > 0 else (o >= tr["stop"])
            _close(tr, o if gap else tr["stop"], ts, "lock" if locked else "stop")
            break
        if (h >= tr["target"]) if s > 0 else (lo <= tr["target"]):
            gap = (o >= tr["target"]) if s > 0 else (o <= tr["target"])
            _close(tr, o if gap else tr["target"], ts, "target")
            break
        bar_end = ts + timedelta(minutes=1)
        if (locked and tr["exit_kind"] != "fixed" and tr["confirm_min"]
                and int((bar_end - EPOCH).total_seconds()) % (tr["confirm_min"] * 60) == 0
                and ((c <= tr["stop"]) if s > 0 else (c >= tr["stop"]))):
            _close(tr, c, ts, "lock")
            break
        if tr["exit_kind"] in ("swing", "ratchet"):
            tr["stop"], tr["best"] = swing_lock(s, tr["entry"], tr["init_stop"], tr["stop"], tr["best"], fav,
                                                "ladder", 0.0, 0.0, tr["ladder"], tr["buffer_r"])
        if tr["exit_kind"] == "ratchet":
            best_r = _r(tr, tr["best"])
            if best_r >= RATCHET_FROM_R:
                k = int((best_r - RATCHET_FROM_R) / RATCHET_STEP_R + 1e-9)
                trig = RATCHET_FROM_R + k * RATCHET_STEP_R
                tgt = tr["entry"] + s * (trig + RATCHET_TARGET_GAP_R) * tr["risk"]
                lock = tr["entry"] + s * (trig - RATCHET_LOCK_GAP_R) * tr["risk"]
                tr["target"] = max(tr["target"], tgt) if s > 0 else min(tr["target"], tgt)
                tr["stop"] = max(tr["stop"], lock) if s > 0 else min(tr["stop"], lock)
        tr["last_ts"] = ts.isoformat()
        if (bar_end - datetime.fromisoformat(tr["entry_ts"])).total_seconds() >= tr["hold_s"]:
            _close(tr, c, ts, "time")
            break
    return tr


def mark(tr: dict, price: float) -> dict | None:
    """An open trade as if closed now at `price` (after fees), or None before its entry."""
    if not tr.get("entry") or not price:
        return None
    return {**tr, "r_net": round(_r(tr, price) - COST * tr["entry"] / tr["risk"], 4), "marked": True}


def week_of(ts: str) -> str:
    y, w, _ = datetime.fromisoformat(ts).isocalendar()
    return f"{y}-W{w:02d}"


def btc_week_dir(btc_days: dict) -> dict:
    """{'2026-W44': +1 | -1} from {'YYYY-MM-DD': [first, last] BTC price seen that day}."""
    weeks: dict[str, list] = {}
    for day in sorted(btc_days):
        weeks.setdefault(week_of(day + "T00:00:00"), []).append(btc_days[day])
    return {w: (1 if v[-1][1] >= v[0][0] else -1) for w, v in weeks.items() if v and v[0][0]}


def _stats(rows: list[dict], weekdir: dict) -> dict | None:
    if not rows:
        return None
    rs = [r["r_net"] for r in rows]

    def avg(sel):
        x = [r["r_net"] for r in rows if sel(r)]
        return (round(sum(x) / len(x), 3), len(x)) if x else (None, 0)
    up = avg(lambda r: weekdir.get(week_of(r["entry_ts"])) == 1)
    down = avg(lambda r: weekdir.get(week_of(r["entry_ts"])) == -1)
    return {"n": len(rows), "avg_r": round(sum(rs) / len(rs), 3), "win_pct": round(100 * sum(x > 0 for x in rs) / len(rs), 1),
            "total_r": round(sum(rs), 2), "long": avg(lambda r: r["side"] > 0), "short": avg(lambda r: r["side"] < 0),
            "rising_weeks": up, "falling_weeks": down}


def scorecard(closed: list[dict], open_marked: list[dict], weekdir: dict, started: str, now: datetime) -> dict:
    """Per track: closed results, open trades marked now, and the promotion gate in plain words."""
    days = (_naive(now) - datetime.fromisoformat(started)).total_seconds() / 86400 if started else 0.0
    tracks: dict = {}
    by = {}
    for r in closed:
        by.setdefault(r["track"], []).append(r)
    for t in sorted({*by, *(r["track"] for r in open_marked)}):
        tracks[t] = {"closed": _stats(by.get(t, []), weekdir),
                     "open_now": _stats([r for r in open_marked if r["track"] == t], weekdir),
                     "labels": {lab: _stats([r for r in by.get(t, []) if r["label"] == lab], weekdir)
                                for lab in sorted({r["label"] for r in by.get(t, [])})} if t == "swing" else {}}
    for t, rec in tracks.items():
        st, ctrl = rec["closed"], (tracks.get(CONTROL.get(t, "")) or {}).get("closed")
        checks = [("running 28+ days", days >= GATE["days"], f"{days:.0f} days"),
                  ("100+ closed trades", bool(st) and st["n"] >= GATE["n"], f"{st['n'] if st else 0} closed"),
                  ("profitable after fees", bool(st) and st["avg_r"] > 0, f"{st['avg_r']:+.3f}R" if st else "-")]
        if t in CONTROL:
            checks.append(("beats its control", bool(st and ctrl) and st["avg_r"] > ctrl["avg_r"],
                           f"control {ctrl['avg_r']:+.3f}R" if ctrl else "control: no trades yet"))
        for kind, key in (("rising", "rising_weeks"), ("falling", "falling_weeks")):
            a, n = st[key] if st else (None, 0)
            checks.append((f"profitable in {kind} BTC weeks", a is not None and n >= GATE["n_each_week_kind"] and a > 0,
                           f"{a:+.3f}R on {n}" if a is not None else "no trades yet"))
        is_control = t not in CONTROL
        passed = all(ok for _, ok, _ in checks)
        rec["gate"] = [{"check": c, "ok": ok, "detail": d} for c, ok, d in checks]
        rec["status"] = ("control" if is_control else "qualifies for live (owner decides)" if passed
                         else "collecting" if days < GATE["days"] or not st or st["n"] < GATE["n"] else "not qualifying")
    return {"days": round(days, 1), "tracks": tracks}
