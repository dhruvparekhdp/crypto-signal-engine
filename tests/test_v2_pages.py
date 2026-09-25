"""The v2 shadow API and the owner's journal API, against a real database."""

import asyncio
import json
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import pandas as pd
from aiohttp.test_utils import make_mocked_request

from analysis.v2_setups import Candidate


class TestPages(unittest.TestCase):
    def test_shadow_and_journal_round_trip(self):
        os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tempfile.mktemp(suffix='.db')}"

        async def run():
            import storage.database as database
            from scheduler import v2_pages
            from storage.database import AsyncSessionFactory, init_db
            from storage.repository import Repository

            await init_db()
            import uuid
            # The engine may already point at a database shared with other
            # test files and earlier runs, so this run's symbol is unique.
            sym = f"pt{uuid.uuid4().hex[:8]}usdt"
            now = pd.Timestamp.now("UTC").tz_localize(None).floor("5min")
            c = Candidate(now, sym, "A", "long", 100.0, 99.0, 102.0, {"level": 99.5})
            async with AsyncSessionFactory() as s:
                repo = Repository(s)
                self.assertEqual(await repo.save_v2_candidates([c]), 1)
                self.assertEqual(await repo.save_v2_candidates([c]), 0)     # no duplicates
                (row,) = [r for r in await repo.open_v2_shadows(sym)]
                await repo.update_v2_shadow(row.id, status="closed", reason="target", r=1.9)

            resp = await v2_pages.api_v2_shadow(make_mocked_request("GET", "/api/v2/shadow"))
            body = json.loads(resp.text)
            mine = [r for r in body["rows"] if r["symbol"] == sym]
            self.assertEqual(mine[0]["status"], "closed")
            self.assertIn("A", body["by_setup"])

            with patch.object(v2_pages, "_admin", AsyncMock(return_value=None)), \
                 patch.object(v2_pages, "journal_features",
                              AsyncMock(return_value={"structure_15m_12h": "up"})):
                req = make_mocked_request("POST", "/api/journal")
                req.json = AsyncMock(return_value={
                    "symbol": "SOLUSDT", "side": "short", "opened_at": "2026-09-25T09:15:00Z",
                    "entry": "240.5", "tags": "sweep_pdl_pdh,made_up", "conviction": 9,
                    "note": "rejected at 15m high"})
                resp = await v2_pages.api_journal(req)
                self.assertEqual(resp.status, 200)
                resp = await v2_pages.api_journal(make_mocked_request("GET", "/api/journal"))
                rows = [r for r in json.loads(resp.text)["rows"]
                        if r["note"] == "rejected at 15m high"]
                self.assertEqual(rows[0]["symbol"], "solusdt")
                self.assertEqual(rows[0]["tags"], "sweep_pdl_pdh")          # unknown tag dropped
                self.assertEqual(rows[0]["conviction"], 5)                 # clamped
                self.assertEqual(rows[0]["features"]["structure_15m_12h"], "up")
                bad = make_mocked_request("POST", "/api/journal")
                bad.json = AsyncMock(return_value={"symbol": "sol", "side": "up"})
                self.assertEqual((await v2_pages.api_journal(bad)).status, 400)

            denied = await v2_pages.api_journal(make_mocked_request("GET", "/api/journal"))
            self.assertNotEqual(denied.status, 200)                        # admin only
            await database.engine.dispose()

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
