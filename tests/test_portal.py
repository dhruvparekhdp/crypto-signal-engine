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

    async def test_every_hub_and_page_serves_the_shell(self):
        for path in [f"/{h}" for h in portal.HUBS] + ["/command/signals", "/book/paper", "/evidence/accuracy", "/system/data"]:
            with self.subTest(path=path):
                resp = await self.client.get(path)
                self.assertEqual(resp.status, 200)
                body = await resp.text()
                self.assertIn('id="view"', body)
                self.assertNotIn("{{", body)                     # every placeholder filled
                self.assertRegex(body, r"/portal/static/core\.js\?v=[0-9a-f]{10}")

    async def test_a_page_name_is_a_slug(self):
        resp = await self.client.get("/book/Not_A_Page")
        self.assertEqual(resp.status, 404)

    async def test_retired_data_page_redirects(self):
        resp = await self.client.get("/data", allow_redirects=False)
        self.assertEqual(resp.status, 301)
        self.assertEqual(resp.headers["Location"], "/system/data")

    async def test_the_classic_dashboard_still_serves_its_two_tools(self):
        resp = await self.client.get("/classic")
        self.assertEqual(resp.status, 200)
        body = await resp.text()
        self.assertIn('id="tab-mirror"', body)
        self.assertIn('id="tab-simulator"', body)

    async def test_assets_are_a_fixed_list_with_versioned_caching(self):
        for name, ctype in portal.ASSETS.items():
            with self.subTest(name=name):
                digest = portal._asset(name)[1]
                resp = await self.client.get(f"/portal/static/{name}?v={digest}")
                self.assertEqual(resp.status, 200)
                self.assertTrue(resp.headers["Content-Type"].startswith(ctype))
                self.assertIn("immutable", resp.headers["Cache-Control"])
                stale = await self.client.get(f"/portal/static/{name}?v=old")
                self.assertEqual(stale.headers["Cache-Control"], "no-cache")
        for bad in ("portal.html", "..%2Fportal.py", "nope.js", "hubs/../portal.html"):
            with self.subTest(bad=bad):
                resp = await self.client.get(f"/portal/static/{bad}")
                self.assertEqual(resp.status, 404)

    async def test_every_tool_page_carries_the_portal_header(self):
        """No page is a dead end: each server-rendered tool loads the shared header and skin."""
        for path in ("/predict", "/moves", "/chart", "/audit", "/settings", "/keys", "/api-docs", "/journal", "/v2",
                     "/pipeline", "/classic", "/settings/classic"):
            with self.subTest(path=path):
                resp = await self.client.get(path)
                self.assertEqual(resp.status, 200)
                body = await resp.text()
                self.assertIn("/portal/static/chrome.js", body)
                self.assertIn("/portal/static/nav.js", body)

    async def test_meta_says_paper_or_live(self):
        resp = await self.client.get("/api/portal/meta")
        body = await resp.json()
        for key in ("live_trading_mode", "paper_trading_enabled", "regime_filter", "vol_rank_max"):
            self.assertIn(key, body)


class TestFrontEndWiring(unittest.TestCase):
    """The nav map, the views the hub modules register and the widgets they use must agree:
    a native page without a view, or a view naming a missing widget, renders an empty hub."""

    def setUp(self):
        import re
        self.re = re
        self.nav = (portal.STATIC / "nav.js").read_text()
        self.hubs = {p.stem: p.read_text() for p in (portal.STATIC / "hubs").glob("*.js")}

    def test_every_hub_has_a_module_and_an_asset_entry(self):
        for hub in portal.HUBS:
            with self.subTest(hub=hub):
                self.assertIn(hub, self.hubs)
                self.assertIn(f"hubs/{hub}.js", portal.ASSETS)

    def test_every_native_page_has_a_view(self):
        hub = None
        for line in self.nav.splitlines():
            m = self.re.search(r'\{ id: "(\w+)", label: "[^"]+", tabs:', line)
            if m:
                hub = m.group(1)
                continue
            t = self.re.search(r'\{ id: "([\w-]+)", label: "[^"]+", native: true', line)
            if t:
                with self.subTest(page=f"{hub}/{t.group(1)}"):
                    self.assertIn(f'UI.view("{hub}/{t.group(1)}"', self.hubs[hub])

    def test_every_widget_in_a_view_is_defined(self):
        for hub, src in self.hubs.items():
            defined = set(self.re.findall(r'UI\.widget\("([\w-]+)"', src))
            for view in self.re.findall(r'UI\.view\("[\w/-]+", (\[.*?\]\));', src, self.re.S):
                for name in self.re.findall(r'"([\w-]+)"', view):
                    if name in ("grow", "side", "full"):
                        continue
                    with self.subTest(hub=hub, widget=name):
                        self.assertIn(name, defined)

    def test_store_keys_used_by_widgets_are_defined(self):
        core = (portal.STATIC / "core.js").read_text()
        keys = set(self.re.findall(r'Store\.def\("([\w:]+)"', core))
        for hub, src in self.hubs.items():
            for uses in self.re.findall(r"uses: \[([^\]]*)\]", src):
                for k in self.re.findall(r'"(\w+)"', uses):
                    with self.subTest(hub=hub, key=k):
                        self.assertIn(k, keys)


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
