"""
The process-local cache in place of Redis (27 Sep): one Python dict, no
new service — see scheduler/cache.py's own docstring for why. These test
the cache module itself, plus that /api/paper never caches the one thing
that must always be live: the mark price.
"""
from __future__ import annotations

import asyncio
import unittest


class TestCachedHelper(unittest.TestCase):
    def setUp(self):
        from scheduler import cache
        cache.clear()

    def test_a_second_call_within_ttl_does_not_call_fetch_again(self):
        from scheduler import cache
        calls = []

        async def fetch():
            calls.append(1)
            return {"n": len(calls)}

        async def go():
            a = await cache.cached("k", 10.0, fetch)
            b = await cache.cached("k", 10.0, fetch)
            return a, b
        a, b = asyncio.run(go())
        self.assertEqual(len(calls), 1)
        self.assertIs(a, b)

    def test_a_call_after_the_ttl_expires_fetches_again(self):
        from scheduler import cache
        calls = []

        async def fetch():
            calls.append(1)
            return len(calls)

        async def go():
            first = await cache.cached("k", 0.05, fetch)
            await asyncio.sleep(0.08)
            second = await cache.cached("k", 0.05, fetch)
            return first, second
        first, second = asyncio.run(go())
        self.assertEqual((first, second), (1, 2))

    def test_different_keys_never_collide(self):
        from scheduler import cache

        async def go():
            a = await cache.cached("a", 10.0, lambda: _const("A"))
            b = await cache.cached("b", 10.0, lambda: _const("B"))
            return a, b
        a, b = asyncio.run(go())
        self.assertEqual((a, b), ("A", "B"))

    def test_invalidate_forces_the_next_call_to_fetch(self):
        from scheduler import cache
        calls = []

        async def fetch():
            calls.append(1)
            return len(calls)

        async def go():
            first = await cache.cached("k", 10.0, fetch)
            cache.invalidate("k")
            second = await cache.cached("k", 10.0, fetch)
            return first, second
        first, second = asyncio.run(go())
        self.assertEqual((first, second), (1, 2))

    def test_invalidating_an_unrelated_key_is_a_no_op(self):
        from scheduler import cache
        calls = []

        async def fetch():
            calls.append(1)
            return len(calls)

        async def go():
            first = await cache.cached("k", 10.0, fetch)
            cache.invalidate("some_other_key")
            second = await cache.cached("k", 10.0, fetch)
            return first, second
        first, second = asyncio.run(go())
        self.assertEqual((first, second), (1, 1))

    def test_stats_reports_age_in_seconds_not_the_cached_value(self):
        from scheduler import cache

        async def go():
            await cache.cached("k", 10.0, lambda: _const({"secret": "value"}))
        asyncio.run(go())
        stats = cache.stats()
        self.assertIn("k", stats)
        self.assertIsInstance(stats["k"], float)
        self.assertNotIn("secret", str(stats))


async def _const(v):
    return v


class TestPaperNeverCachesMarkPrice(unittest.IsolatedAsyncioTestCase):
    """
    /api/paper's own cache (_paper_db_snapshot) covers the database reads —
    cycle, positions, trades. It must never cover the mark price or
    anything computed from it: a cached unrealised P&L would be reporting
    what the market was doing several seconds ago as if it were now.
    """

    async def asyncSetUp(self):
        from scheduler import cache
        cache.clear()

    async def test_a_stale_cached_snapshot_still_reports_the_live_price(self):
        import types
        from unittest.mock import AsyncMock, patch

        from aiohttp.test_utils import make_mocked_request

        import scheduler.health as health

        row = types.SimpleNamespace(
            symbol="btcusdt", side="long", coin_qty=0.01, entry_price=50000.0,
            margin=500.0, stop_price=49000.0, target_price=52000.0, liq_price=45000.0,
            trail_active=False, confidence=0.75, signal_type="confluence",
            opened_at=None, expires_at=None, usdt_inr=90.0, id=1)
        cycle = types.SimpleNamespace(
            id=1, wallet=1000.0, starting_wallet=1000.0, target_wallet=20000.0,
            peak_wallet=1000.0, leverage=10.0, stop_pct_of_margin=0.2, reward_risk=2.0,
            min_confidence=0.7, trailing_enabled=False, scaled_sizing=True,
            started_at=None, ended_at=None, status="running")
        snapshot = {"cycle": cycle, "rows": [row], "trades": [], "running": [cycle]}

        with patch("scheduler.health._paper_db_snapshot", AsyncMock(return_value=snapshot)), \
             patch("analysis.paper_cycle.config_for_cycle", return_value=types.SimpleNamespace()), \
             patch("analysis.paper_cycle.summarise", return_value={}):
            state = types.SimpleNamespace(symbol="btcusdt", current_price=51000.0)
            runner = types.SimpleNamespace(
                crypto_store=types.SimpleNamespace(get_all=AsyncMock(return_value=[state])))
            resp1 = await health._api_paper(runner, make_mocked_request("GET", "/"))
            import json
            mark1 = json.loads(resp1.text)["positions"][0]["mark"]
            self.assertEqual(mark1, 51000.0)

            # The price moves. The (mocked, still "cached") DB snapshot is
            # identical — only the live price changed — and the response
            # must move with it, not repeat the old mark.
            state.current_price = 55000.0
            resp2 = await health._api_paper(runner, make_mocked_request("GET", "/"))
            mark2 = json.loads(resp2.text)["positions"][0]["mark"]
            self.assertEqual(mark2, 55000.0)


if __name__ == "__main__":
    unittest.main()
