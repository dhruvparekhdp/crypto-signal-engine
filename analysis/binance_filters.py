"""
Binance USD-M order rules for the watchlist: the smallest order the exchange accepts, and how finely a quantity
can be stated. Paper trading follows them so a paper trade is one Binance would actually fill.

Fetched from GET https://fapi.binance.com/fapi/v1/exchangeInfo on 7 Oct 2026 (MARKET_LOT_SIZE stepSize,
LOT_SIZE minQty, MIN_NOTIONAL notional, PRICE_FILTER tickSize). Refresh with `python -m scripts.binance_filters`.
Unknown symbols get None: callers fall back to their old rules rather than guessing.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class OrderRules:
    step: float           # quantity must be a multiple of this
    min_qty: float
    min_notional: float   # USDT
    tick: float


# symbol: (step, min_qty, min_notional, tick)
_RAW = {
    "BTCUSDT": (0.001, 0.001, 50, 0.10),
    "ETHUSDT": (0.001, 0.001, 20, 0.01),
    "BCHUSDT": (0.001, 0.001, 20, 0.01),
    "LTCUSDT": (0.001, 0.001, 20, 0.01),
    "LINKUSDT": (0.01, 0.01, 20, 0.001),
    "XRPUSDT": (0.1, 0.1, 5, 0.0001),
    "ADAUSDT": (1, 1, 5, 0.0001),
    "BNBUSDT": (0.01, 0.01, 5, 0.01),
    "DOGEUSDT": (1, 1, 5, 0.00001),
    "SOLUSDT": (0.01, 0.01, 5, 0.01),
    "AVAXUSDT": (1, 1, 5, 0.001),
    "SUIUSDT": (0.1, 0.1, 5, 0.0001),
}
RULES = {k: OrderRules(*v) for k, v in _RAW.items()}


def rules_for(symbol: str) -> OrderRules | None:
    return RULES.get(symbol.upper())


def round_qty(symbol: str, qty: float) -> float:
    """Round a quantity DOWN to the exchange step (never order more than was sized)."""
    r = rules_for(symbol)
    if r is None or qty <= 0:
        return max(qty, 0.0)
    q = math.floor(qty / r.step + 1e-9) * r.step
    return round(q, 10)


def check_order(symbol: str, qty: float, price: float) -> str | None:
    """None if Binance would accept this order, else the skip reason."""
    r = rules_for(symbol)
    if r is None:
        return None
    if qty < r.min_qty - 1e-12:
        return "below_one_lot"
    if qty * price < r.min_notional - 1e-9:
        return "below_min_notional"
    return None
