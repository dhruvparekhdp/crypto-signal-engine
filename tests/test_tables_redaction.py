"""
/api/tables is unauthenticated, and it used to return admin_auth whole —
the live session token included, which is the operator's login. These pin
that no secret column comes back, and that what does come back cannot be
used as a session.
"""
import asyncio
import os
import tempfile
import unittest


class TestTablesRedaction(unittest.TestCase):
    def test_the_table_viewer_never_returns_the_admin_session(self):
        db = tempfile.mktemp(suffix=".db")
        os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{db}"

        async def run():
            from aiohttp.test_utils import TestClient, TestServer

            import storage.database as database
            from storage.database import AsyncSessionFactory, init_db
            from storage.models import SECRET_COLUMNS
            from storage.repository import Repository

            await init_db()
            async with AsyncSessionFactory() as s:
                await Repository(s).set_admin_password("operator-pw")
            async with AsyncSessionFactory() as s:
                ok, token = await Repository(s).verify_admin_password("operator-pw")
            self.assertTrue(ok)

            class Runner:
                collector_enabled: dict = {}

            from scheduler.health import make_app
            app = await make_app(Runner())
            async with TestClient(TestServer(app)) as c:
                r = await c.get("/api/tables?limit=5")
                self.assertEqual(r.status, 200)
                data = await r.json()
                table = next(t for t in data["tables"] if t["name"] == "admin_auth")
                row = dict(zip(table["columns"], table["rows"][0]))
                for col in SECRET_COLUMNS["admin_auth"]:
                    self.assertEqual(row[col], "[redacted]", col)
                self.assertNotIn(token, str(data))

                r2 = await c.post("/api/paper/config", json={"leverage": 50},
                                  headers={"X-Settings-Token": row["session_token"]})
                self.assertEqual(r2.status, 401)
            await database.engine.dispose()

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
