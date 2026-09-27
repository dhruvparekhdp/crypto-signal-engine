"""'Why no trades?': engine log events become a funnel with reasons."""

import asyncio
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import make_mocked_request

from scheduler import pipeline


class TestFunnel(unittest.TestCase):
    def setUp(self):
        pipeline._events.clear()
        pipeline._last.clear()

    def test_log_events_are_counted_with_readable_reasons(self):
        p = pipeline.processor
        p(None, "info", {"event": "crypto_signal_fired", "symbol": "solusdt"})
        p(None, "info", {"event": "crypto_signal_against_daily_trend", "symbol": "btcusdt"})
        p(None, "info", {"event": "paper_trade_skipped", "symbol": "sol",
                         "reason": "outside_session"})
        p(None, "info", {"event": "paper_trade_skipped", "reason": "outside_session"})
        p(None, "info", {"event": "paper.rejected", "reason": "some_new_rule"})
        out = p(None, "info", {"event": "unrelated", "x": 1})
        self.assertEqual(out, {"event": "unrelated", "x": 1})           # passes through
        f = pipeline.funnel(24)
        self.assertEqual(f["stages"], {"fired": 1, "blocked": 1, "paper_skipped": 3})
        self.assertEqual(f["reasons"]["paper_skipped"][0],
                         {"reason": "outside trading hours / weekend", "count": 2})
        self.assertIn({"reason": "some new rule", "count": 1}, f["reasons"]["paper_skipped"])
        self.assertIn("fired", f["last"])

    def test_no_setup_reasons_are_grouped_and_counted_per_scan(self):
        pipeline._scans.clear()
        pipeline._scan_now.clear()
        pipeline.no_setup("volatility in the bottom 12% of its own recent range", "confluence")
        pipeline.no_setup("volatility in the bottom 30% of its own recent range", "confluence")
        pipeline.no_setup("only 2 of 5 families agree", "confluence")
        pipeline.end_scan(5)
        pipeline.no_setup("only 2 of 5 families agree", "confluence")
        pipeline.end_scan(5)
        f = pipeline.funnel()
        self.assertEqual(f["scans"], 2)
        self.assertEqual(f["coins"], 5)
        got = {r["reason"]: r["count"] for r in f["no_setup"]}
        self.assertEqual(got["confluence: volatility at a low for this coin"], 2)
        self.assertEqual(got["confluence: only 2 of 5 families agree"], 2)

    def test_the_confluence_analyser_reports_why_it_found_nothing(self):
        from analysis.crypto_signals import ConfluenceAnalyzer
        from analysis.crypto_state import CryptoState
        pipeline._scan_now.clear()
        ConfluenceAnalyzer().analyze(CryptoState(symbol="btcusdt", base_asset="BTC",
                                                 current_price=100.0))
        self.assertEqual(list(pipeline._scan_now),
                         ["confluence: under 60 one-minute candles: history still loading"])
        pipeline._scan_now.clear()

    def test_a_broken_event_never_breaks_logging(self):
        self.assertEqual(pipeline.processor(None, "info", {"event": None}), {"event": None})


class TestApi(unittest.TestCase):
    def test_gates_and_funnel(self):
        os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tempfile.mktemp(suffix='.db')}"

        async def run():
            import storage.database as database
            from analysis.protections import ProtectionConfig
            from scheduler import cache
            from scheduler.settings_page import pipeline_api
            from storage.database import init_db
            cache.clear()
            await init_db()

            async def get_all():
                return []
            runner = SimpleNamespace(
                _protection_config=lambda: ProtectionConfig(session_filter=True),
                _blackout=lambda now: None, crypto_store=SimpleNamespace(get_all=get_all))
            resp = await pipeline_api(runner)(make_mocked_request("GET", "/api/pipeline"))
            body = json.loads(resp.text)
            self.assertIn("in_session_now", body["gates"])
            self.assertIn("stages", body["day"])
            await database.engine.dispose()

        asyncio.run(run())


class TestPipelineDbReadIsCached(unittest.TestCase):
    """
    /api/pipeline's paper-config and open-positions reads barely change
    between one poll and the next, so a repeat poll within the cache's TTL
    must not pay the database round trip again — the same discipline
    /api/paper's own snapshot already follows.
    """

    def test_a_second_call_within_ttl_does_not_hit_the_database_again(self):
        os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tempfile.mktemp(suffix='.db')}"

        async def run():
            import storage.database as database
            from scheduler import cache
            from scheduler.settings_page import _pipeline_db_read
            from storage.database import init_db
            cache.clear()
            await init_db()

            calls = []
            from storage.repository import Repository
            real_get_paper_config = Repository.get_paper_config

            async def counted(self):
                calls.append(1)
                return await real_get_paper_config(self)
            Repository.get_paper_config = counted
            try:
                await _pipeline_db_read()
                await _pipeline_db_read()
            finally:
                Repository.get_paper_config = real_get_paper_config
            self.assertEqual(len(calls), 1, "second call should hit the cache, not the DB")
            await database.engine.dispose()

        asyncio.run(run())

    def test_a_paper_config_save_invalidates_it_immediately(self):
        os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tempfile.mktemp(suffix='.db')}"

        async def run():
            import storage.database as database
            from scheduler import cache
            from scheduler.health import _api_paper_config_post
            from scheduler.settings_page import _pipeline_db_read
            from storage.database import init_db
            cache.clear()
            await init_db()

            pcfg, _rows = await _pipeline_db_read()
            self.assertEqual(pcfg.max_concurrent, 3)  # default, before the save

            class Body:
                async def json(self):
                    return {"max_concurrent": 9}
            with patch("scheduler.health._verify_admin_session", AsyncMock(return_value=True)):
                fake_req = SimpleNamespace(json=Body().json)
                await _api_paper_config_post(SimpleNamespace(), fake_req)

            pcfg2, _rows2 = await _pipeline_db_read()
            self.assertEqual(pcfg2.max_concurrent, 9,
                             "the save must be visible immediately, not after the cache's TTL")
            await database.engine.dispose()

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
