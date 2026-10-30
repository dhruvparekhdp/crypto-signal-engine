"""P1 collector tests — recorded fixtures only (plan W15)."""
from __future__ import annotations

import os
import unittest

from collectors.news.registry import COLLECTOR_IDS, all_collectors, setting_key_for


class TestNewsCollectorsP1(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        os.environ["NEWS_USE_FIXTURES"] = "1"
        from config import settings as settings_mod

        self._settings = settings_mod.settings
        self._settings.news_engine_enabled = True
        for cid in COLLECTOR_IDS:
            setattr(self._settings, setting_key_for(cid), True)
        self._settings.fmp_api_key = type("S", (), {"get_secret_value": lambda self: "test-fmp"})()
        self._settings.fred_api_key = type("S", (), {"get_secret_value": lambda self: "test-fred"})()

    async def asyncTearDown(self):
        os.environ.pop("NEWS_USE_FIXTURES", None)
        self._settings.news_engine_enabled = False

    async def test_each_p1_collector_polls_fixtures(self):
        collectors = all_collectors()
        self.assertEqual(len(collectors), len(COLLECTOR_IDS))
        for c in collectors:
            with self.subTest(collector=c.collector_id):
                res = await c.poll()
                self.assertGreater(res.items, 0, res.message)
                self.assertEqual(c.health.status, "ok")

    async def test_missing_fmp_key_is_not_configured(self):
        from collectors.news.sources import EconomicCalendarCollector

        self._settings.fmp_api_key = None
        c = EconomicCalendarCollector()
        res = await c.poll()
        self.assertEqual(res.items, 0)
        self.assertEqual(c.health.status, "not_configured")
