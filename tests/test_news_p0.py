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


class TestNewsSettingsDefaultOff(unittest.TestCase):
    def test_master_and_collectors_off(self):
        from config.settings import settings

        self.assertFalse(settings.news_engine_enabled)
        self.assertFalse(settings.news_collector_economic_calendar_enabled)
        self.assertFalse(settings.news_paper_stop_reverse_enabled)
