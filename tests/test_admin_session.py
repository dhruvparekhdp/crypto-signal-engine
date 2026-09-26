"""Admin login lasts 7 days in every tab (HttpOnly cookie), then expires."""

import asyncio
import os
import tempfile
import unittest
from datetime import timedelta
from unittest.mock import MagicMock

from aiohttp.test_utils import make_mocked_request


class TestAdminSession(unittest.TestCase):
    def test_token_sources(self):
        from scheduler.health import ADMIN_COOKIE, _session_token
        r = make_mocked_request("GET", "/", headers={"X-Settings-Token": "h"})
        self.assertEqual(_session_token(r), "h")
        r = make_mocked_request("GET", "/", headers={"Authorization": "Bearer b"})
        self.assertEqual(_session_token(r), "b")
        r = make_mocked_request("GET", "/", headers={"Cookie": f"{ADMIN_COOKIE}=c"})
        self.assertEqual(_session_token(r), "c")
        self.assertEqual(_session_token(make_mocked_request("GET", "/")), "")

    def test_login_sets_a_7_day_httponly_cookie_and_it_expires(self):
        os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tempfile.mktemp(suffix='.db')}"

        async def run():
            import json

            import storage.database as database
            from scheduler import health
            from storage.database import AsyncSessionFactory, init_db
            from storage.models import AdminAuth
            from storage.repository import Repository

            await init_db()
            async with AsyncSessionFactory() as s:
                await Repository(s).set_admin_password("correct horse battery")
            req = make_mocked_request("POST", "/api/settings/auth/login")
            req.json = MagicMock(return_value=asyncio.sleep(0, {"password": "correct horse battery"}))
            resp = await health._api_settings_auth_login(None, req)
            body = json.loads(resp.text)
            self.assertTrue(body["ok"])
            c = resp.cookies[health.ADMIN_COOKIE]
            self.assertEqual(c.value, body["token"])
            self.assertEqual(int(c["max-age"]), 7 * 86400)
            self.assertTrue(c["httponly"])
            self.assertEqual(c["samesite"], "Strict")

            cookie_req = make_mocked_request(
                "GET", "/", headers={"Cookie": f"{health.ADMIN_COOKIE}={body['token']}"})
            self.assertTrue(await health._verify_admin_session(cookie_req))
            async with AsyncSessionFactory() as s:          # 8 days later
                row = await s.get(AdminAuth, 1)
                row.updated_at = row.updated_at - timedelta(days=8)
                await s.commit()
            self.assertFalse(await health._verify_admin_session(cookie_req))
            await database.engine.dispose()

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
