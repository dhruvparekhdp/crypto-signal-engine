"""
Per-market trading specifications, measured from real CoinDCX transactions.

Why per-market
--------------
The cost model used to carry one global fee and one global maintenance margin.
Two account screenshots from 18 Aug 2026 show that is wrong, and wrong by a
large factor:

    ETH/USDT close  fee Rs6.36 on Rs10,776 notional  -> 0.0590%  (0.05% + GST)
    XAU/USDT close  fee Rs1.73 on Rs14,688 notional  -> 0.0118%  (0.01% + GST)

Gold is five times cheaper to trade than ether. Applied to a scalp that is not
a rounding difference — it moves the minimum viable target from 0.336% to
0.071%, which is the difference between a setup being refused and being taken.

Maintenance margin differs too, back-solved from the quoted liquidation prices:

    ETH long 20x  entry 1906.50  liq 1820.97  -> 0.538%
    XAU long 25x  entry 4365.91  liq 4234.93  -> 1.031%

Caveat worth stating plainly: each figure comes from ONE position at ONE
leverage. Exchanges usually tier maintenance margin by position size, so these
are correct for positions of this size and should be re-derived if you start
trading much larger. They are still far better than the 1.5% rate card, which
matched neither observation.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

GST_PCT = 0.18


@dataclass(frozen=True)
class InstrumentSpec:
    """What one market costs and how finely it can be quoted."""

    symbol: str
    taker_pct: float                 # base brokerage, before GST
    maintenance_margin_pct: float
    tick: float                      # 0 = derive from price magnitude
    lot_step: float
    funding_rate_per_8h: float = 0.0000655
    kind: str = "crypto"
    maker_pct: float = 0.0002        # limit orders; capped at taker_pct when used

    @property
    def effective_taker_pct(self) -> float:
        """What actually leaves the account. GST is not optional."""
        return self.taker_pct * (1 + GST_PCT)

    @property
    def round_trip_pct(self) -> float:
        return 2 * self.effective_taker_pct


# Measured where we have a transaction to measure from; the crypto defaults
# otherwise. Commodities on CoinDCX INR futures sit in a cheaper bracket.
_CRYPTO_TAKER = 0.0005
_CRYPTO_MM = 0.0053
_COMMODITY_TAKER = 0.0001
_COMMODITY_MM = 0.0103

_SPECS: dict[str, InstrumentSpec] = {
    "ETH": InstrumentSpec("ETH", _CRYPTO_TAKER, _CRYPTO_MM, 0.01, 0.001),
    "BTC": InstrumentSpec("BTC", _CRYPTO_TAKER, _CRYPTO_MM, 0.1, 0.0001),
    "XRP": InstrumentSpec("XRP", _CRYPTO_TAKER, _CRYPTO_MM, 0.0001, 1.0),
    "SOL": InstrumentSpec("SOL", _CRYPTO_TAKER, _CRYPTO_MM, 0.01, 0.01),
    "LTC": InstrumentSpec("LTC", _CRYPTO_TAKER, _CRYPTO_MM, 0.01, 0.01),
    "BCH": InstrumentSpec("BCH", _CRYPTO_TAKER, _CRYPTO_MM, 0.01, 0.001),
    # Commodities — measured from the XAU close and the open XAU position.
    "XAU": InstrumentSpec("XAU", _COMMODITY_TAKER, _COMMODITY_MM, 0.01, 0.001,
                          kind="commodity"),
    "XAG": InstrumentSpec("XAG", _COMMODITY_TAKER, _COMMODITY_MM, 0.001, 0.01,
                          kind="commodity"),
}

DEFAULT_SPEC = InstrumentSpec("", _CRYPTO_TAKER, _CRYPTO_MM, 0.0, 0.0)


def base_asset(symbol: str) -> str:
    """"ethusdt" -> "ETH". One definition, rather than the five that had grown."""
    s = symbol.upper()
    for quote in ("USDT", "USDC", "BUSD", "INR"):
        if s.endswith(quote):
            return s[: -len(quote)]
    return s


def spec_for(symbol: str) -> InstrumentSpec:
    """
    Look up a market, falling back to crypto rates for anything unknown.

    The fallback is deliberately the EXPENSIVE side. Assuming a cheap fee for
    an unknown market would let through trades that cannot pay for themselves;
    assuming an expensive one only costs us a setup we were unsure about.
    """
    return _SPECS.get(base_asset(symbol), DEFAULT_SPEC)


def tick_for(symbol: str, price: float) -> float:
    """
    The instrument's quoted tick, or a magnitude-derived guess.

    The guess assumes five significant figures, which is right often enough to
    be a safe default and wrong often enough that measured values belong in
    the table above — ETH and XAU both quote to 0.01, where the rule would say
    0.1 and refuse setups that are perfectly tradeable.
    """
    spec = spec_for(symbol)
    if spec.tick > 0:
        return spec.tick
    if price <= 0:
        return 0.0
    return 10.0 ** (math.floor(math.log10(price)) - 4)
