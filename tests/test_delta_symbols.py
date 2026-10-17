"""Watchlist search on Delta India (owner, 18 Oct 2026: "why still binance in watchlist... i am not able to add gold")."""
from unittest.mock import AsyncMock, patch

import pytest

from collectors import delta_symbols as D

ROWS = [
    {"symbol": "XAUTUSD", "contract_type": "perpetual_futures", "mark_price": "4156.08", "open": 4165.87, "close": 4156.05,
     "turnover_usd": 34_617_684, "underlying_asset_symbol": "XAUT", "description": "Tether Gold Token Perpetual"},
    {"symbol": "PAXGUSD", "contract_type": "perpetual_futures", "mark_price": "4160", "open": 4160, "close": 4160,
     "turnover_usd": 1_000_000, "underlying_asset_symbol": "PAXG", "description": "PAX Gold Perpetual"},
    {"symbol": "SOLUSD", "contract_type": "perpetual_futures", "mark_price": "103.1", "open": 104.5, "close": 103.1,
     "turnover_usd": 90_000_000, "underlying_asset_symbol": "SOL", "description": "Solana Perpetual"},
    {"symbol": "SOLVUSD", "contract_type": "perpetual_futures", "mark_price": "0.03", "open": 0.03, "close": 0.03,
     "turnover_usd": 20_000, "underlying_asset_symbol": "SOLV", "description": "Solv Perpetual"},
    {"symbol": "C-BTC-90000-181026", "contract_type": "call_options", "mark_price": "10", "open": 1, "close": 1},
]


def test_tickers_become_internal_pairs_with_delta_prices():
    p = D.parse_tickers(ROWS)
    assert set(p) == {"XAUT", "PAXG", "SOL", "SOLV"}                  # options are not pairs
    assert p["XAUT"]["symbol"] == "xautusdt" and p["XAUT"]["delta_symbol"] == "XAUTUSD"
    assert p["XAUT"]["price"] == pytest.approx(4156.08)
    assert p["SOL"]["change_pct"] == pytest.approx((103.1 / 104.5 - 1) * 100, abs=1e-3)


@pytest.mark.parametrize("q", ["gold", "xau", "XAU", "xaut", "tether gold"])
def test_every_way_of_typing_gold_finds_xaut_first(q):
    assert D.rank(q, D.parse_tickers(ROWS))[0] == "XAUT"


def test_an_exact_ticker_beats_a_longer_one():
    assert D.rank("sol", D.parse_tickers(ROWS))[:2] == ["SOL", "SOLV"]


@pytest.mark.asyncio
async def test_search_marks_what_is_already_on_the_watchlist():
    with patch.object(D, "load", new=AsyncMock(return_value=D.parse_tickers(ROWS))):
        out = await D.search("gold", {"xautusdt"})
    assert out["available"] and out["venue"] == "Delta India"
    top = out["results"][0]
    assert (top["symbol"], top["on_watchlist"], top["pair"]) == ("xautusdt", True, "XAUT/USD")


@pytest.mark.asyncio
async def test_delta_unreachable_says_so_instead_of_nothing_listed():
    with patch.object(D, "load", new=AsyncMock(return_value=None)):
        out = await D.search("gold", set())
    assert out["available"] is False and out["results"] == []
