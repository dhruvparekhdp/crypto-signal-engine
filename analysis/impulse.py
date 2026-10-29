"""
Live move detector (owner 30 Oct 2026: "fulltime monitor for every small things happening with live data, if data has
moved to a nice extent not 100 ... then also it should predict the direction"; "use ai little bit more for this, because
ai will have a brain with live news to analyse the scenario properly"). Alerts and paper only - nothing here trades.

A move ("impulse") is a coin's last 15 minutes moving at least K times its normal 15-minute move (the standard deviation
of its 15-minute returns over the last 24 h) and at least 1%, on at least 3x its normal 15-minute volume. The lab
(scripts in the trial ledger, family "impulse", 9 coins 2021-26, 13k events at K=4) found the direction is real:
following the move beat going against it in every setting (2R 24 h: -0.05 / -0.03R vs -0.30 / -0.30R after fees), but
after fees following alone only broke even (stop 2x the window's range, at least 1.5%, 3R, 3 days: +0.026 / +0.012R).
So each move is scored three ways on paper: follow, fade (the control) and the AI's call with the live news, to learn
which kinds of move pay.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass

K_SD = 4.0                 # the lab's better setting (K=3 was worse in both periods)
MIN_MOVE = 0.01
VOL_MULT = 3.0
STOP_RANGE_MULT = 2.0      # stop beyond the window's opposite extreme, twice as far: fees are a smaller share of R
MIN_STOP = 0.015
TARGET_R = 3.0
HOLD_H = 72
COOLDOWN_H = 4
# Binance futures has no XAUT: gold's moves are read from PAXG (the same ounce, as in scripts/live_sim)
DATA_SYMBOL = {"XAUTUSDT": "PAXGUSDT"}


@dataclass
class Impulse:
    symbol: str
    side: int               # +1 up, -1 down
    move_pct: float         # last 15 min, %
    sd_mult: float          # move / normal 15-min move
    vol_mult: float         # 15-min volume / normal 15-min volume
    price: float
    window_high: float
    window_low: float
    at_ms: int              # close time of the last 1m bar in the window

    def as_dict(self) -> dict:
        return asdict(self)


def baseline(closes_15m: list[float], vols_15m: list[float]) -> tuple[float, float] | None:
    """(std of 15-minute returns, mean 15-minute volume) over the closed 15m bars given (24 h = 96). None if too few."""
    if len(closes_15m) < 49 or len(vols_15m) < 48:
        return None
    rets = [closes_15m[i] / closes_15m[i - 1] - 1 for i in range(1, len(closes_15m)) if closes_15m[i - 1] > 0]
    if len(rets) < 48:
        return None
    m = sum(rets) / len(rets)
    sd = math.sqrt(sum((r - m) ** 2 for r in rets) / (len(rets) - 1))
    vm = sum(vols_15m) / len(vols_15m)
    return (sd, vm) if sd > 0 and vm > 0 else None


def detect(symbol: str, bars_1m: list[tuple], base: tuple[float, float] | None, k_sd: float = K_SD,
           min_move: float = MIN_MOVE, vol_mult: float = VOL_MULT) -> Impulse | None:
    """bars_1m: the last 16+ CLOSED 1m bars as (open_ms, open, high, low, close, volume), oldest first. The move is the
    last close against the close 15 bars earlier; volume is the last 15 bars."""
    if base is None or len(bars_1m) < 16:
        return None
    sd, vm = base
    win = bars_1m[-15:]
    ref = bars_1m[-16][4]
    last = win[-1][4]
    if ref <= 0:
        return None
    r = last / ref - 1
    v15 = sum(b[5] for b in win)
    if abs(r) < max(k_sd * sd, min_move) or v15 < vol_mult * vm:
        return None
    return Impulse(symbol.upper(), 1 if r > 0 else -1, round(r * 100, 3), round(abs(r) / sd, 2), round(v15 / vm, 2),
                   last, max(b[2] for b in win), min(b[3] for b in win), int(win[-1][0]) + 60_000)


def levels(imp: Impulse, side: int) -> tuple[float, float]:
    """(stop, target) for a trade on `side` from the move's price: stop beyond the window's extreme on the far side,
    twice as far, at least MIN_STOP; target TARGET_R."""
    p = imp.price
    ext = imp.window_low if side > 0 else imp.window_high
    dist = max(STOP_RANGE_MULT * abs(p - ext), MIN_STOP * p)
    return p - side * dist, p + side * TARGET_R * dist


def breadth(moves_15m: dict[str, float], side: int, min_pct: float = 0.5) -> dict:
    """How many watchlist coins moved the same way in the same 15 minutes, and whether BTC led."""
    same = [s for s, m in moves_15m.items() if m * side >= min_pct]
    btc = moves_15m.get("BTCUSDT", 0.0)
    return {"same_side": len(same), "coins": len(moves_15m), "btc_move_pct": round(btc, 2),
            "btc_led": btc * side >= 1.0}


# ── the AI's read, with live news ─────────────────────────────────────────────────────────────────────────────────

AI_SYSTEM = (
    "You watch crypto markets live for a trading desk. A sudden move just happened. Use the facts given AND search the "
    "web for news from the last few hours to find the cause. Decide if the move is more likely to CONTINUE over the next "
    "hours or REVERSE, or say UNCLEAR. History: after such moves, continuing is more common than reversing, but most "
    "small continuations do not pay after fees; reversals happen after false headlines, quickly denied news, or forced "
    "liquidations that end. Never invent a cause: if you find none, say so. Reply with JSON only: "
    '{"moves": [{"symbol": "BTCUSDT", "call": "continue|reverse|unclear", "confidence": 0.0, "cause": "short plain '
    'words", "cause_type": "news|macro_release|liquidations|flows|technical|unknown", "source": "outlet or none", '
    '"reason": "one sentence"}], "summary": "one or two sentences for the owner"}')


def ai_prompt(moves: list[dict], context: dict) -> str:
    """The facts for one AI call covering every move detected in the same minute."""
    lines = ["MOVES (last 15 minutes):"]
    for m in moves:
        lines.append(f"- {m['symbol']}: {m['move_pct']:+.2f}% ({m['sd_mult']:.1f}x its normal 15-min move), volume "
                     f"{m['vol_mult']:.1f}x normal, price {m['price']:.6g}")
    b = context.get("breadth") or {}
    if b:
        lines.append(f"BREADTH: {b.get('same_side')} of {b.get('coins')} watchlist coins moved the same way; "
                     f"BTC {b.get('btc_move_pct', 0):+.2f}% in 15 min")
    for title, key in (("SCHEDULED RELEASES NEAR NOW", "calendar"), ("OPEN WORLD EVENTS", "events"),
                       ("HEADLINES, LAST 2 HOURS (newest first)", "headlines")):
        items = context.get(key) or []
        if items:
            lines.append(title + ":")
            lines += [f"- {x}" for x in items[:15]]
    if context.get("briefing"):
        lines.append("LATEST BRIEFING: " + str(context["briefing"])[:600])
    lines.append(f"TIME (UTC): {context.get('now', '')}")
    return "\n".join(lines)


CALLS = ("continue", "reverse", "unclear")
CAUSE_TYPES = ("news", "macro_release", "liquidations", "flows", "technical", "unknown")


def parse_ai(data, symbols: set[str]) -> dict:
    """{symbol: read} for the symbols asked about, plus 'summary'. Bad entries become 'unclear' with confidence 0."""
    out: dict = {"summary": "", "reads": {}}
    if not isinstance(data, dict):
        return out
    out["summary"] = str(data.get("summary") or "")[:400]
    for m in data.get("moves") or []:
        if not isinstance(m, dict):
            continue
        sym = str(m.get("symbol") or "").upper()
        if sym not in symbols:
            continue
        call = str(m.get("call") or "").lower().strip()
        try:
            conf = max(0.0, min(1.0, float(m.get("confidence") or 0)))
        except (TypeError, ValueError):
            conf = 0.0
        ct = str(m.get("cause_type") or "unknown").lower().strip()
        out["reads"][sym] = {"call": call if call in CALLS else "unclear", "confidence": round(conf, 2),
                             "cause": str(m.get("cause") or "")[:160], "cause_type": ct if ct in CAUSE_TYPES else "unknown",
                             "source": str(m.get("source") or "")[:80], "reason": str(m.get("reason") or "")[:240]}
    return out


def ai_side(imp_side: int, read: dict | None, min_conf: float = 0.5) -> int:
    """The side the AI's call implies: +/-1, or 0 when it is unclear or not confident enough."""
    if not read or read.get("confidence", 0) < min_conf:
        return 0
    if read.get("call") == "continue":
        return imp_side
    if read.get("call") == "reverse":
        return -imp_side
    return 0


def alert_text(events: list[dict], summary: str = "") -> str:
    """One Telegram message for the moves of one minute (HTML)."""
    lines = ["⚡ <b>Live move</b> (paper only, no trade)"]
    for ev in events:
        arrow = "🟢 up" if ev["side"] > 0 else "🔴 down"
        coin = ev["symbol"].removesuffix("USDT")
        lines.append(f"<b>{coin}</b> {arrow} {ev['move_pct']:+.2f}% in 15 min · {ev['sd_mult']:.1f}x normal · volume "
                     f"{ev['vol_mult']:.1f}x")
        a = ev.get("ai")
        if a:
            lines.append(f"   AI: <b>{a['call']}</b> ({int(a['confidence'] * 100)}%) · {a['cause_type']}: {a['cause'] or 'no cause found'}")
    b = events[0].get("breadth") or {}
    if b:
        lines.append(f"{b.get('same_side')} of {b.get('coins')} coins moved the same way · BTC {b.get('btc_move_pct', 0):+.2f}%")
    if summary:
        lines.append(summary)
    return "\n".join(lines)
