"""Chart Pro APIs and strategy scan."""
from __future__ import annotations

import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp.test_utils import AioHTTPTestCase

from analysis.chart_scan import scan_strategy_markers
from scheduler.chart_page import api_chart_strategies


def _sample_bars(n: int = 120):
    import time

    t0 = int(time.time()) - n * 3600
    p = 100.0
    bars = []
    for i in range(n):
        p *= 1.001 if i % 7 else 0.998
        bars.append(
            {
                "time": t0 + i * 3600,
                "open": p,
                "high": p * 1.002,
                "low": p * 0.998,
                "close": p,
                "volume": 1000 + i,
            }
        )
    return bars


class TestChartScan(unittest.TestCase):
    def test_scan_strategy_markers_returns_list(self):
        markers = scan_strategy_markers(_sample_bars(150), strategy="confluence", step=5, max_markers=10)
        self.assertIsInstance(markers, list)
        for m in markers:
            self.assertIn(m["direction"], ("LONG", "SHORT"))
            self.assertIn("strategy", m)
            self.assertIn("time", m)


class TestChartRoutes(AioHTTPTestCase):
    async def get_application(self):
        from scheduler.health import make_app

        return await make_app(MagicMock())

    async def test_chart_page_loads_pro_script(self):
        resp = await self.client.get("/chart")
        self.assertEqual(resp.status, 200)
        body = await resp.text()
        self.assertIn("chart-pro.js", body)
        self.assertIn("chart-indicators.js", body)
        self.assertIn("lightweight-charts", body)
        self.assertIn('integrity="', body)
        self.assertIn('crossorigin="anonymous"', body)
        self.assertIn("/portal/static/chrome.js", body)
        self.assertIn("intraday detectors, no proven edge", body.lower())

    async def test_chart_strategies_api(self):
        resp = await self.client.get("/api/chart/strategies")
        self.assertEqual(resp.status, 200)
        data = json.loads(await resp.text())
        self.assertTrue(any(s["id"] == "confluence" for s in data))

    async def test_strategy_scan_api(self):
        resp = await self.client.get("/api/chart/strategy-scan?symbol=BTCUSDT&tf=1h&strategy=all&limit=200")
        self.assertEqual(resp.status, 200)
        data = json.loads(await resp.text())
        self.assertIn("markers", data)

    async def test_indicator_catalog_api(self):
        resp = await self.client.get("/api/chart/indicator-catalog")
        self.assertEqual(resp.status, 200)
        data = json.loads(await resp.text())
        self.assertGreaterEqual(data.get("count", 0), 50)

    async def test_parity_doc_api(self):
        resp = await self.client.get("/api/chart/parity")
        self.assertEqual(resp.status, 200)
        data = json.loads(await resp.text())
        self.assertIn("TradingView", data.get("markdown", ""))


class TestChartBarMerge(unittest.TestCase):
    def test_merge_prefers_recent_over_lake_at_same_time(self):
        from scheduler.chart_page import _merge_bars

        lake = [{"time": 100, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}]
        recent = [{"time": 100, "open": 2, "high": 2, "low": 2, "close": 2, "volume": 2}, {"time": 200, "open": 3, "high": 3, "low": 3, "close": 3, "volume": 3}]
        merged = _merge_bars(lake, recent, 10)
        self.assertEqual(merged[0]["close"], 2)
        self.assertEqual(len(merged), 2)

class TestChartDeltaBars(unittest.IsolatedAsyncioTestCase):
    async def test_klines_uses_delta_when_runner_has_it(self):
        from unittest.mock import AsyncMock, MagicMock
        from datetime import datetime, timezone

        from scheduler.chart_page import _load_chart_bars_async

        runner = MagicMock()
        ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
        runner.delta_market.fetch_candles = AsyncMock(
            return_value=[{"timestamp": ts, "open": 1.0, "high": 1.1, "low": 0.9, "close": 1.05, "volume": 10.0}]
        )
        with patch("scheduler.chart_page._lake_bars", return_value=[]):
            bars = await _load_chart_bars_async(runner, "BTCUSDT", "1h", 50)
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0]["close"], 1.05)
        runner.delta_market.fetch_candles.assert_awaited_once()


class TestChartDataIsReal(AioHTTPTestCase):
    """30 Oct 2026 review of the Chart Pro commits: no invented prices, clear errors for bad inputs."""

    async def get_application(self):
        from scheduler.health import make_app

        return await make_app(MagicMock())

    async def test_a_coin_without_data_gets_an_empty_chart_not_random_prices(self):
        resp = await self.client.get("/api/chart/klines?symbol=NOSUCHCOINUSDT&tf=1h&limit=100")
        self.assertEqual(resp.status, 200)
        self.assertEqual(json.loads(await resp.text()), [])
        scan = json.loads(await (await self.client.get("/api/chart/strategy-scan?symbol=NOSUCHCOINUSDT&tf=1h")).text())
        self.assertEqual(scan["markers"], [])

    async def test_bad_numbers_are_a_400_not_a_crash(self):
        for url in ("/api/chart/klines?limit=abc", "/api/chart/strategy-scan?min_confidence=x",
                    "/api/chart/history-signals?days=ten"):
            self.assertEqual((await self.client.get(url)).status, 400, url)
