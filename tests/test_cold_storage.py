"""At most a year in the database; older rows move to JSON files, never lost."""

import asyncio
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
            self.assertEqual([f[:7] for f in files], ["2024-01", "2024-01", "2024-02"])
            self.assertFalse(any(f.endswith(".tmp") for f in files))
            got = list(read_rows(root, "crypto_signal_log"))
            self.assertEqual([r["id"] for r in got], [1, 2])
            got = list(read_rows(root, "crypto_signal_log", start=datetime(2024, 1, 20)))
            self.assertEqual([r["id"] for r in got], [2])

    def test_a_crash_mid_write_loses_nothing_already_written(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            append_rows(root, "t", [{"id": 1, "created_at": "2024-01-05T10:00:00"}],
                        "created_at")
            # a batch killed mid-write leaves only its .tmp, never a broken file
            (root / "t" / "2024-01.9-9.1.jsonl.gz.tmp").write_bytes(b"\x1f\x8b garbage")
            append_rows(root, "t", [{"id": 2, "created_at": "2024-01-06T10:00:00"}],
                        "created_at")
            self.assertEqual([r["id"] for r in read_rows(root, "t")], [1, 2])

    def test_reused_ids_are_not_mistaken_for_repeats(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            append_rows(root, "t", [{"id": 1, "created_at": "2024-01-05T10:00:00"}],
                        "created_at")
            append_rows(root, "t", [{"id": 1, "created_at": "2024-03-05T10:00:00"}],
                        "created_at")
            self.assertEqual(len(list(read_rows(root, "t"))), 2)


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


    def test_a_table_with_nothing_old_costs_one_round_trip_not_two(self):
        """
        Most of the 15 offloaded tables have nothing past a year old on any
        given day. The old code always paid a max(id) round trip AND a batch
        SELECT round trip for those tables — the batch select just came back
        empty. Scoping the max(id) query to the cutoff answers "anything to
        do here?" in that same single round trip, which is most of what made
        db_cleanup ~29s in production (mostly empty work, each round trip
        paying this database's network floor).
        """
        db = tempfile.mktemp(suffix=".db")
        os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{db}"

        async def run():
            import storage.database as database
            from storage.cold_storage import offload
            from storage.database import AsyncSessionFactory, init_db
            from storage.models import CryptoSignalLog

            await init_db()
            now = datetime.now(UTC).replace(tzinfo=None)
            async with AsyncSessionFactory() as s:
                # Only recent rows: nothing here should ever be old enough
                # to move under a 365-day cutoff.
                s.add(CryptoSignalLog(
                    symbol="rt-test", signal_type="x", direction="long",
                    trigger_description="", confidence=0.5, current_price=1.0,
                    edge_pct=0.0, stake_pct=0.0, timeframe="1h",
                    timestamp=now - timedelta(days=10)))
                await s.commit()

            with tempfile.TemporaryDirectory() as d:
                root = Path(d)
                async with AsyncSessionFactory() as s:
                    calls = 0
                    real_execute = s.execute

                    async def counting_execute(*a, **kw):
                        nonlocal calls
                        calls += 1
                        return await real_execute(*a, **kw)

                    s.execute = counting_execute
                    moved = await offload(s, root, days=365, tables=["crypto_signal_log"])
            self.assertEqual(moved["crypto_signal_log"], 0)
            self.assertEqual(calls, 1, f"expected one round trip, made {calls}")
            await database.engine.dispose()

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
