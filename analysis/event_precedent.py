"""
Precedent-based event context — NOT a price forecaster.

The owner was told plainly that no detector stack here can promise what a
multi-day price move will be, and agreed. What this module does instead:
when a calendar event is coming up (a Fed decision, a fiscal-year start, an
India budget), ask a web-search model for REAL, VERIFIABLE past occurrences
of comparable events — matched on category and on which US administration
was in office, since that changes the dynamics — then measure OUR OWN
historical OHLCV data (never the model's memory) for what actually happened
around those precedent dates, then ask a model to characterise the pattern
grounded in those measured numbers.

The result is advisory context only, clamped the same way
analysis.event_calendar.caution() already clamps calendar caution: it can
soften confidence by a small, capped amount (the identical penalty-bucket
scale caution() uses, keyed by the triggering event's own level) and it can
mark a signal eligible for a longer paper-trade hold ceiling. It never
raises confidence and never lowers the entry bar — see
`is_usable`/`confidence_penalty`/`allows_extended_hold` below, and
scheduler.runner._event_precedent_job / analysis.crypto_engine.process for
where it is actually applied.

Zero real precedents is a valid, common, CORRECT outcome for a genuinely
novel event — not an error. Nothing is cached or acted on for it.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta

import pandas as pd

CONFIDENCE_LEVELS = ("high", "medium", "low")
DIRECTION_BIASES = ("bullish", "bearish", "mixed")

# Identical to the bucket analysis.crypto_engine.process() and
# scheduler.settings_page._calendar_caution_row already use for
# calendar_caution — on purpose. The instruction was to reuse the existing
# penalty-bucket pattern rather than invent a second, uncapped nudge
# mechanism, so a precedent brief only ever costs confidence on the same
# scale a structural calendar item already does.
_PENALTY_BY_LEVEL = {5: 0.08, 4: 0.06, 3: 0.03}


def confidence_penalty(level: int) -> float:
    return _PENALTY_BY_LEVEL.get(level, 0.01)


def is_usable(brief: dict, min_sample_size: int) -> bool:
    """
    A brief may only touch anything live once it has real support behind
    it: at least `min_sample_size` precedent occurrences that actually had
    measured market data (never the model's own count), and no precedent
    used was itself flagged 'low' confidence of being real.
    """
    return (int(brief.get("sample_size", 0)) >= max(1, min_sample_size)
            and brief.get("confidence_real", "low") != "low")


def allows_extended_hold(brief: dict, signal_direction: str) -> bool:
    """
    The extended hold is the "take longer time trades" part of the ask, for
    a setup that already cleared the entry bar on its own — never a reason
    to enter. A precedent brief with a real directional lean only grants it
    to a signal moving the SAME way that lean has historically gone; a
    signal against that lean gets nothing extra. A 'mixed' brief (typical of
    events with genuinely two-sided history) grants it either way.
    """
    bias = brief.get("direction_bias", "mixed")
    if bias not in ("bullish", "bearish"):
        return True
    wanted = "long" if bias == "bullish" else "short"
    return signal_direction == wanted


# ── Cache the scheduler job refreshes, read synchronously from
# analysis.crypto_engine.process() ─────────────────────────────────────────
#
# process() runs on every tick and is synchronous; the brief itself lives in
# the database and takes a DB session and AI calls to build. The pattern
# already used for the calendar (a pure function over `now`) does not fit a
# DB-backed value, so instead the scheduler job
# (scheduler.runner._event_precedent_job) refreshes a small in-process cache
# every time it runs, and this module offers the same kind of pure,
# synchronous lookup over that cache that caution() offers over the
# calendar. Empty until the job first runs, or whenever the feature is off.

_active: dict[str, dict] = {}


def set_active_briefs(briefs: dict[str, dict]) -> None:
    """Replace the whole cache — called once per run of the scheduler job."""
    global _active
    _active = dict(briefs)


def current_brief(now: datetime) -> dict | None:
    """The one active precedent brief for right now, or None."""
    for brief in _active.values():
        if brief["window_start"] <= now <= brief["window_end"]:
            return brief
    return None


def brief_from_row(row, lookahead_days: int) -> dict:
    """A cached DB row as the small dict `current_brief`/the confidence and
    hold-time gates read. The active window starts `lookahead_days` before
    the event (matching how far ahead the job looks for it) and runs until
    48 hours after — long enough to cover the event itself and its
    immediate aftermath without the window overstaying for weeks."""
    window_start = row.event_at - timedelta(days=lookahead_days)
    window_end = row.event_at + timedelta(hours=48)
    return {
        "event_key": row.event_key,
        "event_name": row.event_name,
        "level": row.level,
        "sample_size": row.sample_size,
        "confidence_real": row.confidence_real,
        "direction_bias": row.direction_bias,
        "typical_magnitude_pct": row.typical_magnitude_pct,
        "typical_duration_days": row.typical_duration_days,
        "summary": row.summary,
        "window_start": window_start,
        "window_end": window_end,
    }


# ── Step A: find real precedent (AI, web-search) ───────────────────────────

PRECEDENT_FIND_SYSTEM = (
    "You are a market historian helping a trading desk understand what "
    "usually happens around a recurring kind of event.\n\n"
    "Find REAL, VERIFIABLE past occurrences of events comparable to the one "
    "described below. Match on: the same category of event (e.g. a Fed rate "
    "decision, a US jobs report, a fiscal-year-start funding fight, a "
    "monthly options expiry) AND the same US administration/president in "
    "office at the time — the dynamics are not the same across "
    "administrations, so do not mix precedents from a different one.\n\n"
    "Return only occurrences you are confident really happened, with real "
    "dates. If you cannot find any comparable occurrence, or you are unsure "
    "whether one really happened, return an EMPTY LIST rather than invent a "
    "plausible-sounding one — guessing is worse than saying nothing, and an "
    "empty list is a normal, correct answer for a genuinely novel event.\n\n"
    "JSON only:\n"
    '{"precedents": [{"name": "short name, e.g. \'Dec 2018 FOMC hike\'", '
    '"start_date": "YYYY-MM-DD", '
    '"end_date": "YYYY-MM-DD, or empty if it was a single day", '
    '"confidence_this_is_real": "high|medium|low"}]}'
)


def find_prompt(event_name: str, when: str, category_hint: str = "") -> str:
    return (
        f"Upcoming event: {event_name}\n"
        f"When: {when}\n"
        + (f"Category hint: {category_hint}\n" if category_hint else "")
        + "\nFind real past occurrences of comparable events, matched on "
        "category and the same US administration in office at the time."
    )


def _clean_date(raw) -> str:
    s = str(raw or "").strip()[:20]
    try:
        datetime.fromisoformat(s)
    except ValueError:
        return ""
    return s


def parse_precedents(data: dict) -> list[dict]:
    """Clean a model reply. A precedent with no name, or a date that does
    not parse, is dropped rather than guessed at."""
    out = []
    for p in (data.get("precedents") or [])[:12]:
        if not isinstance(p, dict):
            continue
        name = str(p.get("name") or "").strip()[:200]
        start = _clean_date(p.get("start_date"))
        if not name or not start:
            continue
        end = _clean_date(p.get("end_date")) or start
        conf = str(p.get("confidence_this_is_real") or "").strip().lower()
        out.append({
            "name": name,
            "start_date": start,
            "end_date": end,
            "confidence_this_is_real": conf if conf in CONFIDENCE_LEVELS else "low",
        })
    return out


def confidence_bucket(used_precedents: list[dict]) -> str:
    """The most cautious confidence_this_is_real among the precedents that
    actually had measured market data behind them — a brief is only as
    trustworthy as its worst-supported ingredient. No usable precedents at
    all reads as 'low' (the caller should not have built a brief from zero
    anyway; this is a safe default, not the expected path)."""
    levels = {p["confidence_this_is_real"] for p in used_precedents}
    if not levels or "low" in levels:
        return "low"
    return "medium" if "medium" in levels else "high"


# ── Step B: real market data for those dates, from the Binance lake ────────

def measure_window(symbol: str, start: datetime, end: datetime, root: str = "data/lake",
                   baseline_days: int = 14) -> dict | None:
    """
    Plain, real numbers for one symbol around one precedent occurrence, read
    from the local Binance Parquet lake — never the AI, never a live fetch.

    Returns None (never raises) when the lake has no usable coverage for
    these dates: before the symbol existed, before the lake's own history
    starts, or too few bars either side to mean anything. A caller iterating
    many (precedent, symbol) pairs must be able to skip these silently.
    """
    from analysis.structure import atr
    from collectors.binance_lake import read

    try:
        start_ts, end_ts = pd.Timestamp(start), pd.Timestamp(end)
    except (TypeError, ValueError):
        return None
    if end_ts < start_ts:
        start_ts, end_ts = end_ts, start_ts
    pad = timedelta(days=baseline_days + 5)
    try:
        df = read("klines", symbol, start_ts - pad, end_ts + timedelta(days=3),
                  interval="1d", market="um", root=root)
    except Exception:
        return None
    if df is None or df.empty or len(df) < 5 or "close" not in df.columns:
        return None
    df = df.sort_values("ts").reset_index(drop=True)

    during = df[(df["ts"] >= start_ts) & (df["ts"] <= end_ts)]
    before = df[(df["ts"] >= start_ts - timedelta(days=baseline_days)) & (df["ts"] < start_ts)]
    if during.empty or before.empty:
        return None

    price_start = float(during.iloc[0]["open"])
    price_end = float(during.iloc[-1]["close"])
    if not price_start or not math.isfinite(price_start) or price_start <= 0:
        return None
    price_change_pct = (price_end - price_start) / price_start * 100.0

    vol_during = during["volume"].mean()
    vol_baseline = before["volume"].mean()
    volume_change_pct = (
        round(float((vol_during - vol_baseline) / vol_baseline * 100.0), 2)
        if vol_baseline and vol_baseline > 0 and math.isfinite(vol_baseline) else None)

    df["atr_pct"] = atr(df, n=14) / df["close"] * 100.0
    after_mask = (df["ts"] > end_ts) & (df["ts"] <= end_ts + timedelta(days=3))

    def _avg(mask) -> float | None:
        vals = df.loc[mask, "atr_pct"].dropna()
        return round(float(vals.mean()), 3) if len(vals) else None

    return {
        "symbol": symbol,
        "price_change_pct": round(price_change_pct, 3),
        "volume_change_pct": volume_change_pct,
        "atr_pct_before": _avg(df["ts"] < start_ts),
        "atr_pct_during": _avg((df["ts"] >= start_ts) & (df["ts"] <= end_ts)),
        "atr_pct_after": _avg(after_mask),
        "bars": int(len(during)),
    }


# ── Step C: synthesise, grounded in the measured numbers ───────────────────

PRECEDENT_SYNTH_SYSTEM = (
    "You characterise what typically happens to crypto prices around a "
    "recurring kind of event, from REAL measured market data — never from "
    "general knowledge or a commonly believed narrative.\n\n"
    "You are given a list of past occurrences already identified as "
    "comparable, and for each one, REAL numbers measured from actual "
    "historical price data (never invent, adjust or round these numbers): "
    "price change % across the event window, volume change % vs a trailing "
    "baseline, and ATR% (volatility) before/during/after. Some occurrences "
    "have no measured numbers for a given coin — it did not exist yet, or "
    "the data does not reach that far back — ignore those for the "
    "statistics; do not guess a number to fill the gap.\n\n"
    "Ground everything in the numbers you were given. If they contradict a "
    "narrative you might otherwise reach for ('rate cuts are bullish', "
    "'shutdowns cause panic'), trust the numbers and say so plainly.\n\n"
    "JSON only:\n"
    '{"direction_bias": "bullish|bearish|mixed", '
    '"typical_magnitude_pct": 0.0, '
    '"typical_duration_days": 0.0, '
    '"summary": "2 to 4 sentences, grounded in the numbers you were given"}'
)


def synth_prompt(event_name: str, precedents: list[dict], measurements: dict) -> str:
    import json
    lines = [f"Event: {event_name}", "",
             "Precedents identified, with real measured market data:"]
    for p in precedents:
        lines.append(f"- {p['name']} ({p['start_date']} to {p['end_date']}), "
                     f"confidence this is real: {p['confidence_this_is_real']}")
        per_symbol = measurements.get(p["name"])
        if per_symbol:
            for sym, m in per_symbol.items():
                lines.append(f"  {sym.upper()}: {json.dumps(m, separators=(',', ':'))}")
        else:
            lines.append("  no measured data available for this occurrence "
                         "(predates our data, or the coin did not exist yet)")
    return "\n".join(lines)


def _clamp_abs(x, hi: float) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(v):
        return 0.0
    return max(-hi, min(hi, v))


def parse_synthesis(data: dict, sample_size: int) -> dict:
    """
    Clean a model reply. `sample_size` is passed in by the caller — how many
    precedent occurrences actually had measured numbers behind them — and
    used verbatim rather than trusting the model to count its own input
    correctly; the count is a fact the caller already knows exactly.
    """
    bias = str(data.get("direction_bias") or "").strip().lower()
    return {
        "direction_bias": bias if bias in DIRECTION_BIASES else "mixed",
        "typical_magnitude_pct": round(_clamp_abs(data.get("typical_magnitude_pct"), 50.0), 3),
        "typical_duration_days": round(_clamp_abs(data.get("typical_duration_days"), 30.0), 2),
        "sample_size": int(sample_size),
        "summary": str(data.get("summary") or "").strip()[:900],
    }
