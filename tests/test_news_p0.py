"""P0 foundation tests (plan W14, W15)."""
from __future__ import annotations

import unittest

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from analysis.news.event_types import load_event_types
from analysis.news.severity import classify_severity
from analysis.news.seed import seed_institutions
from storage.database import Base
from storage.news_models import NewsInstitution


class TestNewsConfig(unittest.TestCase):
    def test_w4_1_has_twenty_countries(self):
        from analysis.news.countries import load_countries

        self.assertEqual(len(load_countries()), 20)

    def test_w5_event_types_and_severity(self):
        types = load_event_types()
        self.assertIn("central_bank", types)
        self.assertGreaterEqual(classify_severity("FOMC rate decision", "central_bank"), 4)
        self.assertEqual(classify_severity("Local weather note", "other"), 1)

    def test_w4_3_institutions_seed_count(self):
        from analysis.news.institutions import load_institutions

        self.assertGreaterEqual(len(load_institutions()), 15)


class TestNewsTables(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_create_all_includes_news_institutions(self):
        async with self.Session() as session:
            from sqlalchemy import select

            n = await seed_institutions(session)
            self.assertGreater(n, 0)
            again = await seed_institutions(session)
            self.assertEqual(again, 0)
            rows = (await session.scalars(select(NewsInstitution))).all()
            self.assertGreater(len(rows), 10)


class TestNewsSettingsDefaults(unittest.TestCase):
    def test_keyless_parts_on_keyed_and_paper_parts_off(self):
        # owner 31 Oct 2026: "turn on for now and let's review on 2nd nov"
        from config.settings import Settings

        f = Settings.model_fields
        for k in ("news_engine_enabled", "news_portal_widgets_enabled", "news_event_resolver_enabled",
                  "news_collector_central_bank_rss_enabled", "news_collector_regional_rss_enabled"):
            self.assertTrue(f[k].default, k)
        for k in ("news_collector_economic_calendar_enabled", "news_collector_us_treasury_fred_enabled",
                  "news_paper_stop_reverse_enabled", "news_paper_fed_reaction_enabled", "news_sentiment_indices_enabled",
                  "news_engine_feeds_monitor"):
            self.assertFalse(f[k].default, k)
