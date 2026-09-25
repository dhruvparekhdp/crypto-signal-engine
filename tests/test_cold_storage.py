"""At most a year in the database; older rows move to JSON files, never lost."""

import asyncio
import gzip
import json
import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from storage.cold_storage import append_rows, read_rows


class TestFiles(unittest.TestCase):
    def test_append_groups_by_month_and_read_drops_repeats(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            rows = [{"id": 1, "timestamp": "2024-01-05T10:00:00", "v": 1},
                    {"id": 2, "timestamp": "2024-02-01T00:00:00", "v": 2}]
            append_rows(root, "crypto_signal_log", rows, "timestamp")
            # a crash between write and delete means the next run writes again
            append_rows(root, "crypto_signal_log", rows[:1], "timestamp")
            files = sorted(p.name for p in (root / "crypto_signal_log").iterdir())
            self.assertEqual(files, ["2024-01.jsonl.gz", "2024-02.jsonl.gz"])
            with gzip.open(root / "crypto_signal_log" / "2024-01.jsonl.gz", "rt") as fh:
                self.assertEqual(len(fh.readlines()), 2)       # both appended members
            got = list(read_rows(root, "crypto_signal_log"))
            self.assertEqual([r["id"] for r in got], [1, 2])
            got = list(read_rows(root, "crypto_signal_log", start=datetime(2024, 1, 20)))
            self.assertEqual([r["id"] for r in got], [2])


class TestOffload(unittest.TestCase):
    def test_old_rows_leave_the_database_for_files(self):
        db = tempfile.mktemp(suffix=".db")
        os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{db}"

        async def run():
            from sqlalchemy import func, select

            import storage.database as database
            from storage.cold_storage import offload
            from storage.database import AsyncSessionFactory, init_db
            from storage.models import CryptoSignalLog

            await init_db()
            marker = "cold-test"

            def n():
                return select(func.count()).select_from(CryptoSignalLog).where(
                    CryptoSignalLog.symbol == marker)

            now = datetime.now(UTC).replace(tzinfo=None)
            async with AsyncSessionFactory() as s:
                before = (await s.execute(n())).scalar()
                for age in (10, 400, 800):
                    s.add(CryptoSignalLog(
                        symbol=marker, signal_type="x", direction="long",
                        trigger_description="", confidence=0.5, current_price=1.0,
                        edge_pct=0.0, stake_pct=0.0, timeframe="1h",
                        timestamp=now - timedelta(days=age)))
                await s.commit()
            with tempfile.TemporaryDirectory() as d:
                root = Path(d)
                async with AsyncSessionFactory() as s:
                    dry = await offload(s, root, days=365, dry_run=True,
                                        tables=["crypto_signal_log"])
                self.assertGreaterEqual(dry["crypto_signal_log"], 2)
                self.assertFalse((root / "crypto_signal_log").exists())
                async with AsyncSessionFactory() as s:
                    await offload(s, root, days=365, batch=1, tables=["crypto_signal_log"])
                async with AsyncSessionFactory() as s:
                    after = (await s.execute(n())).scalar()
                self.assertEqual(after - before, 1)            # only the recent one stays
                mine = [r for r in read_rows(root, "crypto_signal_log")
                        if r["symbol"] == marker]
                self.assertEqual(len(mine), 2)
                self.assertTrue(all(json.dumps(r) for r in mine))
                async with AsyncSessionFactory() as s:
                    again = await offload(s, root, days=365, tables=["crypto_signal_log"])
                self.assertEqual(again["crypto_signal_log"], 0)
            await database.engine.dispose()

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
