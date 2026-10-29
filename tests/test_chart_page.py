"""Chart Pro APIs and strategy scan."""
from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock

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

        return make_app(MagicMock())

    async def test_chart_page_loads_pro_script(self):
        resp = await self.client.get("/chart")
        self.assertEqual(resp.status, 200)
        body = await resp.text()
        self.assertIn("chart-pro.js", body)
        self.assertIn("lightweight-charts", body)
        self.assertIn("/portal/static/chrome.js", body)

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
