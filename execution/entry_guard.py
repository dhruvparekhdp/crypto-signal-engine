"""
Spread and depth guard for a live market entry (owner, 19 Oct 2026: "go ahead with ... spread guard"; the idea is
Freqtrade's SpreadFilter / PrecisionFilter, applied at the moment of the order rather than to a pair list).

Before a real-money market order, Delta India's order book is read and the order is refused when:
  - the book cannot be read or one side is empty (fail closed: no order into a book we cannot see);
  - the bid-ask spread is wider than max_spread_pct (19 Oct: 0.00-0.09% on the 12 live coins; 0.5% is abnormal);
  - the visible book cannot fill our size;
  - walking the book for our size would cost more than max_slip_r of the trade's risk (fill vs the mid price, as a
    share of the stop distance - a 0.3% slip is nothing on a 6% swing stop and a lot on a 0.5% one).
The paper book runs the same check for the live coins when it copies live, so a trade live refuses is one paper
refuses too.
"""
from __future__ import annotations

from dataclasses import dataclass

MAX_SPREAD_PCT = 0.5
MAX_SLIP_R = 0.10


@dataclass(frozen=True)
class GuardResult:
    reason: str = ""                    # "" = the order may go
    spread_pct: float | None = None
    slip_pct: float | None = None
    slip_r: float | None = None
    fill_price: float | None = None

    @property
    def ok(self) -> bool:
        return not self.reason


def _levels(rows) -> list[tuple[float, float]]:
    out = []
    for r in rows or []:
        try:
            out.append((float(r["price"]), float(r["size"])))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def check(book: dict | None, side: str, contracts: float, stop_frac: float,
          max_spread_pct: float = MAX_SPREAD_PCT, max_slip_r: float = MAX_SLIP_R) -> GuardResult:
    """book: Delta's /v2/l2orderbook result ({'buy': [...], 'sell': [...]}, best first, sizes in contracts).
    side: 'buy' or 'sell' for the ENTRY order (a buy walks the asks)."""
    bids = sorted(_levels((book or {}).get("buy")), key=lambda x: -x[0])
    asks = sorted(_levels((book or {}).get("sell")), key=lambda x: x[0])
    if not bids or not asks:
        return GuardResult("orderbook_empty")
    bid, ask = bids[0][0], asks[0][0]
    if bid <= 0 or ask <= 0 or ask < bid:
        return GuardResult("orderbook_crossed")
    mid = (bid + ask) / 2
    spread = (ask - bid) / mid * 100
    if spread > max_spread_pct:
        return GuardResult("spread_too_wide", spread_pct=spread)
    need, cost, got = float(contracts), 0.0, 0.0
    for px, size in (asks if side == "buy" else bids):
        take = min(size, need - got)
        cost += take * px
        got += take
        if got >= need - 1e-12:
            break
    if got < need - 1e-12 or got <= 0:
        return GuardResult("book_too_thin", spread_pct=spread)
    fill = cost / got
    slip = abs(fill - mid) / mid * 100
    slip_r = (slip / 100) / stop_frac if stop_frac > 0 else float("inf")
    if slip_r > max_slip_r:
        return GuardResult("slippage_too_high", spread, slip, slip_r, fill)
    return GuardResult("", spread, slip, slip_r, fill)
