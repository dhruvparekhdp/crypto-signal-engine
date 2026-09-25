"""
Years of Binance history, turned into labelled events a model can learn from.

Three steps, cheapest first:

1. `notable_moves` finds every hour whose move was large for that coin at
   that time (a z-score against the trailing week), for free, over the whole
   lake. Five years of seven coins is a few thousand events.
2. `facts_before` measures what a trader could have seen in the hours before
   each one: 15m structure and levels, volume against normal, open interest
   and funding, the long/short ratios and taker flow. Numbers, not opinions.
3. A model labels each event from those facts (`REVIEW_SYSTEM`): what kind of
   move it was, whether the chart gave it away, which setup would have caught
   it, and the earliest sign. The local model takes the bulk; Groq with web
   search takes the biggest events, where "what happened that day" matters.

`timeframe_costs` answers a separate question the same data can: on which
bar size does a typical move clear the round-trip cost by enough to trade?
That is the evidence a dynamic signal timeframe has to rest on.
"""
from __future__ import annotations

import json
import math

import pandas as pd

SETUPS = ("trend_pullback", "sweep_reclaim", "range_fade", "breakout_retest",
          "news_shock", "liquidation_cascade", "none")
MOVE_TYPES = ("news", "market_wide", "coin_specific", "liquidation", "technical_break",
              "no_clear_cause")


# ── 1. Find the moves ────────────────────────────────────────────────────────

def notable_moves(k1h: pd.DataFrame, z: float = 3.0, lookback: int = 168,
                  min_pct: float = 1.0) -> pd.DataFrame:
    """
    Hours whose close-to-close return is at least `z` trailing standard
    deviations and at least `min_pct` percent. The std uses only past bars.
    Columns: ts, ret_pct, z, direction.
    """
    if k1h.empty or len(k1h) < lookback + 2:
        return pd.DataFrame(columns=["ts", "ret_pct", "z", "direction"])
    df = k1h.sort_values("ts").reset_index(drop=True)
    ret = df["close"].pct_change() * 100.0
    sd = ret.shift(1).rolling(lookback, min_periods=lookback // 2).std()
    zz = ret / sd
    hit = (zz.abs() >= z) & (ret.abs() >= min_pct)
    out = pd.DataFrame({"ts": df["ts"], "ret_pct": ret, "z": zz})[hit].copy()
    out["direction"] = out["ret_pct"].map(lambda r: "up" if r > 0 else "down")
    return out.reset_index(drop=True)


# ── 2. What could be seen before ─────────────────────────────────────────────

def _bars(df: pd.DataFrame, start, end) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    return df[(df["ts"] >= start) & (df["ts"] < end)]


def _candles(df: pd.DataFrame):
    """DataFrame rows -> the OHLC objects analysis.price_action reads."""
    from types import SimpleNamespace
    return [SimpleNamespace(open=r.open, high=r.high, low=r.low, close=r.close,
                            volume=r.volume, is_closed=True)
            for r in df.itertuples(index=False)]


def _r(x, n=4):
    return None if x is None or (isinstance(x, float) and not math.isfinite(x)) else round(x, n)


def facts_before(at, k15: pd.DataFrame, k1h: pd.DataFrame,
                 metrics: pd.DataFrame | None = None,
                 funding: pd.DataFrame | None = None) -> dict:
    """
    Everything measurable in the 24 hours up to the start of the move's hour
    `at`. Nothing from `at` onwards is used, so a label can never lean on
    the answer.
    """
    from analysis.price_action import candle_word, levels, structure

    at = pd.Timestamp(at)
    f: dict = {}
    b15 = _bars(k15, at - pd.Timedelta(hours=12), at)
    if len(b15) >= 12:
        c = _candles(b15)
        price = float(b15["close"].iloc[-1])
        f["price_before"] = _r(price, 6)
        f["structure_15m_12h"] = structure(c)
        sup, res = levels(c, price)
        if sup:
            f["support_pct"] = _r((sup - price) / price * 100, 2)
        if res:
            f["resistance_pct"] = _r((res - price) / price * 100, 2)
        hi, lo = float(b15["high"].max()), float(b15["low"].min())
        f["range_12h_pct"] = _r((hi - lo) / lo * 100, 2)
        f["pos_in_range_12h"] = _r((price - lo) / (hi - lo), 2) if hi > lo else None
        f["last_4x15m"] = " ".join(candle_word(x) for x in c[-4:])
        if "taker_buy_volume" in b15:
            last = b15.tail(8)
            vol = float(last["volume"].sum())
            if vol > 0:
                f["taker_buy_share_2h"] = _r(float(last["taker_buy_volume"].sum()) / vol, 3)
    b1h = _bars(k1h, at - pd.Timedelta(hours=24), at)
    if len(b1h) >= 12:
        v = b1h["volume"]
        base = float(v.iloc[:-3].mean())
        if base > 0:
            f["volume_3h_vs_21h"] = _r(float(v.iloc[-3:].mean()) / base, 2)
        f["change_24h_pct"] = _r(
            (float(b1h["close"].iloc[-1]) / float(b1h["open"].iloc[0]) - 1) * 100, 2)
    m = _bars(metrics, at - pd.Timedelta(hours=6), at)
    if m is not None and len(m) >= 2:
        oi0, oi1 = float(m["sum_open_interest"].iloc[0]), float(m["sum_open_interest"].iloc[-1])
        if oi0 > 0:
            f["oi_change_6h_pct"] = _r((oi1 / oi0 - 1) * 100, 2)
        for col, name in (("sum_toptrader_long_short_ratio", "top_trader_ls"),
                          ("count_long_short_ratio", "global_ls"),
                          ("sum_taker_long_short_vol_ratio", "taker_ls")):
            if col in m:
                f[name] = _r(float(m[col].iloc[-1]), 3)
    fr = _bars(funding, at - pd.Timedelta(hours=24), at)
    if fr is not None and len(fr):
        f["funding_last_pct"] = _r(float(fr["last_funding_rate"].iloc[-1]) * 100, 4)
    return f


def outcome_after(at, k1h: pd.DataFrame) -> dict:
    """What the move did next: the hour itself, then 4h and 24h on."""
    at = pd.Timestamp(at)
    after = _bars(k1h, at, at + pd.Timedelta(hours=25))
    if len(after) < 2:
        return {}
    p0 = float(after["open"].iloc[0])
    out = {"move_1h_pct": _r((float(after["close"].iloc[0]) / p0 - 1) * 100, 2)}
    for h in (4, 24):
        if len(after) > h:
            out[f"move_{h}h_pct"] = _r((float(after["close"].iloc[h - 1]) / p0 - 1) * 100, 2)
    return out


# ── 3. The label ─────────────────────────────────────────────────────────────

REVIEW_SYSTEM = (
    "You study past crypto moves so a trading system can learn to see the "
    "next one coming. You get one large hourly move: the coin, the time, the "
    "size, what happened after, and measured facts from the 24 hours BEFORE "
    "it (15m structure and levels, volume against normal, open-interest "
    "change, long/short ratios, taker flow, funding). If you can search the "
    "web, check what news, if any, hit at that time.\n\n"
    "Answer from the facts. Say plainly when the move was not visible "
    "beforehand; a system that learns false early signs loses money on "
    "them. Crowded positioning (high funding, long/short far from 1) plus a "
    "break of a level is a classic liquidation setup; rising OI into a move "
    "is new positions, falling OI is positions closing.\n\n"
    f"move_type, one of: {', '.join(MOVE_TYPES)}\n"
    f"setup (which rule would have caught it), one of: {', '.join(SETUPS)}\n\n"
    "JSON only:\n"
    '{"move_type": "...", "cause": "short, name the event if news", '
    '"visible_before": true|false, "early_signs": ["fact with its number"], '
    '"setup": "...", "entry_idea": "where and how a trader could have entered, '
    'or empty", "lesson": "one sentence", "confidence": 0.0-1.0}'
)


def review_prompt(symbol: str, event: dict) -> str:
    return (f"{symbol.upper()} moved {event['ret_pct']:+.2f}% in the hour starting "
            f"{event['at']} UTC ({event['z']:+.1f} standard deviations for this coin).\n"
            f"After: {json.dumps(event.get('after', {}))}\n"
            f"Facts before: {json.dumps(event.get('facts', {}))}")


def parse_review(data: dict) -> dict:
    def pick(v, allowed, default):
        v = str(v or "").strip().lower()
        return v if v in allowed else default
    try:
        conf = float(data.get("confidence", 0.5))
    except (TypeError, ValueError):
        conf = 0.5
    # min/max pass NaN through as 1.0 depending on order; reject it first.
    conf = max(0.0, min(1.0, conf)) if math.isfinite(conf) else 0.5
    return {
        "move_type": pick(data.get("move_type"), MOVE_TYPES, "no_clear_cause"),
        "cause": str(data.get("cause") or "").strip()[:200],
        "visible_before": bool(data.get("visible_before")),
        "early_signs": [str(x)[:120] for x in (data.get("early_signs") or [])][:6],
        "setup": pick(data.get("setup"), SETUPS, "none"),
        "entry_idea": str(data.get("entry_idea") or "").strip()[:300],
        "lesson": str(data.get("lesson") or "").strip()[:300],
        "confidence": conf,
    }


# ── Which timeframe can pay its fees ─────────────────────────────────────────

def timeframe_costs(k1m: pd.DataFrame, round_trip_pct: float = 0.118,
                    frames=("1min", "3min", "5min", "15min", "30min", "1h", "4h")) -> list[dict]:
    """
    For each bar size: the median bar range (high-low) in percent, and how
    many round trips of cost it covers. A setup targeting about one bar's
    range needs that ratio at 3 or more to survive fees and slippage.
    """
    if k1m.empty:
        return []
    s = k1m.set_index("ts").sort_index()
    out = []
    for tf in frames:
        bars = s.resample(tf).agg({"high": "max", "low": "min", "close": "last"}).dropna()
        if bars.empty:
            continue
        rng = ((bars["high"] - bars["low"]) / bars["close"] * 100).median()
        out.append({"timeframe": tf, "median_range_pct": round(float(rng), 3),
                    "cost_multiple": round(float(rng) / round_trip_pct, 2)})
    return out


def pick_timeframe(costs: list[dict], need: float = 3.0) -> str | None:
    """The smallest bar size whose median range covers the cost `need` times."""
    for row in costs:
        if row["cost_multiple"] >= need:
            return row["timeframe"]
    return None
