"""
The event monitor: what is happening in the world, how big it is, and what
trading around it would have done — run in shadow mode, so nothing here
changes a real paper trade yet.

Four pieces, all pure functions so they can be tested without a network:

1. Levels 1-5 and a category for every event (LEVELS, CATEGORIES). Groq
   gives a first level; the market gives the confirmed one afterwards
   (confirmed_level), from how far BTC actually moved. Two weeks of both is
   the dataset that says whether the model's levels mean anything.

2. The prompt (monitor_prompt) starts with the calendar — what kind of day,
   week, month and quarter it is — then the active events with their ids, so
   the model updates an event instead of reporting it again.

3. Adaptive timing (next_delay_minutes): calm means about 45 minutes between
   checks, a level-5 event about 5, each with +/-40% jitter, under a daily cap
   that keeps Groq's free tier (250 web-search calls) intact.

4. Shadow books (simulate_books) for every level 4-5 event, on the prices that
   followed it:
     A  pause         no trade — the baseline
     B  your idea     2x size from the release minute, in the direction of the
                      first move, tight 0.3% trailing stop
     C  confirmation  wait for the spike to settle (15 min at L4, 30 at L5),
                      enter on a break of that range with the stop just inside
                      it, 2x size, same rupee risk as a normal trade, trail 0.5%
     D  basket        C's entry on BTC, ETH and SOL together at normal size —
                      three correlated coins, measured as the one bet it is
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import datetime, timedelta

LEVELS = {
    5: "Major shock: changes the market for days (emergency or surprise central-bank move, "
       "war breaking out, exchange collapse, stablecoin depeg, US default, sweeping tariffs)",
    4: "High: moves everything for hours (FOMC decision, CPI far from forecast, ETF approval "
       "or rejection, hack over $100M, new sanctions, quarterly expiry)",
    3: "Medium: moves some coins or the market for an hour (CPI or jobs close to forecast, "
       "big ETF flows, regulator acting on one exchange, liquidation cascade)",
    2: "Low: worth knowing (Fed speeches, crypto-stock earnings, token unlocks, listings)",
    1: "Background: commentary, forecasts, small news",
}

CATEGORIES = (
    "central_bank", "inflation", "jobs", "growth", "fiscal_debt", "trade_tariffs",
    "geopolitics_war", "sanctions", "energy_commodities", "fx_dollar", "bonds_credit",
    "banking_stress", "equities", "crypto_regulation", "crypto_etf_flows", "stablecoin",
    "exchange_hack_insolvency", "liquidations_funding", "corporate_treasury",
    "crypto_supply_unlocks", "derivatives_expiry", "india", "asia", "other",
)

# How long an event stays in the monitor's memory, by level.
MEMORY_DAYS = {5: 7.0, 4: 3.0, 3: 1.0, 2: 0.5, 1: 0.25}


@dataclass
class ActiveEvent:
    id: int
    title: str
    category: str
    level: int
    first_seen: datetime
    direction: str = "mixed"


def active_window(events: list[ActiveEvent], now: datetime) -> list[ActiveEvent]:
    """Events still inside their memory window: L4-5 for up to 14 days, less for smaller."""
    keep = []
    for e in events:
        days = min(14.0, MEMORY_DAYS.get(e.level, 0.25))
        if now - e.first_seen <= timedelta(days=days):
            keep.append(e)
    return sorted(keep, key=lambda e: (-e.level, e.first_seen))


def monitor_prompt(calendar: str, active: list[ActiveEvent]) -> str:
    lines = [calendar, "", "Events already being tracked (update by id, do not report again):"]
    if not active:
        lines.append("- none")
    for e in active:
        lines.append(f"- id {e.id} [L{e.level}, {e.category}] {e.title} "
                     f"(since {e.first_seen:%d %b %H:%M} UTC)")
    return "\n".join(lines)


MONITOR_SYSTEM = (
    "You watch the world for a crypto trading desk. Search the web for what "
    "happened since the last check that moves crypto or risk assets.\n\n"
    "Use the calendar at the top: ask about what is actually scheduled now "
    "(an expiry in expiry week, a data release on release day), and do not "
    "go looking for scheduled events that are not due.\n\n"
    "Grade every event on this scale:\n"
    + "\n".join(f"{k}: {v}" for k, v in sorted(LEVELS.items(), reverse=True))
    + "\n\n"
    f"category, use only these: {', '.join(CATEGORIES)}\n\n"
    "Only report things you found in search results. Update an event already "
    "being tracked by its id (new facts, a changed level, or that it is over) "
    "instead of reporting it again. If nothing new happened, return empty lists.\n\n"
    "The reader is in India: in every text field (summary, titles, notes) "
    "write times in IST (UTC+5:30), e.g. '19:05 IST'. Only the `when` field "
    "stays ISO UTC, because code reads it.\n\n"
    "Reply with JSON only:\n"
    '{"risk_tone": -1.0 to 1.0, "summary": "2 to 4 plain sentences", '
    '"new": [{"title": "short factual line", "category": "tag", "level": 1-5, '
    '"direction": "up|down|mixed", "when": "ISO UTC or empty", "score": -1.0 to 1.0, '
    '"confidence": 0.0 to 1.0, "source": "site"}], '
    '"updates": [{"id": 0, "level": 1-5, "note": "what changed"}], '
    '"resolved": [0]}'
)


def _num(x, lo, hi, default):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v)) if math.isfinite(v) else default


def parse_monitor(data: dict, known_ids: set[int]) -> dict:
    """Clean a monitor reply; unknown ids and categories are dropped or mapped."""
    new = []
    for e in (data.get("new") or [])[:12]:
        if not isinstance(e, dict) or not str(e.get("title") or "").strip():
            continue
        cat = str(e.get("category") or "other").strip().lower()
        direction = str(e.get("direction") or "mixed").strip().lower()
        new.append({
            "title": str(e["title"]).strip()[:240],
            "category": cat if cat in CATEGORIES else "other",
            "level": int(_num(e.get("level"), 1, 5, 2)),
            "direction": direction if direction in ("up", "down", "mixed") else "mixed",
            "when": str(e.get("when") or "").strip()[:40],
            "score": _num(e.get("score"), -1, 1, 0.0),
            "confidence": _num(e.get("confidence"), 0, 1, 0.5),
            "source": str(e.get("source") or "").strip()[:80],
        })
    updates = []
    for u in (data.get("updates") or [])[:20]:
        if isinstance(u, dict) and int(_num(u.get("id"), 0, 1e12, 0)) in known_ids:
            updates.append({"id": int(u["id"]), "level": int(_num(u.get("level"), 1, 5, 0)) or None,
                            "note": str(u.get("note") or "").strip()[:300]})
    resolved = [int(r) for r in (data.get("resolved") or [])
                if isinstance(r, (int, float, str)) and str(r).isdigit() and int(r) in known_ids]
    return {"risk_tone": _num(data.get("risk_tone"), -1, 1, 0.0),
            "summary": str(data.get("summary") or "").strip()[:1200],
            "new": new, "updates": updates, "resolved": resolved}


# ── When to check next ────────────────────────────────────────────────────

BASE_MINUTES = {0: 45, 1: 45, 2: 45, 3: 20, 4: 10, 5: 5}


def next_delay_minutes(max_level: int, rng: random.Random | None = None) -> float:
    """Minutes until the next check: shorter when bigger events are live, +/-40% jitter."""
    r = rng or random
    base = BASE_MINUTES.get(max(0, min(5, max_level)), 45)
    return round(base * r.uniform(0.6, 1.4), 1)


def monitor_model_role(max_level: int) -> str:
    """Calm checks use the fast web model; level 3+ uses the deeper one."""
    return "briefing" if max_level >= 3 else "briefing_calm"


# ── What the market said afterwards ───────────────────────────────────────

def confirmed_level(abs_move_pct: float) -> int:
    """The level the market gave an event: BTC's largest move in the 2 hours after it."""
    for threshold, level in ((4.0, 5), (2.0, 4), (1.0, 3), (0.5, 2)):
        if abs_move_pct >= threshold:
            return level
    return 1


def max_abs_move(points: list[tuple[datetime, float]], start: datetime,
                 hours: float = 2.0) -> float | None:
    """Largest % distance from the price at `start` within the window."""
    window = [(t, p) for t, p in points if start <= t <= start + timedelta(hours=hours) and p > 0]
    if len(window) < 2:
        return None
    base = window[0][1]
    return max(abs(p - base) / base * 100 for _, p in window)


# ── Shadow books ──────────────────────────────────────────────────────────

ROUND_TRIP_COST = 0.00118        # taker both ways incl. GST, as a fraction of notional
NORMAL_NOTIONAL = 0.5            # a normal trade's notional as a fraction of wallet
SETTLE_MINUTES = {4: 15, 5: 30}


@dataclass
class BookResult:
    book: str
    symbol: str
    side: str = ""
    entry: float = 0.0
    exit: float = 0.0
    wallet_pct: float = 0.0      # P&L as % of wallet, after costs
    note: str = ""


def _trail(path: list[tuple[datetime, float]], side: str, entry: float, stop: float,
           trail_pct: float, until: datetime) -> tuple[float, str]:
    """Walk prices, ratcheting a trailing stop. Returns (exit price, reason)."""
    best = entry
    for t, p in path:
        if t > until:
            return p, "time"
        if side == "long":
            best = max(best, p)
            stop = max(stop, best * (1 - trail_pct))
            if p <= stop:
                return stop, "stop"
        else:
            best = min(best, p)
            stop = min(stop, best * (1 + trail_pct))
            if p >= stop:
                return stop, "stop"
    return (path[-1][1] if path else entry), "end"


def _pnl(side: str, entry: float, exit_: float, notional: float) -> float:
    move = (exit_ - entry) / entry if side == "long" else (entry - exit_) / entry
    return round((move - ROUND_TRIP_COST) * notional * 100, 3)


def simulate_books(event_at: datetime, level: int,
                   paths: dict[str, list[tuple[datetime, float]]],
                   hold_hours: float = 6.0) -> list[BookResult]:
    """All four books for one event. `paths` maps symbol -> (time, price), sorted."""
    until = event_at + timedelta(hours=hold_hours)
    out = [BookResult("A_pause", "btcusdt", note="no trade")]
    btc = [x for x in paths.get("btcusdt", []) if x[0] >= event_at]
    if len(btc) < 3:
        return out + [BookResult(b, "btcusdt", note="not enough prices")
                      for b in ("B_double_at_release", "C_confirmed_breakout", "D_basket")]

    # B: first move after release decides the side; 2x size; 0.3% trail.
    first = next(((t, p) for t, p in btc if t >= event_at + timedelta(minutes=1)), btc[1])
    side_b = "long" if first[1] >= btc[0][1] else "short"
    entry_b = first[1]
    stop_b = entry_b * (0.997 if side_b == "long" else 1.003)
    rest_b = [x for x in btc if x[0] > first[0]]
    exit_b, why_b = _trail(rest_b, side_b, entry_b, stop_b, 0.003, until)
    out.append(BookResult("B_double_at_release", "btcusdt", side_b, entry_b, exit_b,
                          _pnl(side_b, entry_b, exit_b, 2 * NORMAL_NOTIONAL), why_b))

    # C: wait for the spike to settle, trade the break of its range.
    settle = event_at + timedelta(minutes=SETTLE_MINUTES.get(level, 15))
    rng = [p for t, p in btc if event_at <= t <= settle]
    after = [(t, p) for t, p in btc if t > settle]
    entry_c = None
    if rng and after:
        hi, lo = max(rng), min(rng)
        for t, p in after:
            if t > until:
                break
            if p > hi or p < lo:
                entry_c = (t, p, "long" if p > hi else "short", hi, lo)
                break
    if entry_c is None:
        out.append(BookResult("C_confirmed_breakout", "btcusdt", note="no breakout"))
        out.append(BookResult("D_basket", "btcusdt+ethusdt+solusdt", note="no breakout"))
        return out
    t_c, p_c, side_c, hi, lo = entry_c
    stop_c = lo if side_c == "long" else hi
    risk = abs(p_c - stop_c) / p_c or 0.001
    # Same rupee risk as a normal trade stopped at 1%: notional grows as the stop tightens,
    # capped at 2x normal.
    notional_c = min(2 * NORMAL_NOTIONAL, NORMAL_NOTIONAL * 0.01 / risk)
    rest_c = [x for x in btc if x[0] > t_c]
    exit_c, why_c = _trail(rest_c, side_c, p_c, stop_c, 0.005, until)
    out.append(BookResult("C_confirmed_breakout", "btcusdt", side_c, p_c, exit_c,
                          _pnl(side_c, p_c, exit_c, notional_c), why_c))

    # D: the same entry signal on three correlated coins, normal size each.
    total, notes = 0.0, []
    for sym in ("btcusdt", "ethusdt", "solusdt"):
        path = [x for x in paths.get(sym, []) if x[0] >= t_c]
        if len(path) < 2:
            notes.append(f"{sym}: no prices")
            continue
        e = path[0][1]
        s = e * (1 - risk) if side_c == "long" else e * (1 + risk)
        x, _ = _trail(path[1:], side_c, e, s, 0.005, until)
        total += _pnl(side_c, e, x, NORMAL_NOTIONAL)
        notes.append(f"{sym} {(x - e) / e * 100:+.2f}%")
    out.append(BookResult("D_basket", "btcusdt+ethusdt+solusdt", side_c, 0, 0,
                          round(total, 3), "; ".join(notes)))
    return out
