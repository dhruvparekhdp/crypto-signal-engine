"""The portal shell (scheduler/portal.py): routes, redirects, the asset whitelist and its three APIs."""

from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import numpy as np
from aiohttp.test_utils import AioHTTPTestCase, make_mocked_request

from scheduler import portal


class FakeStore:
    async def get_all(self):
        return []

    async def get_symbols(self):
        return []

    async def count(self):
        return 0


class FakeRunner:
    def __init__(self):
        self.crypto_store = FakeStore()
        self.collector_enabled = {}

    def get_status(self):
        return {}


class TestPortalRoutes(AioHTTPTestCase):
    async def get_application(self):
        from config.settings import settings
        from scheduler.health import make_app
        settings.api_rate_limit_requests = 1000
        settings.api_rate_limit_window_seconds = 60
        return await make_app(FakeRunner())

    async def test_root_redirects_to_command(self):
        resp = await self.client.get("/", allow_redirects=False)
        self.assertEqual(resp.status, 302)
        self.assertEqual(resp.headers["Location"], "/command")

    async def test_every_hub_serves_the_shell(self):
        for hub in portal.HUBS:
            with self.subTest(hub=hub):
                resp = await self.client.get(f"/{hub}")
                self.assertEqual(resp.status, 200)
                body = await resp.text()
                self.assertIn('data-view="command"', body)
                self.assertIn("/portal/static/portal.js", body)

    async def test_the_classic_dashboard_moved_not_vanished(self):
        resp = await self.client.get("/classic")
        self.assertEqual(resp.status, 200)
        self.assertIn('id="tab-paper"', await resp.text())

    async def test_assets_are_a_fixed_list(self):
        for name, ctype in portal.ASSETS.items():
            resp = await self.client.get(f"/portal/static/{name}")
            self.assertEqual(resp.status, 200)
            self.assertTrue(resp.headers["Content-Type"].startswith(ctype))
        for bad in ("portal.html", "..%2Fportal.py", "nope.js"):
            with self.subTest(bad=bad):
                resp = await self.client.get(f"/portal/static/{bad}")
                self.assertEqual(resp.status, 404)

    async def test_meta_says_paper_or_live(self):
        resp = await self.client.get("/api/portal/meta")
        body = await resp.json()
        for key in ("live_trading_mode", "paper_trading_enabled", "regime_filter", "vol_rank_max"):
            self.assertIn(key, body)


class TestCandles(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        portal._candles.clear()

    async def test_bad_input_is_400(self):
        for q in ("symbol=BTC-USDT", "tf=3m"):
            resp = await portal.api_candles(make_mocked_request("GET", f"/api/portal/candles?{q}"))
            self.assertEqual(resp.status, 400)

    async def test_bars_are_cached_and_trimmed(self):
        n = 160
        bars = SimpleNamespace(t=np.arange(n) * 1000, o=np.ones(n), h=np.ones(n) * 2, l=np.zeros(n), c=np.ones(n) * 1.5)
        fetch = AsyncMock(return_value=bars)
        with patch("analysis.swing_book.fetch_bars", fetch):
            r1 = await portal.api_candles(make_mocked_request("GET", "/api/portal/candles?symbol=solusdt&tf=4h&limit=20"))
            r2 = await portal.api_candles(make_mocked_request("GET", "/api/portal/candles?symbol=SOLUSDT&tf=4h&limit=30"))
        self.assertEqual(fetch.await_count, 1)                       # second call served from the cache
        self.assertEqual(len(json.loads(r1.text)), 20)
        last = json.loads(r2.text)[-1]
        self.assertEqual(last, {"t": (n - 1) * 1000, "o": 1.0, "h": 2.0, "l": 0.0, "c": 1.5})

    async def test_exchange_failure_is_502_not_a_crash(self):
        with patch("analysis.swing_book.fetch_bars", AsyncMock(side_effect=RuntimeError("boom"))):
            resp = await portal.api_candles(make_mocked_request("GET", "/api/portal/candles?symbol=ETHUSDT"))
        self.assertEqual(resp.status, 502)


class TestSignals(unittest.IsolatedAsyncioTestCase):
    def _row(self, i, skip_reason="", tag=""):
        return SimpleNamespace(
            id=i, timestamp=datetime(2026, 10, 7, 8, 0), symbol="solusdt", signal_type="swing_donchian",
            direction="long", timeframe="4h", current_price=150.0, stop_loss=144.0, target_price=168.0,
            outcome="pending", pnl_pct=0.0, skip_reason=skip_reason, indicators_summary=tag)

    async def test_status_and_filter_tag(self):
        rows = [
            self._row(1),
            self._row(2, "max_concurrent"),
            self._row(3, "regime_skip", "x | regime: vol_rank=0.81 adx=na verdict=skip(vol)"),
        ]
        with patch("storage.repository.Repository.swing_signals_since", AsyncMock(return_value=rows)):
            resp = await portal.api_signals(make_mocked_request("GET", "/api/portal/signals"))
        sigs = json.loads(resp.text)["signals"]
        self.assertEqual([s["status"] for s in sigs], ["TRADED", "NOT TRADED", "FILTER BLOCK"])
        self.assertEqual(sigs[1]["skip_reason_text"], "max open trades reached")
        self.assertEqual(sigs[2]["btc_vol_rank"], 0.81)
        self.assertTrue(sigs[2]["filter_skip"])
        self.assertEqual(sigs[0]["symbol"], "SOLUSDT")
        self.assertTrue(sigs[0]["time"].endswith("+00:00"))
        self.assertEqual(datetime.fromisoformat(sigs[0]["time"]).tzinfo, UTC)

    async def test_database_error_still_answers(self):
        with patch("storage.repository.Repository.swing_signals_since", AsyncMock(side_effect=RuntimeError("db down"))):
            resp = await portal.api_signals(make_mocked_request("GET", "/api/portal/signals"))
        body = json.loads(resp.text)
        self.assertEqual(body["signals"], [])
        self.assertIn("db down", body["error"])


if __name__ == "__main__":
    unittest.main()
