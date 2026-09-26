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

    def test_a_key_already_in_env_is_recognised_without_ever_touching_settings(self):
        """
        Most keys the owner has are already in .env on the server, set long
        before this page existed. Precedence is database > .env > default —
        this is the .env rung: nothing saved on /settings, and it must still
        show as set, from a real Settings() instance reading a real env var,
        not a stand-in object.
        """
        import os as _os

        from config.overrides import describe
        from config.settings import Settings

        _os.environ["GROQ_API_KEY"] = "gsk_from_env_abc123"
        try:
            fresh = Settings()
            self.assertEqual(fresh.groq_api_key.get_secret_value(), "gsk_from_env_abc123")
            row = next(r for r in describe(fresh, {}) if r["key"] == "groq_api_key")
            self.assertTrue(row["is_set"])
            self.assertIsNone(row["value"])                # still never echoed
            self.assertEqual(row["source"], "default")      # not saved on the page — from .env
        finally:
            del _os.environ["GROQ_API_KEY"]

    def test_a_database_save_overrides_env_not_the_other_way_round(self):
        """The documented precedence (database > .env > default): a key
        pasted on /settings must win over whatever .env already has."""
        import os as _os

        from config.settings import Settings

        _os.environ["GROQ_API_KEY"] = "gsk_from_env_abc123"
        try:
            fresh = Settings()
            done = apply(fresh, {"groq_api_key": "gsk_from_database_xyz"})
            self.assertEqual(done, ["groq_api_key"])
            self.assertEqual(fresh.groq_api_key.get_secret_value(), "gsk_from_database_xyz")
        finally:
            del _os.environ["GROQ_API_KEY"]

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

    def test_a_blank_key_box_leaves_the_stored_key_alone(self):
        """The whole point of "leave blank to keep it": apply() must never
        let an empty submission clear a key that is already set."""
        s = SimpleNamespace(openrouter_api_key="sentinel", twelvedata_api_key="sentinel2")
        done = apply(s, {"openrouter_api_key": "", "twelvedata_api_key": ""})
        self.assertEqual(done, [])
        self.assertEqual(s.openrouter_api_key, "sentinel")
        self.assertEqual(s.twelvedata_api_key, "sentinel2")

    def test_a_pasted_key_is_wrapped_in_secretstr_where_the_model_expects_one(self):
        from pydantic import SecretStr
        s = SimpleNamespace(openrouter_api_key=None, twelvedata_api_key=None)
        done = apply(s, {"openrouter_api_key": "  sk-or-abc123  ",
                         "twelvedata_api_key": "plain-key"})
        self.assertEqual(sorted(done), ["openrouter_api_key", "twelvedata_api_key"])
        self.assertIsInstance(s.openrouter_api_key, SecretStr)
        self.assertEqual(s.openrouter_api_key.get_secret_value(), "sk-or-abc123")
        self.assertEqual(s.twelvedata_api_key, "plain-key")            # not wrapped

    def test_describe_never_carries_a_key_only_whether_one_is_set(self):
        from pydantic import SecretStr

        from config.overrides import describe
        s = SimpleNamespace(groq_api_key=SecretStr("a-real-secret"), openrouter_api_key=None,
                            twelvedata_api_key="also-real", session_end_utc=22)
        rows = {r["key"]: r for r in describe(s, {})}
        self.assertIsNone(rows["groq_api_key"]["value"])
        self.assertTrue(rows["groq_api_key"]["is_set"])
        self.assertIsNone(rows["openrouter_api_key"]["value"])
        self.assertFalse(rows["openrouter_api_key"]["is_set"])
        self.assertIsNone(rows["twelvedata_api_key"]["value"])
        self.assertTrue(rows["twelvedata_api_key"]["is_set"])
        self.assertEqual(rows["session_end_utc"]["value"], 22)         # not a secret


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

    def test_a_pasted_key_never_comes_back_and_a_blank_box_does_not_clear_it(self):
        os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tempfile.mktemp(suffix='.db')}"

        async def run():
            import storage.database as database
            from config.settings import settings
            from scheduler import settings_page
            from storage.database import init_db
            from storage.repository import Repository

            await init_db()
            get, post = settings_page.settings_api(SimpleNamespace(collector_enabled={}))
            before_key = settings.openrouter_api_key
            with patch.object(settings_page, "_admin", AsyncMock(return_value=None)):
                req = make_mocked_request("POST", "/api/app-settings")
                req.json = AsyncMock(
                    return_value={"values": {"openrouter_api_key": "sk-or-v1-realsecret"}})
                body = json.loads((await post(req)).text)
                self.assertEqual(body["applied"], ["openrouter_api_key"])
                self.assertEqual(settings.openrouter_api_key.get_secret_value(),
                                 "sk-or-v1-realsecret")

                fields = json.loads((await get(make_mocked_request("GET", "/"))).text)["fields"]
                row = next(f for f in fields if f["key"] == "openrouter_api_key")
                self.assertIsNone(row["value"])                        # never echoed
                self.assertTrue(row["is_set"])
                self.assertNotIn("sk-or-v1-realsecret", json.dumps(fields))

                blank = make_mocked_request("POST", "/api/app-settings")
                blank.json = AsyncMock(return_value={"values": {"openrouter_api_key": ""}})
                body2 = json.loads((await post(blank)).text)
                self.assertEqual(body2["applied"], [])                 # nothing to apply
                self.assertEqual(settings.openrouter_api_key.get_secret_value(),
                                 "sk-or-v1-realsecret")                # unchanged
            async with database.AsyncSessionFactory() as s:
                stored = await Repository(s).get_app_settings()
            self.assertEqual(stored["openrouter_api_key"], "sk-or-v1-realsecret")

            settings.openrouter_api_key = before_key
            await database.engine.dispose()

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
