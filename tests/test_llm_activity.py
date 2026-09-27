"""
"Which provider actually answered lately" — not just which one is configured.

The owner's real question was "how do I check openrouter is being called".
settings.llm_chain_* only says what a role WOULD try; a rate-limited primary
being silently caught (or not) by its fallback was invisible without reading
server logs. These tests cover the per-role query and the settings page's
use of it.
"""
import asyncio
import unittest
import uuid

from storage.database import AsyncSessionFactory, init_db
from storage.repository import Repository


def _run(coro):
    return asyncio.run(coro)


class TestRecentServedBy(unittest.TestCase):
    def test_reports_the_most_recent_model_and_who_else_answered_lately(self):
        async def go():
            await init_db()
            sym = f"activity{uuid.uuid4().hex[:8]}usdt"
            async with AsyncSessionFactory() as s:
                repo = Repository(s)
                await repo.save_review("pre", sym, model="groq/openai/gpt-oss-120b")
                await repo.save_review("pre", sym, model="groq/openai/gpt-oss-120b")
                await repo.save_review("pre", sym, model="openrouter/qwen/qwen3-32b")

                from storage.models import SignalReview
                got = await repo.recent_served_by(
                    SignalReview, SignalReview.created_at, SignalReview.model,
                    phase="pre", phase_col=SignalReview.phase)
            self.assertEqual(got["last_served_by"], "openrouter/qwen/qwen3-32b")
            self.assertEqual(got["used"], ["groq", "openrouter"])
            self.assertGreaterEqual(got["checked"], 3)
        _run(go())

    def test_a_phase_with_no_rows_reports_nothing_served_not_an_error(self):
        async def go():
            await init_db()
            from storage.models import SignalReview
            async with AsyncSessionFactory() as s:
                got = await Repository(s).recent_served_by(
                    SignalReview, SignalReview.created_at, SignalReview.model,
                    phase=f"nonexistent-{uuid.uuid4().hex}", phase_col=SignalReview.phase)
            self.assertIsNone(got["last_served_by"])
            self.assertEqual(got["used"], [])
        _run(go())

    def test_a_table_with_no_phase_column_is_read_whole(self):
        """briefing and attribution have one stream each, no phase to filter."""
        async def go():
            await init_db()
            async with AsyncSessionFactory() as s:
                repo = Repository(s)
                await repo.save_briefing(0.1, "s", [], "openrouter/qwen/qwen3-32b:online", 100)

                from storage.models import MarketBriefing
                got = await repo.recent_served_by(
                    MarketBriefing, MarketBriefing.created_at, MarketBriefing.model)
            self.assertEqual(got["last_served_by"], "openrouter/qwen/qwen3-32b:online")
        _run(go())


class TestSettingsPageShowsRecentActivity(unittest.TestCase):
    def test_the_settings_api_response_carries_who_answered_each_role(self):
        async def go():
            from types import SimpleNamespace

            from scheduler import cache, settings_page
            cache.clear()   # GET /api/app-settings caches its DB read briefly

            await init_db()
            sym = f"activity{uuid.uuid4().hex[:8]}usdt"
            async with AsyncSessionFactory() as s:
                await Repository(s).save_review(
                    "pre", sym, model="openrouter/qwen/qwen3-32b")

            get, _post = settings_page.settings_api(SimpleNamespace(collector_enabled={}))
            import json

            from aiohttp.test_utils import make_mocked_request
            body = json.loads((await get(make_mocked_request("GET", "/"))).text)
            self.assertIn("models_activity", body)
            self.assertIn("pre_trade", body["models_activity"])
            self.assertIn("last_served_by", body["models_activity"]["pre_trade"])
        _run(go())


if __name__ == "__main__":
    unittest.main()
