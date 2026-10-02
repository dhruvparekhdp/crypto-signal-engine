"""Optional recent-news lookup. Refuses old trades: searching today for a 2022 trade is hindsight."""
from __future__ import annotations

import datetime as dt

BASE = {"BTCUSDT": "bitcoin", "ETHUSDT": "ethereum", "SOLUSDT": "solana", "XRPUSDT": "xrp ripple",
        "BNBUSDT": "binance coin bnb", "ADAUSDT": "cardano", "DOGEUSDT": "dogecoin", "AVAXUSDT": "avalanche",
        "LINKUSDT": "chainlink", "LTCUSDT": "litecoin", "BCHUSDT": "bitcoin cash", "SUIUSDT": "sui"}


def headlines(symbol: str, entry: dt.datetime, max_age_days: int = 7, k: int = 5):
    """News published BEFORE `entry`, or [] when the trade is older than max_age_days or search is unavailable."""
    now = dt.datetime.utcnow()
    if (now - entry).days > max_age_days:
        return [], "skipped: trade older than the recent window"
    try:
        from ddgs import DDGS
    except ImportError:
        return [], "ddgs not installed"
    try:
        res = DDGS().news(f"{BASE.get(symbol, symbol)} crypto", max_results=25, timelimit="w")
    except Exception as e:  # network or rate limit
        return [], f"search failed: {str(e)[:60]}"
    out = []
    for r in res:
        try:
            d = dt.datetime.fromisoformat(str(r.get("date", "")).replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            continue
        if d < entry:
            out.append(f"{d:%Y-%m-%d %H:%M} {r.get('title', '')[:140]}")
    return out[:k], "ok"
