"""PR #45 pre-merge fixes: boundaries, sanitize, pipeline, impulse window, fed direction."""
from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from aiohttp.test_utils import AioHTTPTestCase
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from analysis.news.classifier import classify_rule_based
from analysis.news.impulse_link import events_open_at
from analysis.news.paper_strategies import fed_reaction_side
from analysis.news.sanitize import sanitize_error_message
from collectors.news.registry import setting_key_for
from config import settings as settings_mod
from storage.database import Base


class TestWordBoundaries(unittest.TestCase):
    def test_warsh_fed_cut_is_central_bank_not_war(self):
        c = classify_rule_based("Warsh says Fed will cut rates")
        self.assertEqual(c["event_type"], "central_bank")
        self.assertLess(c["severity"], 5)

    def test_binance_hack_no_india_in(self):
        c = classify_rule_based("Binance hack drains bridge")
        self.assertNotIn("IN", c["countries"])

    def test_software_award_is_other_severity_one(self):
        c = classify_rule_based("Software award for bonus scheme")
        self.assertEqual(c["event_type"], "other")
        self.assertEqual(c["severity"], 1)


class TestSanitizeErrors(unittest.TestCase):
    def test_api_key_stripped_from_error(self):
        msg = "HTTP 403 for https://api.example.com/v3/calendar?apikey=SECRET123&fmt=json"
        out = sanitize_error_message(msg)
        self.assertNotIn("SECRET123", out)
        self.assertIn("?…", out)


class TestImpulseWindow(unittest.TestCase):
    def test_open_events_within_sixty_minutes_only(self):
        at = datetime(2026, 10, 30, 12, 0)
        evs = [
            {"id": 1, "status": "open", "started_at": at.isoformat()},
            {"id": 2, "status": "open", "started_at": (at - timedelta(minutes=61)).isoformat()},
        ]
        self.assertEqual(events_open_at(evs, at, window_min=60), [1])


class TestFedReactionSide(unittest.TestCase):
    def test_dovish_and_hawkish(self):
        self.assertEqual(fed_reaction_side("central_bank", 4, title="Fed signals rate cut"), 1)
        self.assertEqual(fed_reaction_side("central_bank", 4, title="Fed hawkish on inflation"), -1)


class TestNewsPipelineIntegration(AioHTTPTestCase):
    async def get_application(self):
        from scheduler.health import make_app
        from unittest.mock import MagicMock

        return await make_app(MagicMock())

    async def test_fixture_poll_creates_events_for_portal(self):
        os.environ["NEWS_USE_FIXTURES"] = "1"
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

        s = settings_mod.settings
        s.news_engine_enabled = True
        s.news_portal_widgets_enabled = True
        s.news_event_resolver_enabled = True
        for cid in ("central_bank_rss",):
            setattr(s, setting_key_for(cid), True)

        from scheduler.news_engine_job import run_news_engine_cycle
        with patch("storage.database.AsyncSessionFactory", Session), patch(
            "scheduler.news_engine_job.AsyncSessionFactory", Session
        ), patch("scheduler.news_portal_api.AsyncSessionFactory", Session):
            stats = await run_news_engine_cycle()
            self.assertGreater(stats.get("items", 0), 0)
            self.assertGreater(stats.get("events_created", 0), 0)
            resp = await self.client.get("/api/portal/news")
            data = __import__("json").loads(await resp.text())
            self.assertTrue(data["enabled"])
            self.assertGreater(len(data["events"]), 0)

        s.news_engine_enabled = False
        s.news_portal_widgets_enabled = False
        s.news_event_resolver_enabled = False
        os.environ.pop("NEWS_USE_FIXTURES", None)
        await engine.dispose()
