"""
A remote database's cost is round trips, not query complexity: the
diagnostics bundle from /api-docs showed a bare SELECT 1 at ~1.4s median.
These two jobs used to commit once per row (crypto_snapshot_job) or hold a
connection checked out, idle, across a CPU-bound step() (v2_shadow_job) —
both multiply that per-round-trip cost by the watchlist size instead of
paying it once.
"""
from __future__ import annotations

import os
import tempfile
import types
import unittest
import uuid
from unittest.mock import AsyncMock, patch

import pandas as pd


def _sqlite_url() -> str:
    return f"sqlite+aiosqlite:///{tempfile.mktemp(suffix='.db')}"


class TestCommitIsBatchable(unittest.TestCase):
    """
    The repository methods used by both jobs accept commit=False.

    DATABASE_URL is set, and everything storage-related is imported, only
    inside the test — storage/database.py binds its engine to whatever URL
    is current the first time it is imported anywhere in the process, and a
    module-level import here would race every other test file for that.
    """

    def test_snapshots_and_v2_writes_can_defer_their_commit(self):
        os.environ["DATABASE_URL"] = _sqlite_url()

        async def run():
            from analysis.v2_setups import Candidate
            from storage.database import AsyncSessionFactory, init_db
            from storage.repository import Repository
            await init_db()
            sym = f"batch{uuid.uuid4().hex[:8]}usdt"
            now = pd.Timestamp.now("UTC").tz_localize(None).floor("5min")
            c = Candidate(now, sym, "A", "long", 100.0, 99.0, 102.0, {})
            async with AsyncSessionFactory() as s:
                repo = Repository(s)
                await repo.save_crypto_snapshot(
                    symbol=sym, price=1.0, volume_24h=1.0, rsi_14=50.0,
                    macd_line=0.0, macd_signal=0.0, bollinger_upper=1.0,
                    bollinger_lower=1.0, atr_14=0.01, sentiment_score=0.0,
                    commit=False)
                await repo.save_commodity_snapshot(
                    symbol="XAUUSD", price=1.0, rsi_14=50.0, atr_14=0.01, commit=False)
                new = await repo.save_v2_candidates([c], commit=False)
                self.assertEqual(new, 1)
                # Not visible from another connection until this one commits.
                async with AsyncSessionFactory() as s2:
                    rows = await Repository(s2).open_v2_shadows(sym)
                    self.assertEqual(rows, [])
                await s.commit()          # one round trip for all three writes
            async with AsyncSessionFactory() as s2:
                rows = await Repository(s2).open_v2_shadows(sym)
                self.assertEqual(len(rows), 1)
                await Repository(s2).update_v2_shadow(rows[0].id, commit=False,
                                                       status="closed", r=1.0)
                await s2.commit()
        asyncio_run(run())


def asyncio_run(coro):
    import asyncio
    asyncio.run(coro)


class TestJobsUseOneRoundTripNotOnePerRow(unittest.TestCase):
    """
    Count how many times each job opens a database session. A regression
    back to per-row commits would show up here as N sessions instead of 1-2,
    long before it shows up as a slow page in production.
    """

    def test_crypto_snapshot_job_commits_once_for_the_whole_watchlist(self):
        async def run():
            from scheduler.runner import AppRunner
            states = [types.SimpleNamespace(
                symbol=f"c{i}usdt", current_price=1.0, volume_24h=1.0, rsi_14=50.0,
                macd_line=0.0, macd_signal=0.0, bollinger_upper=1.0, bollinger_lower=1.0,
                atr_14=0.01, sentiment_score=0.0) for i in range(6)]
            fake = types.SimpleNamespace(
                crypto_store=types.SimpleNamespace(get_all=AsyncMock(return_value=states)),
                commodity_store=types.SimpleNamespace(get_all=AsyncMock(return_value=[])))

            opens = []
            real_factory = _fake_session_factory(opens)
            with patch("scheduler.runner.AsyncSessionFactory", real_factory):
                await AppRunner._crypto_snapshot_job(fake)

            self.assertEqual(len(opens), 1, "one session for the whole watchlist")
            self.assertEqual(opens[0].commits, 1, "one commit, not one per coin")
            self.assertEqual(opens[0].adds, 6)
        asyncio_run(run())

    def test_v2_shadow_job_opens_at_most_two_sessions_for_any_watchlist_size(self):
        async def run():
            from scheduler.runner import AppRunner
            symbols = [f"v{i}usdt" for i in range(5)]
            fake = types.SimpleNamespace(
                crypto_store=types.SimpleNamespace(get_symbols=AsyncMock(return_value=symbols)))

            empty = pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume"])
            with patch("scheduler.runner.settings") as fake_settings, \
                 patch("analysis.instruments.spec_for",
                       return_value=types.SimpleNamespace(kind="crypto")), \
                 patch("collectors.v2_feed.fetch_frames",
                       new=AsyncMock(return_value={"5m": empty})), \
                 patch("collectors.v2_feed.fetch_funding", new=AsyncMock(return_value=0.0)):
                fake_settings.v2_shadow_enabled = True
                fake_settings.session_filter_enabled = False
                fake_settings.session_start_utc = 0
                fake_settings.session_end_utc = 24
                fake_settings.session_weekdays_only = False

                opens = []
                with patch("scheduler.runner.AsyncSessionFactory",
                          _fake_session_factory(opens)):
                    await AppRunner._v2_shadow_job(fake)

            # Empty 5m frames mean every symbol is skipped before step() runs,
            # so only the read session opens — never one per symbol.
            self.assertLessEqual(len(opens), 2)
        asyncio_run(run())


def _fake_session_factory(opens: list):
    """A minimal stand-in for AsyncSessionFactory() that counts round trips."""
    from storage.repository import Repository as _Repo  # noqa: F401

    class FakeSession:
        def __init__(self):
            self.commits = 0
            self.adds = 0

        def add(self, _obj):
            self.adds += 1

        async def commit(self):
            self.commits += 1

        async def execute(self, *a, **k):
            class R:
                def scalar_one_or_none(self):
                    return None

                def scalars(self):
                    class S:
                        def all(self_inner):
                            return []
                    return S()
            return R()

        async def get(self, *a, **k):
            return None

        async def __aenter__(self):
            opens.append(self)
            return self

        async def __aexit__(self, *a):
            return False

    def factory():
        return FakeSession()
    return factory
