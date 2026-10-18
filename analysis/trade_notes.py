"""
Trade notes (owner, 18 Oct 2026, AI plan step 7): a short plain-English note when a swing trade opens (why it fired)
and when it closes (what happened, and what news came while it was open). It goes on the trade card and to Telegram.

The note is written ONLY from facts we hand over - the model is told to add nothing - and it never touches the trade:
it is written after the order is placed, in its own task, and a failed note is simply no note. One Gemini Flash-Lite
call per event (about 10 a day at most, inside the free 500).
"""
from __future__ import annotations

import json
from datetime import datetime

NOTE_SYSTEM = (
    "You write a short note for a trader's journal about one trade. Use ONLY the facts in the JSON you are given - "
    "never add prices, news, reasons or numbers that are not there, and never predict or advise. 2 or 3 plain "
    "sentences, at most 60 words, rupees as Rs, times as given. For an 'open' note say what fired and how the trade "
    "is set up (stop, target, leverage, risk). For a 'close' note say how it ended, the result in R and rupees, how "
    "long it ran, and mention the news only if some is listed.\n"
    'Reply with JSON only: {"note": "..."}'
)
MAX_CHARS = 420


def _pct(a: float, b: float) -> float:
    return round(abs(a - b) / a * 100, 2) if a else 0.0


def open_facts(pos, signal_type: str, timeframe: str, risk_pct: float, verdict=None, rating=None,
               live: bool = False) -> dict:
    """What the note may say about a new trade."""
    f = {"kind": "open", "coin": pos.symbol.upper(), "side": getattr(pos.side, "value", pos.side),
         "strategy": signal_type.removeprefix("swing_").replace("_", " "), "timeframe": timeframe,
         "entry": round(pos.entry_price, 6), "stop": round(pos.stop_price, 6),
         "stop_distance_pct": _pct(pos.entry_price, pos.stop_price), "target": round(pos.target_price, 6),
         "target_in_R": 3, "leverage": round(float(pos.leverage), 1), "risk_pct_of_wallet": round(risk_pct * 100, 2),
         "time_limit_days": 7, "also_on_live_delta": bool(live)}
    if verdict is not None and getattr(verdict, "btc_vol_rank", None) is not None:
        f["btc_volatility_rank"] = round(float(verdict.btc_vol_rank), 2)      # 0 calm .. 1 wild; trades need <= 0.67
    if rating:
        f["backtest_grade"] = rating.get("grade")
    return f


def close_facts(row, trade, headlines: list, now: datetime) -> dict:
    """What the note may say about a finished trade. `headlines`: (published_at, source, headline) during the trade."""
    risk = abs(row.entry_price - (row.initial_stop_price or row.stop_price))
    side = 1 if row.side == "long" else -1
    r = side * (trade.exit_price - row.entry_price) / risk if risk else 0.0
    opened = row.opened_at.replace(tzinfo=None) if row.opened_at.tzinfo else row.opened_at
    hours = max(0.0, ((now.replace(tzinfo=None) if now.tzinfo else now) - opened).total_seconds() / 3600)
    return {"kind": "close", "coin": row.symbol.upper(), "side": row.side,
            "strategy": str(row.signal_type or "").removeprefix("swing_").replace("_", " "),
            "how_it_ended": trade.reason.value, "entry": round(row.entry_price, 6),
            "exit": round(trade.exit_price, 6), "result_R": round(r, 2), "net_rupees": round(trade.net_pnl, 2),
            "ran_for_hours": round(hours, 1),
            "news_while_open": [f"{t:%d %b %H:%M} UTC {src}: {h}" for t, src, h in headlines[:5]]}


def clean_note(data: dict) -> str:
    """The model's note, trimmed; '' when there is nothing usable."""
    note = data.get("note") if isinstance(data, dict) else None
    if not isinstance(note, str):
        return ""
    note = " ".join(note.split())
    return note if len(note) <= MAX_CHARS else note[: MAX_CHARS - 1].rsplit(" ", 1)[0] + "…"


async def write_note(facts: dict) -> tuple[str, str]:
    """(note, served_by); ('', '') when no model answered. Never raises."""
    try:
        from collectors.llm_client import ask_json
        reply = await ask_json("trade_note", NOTE_SYSTEM, json.dumps(facts, default=str), max_tokens=220,
                               temperature=0.2, timeout=20.0)
        return (clean_note(reply.data), reply.served_by) if reply else ("", "")
    except Exception:  # noqa: BLE001 - a note is never worth an error
        return "", ""
