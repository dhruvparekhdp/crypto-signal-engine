"""
Delta-hedged straddles, paper research only (owner, 22 Oct 2026: "in stocks we have delta trading and hedging concept
... implement it in system and paper trade"; chose "sell vs buy volatility"). Never places an order.

Two mirror books on Delta India's BTC and ETH options, each for a daily and a weekly expiry:
  sell   sell the at-the-money call and put (a straddle) at the bid, keep the position's delta near zero with the
         perpetual future. Earns the options' time value; loses when the coin moves more than the options priced in.
  buy    the same straddle bought at the ask, hedged the same way. Its result is roughly the sell book's mirror,
         minus the spread and fees both sides pay.
Which side wins is the question "is implied volatility (what options charge) above the volatility that then happens?"
- the one thing a straddle bets on once its delta is hedged away.

Rules (fixed before any result was seen):
  size     $1,000 of underlying per straddle (settings.vol_notional_usd), fractional units allowed in paper
  expiry   daily: the nearest expiry 18-40 h away; weekly: the nearest 3-10 days away (Delta India settles 12:00 UTC)
  strike   the one closest to the perpetual's price with both legs quoted and the straddle spread under 10% of mid
  hedge    every check (5 min): if the net delta is more than 0.10 per unit of underlying, trade the perpetual back to
           zero (taker fee + GST)
  exit     20 minutes before expiry at the bid / ask; if quotes are gone (or expiry passed), settle at intrinsic value
  fees     options: 0.01% of underlying notional per leg, at most 10% of the leg's premium, + 18% GST (Delta India's
           taker rate on the product); perpetual: 0.05% + GST
All P&L is in USD and as a share of the $1,000 notional. Open positions are marked at Delta's option mark price.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta

GST = 0.18
OPT_FEE = 0.0001              # Delta India options taker_commission_rate (products API, 22 Oct 2026)
OPT_FEE_CAP = 0.10            # of the premium
PERP_FEE = 0.0005
BAND = 0.10                   # re-hedge when |net delta| > 0.10 per unit
MAX_SPREAD = 0.10             # of the straddle mid
CLOSE_BEFORE = timedelta(minutes=20)
TENORS = {"daily": (timedelta(hours=18), timedelta(hours=40)), "weekly": (timedelta(days=3), timedelta(days=10))}
YEAR_S = 365 * 86400


def _f(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def parse_chain(rows: list[dict]) -> dict:
    """Delta /v2/tickers option rows -> {underlying: {expiry iso: {strike: {"C": leg, "P": leg}}}}."""
    out: dict = {}
    for r in rows or []:
        try:
            kind, u, strike, d = str(r["symbol"]).split("-")
            exp = datetime.strptime(d, "%d%m%y").replace(hour=12)
        except (KeyError, ValueError):
            continue
        if kind not in ("C", "P"):
            continue
        q, g = r.get("quotes") or {}, r.get("greeks") or {}
        leg = {"symbol": r["symbol"], "bid": _f(q.get("best_bid")), "ask": _f(q.get("best_ask")),
               "mark": _f(r.get("mark_price")), "iv": _f(q.get("mark_iv")), "delta": _f(g.get("delta"))}
        out.setdefault(u, {}).setdefault(exp.isoformat(), {}).setdefault(float(strike), {})[kind] = leg
    return out


def _quoted(leg) -> bool:
    return bool(leg) and (leg["bid"] or 0) > 0 and (leg["ask"] or 0) >= (leg["bid"] or 0) and leg["delta"] is not None


def tradable(legs) -> bool:
    if not legs or not (_quoted(legs.get("C")) and _quoted(legs.get("P"))):
        return False
    bid, ask = legs["C"]["bid"] + legs["P"]["bid"], legs["C"]["ask"] + legs["P"]["ask"]
    return (ask - bid) / ((ask + bid) / 2) <= MAX_SPREAD


def pick(chain_u: dict, price: float, now: datetime, tenor: str):
    """(expiry iso, strike) of the straddle to open, or None."""
    lo, hi = TENORS[tenor]
    for e in sorted(e for e in chain_u if lo <= datetime.fromisoformat(e) - now <= hi):
        ks = [k for k, legs in chain_u[e].items() if tradable(legs)]
        if ks:
            return e, min(ks, key=lambda k: abs(k - price))
    return None


def _opt_fee(legs: dict, prices: tuple, price: float, q: float) -> float:
    return sum(min(OPT_FEE * price * q, OPT_FEE_CAP * p * q) for p in prices) * (1 + GST)


def _rehedge(pos: dict, legs: dict, price: float, band: float) -> None:
    d = pos["side"] * pos["q"] * (legs["C"]["delta"] + legs["P"]["delta"]) + pos["hedge"]
    if abs(d) > band * pos["q"]:
        pos["cash"] -= -d * price
        pos["hedge"] -= d
        pos["fees"] += abs(d) * price * PERP_FEE * (1 + GST)
        pos["hedges"] += 1
        d = 0.0
    pos["delta_now"] = round(d / pos["q"], 4)


def _mark(pos: dict, price: float, value: float | None) -> None:
    if value is not None:
        pos["value_now"] = value
    pos["opt_pnl"] = round(pos["side"] * (pos.get("value_now", pos["entry_premium"]) - pos["entry_premium"])
                           * pos["q"], 4)
    pos["hedge_pnl"] = round(pos["cash"] + pos["hedge"] * price, 4)
    pos["total_usd"] = round(pos["opt_pnl"] + pos["hedge_pnl"] - pos["fees"], 4)
    pos["total_pct"] = round(100 * pos["total_usd"] / pos["notional"], 4)


def _vol(pos: dict, price: float, now: datetime) -> None:
    pos["sum_r2"] += math.log(price / pos["last_px"]) ** 2
    pos["last_px"] = price
    yrs = (now - datetime.fromisoformat(pos["opened"])).total_seconds() / YEAR_S
    pos["realized_vol"] = round(math.sqrt(pos["sum_r2"] / yrs), 4) if yrs > 0 else None


def open_pos(key: str, u: str, tenor: str, side: int, expiry: str, strike: float, legs: dict, price: float,
             now: datetime, notional: float, band: float = BAND) -> dict:
    q = notional / price
    c, p = legs["C"], legs["P"]
    prices = (c["ask"], p["ask"]) if side > 0 else (c["bid"], p["bid"])
    pos = {"key": key, "underlying": u, "tenor": tenor, "side": side, "expiry": expiry, "strike": strike,
           "legs": [c["symbol"], p["symbol"]], "q": q, "notional": notional, "opened": now.isoformat(),
           "entry_price": price, "entry_premium": sum(prices),
           "entry_mid": (c["bid"] + c["ask"] + p["bid"] + p["ask"]) / 2,
           "entry_iv": round(((c["iv"] or 0) + (p["iv"] or 0)) / 2, 4), "iv_now": None,
           "hedge": 0.0, "cash": 0.0, "fees": _opt_fee(legs, prices, price, q), "hedges": 0,
           "sum_r2": 0.0, "last_px": price, "realized_vol": None, "status": "open"}
    _rehedge(pos, legs, price, band)
    _mark(pos, price, (c["mark"] or 0) + (p["mark"] or 0))
    return pos


def step(pos: dict, legs: dict | None, price: float, now: datetime, band: float = BAND) -> dict:
    """One check: realized volatility, mark at Delta's option mark price, re-hedge when the delta has drifted."""
    _vol(pos, price, now)
    if legs and legs.get("C") and legs.get("P") and legs["C"]["delta"] is not None and legs["P"]["delta"] is not None:
        if legs["C"]["mark"] is not None and legs["P"]["mark"] is not None:
            pos["iv_now"] = round(((legs["C"]["iv"] or 0) + (legs["P"]["iv"] or 0)) / 2, 4)
            _mark(pos, price, legs["C"]["mark"] + legs["P"]["mark"])
        _rehedge(pos, legs, price, band)
    _mark(pos, price, None)
    return pos


def close(pos: dict, legs: dict | None, price: float, now: datetime) -> dict:
    """Exit at the bid (bought straddle) / ask (sold straddle), or settle at intrinsic when there are no quotes."""
    _vol(pos, price, now)
    if legs is not None and tradable(legs):
        c, p = legs["C"], legs["P"]
        prices = (c["bid"], p["bid"]) if pos["side"] > 0 else (c["ask"], p["ask"])
        value, mid = sum(prices), (c["bid"] + c["ask"] + p["bid"] + p["ask"]) / 2
        pos["fees"] += _opt_fee(legs, prices, price, pos["q"])
        pos["exit"] = "before expiry"
    else:
        value = mid = abs(price - pos["strike"])
        pos["exit"] = "settled"
    pos["value_now"] = value
    pos["cash"] += pos["hedge"] * price
    pos["fees"] += abs(pos["hedge"]) * price * PERP_FEE * (1 + GST)
    pos["hedge"] = 0.0
    pos["spread_paid"] = round(pos["q"] * (abs(pos["entry_premium"] - pos["entry_mid"]) + abs(value - mid)), 4)
    _mark(pos, price, None)
    pos.update(status="closed", closed=now.isoformat(), exit_price=price,
               move_pct=round(100 * (price / pos["entry_price"] - 1), 3))
    return pos


def summary(closed: list[dict]) -> dict:
    def stats(rows):
        if not rows:
            return None
        pct = [r["total_pct"] for r in rows]
        rv = [r["realized_vol"] for r in rows if r.get("realized_vol")]
        return {"n": len(rows), "avg_pct": round(sum(pct) / len(pct), 3), "win_pct": round(100 * sum(x > 0 for x in pct)
                / len(pct), 1), "total_usd": round(sum(r["total_usd"] for r in rows), 2),
                "avg_entry_iv": round(100 * sum(r["entry_iv"] for r in rows) / len(rows), 1),
                "avg_realized_vol": round(100 * sum(rv) / len(rv), 1) if rv else None,
                "avg_hedges": round(sum(r["hedges"] for r in rows) / len(rows), 1),
                "avg_fees_usd": round(sum(r["fees"] for r in rows) / len(rows), 2)}
    side = {1: "buy", -1: "sell"}
    groups: dict[str, list] = {"all": list(closed)}
    for r in closed:
        for k in (f"side:{side[r['side']]}", f"side_tenor:{side[r['side']]}|{r['tenor']}",
                  f"book:{r['underlying']}|{r['tenor']}|{side[r['side']]}"):
            groups.setdefault(k, []).append(r)
    return {k: stats(v) for k, v in groups.items()}
