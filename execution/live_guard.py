"""Process-wide lock on exchange order placement.

Every order, cancel, and modify goes through ``assert_live_trading_allowed``
before any HTTP call. Live trading is allowed only when both process
environment variables are set:

    LIVE_TRADING_ENABLED=true
    LIVE_TRADING_CONFIRM equals CONFIRM_VALUE, exactly

They are read from the process environment, not from the settings database,
so /settings cannot turn them on. Both are absent by default, so live
trading is blocked and the engine stays in paper/research mode.
"""
from __future__ import annotations

import os
from typing import Any

# Second confirmation. Must match LIVE_TRADING_CONFIRM exactly.
CONFIRM_VALUE = "YES_PLACE_REAL_ORDERS"

# Attribute calls of these names are exchange mutations. The scan test
# allows them only in this module. Add a name here when a new order method
# is introduced, and call it only from a wrapper below.
ORDER_METHODS = frozenset({
    "prepare",
    "market",
    "protect",
    "cancel_all",
    "place_market",
    "place_stop_market",
    "place_take_profit_market",
    "cancel_order",
    "edit_stop",
    "set_leverage",
})


class LiveTradingDisabled(RuntimeError):
    """An order path ran without both live-trading flags."""


def live_trading_allowed() -> bool:
    enabled = os.environ.get("LIVE_TRADING_ENABLED", "").strip().lower() == "true"
    confirm = os.environ.get("LIVE_TRADING_CONFIRM", "").strip()
    return bool(enabled and confirm == CONFIRM_VALUE)


def assert_live_trading_allowed() -> None:
    if not live_trading_allowed():
        raise LiveTradingDisabled(
            "live trading is OFF "
            "(LIVE_TRADING_ENABLED and LIVE_TRADING_CONFIRM are required)"
        )


def trading_mode_fields() -> dict[str, str]:
    """Fields for the startup log. Default is paper/research with live OFF."""
    if live_trading_allowed():
        return {"mode": "live", "live": "ON"}
    return {"mode": "paper/research", "live": "OFF"}


async def binance_prepare(client: Any, symbol: str, leverage: int) -> None:
    assert_live_trading_allowed()
    await client.prepare(symbol, leverage)


async def binance_market(
    client: Any,
    symbol: str,
    side: str,
    qty: float,
    reduce_only: bool = False,
    client_id: str | None = None,
) -> Any:
    assert_live_trading_allowed()
    return await client.market(
        symbol, side, qty, reduce_only=reduce_only, client_id=client_id,
    )


async def binance_protect(
    client: Any, symbol: str, close_side: str, kind: str, trigger: float,
) -> Any:
    assert_live_trading_allowed()
    return await client.protect(symbol, close_side, kind, trigger)


async def binance_cancel_all(client: Any, symbol: str) -> None:
    assert_live_trading_allowed()
    await client.cancel_all(symbol)


async def delta_place_market(
    client: Any,
    product_id: int,
    side: str,
    size: int,
    reduce_only: bool = False,
) -> Any:
    assert_live_trading_allowed()
    return await client.place_market(
        product_id, side, size, reduce_only=reduce_only,
    )


async def delta_place_stop_market(
    client: Any, product_id: int, side: str, size: int, stop_price: float,
) -> Any:
    assert_live_trading_allowed()
    return await client.place_stop_market(product_id, side, size, stop_price)


async def delta_place_take_profit_market(
    client: Any, product_id: int, side: str, size: int, stop_price: float,
) -> Any:
    assert_live_trading_allowed()
    return await client.place_take_profit_market(
        product_id, side, size, stop_price,
    )


async def delta_cancel_all(client: Any, product_id: int) -> Any:
    assert_live_trading_allowed()
    return await client.cancel_all(product_id)


async def delta_cancel_order(client: Any, order_id: int, product_id: int) -> Any:
    assert_live_trading_allowed()
    return await client.cancel_order(order_id, product_id)


async def delta_edit_stop(
    client: Any, order_id: int, product_id: int, size: int, stop_price: float,
) -> Any:
    assert_live_trading_allowed()
    return await client.edit_stop(order_id, product_id, size, stop_price)


async def delta_set_leverage(client: Any, product_id: int, leverage: int) -> Any:
    assert_live_trading_allowed()
    return await client.set_leverage(product_id, leverage)
