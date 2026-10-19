"""Book > Live (18 Oct 2026): the real-money page joins our record with Delta's own position and orders, flags any
mismatch or a position without a stop, and is admin-only."""
import pytest

from scheduler.live_api import live_rows

REC = {"XRPUSD": {"side": "sell", "size": 2, "entry": 1.30, "entry_fill": "1.3010", "stop": 1.3765, "init_stop": 1.3765,
                  "risk": 0.0765, "product_id": 93723, "leverage": 8, "binance_symbol": "xrpusdt",
                  "signal_type": "swing_donchian", "opened_ms": 1}}
EX = [{"product_symbol": "XRPUSD", "product_id": 93723, "size": -2, "entry_price": "1.3010", "mark_price": "1.2700",
       "liquidation_price": "1.4580", "margin": "0.33", "unrealized_pnl": "0.062"}]
ORD = [{"product_id": 93723, "stop_order_type": "stop_loss_order", "stop_price": "1.3765"},
       {"product_id": 93723, "stop_order_type": "take_profit_order", "stop_price": "1.0715"}]


def test_position_row_has_real_pnl_stop_target_and_liquidation():
    r = live_rows(REC, EX, ORD, 85.0, 500.0)[0]
    assert r["side"] == "short" and r["contracts"] == 2 and r["leverage"] == 8
    assert r["upnl_inr"] == pytest.approx(0.062 * 85, abs=0.01) and r["upnl_pct_wallet"] == pytest.approx(1.05, abs=0.01)
    assert r["exchange_stop"] == 1.3765 and r["exchange_target"] == 1.0715 and r["liquidation"] == 1.458
    assert r["r_now"] == pytest.approx((1.3010 - 1.27) / 0.0755, abs=0.01) and r["flags"] == []


def test_missing_stop_and_unknown_positions_are_flagged_not_hidden():
    r = live_rows(REC, EX, [ORD[1]], 85.0, 500.0)[0]
    assert "NO STOP ON EXCHANGE" in r["flags"]
    rows = live_rows({}, EX, ORD, 85.0, 500.0)
    assert rows and "on Delta but not in our records" in rows[0]["flags"]
    rows = live_rows(REC, [], [], 85.0, 500.0)
    assert "in our records but flat on Delta" in rows[0]["flags"]


@pytest.mark.asyncio
async def test_live_endpoint_needs_admin(aiohttp_client=None):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from scheduler.live_api import register
    app = web.Application()
    register(app, None)
    async with TestClient(TestServer(app)) as client:
        r = await client.get("/api/delta/live")
        assert r.status == 401


@pytest.mark.asyncio
async def test_portal_meta_reports_the_delta_live_book(monkeypatch):
    # 18 Oct 2026: the top bar said "LIVE TRADING: OFF" while real Delta orders were ON (it read the Binance mode)
    import json

    from config.settings import settings
    from scheduler.portal import api_meta
    monkeypatch.setattr(settings, "delta_india_mode", "live")
    monkeypatch.setattr(settings, "delta_india_live_orders", True)
    monkeypatch.setattr(settings, "delta_india_risk_pct", 0.03)
    d = json.loads((await api_meta(None)).text)
    assert d["delta_live"] is True and d["delta_mode"] == "live" and d["delta_risk_pct"] == 0.03
    monkeypatch.setattr(settings, "delta_india_live_orders", False)
    assert json.loads((await api_meta(None)).text)["delta_live"] is False


# 19 Oct 2026, owner: "real value of delta are different on this page". Delta's positions feed carries an
# unrealized_pnl that is not marked to the current price (it read -0.1094 while UPL@Mark in the app was -0.108);
# the app's P&L, ROE and available margin all use the mark price.
STALE = [{"product_symbol": "XRPUSD", "product_id": 93723, "size": -2, "entry_price": "1.263", "mark_price": "1.3170",
          "margin": "0.28066667", "unrealized_pnl": "-0.10940000", "product": {"contract_value": "1"}}]


def test_pnl_is_marked_to_the_mark_price_like_the_delta_app():
    r = live_rows(REC, STALE, ORD, 85.0, 499.79)[0]
    assert r["upnl_inr"] == pytest.approx(-9.18, abs=0.005)            # Delta app: UPL@Mark -Rs9.18
    assert r["roe_pct"] == pytest.approx(-38.48, abs=0.05)             # Delta app: -38.48 %
    assert r["margin_inr"] == pytest.approx(23.86, abs=0.01)


def test_contract_size_falls_back_to_our_table_not_one_coin():
    rec = {"DOGEUSD": {"side": "sell", "size": 1, "binance_symbol": "dogeusdt", "product_id": 7}}
    ex = [{"product_symbol": "DOGEUSD", "product_id": 7, "size": -1, "entry_price": "0.20", "mark_price": "0.21",
           "margin": "3"}]
    assert live_rows(rec, ex, [], 85.0, 500.0)[0]["upnl_inr"] == pytest.approx(-0.01 * 100 * 85)  # 100 DOGE a contract


def test_r_is_measured_from_the_real_fill():
    rec = {"XRPUSD": dict(REC["XRPUSD"], entry=1.2765, risk=0.10)}    # signal price 1.2765, but filled 1.3010
    r = live_rows(rec, EX, ORD, 85.0, 500.0)[0]                       # stop 1.3765: real 1R = 0.0755, not 0.10
    assert r["r_now"] == pytest.approx((1.3010 - 1.27) / 0.0755, abs=0.006)


def test_wallet_counts_open_losses_like_the_delta_app():
    from scheduler.live_api import wallet_summary
    w = wallet_summary(total_inr=499.79, available_inr=442.59, rows=[{"upnl_inr": -9.18}, {"upnl_inr": -8.46}], rate=85.0)
    assert w["upnl_inr"] == pytest.approx(-17.64) and w["equity_inr"] == pytest.approx(482.15)
    assert w["free_inr"] == pytest.approx(424.95)                      # owner read ~Rs426 available in the app
    up = wallet_summary(total_inr=500.0, available_inr=440.0, rows=[{"upnl_inr": 12.0}], rate=85.0)
    assert up["free_inr"] == 440.0 and up["equity_inr"] == 512.0       # an open profit is not counted as free money
