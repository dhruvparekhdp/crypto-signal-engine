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
    assert r["r_now"] == pytest.approx((1.3010 - 1.27) / 0.0765, abs=0.01) and r["flags"] == []


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
