"""
Why did the market move? Asked every hour, answered with the news.

The briefing says what happened in the world. This asks the other half: of
the moves the watchlist actually made, which ones did an event cause, which
were the whole market moving together, which were one coin on its own — and
which have no news behind them at all, with the reasoning for saying so.
Then it grades our own signals from the same hours against that picture:
were we on the right side of the news, or trading an RSI reading through a
Fed decision?

The numbers are computed here, never by the model. It is handed the moves
(1h, 4h and 12h change, the 12h range, BTC's move for comparison), the
signals we fired and how they ended, the latest web briefing and the scored
headlines, and asked to explain them. Groq's gpt-oss with browser_search can search the
web to check a cause it is unsure of.

Every answer is stored in `move_attributions`, so the local model can later
ask across weeks of them which kinds of news move which coins, and how our
signals behave around them.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

CAUSE_TYPES = ("news", "market_wide", "coin_specific", "no_clear_cause")


@dataclass(frozen=True)
class Move:
    symbol: str
    price: float
    change_1h: float | None
    change_4h: float | None
    change_12h: float | None
    range_12h: float | None      # (high - low) / low, percent

    def as_dict(self) -> dict:
        return {k: (round(v, 3) if isinstance(v, float) else v)
                for k, v in self.__dict__.items()}


def _pct(now: float, then: float | None) -> float | None:
    if not then or then <= 0 or not math.isfinite(then):
        return None
    return (now - then) / then * 100.0


def _price_at(points: list[tuple[datetime, float]], at: datetime,
              tolerance: timedelta = timedelta(minutes=10)) -> float | None:
    """The snapshot price closest to `at`, if one is within the tolerance."""
    best = None
    for ts, price in points:
        gap = abs(ts - at)
        if gap <= tolerance and (best is None or gap < best[0]):
            best = (gap, price)
    return best[1] if best else None


def summarise_moves(points_by_symbol: dict[str, list[tuple[datetime, float]]],
                    now: datetime) -> list[Move]:
    """Moves per symbol from (timestamp, price) snapshots, newest price as 'now'."""
    out = []
    for sym, points in points_by_symbol.items():
        pts = sorted((t, p) for t, p in points if p and p > 0 and math.isfinite(p))
        if not pts:
            continue
        last_t, last = pts[-1]
        window = [p for t, p in pts if t >= last_t - timedelta(hours=12)]
        lo, hi = min(window), max(window)
        out.append(Move(
            symbol=sym, price=last,
            change_1h=_pct(last, _price_at(pts, last_t - timedelta(hours=1))),
            change_4h=_pct(last, _price_at(pts, last_t - timedelta(hours=4))),
            change_12h=_pct(last, _price_at(pts, last_t - timedelta(hours=12),
                                            timedelta(minutes=30))),
            range_12h=(hi - lo) / lo * 100.0 if lo > 0 else None,
        ))
    return sorted(out, key=lambda m: m.symbol)


def coin_facts(state, move: Move | None = None, btc: Move | None = None) -> dict:
    """
    Hard numbers about one coin, computed here so the model has something
    concrete to cite instead of "liquidity drift".

    From the live state: volume against its average, the largest 1h candle
    of the window and when, 15m structure with the nearest levels, funding,
    open-interest change and order-flow read, and the move net of BTC.
    Anything the state cannot supply is left out, never guessed.
    """
    from analysis.price_action import levels, structure

    facts: dict = {}
    if state is None:
        return facts
    price = float(getattr(state, "current_price", 0) or 0)
    ratio = getattr(state, "volume_ratio", None)
    try:
        ratio = ratio() if callable(ratio) else ratio
    except Exception:
        ratio = None
    if ratio:
        facts["volume_vs_avg"] = round(float(ratio), 2)
    try:
        h1 = [c for c in state.get_candles("1h") if getattr(c, "is_closed", True)][-12:]
        c15 = [c for c in state.get_candles("15m") if getattr(c, "is_closed", True)]
    except Exception:
        h1, c15 = [], []
    if h1:
        big = max(h1, key=lambda c: abs(c.close - c.open) / c.open if c.open else 0)
        if big.open:
            facts["largest_1h_candle"] = {
                # Shown on /moves and read by the model: the owner reads IST.
                "at": (big.timestamp + timedelta(hours=5, minutes=30)).strftime("%H:%M IST"),
                "change_pct": round((big.close - big.open) / big.open * 100, 2),
            }
        vols = [c.volume for c in h1]
        if len(vols) >= 6 and sum(vols[:-3]) > 0:
            facts["volume_last3h_vs_prior"] = round(
                (sum(vols[-3:]) / 3) / (sum(vols[:-3]) / len(vols[:-3])), 2)
    if len(c15) >= 12 and price > 0:
        facts["structure_15m"] = structure(c15)
        sup, res = levels(c15, price)
        if sup:
            facts["support"] = round(sup, 6)
        if res:
            facts["resistance"] = round(res, 6)
        facts["high_12h"] = round(max(c.high for c in c15[-48:]), 6)
        facts["low_12h"] = round(min(c.low for c in c15[-48:]), 6)
    fr = getattr(state, "funding_rate_per_8h", None)
    if fr is not None:
        facts["funding_8h_pct"] = round(fr * 100, 4)
    oi = getattr(state, "oi_change_1h_pct", 0.0)
    if oi:
        facts["open_interest_1h_pct"] = round(oi, 2)
    cvd = getattr(state, "cvd_trend", "")
    if cvd and cvd != "neutral":
        facts["order_flow"] = cvd
    if move is not None and btc is not None and move.symbol != btc.symbol:
        if move.change_4h is not None and btc.change_4h is not None:
            facts["vs_btc_4h_pct"] = round(move.change_4h - btc.change_4h, 2)
    return facts


def signals_digest(signals) -> list[dict]:
    """The signals from the window, in the form the model and the page read."""
    return [{
        "symbol": s.symbol,
        # UTC with an explicit Z, so the page converts it to IST reliably.
        "at": s.timestamp.strftime("%Y-%m-%dT%H:%M:00Z") if s.timestamp else "",
        "type": s.signal_type,
        "direction": s.direction,
        "confidence": round(float(s.confidence or 0), 2),
        "outcome": s.outcome,
        "pnl_pct": round(float(s.pnl_pct or 0), 3),
        "blocked_by": s.suppressed_by or "",
    } for s in signals]


ATTRIBUTION_SYSTEM = (
    "You explain crypto price moves to a trading desk, and grade the desk's "
    "own signals against what really drove the market.\n\n"
    "You get: each watchlist coin's move over 1h, 4h and 12h and its 12h range "
    "(computed from real prices; trust these numbers and do not recompute "
    "them), the signals the desk fired in the same hours and how they ended, "
    "a web briefing of world events, and scored headlines. You may search the "
    "web to check whether an event really happened and when.\n\n"
    "For each coin decide the cause of its 4h and 12h move:\n"
    "- news: a specific event moved it. Name the event and the time.\n"
    "- market_wide: it moved with BTC and the rest, no coin-specific reason.\n"
    "- coin_specific: something about this coin alone.\n"
    "- no_clear_cause: no news fits. Then explain why you think it moved "
    "anyway (liquidity, a liquidation cascade, drift, a move too small to "
    "mean anything) and say so plainly. Never invent a cause to fill the gap.\n"
    "Compare each coin with BTC: moving with BTC is usually market_wide.\n\n"
    "Be concrete. Each coin comes with measured facts (volume against its "
    "average, the largest 1h candle and when, 15m structure, support and "
    "resistance, funding, open-interest change, order flow, move net of "
    "BTC). Every reasoning must cite at least two of them with their "
    "numbers, e.g. 'volume 2.3x average, the +1.8% candle at 14:00 broke "
    "resistance 339.5, OI +4% so new longs opened'. Words like 'drift', "
    "'liquidity swings' or 'risk-on tilt' are not causes unless a number "
    "backs them. A rise on falling OI is shorts covering; on rising OI with "
    "positive funding it is new longs, crowded if funding is high.\n\n"
    "Then say what to watch next for each coin, as prices: the level whose "
    "break continues the move, the level whose loss ends it, and whether "
    "that makes a long, a short or no trade right now.\n\n"
    "Then grade the signals: were they on the right side of what drove the "
    "market, did any fire into a news event they could not see, and what "
    "would have been the better call. Be specific, cite times.\n\n"
    "Times: the reader is in India. Every time you write, in any field, "
    "must be in IST (UTC+5:30) and say so, e.g. '19:05 IST'. The data below "
    "gives times in UTC (ending in Z); convert them.\n\n"
    f"cause_type, use only these: {', '.join(CAUSE_TYPES)}\n\n"
    "Reply with JSON only:\n"
    '{"overall": "3 to 6 sentences: what drove the market in this window", '
    '"drivers": [{"event": "what happened", "when": "time in IST, e.g. 19:05 IST, or empty", '
    '"coins": ["btcusdt"], "direction": "up|down|mixed", "confidence": 0.0-1.0}], '
    '"coins": [{"symbol": "btcusdt", "cause_type": "news", '
    '"cause": "short name of the cause", "confidence": 0.0-1.0, '
    '"reasoning": "2 to 4 sentences", '
    '"signals_review": "how our signals on this coin fit the cause, or empty", '
    '"watch": "above X it continues to Y; below Z the move is over", '
    '"bias": "long|short|wait"}], '
    '"signals_verdict": "2 to 4 sentences grading the desk overall", '
    '"lesson": "one practical change for the desk, or empty"}'
)


def build_prompt(moves: list[Move], signals: list[dict], news: str,
                 window_hours: int, facts: dict | None = None) -> str:
    def f(v):
        return "n/a" if v is None else f"{v:+.2f}%"
    import json

    lines = [f"Window: last {window_hours} hours.", "", "Moves (price, 1h, 4h, 12h, 12h range):"]
    for m in moves:
        lines.append(f"- {m.symbol.upper()}: {m.price:,.6g} | 1h {f(m.change_1h)} | "
                     f"4h {f(m.change_4h)} | 12h {f(m.change_12h)} | range {f(m.range_12h)}")
        if facts and facts.get(m.symbol):
            lines.append(f"  facts: {json.dumps(facts[m.symbol], separators=(',', ':'))}")
    lines += ["", f"Our signals in the window ({len(signals)}):"]
    if not signals:
        lines.append("- none fired")
    for s in signals[:40]:
        blocked = f", blocked by {s['blocked_by']}" if s["blocked_by"] else ""
        lines.append(f"- {s['at']} {s['symbol'].upper()} {s['direction']} {s['type']} "
                     f"conf {s['confidence']:.2f} -> {s['outcome']} {s['pnl_pct']:+.2f}%{blocked}")
    lines += ["", "News:", news or "No news in the window."]
    return "\n".join(lines)


def _clamp(x, lo, hi, default=0.0) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v)) if math.isfinite(v) else default


def parse_attribution(data: dict, symbols: set[str]) -> dict:
    """Clean a model reply. Coins not on the watchlist are dropped."""
    coins = []
    for c in (data.get("coins") or []):
        if not isinstance(c, dict):
            continue
        sym = str(c.get("symbol") or "").strip().lower()
        if sym not in symbols:
            continue
        ctype = str(c.get("cause_type") or "").strip().lower()
        coins.append({
            "symbol": sym,
            "cause_type": ctype if ctype in CAUSE_TYPES else "no_clear_cause",
            "cause": str(c.get("cause") or "").strip()[:160],
            "confidence": _clamp(c.get("confidence"), 0.0, 1.0, 0.5),
            "reasoning": str(c.get("reasoning") or "").strip()[:900],
            "signals_review": str(c.get("signals_review") or "").strip()[:600],
            "watch": str(c.get("watch") or "").strip()[:300],
            "bias": (str(c.get("bias") or "").strip().lower()
                     if str(c.get("bias") or "").strip().lower() in ("long", "short", "wait")
                     else ""),
        })
    drivers = []
    for d in (data.get("drivers") or [])[:10]:
        if not isinstance(d, dict) or not str(d.get("event") or "").strip():
            continue
        direction = str(d.get("direction") or "mixed").strip().lower()
        drivers.append({
            "event": str(d.get("event")).strip()[:200],
            "when": str(d.get("when") or "").strip()[:40],
            "coins": [str(x).lower() for x in (d.get("coins") or [])
                      if str(x).lower() in symbols][:20],
            "direction": direction if direction in ("up", "down", "mixed") else "mixed",
            "confidence": _clamp(d.get("confidence"), 0.0, 1.0, 0.5),
        })
    return {
        "overall": str(data.get("overall") or "").strip()[:1500],
        "drivers": drivers,
        "coins": coins,
        "signals_verdict": str(data.get("signals_verdict") or "").strip()[:1000],
        "lesson": str(data.get("lesson") or "").strip()[:400],
        "skipped": [],
    }
