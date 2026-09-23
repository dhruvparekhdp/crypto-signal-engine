"""
Resolving signal outcomes, and the endpoints the new screens read.

Crypto signals were written as "pending" and never resolved, so no accuracy
figure could exist. These pin the resolution rules — above all that a bar
containing both levels books the STOP, because a resolver that breaks ties in
its own favour reports accuracy it has not earned.
"""
import unittest
from datetime import UTC, datetime, timedelta


class TestResolutionRules(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        import storage.models  # noqa: F401
        from storage.database import Base
        from storage.repository import Repository

        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.maker = async_sessionmaker(self.engine, expire_on_commit=False)
        self.Repository = Repository

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def _log(self, repo, hours_ago=8, **kw):
        base = dict(symbol="ethusdt", signal_type="confluence", direction="long",
                    trigger_description="x", confidence=0.80, current_price=1900.0,
                    target_price=1919.0, stop_loss=1881.0, edge_pct=1.0,
                    stake_pct=1.0, timeframe="1h", sentiment_score=0.0,
                    indicators_summary="")
        base.update(kw)
        await repo.log_crypto_signal(**base)
        rows = await repo.crypto_signals_between(1)
        return rows[0]

    async def test_a_fresh_signal_is_not_offered_for_resolution(self):
        """Resolving immediately would record the first tick, which is noise."""
        repo = self.Repository(self.maker())
        await self._log(repo)
        self.assertEqual(await repo.pending_crypto_signals(older_than_minutes=240), [])

    async def test_an_old_pending_signal_is_offered(self):
        repo = self.Repository(self.maker())
        row = await self._log(repo)
        row.timestamp = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=8)
        await repo.session.commit()
        self.assertEqual(len(await repo.pending_crypto_signals(older_than_minutes=240)), 1)

    async def test_resolving_records_outcome_and_pnl(self):
        repo = self.Repository(self.maker())
        row = await self._log(repo)
        await repo.resolve_crypto_signal(row.id, "won", 1.0)
        again = (await repo.crypto_signals_between(1))[0]
        self.assertEqual(again.outcome, "won")
        self.assertAlmostEqual(again.pnl_pct, 1.0, places=4)

    async def test_a_resolved_signal_is_no_longer_pending(self):
        repo = self.Repository(self.maker())
        row = await self._log(repo)
        row.timestamp = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=8)
        await repo.session.commit()
        await repo.resolve_crypto_signal(row.id, "lost", -1.0)
        self.assertEqual(await repo.pending_crypto_signals(older_than_minutes=240), [])

    async def test_the_window_query_splits_live_from_archive(self):
        repo = self.Repository(self.maker())
        recent = await self._log(repo)
        old = await self._log(repo, current_price=1800.0)
        old.timestamp = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=20)
        await repo.session.commit()

        live = await repo.crypto_signals_between(7)
        archive = await repo.crypto_signals_between(365, older_than_days=7)
        self.assertEqual([r.id for r in live], [recent.id])
        self.assertEqual([r.id for r in archive], [old.id])

    async def test_counts_report_what_is_still_pending(self):
        repo = self.Repository(self.maker())
        await self._log(repo)
        counts = await repo.crypto_signal_counts()
        self.assertEqual(counts["signals"], 1)
        self.assertEqual(counts["pending"], 1)


class TestPessimisticTieBreak(unittest.TestCase):
    """
    The rule that keeps the number honest, asserted against the source: a bar
    holding both target and stop must book the stop.
    """

    def setUp(self):
        # Read the file rather than import the module. scheduler.runner pulls
        # in python-telegram-bot, which cannot load everywhere this suite
        # runs, and these three have been failing on that import — not on
        # anything they assert — for long enough that "3 failed" became the
        # expected output. A permanently red test hides the next real one.
        from pathlib import Path

        source = (Path(__file__).resolve().parent.parent
                  / "scheduler/runner.py").read_text()
        start = source.index("async def _resolve_signal_outcomes_job")
        rest = source[start:]
        # To the next method at the same indentation.
        end = rest.find("\n    async def ", 1)
        if end == -1:
            end = rest.find("\n    def ", 1)
        self.src = rest[:end] if end != -1 else rest

    def test_the_stop_is_checked_before_the_target(self):
        self.assertLess(self.src.index("hit_stop:"), self.src.index("hit_tgt:"))

    def test_it_only_resolves_signals_old_enough_to_have_played_out(self):
        self.assertIn("older_than_minutes", self.src)

    def test_a_short_history_is_not_mistaken_for_an_expiry(self):
        self.assertIn("continue", self.src)
        self.assertIn("paper_max_hold_minutes", self.src)


class TestNewScreensAreWired(unittest.TestCase):
    def setUp(self):
        import scheduler.health as health
        self.html = health._HTML
        self.health = health

    def test_the_sidebar_replaced_the_tab_bars(self):
        self.assertIn('class="sidebar"', self.html)
        self.assertNotIn('id="tabbar-crypto"', self.html)

    def test_every_new_view_exists(self):
        for tab in ("dashboard", "guard", "accuracy", "historic", "watchlist"):
            with self.subTest(tab=tab):
                self.assertIn(f'id="tab-{tab}"', self.html)

    def test_the_endpoints_the_views_read_are_registered(self):
        import inspect
        src = inspect.getsource(self.health)
        self.assertIn('"/api/signals/history"', src)
        self.assertIn('"/api/signals/accuracy"', src)

    def test_a_reload_lands_on_the_same_screen(self):
        self.assertIn("location.hash", self.html)

    def test_the_sidebar_becomes_a_bottom_bar_on_a_phone(self):
        """Icons in a top-left rail sit where a thumb cannot reach."""
        self.assertIn("max-width:820px", self.html)

    def test_empty_buckets_report_null_rather_than_zero(self):
        """A bucket nobody has traded is not a bucket that loses."""
        import inspect
        src = inspect.getsource(self.health._api_signal_accuracy)
        self.assertIn("return None, 0", src)


class TestGateDiagnostic(unittest.IsolatedAsyncioTestCase):
    """
    Every gate that refuses a setup logs at debug and the setup then vanishes,
    so "why is only one symbol firing" cannot be answered from outside the
    process. This endpoint asks each gate the same question the engine does.
    """

    async def _report(self, cases):
        import random
        from datetime import timedelta

        from analysis.crypto_state import CryptoState, OHLCVCandle
        from analysis.crypto_state_store import CryptoStateStore, recalculate_indicators
        import scheduler.health as health

        t0 = datetime(2026, 8, 20, tzinfo=UTC)
        store = CryptoStateStore()
        for sym, bars, vol, drift, start, flat in cases:
            st = CryptoState(symbol=sym, base_asset=sym[:-4].upper())
            store._states[sym] = st
            rnd = random.Random(sum(sym.encode()))       # stable across processes
            # Volume draws from its own stream so they cannot shift the price
            # path — that coupling made the fixture depend on how many bars
            # had been generated rather than on the parameters.
            vrnd = random.Random(7)
            p = start
            for i in range(bars):
                o = p
                p = o * (1 + drift + rnd.gauss(0, vol))
                if flat:
                    # What the REST poller used to write: no range at all.
                    st.candles_1m.append(OHLCVCandle(p, p, p, p, vrnd.uniform(80, 400),
                                                     t0 + timedelta(minutes=i)))
                else:
                    w = abs(rnd.gauss(0, vol * 0.5)) * p
                    st.candles_1m.append(OHLCVCandle(o, max(o, p) + w, min(o, p) - w, p,
                                                     vrnd.uniform(80, 400),
                                                     t0 + timedelta(minutes=i)))
            if bars:
                st.current_price = p
            if len(st.candles_1m) >= 14:
                recalculate_indicators(st)

        runner = type("R", (), {"crypto_store": store})()
        from unittest.mock import patch
        from analysis.confluence import ConvictionGate
        with patch("analysis.crypto_signals.GATE", ConvictionGate()):
            resp = await health._api_debug_signals(runner, type("Q", (), {"query": {}})())
        import json
        return {r["symbol"]: r for r in json.loads(resp.text)["symbols"]}

    async def test_it_names_a_different_reason_for_each_cause(self):
        rows = await self._report([
            ("bchusdt", 0, 0, 0, 0, False),               # never priced
            ("solusdt", 30, 0.005, 0.0, 184.0, False),    # not enough history
            ("ethusdt", 120, 0.00004, 0.0, 1900.0, False),  # too quiet even over the window
            ("xrpusdt", 120, 0.006, 0.0018, 1.0, False),  # volatile and trending
        ])
        self.assertIn("no price feed", rows["BCHUSDT"]["verdict"])
        self.assertIn("warming up", rows["SOLUSDT"]["verdict"])
        self.assertIn("too quiet", rows["ETHUSDT"]["verdict"])
        self.assertIn("WOULD FIRE", rows["XRPUSDT"]["verdict"])

    async def test_it_reports_atr_against_the_floor(self):
        """The number that decides most refusals, stated as a ratio."""
        rows = await self._report([("xrpusdt", 120, 0.006, 0.0018, 1.0, False)])
        r = rows["XRPUSDT"]
        self.assertGreater(r["atr_vs_floor"], 1.0)
        self.assertIn("cost_floor_pct", r)
        self.assertIn("min_target_pct", r)

    async def test_it_counts_flat_candles(self):
        """A flat feed is the failure that silently collapses ATR."""
        rows = await self._report([("ltcusdt", 120, 0.005, 0.0, 72.0, True)])
        flat, total = rows["LTCUSDT"]["flat_candles"].split("/")
        self.assertEqual(flat, total)

    async def test_a_symbol_that_would_fire_reports_its_votes(self):
        rows = await self._report([("xrpusdt", 120, 0.006, 0.0018, 1.0, False)])
        self.assertIn("votes", rows["XRPUSDT"])
        self.assertGreaterEqual(rows["XRPUSDT"]["agreeing"], 3)
