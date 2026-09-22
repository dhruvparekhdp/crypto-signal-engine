"""
Two cycles running at once, and a trade that vanished.

Reported 22 Sep 22:39. Telegram announced an ETHUSDT stop-out at 22:20
leaving the wallet at 2,281.29; the page showed 2,194.11, a realised figure
frozen for over an hour, and no such trade in any of its seven rows. The
engine had also reported starting twice, a minute apart, after a deploy.

_ensure_cycle checks for a running cycle and creates one with nothing held
in between. Two workers — two processes, when a restart leaves the old one
alive — can both see none and both start one. Trades then land on whichever
cycle their own worker holds, while the dashboard reads
`ORDER BY id DESC LIMIT 1` and renders the other. Nothing errors. The page
is simply describing a different cycle than the one Telegram is reporting.

The oldest cycle survives a cleanup because it owns the earlier trades.
"""
import asyncio
import unittest

from storage.database import AsyncSessionFactory, init_db
from storage.repository import Repository


def _run(coro):
    return asyncio.run(coro)


async def _new_cycle(repo, wallet=3000.0):
    return await repo.start_cycle(
        starting_wallet=wallet, target_wallet=20000.0, leverage=10.0,
        stop_pct_of_margin=0.20, reward_risk=2.0, min_confidence=0.70,
        trailing_enabled=True, scaled_sizing=True)


class TestDuplicateCycles(unittest.TestCase):
    def test_the_newest_wins_the_dashboard_which_is_how_a_trade_hides(self):
        """Reproduces the reported symptom before asserting the fix."""
        async def go():
            await init_db()
            async with AsyncSessionFactory() as s:
                repo = Repository(s)
                for c in await repo.running_cycles():
                    c.status = "closed"
                await s.commit()
                first = await _new_cycle(repo)
                second = await _new_cycle(repo)
                shown = await repo.get_running_cycle()
                return first.id, second.id, shown.id
        first, second, shown = _run(go())
        self.assertGreater(second, first)
        self.assertEqual(shown, second,
                         "the page follows the newest, so the older cycle's "
                         "trades are invisible while its worker keeps trading")

    def test_the_duplicate_is_retired_and_the_older_one_kept(self):
        async def go():
            await init_db()
            async with AsyncSessionFactory() as s:
                repo = Repository(s)
                for c in await repo.running_cycles():
                    c.status = "closed"
                await s.commit()
                first = await _new_cycle(repo)
                await _new_cycle(repo)
                closed = await repo.close_duplicate_cycles()
                remaining = await repo.running_cycles()
                return first.id, closed, [c.id for c in remaining]
        first, closed, remaining = _run(go())
        self.assertEqual(closed, 1)
        self.assertEqual(remaining, [first],
                         "the oldest owns the earlier trades and must survive")

    def test_cleaning_up_when_there_is_nothing_to_clean_is_a_no_op(self):
        async def go():
            await init_db()
            async with AsyncSessionFactory() as s:
                repo = Repository(s)
                for c in await repo.running_cycles():
                    c.status = "closed"
                await s.commit()
                await _new_cycle(repo)
                return await repo.close_duplicate_cycles()
        self.assertEqual(_run(go()), 0)

    def test_the_page_is_told_which_cycles_are_running(self):
        """Silence is what made this take an hour and three screenshots."""
        from scheduler.health import _HTML
        self.assertIn("running_cycles", _HTML)
        self.assertIn("Two paper cycles are running", _HTML)


if __name__ == "__main__":
    unittest.main()
