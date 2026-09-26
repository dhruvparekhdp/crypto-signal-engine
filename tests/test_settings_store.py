"""Settings saved on /settings reach the engine: stored, applied, validated."""

import asyncio
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import make_mocked_request

from config.overrides import BY_KEY, FIELDS, apply, coerce


class TestOverrides(unittest.TestCase):
    def test_every_field_exists_on_settings(self):
        from config.settings import settings
        for f in FIELDS:
            self.assertTrue(hasattr(settings, f.key), f.key)

    def test_coerce_and_ranges(self):
        self.assertIs(coerce(BY_KEY["binance_only_mode"], "on"), True)
        self.assertEqual(coerce(BY_KEY["session_start_utc"], "7.4"), 7)
        with self.assertRaises(ValueError):
            coerce(BY_KEY["crypto_min_confidence"], 2)
        with self.assertRaises(ValueError):
            coerce(BY_KEY["daily_loss_limit_pct"], "nan")

    def test_apply_changes_the_live_object_and_ignores_unknown_keys(self):
        s = SimpleNamespace(binance_only_mode=False, crypto_min_confidence=0.6)
        done = apply(s, {"binance_only_mode": True, "crypto_min_confidence": "0.7",
                         "api_auth_token": "x", "daily_loss_limit_pct": "bad"})
        self.assertEqual(sorted(done), ["binance_only_mode", "crypto_min_confidence"])
        self.assertTrue(s.binance_only_mode)
        self.assertAlmostEqual(s.crypto_min_confidence, 0.7)
        self.assertFalse(hasattr(s, "api_auth_token"))


class TestApi(unittest.TestCase):
    def test_save_is_stored_applied_and_reaches_the_runner(self):
        os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tempfile.mktemp(suffix='.db')}"

        async def run():
            import storage.database as database
            from config.settings import settings
            from scheduler import settings_page
            from storage.database import AsyncSessionFactory, init_db
            from storage.repository import Repository

            await init_db()
            changed = []
            runner = SimpleNamespace(collector_enabled={"coindcx": True},
                                     on_settings_changed=changed.extend)
            get, post = settings_page.settings_api(runner)
            before = settings.session_end_utc
            with patch.object(settings_page, "_admin", AsyncMock(return_value=None)):
                req = make_mocked_request("POST", "/api/app-settings")
                req.json = AsyncMock(return_value={"values": {"session_end_utc": 18,
                                                              "high_conviction_only": True}})
                body = json.loads((await post(req)).text)
                self.assertEqual(sorted(body["applied"]), ["high_conviction_only",
                                                           "session_end_utc"])
                self.assertEqual(body["needs_restart"], ["high_conviction_only"])
                self.assertEqual(settings.session_end_utc, 18)          # live
                self.assertIn("session_end_utc", changed)               # runner told
                bad = make_mocked_request("POST", "/api/app-settings")
                bad.json = AsyncMock(return_value={"values": {"session_end_utc": 99}})
                self.assertEqual((await post(bad)).status, 400)
            async with AsyncSessionFactory() as s:
                self.assertEqual((await Repository(s).get_app_settings())["session_end_utc"], 18)
            fields = json.loads((await get(make_mocked_request("GET", "/"))).text)["fields"]
            row = next(f for f in fields if f["key"] == "session_end_utc")
            self.assertEqual((row["value"], row["source"]), (18, "saved"))
            denied = await post(make_mocked_request("POST", "/api/app-settings"))
            self.assertNotEqual(denied.status, 200)                     # admin only
            settings.session_end_utc = before
            settings.high_conviction_only = False
            await database.engine.dispose()

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
