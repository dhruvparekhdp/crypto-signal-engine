"""
Grading the grader.

The AI reviewer now costs a signal 0.15 of confidence when it says REJECT,
which is enough to stop most setups. Nothing was checking whether it was
right, and the obvious check does not work: a filter measured only on the
trades it allowed through cannot be wrong, because every rejection it got
wrong has already been deleted.

So a suppressed signal is still written to crypto_signal_log, carrying the
name of the filter that stopped it, and the ordinary outcome resolver scores
it against what price actually did. The rejections become countable, and the
reviewer can be shown to be wrong.

These tests build the case that matters: a reviewer whose REJECTs would have
won. If the scorecard cannot surface that, it is decoration.
"""
import asyncio
import unittest

from storage.database import AsyncSessionFactory, init_db
from storage.repository import Repository


def _run(coro):
    return asyncio.run(coro)


class TestSuppressedSignalsAreStillScored(unittest.TestCase):
    def test_a_shadow_is_logged_and_kept_out_of_published_stats(self):
        async def go():
            await init_db()
            async with AsyncSessionFactory() as s:
                repo = Repository(s)
                fired = await repo.log_crypto_signal(
                    symbol="btcusdt", signal_type="confluence", direction="long",
                    trigger_description="t", confidence=0.8, current_price=100.0,
                    target_price=101.0, stop_loss=99.5, edge_pct=1.0,
                    stake_pct=0.01, timeframe="20m")
                shadow = await repo.log_crypto_signal(
                    symbol="ethusdt", signal_type="volume_spike", direction="short",
                    trigger_description="t", confidence=0.58, current_price=100.0,
                    target_price=99.0, stop_loss=100.5, edge_pct=1.0,
                    stake_pct=0.01, timeframe="20m", suppressed_by="ai_review")

                published = await repo.crypto_signals_between(7)
                everything = await repo.crypto_signals_between(
                    7, include_suppressed=True)
                ids_pub = {r.id for r in published}
                ids_all = {r.id for r in everything}
                return fired, shadow, ids_pub, ids_all

        fired, shadow, ids_pub, ids_all = _run(go())
        self.assertIn(fired, ids_pub)
        self.assertNotIn(shadow, ids_pub, "a suppressed signal never fired and "
                                          "must not count as published")
        self.assertIn(shadow, ids_all, "but it must still be retrievable, or "
                                       "the cost of blocking it is unknowable")

    def test_the_resolver_still_picks_up_shadows(self):
        """If shadows are never resolved the scorecard has nothing to read."""
        async def go():
            await init_db()
            async with AsyncSessionFactory() as s:
                repo = Repository(s)
                await repo.log_crypto_signal(
                    symbol="solusdt", signal_type="confluence", direction="long",
                    trigger_description="t", confidence=0.5, current_price=100.0,
                    target_price=101.0, stop_loss=99.0, edge_pct=1.0,
                    stake_pct=0.01, timeframe="20m", suppressed_by="ai_review")
                pending = await repo.pending_crypto_signals(older_than_minutes=0)
                return [r.symbol for r in pending]

        self.assertIn("solusdt", _run(go()))


class TestScorecardCatchesABadReviewer(unittest.TestCase):
    def test_a_reviewer_whose_rejects_would_have_won_is_visible(self):
        """
        The whole point. Three REJECTs that went on to win, three APPROVEs that
        lost — the scorecard has to say so plainly, or it is flattering the
        thing it exists to check.
        """
        plan = [("REJECT", "won", 1.4), ("REJECT", "won", 1.1),
                ("REJECT", "won", 0.9), ("APPROVE", "lost", -0.6),
                ("APPROVE", "lost", -0.8), ("APPROVE", "won", 1.0)]

        async def go():
            await init_db()
            async with AsyncSessionFactory() as s:
                repo = Repository(s)
                for i, (verdict, outcome, pnl) in enumerate(plan):
                    sym = f"score{i}usdt"
                    sid = await repo.log_crypto_signal(
                        symbol=sym, signal_type="confluence", direction="long",
                        trigger_description="t", confidence=0.72,
                        current_price=100.0, target_price=101.0, stop_loss=99.0,
                        edge_pct=1.0, stake_pct=0.01, timeframe="20m",
                        suppressed_by="ai_review" if verdict == "REJECT" else "")
                    await repo.resolve_crypto_signal(sid, outcome, pnl)
                    await repo.save_review(
                        "pre", sym, signal_type="confluence", verdict=verdict,
                        summary="", factors="", confidence_delta=0.0)
                return await repo.reviewer_scorecard(days=1)

        card = _run(go())
        v = card["verdicts"]
        self.assertIn("REJECT", v)
        self.assertIn("APPROVE", v)
        self.assertEqual(v["REJECT"]["win_rate_pct"], 100.0,
                         "every rejected signal won — the scorecard must say so")
        self.assertLess(v["APPROVE"]["win_rate_pct"], 50.0)
        self.assertGreater(v["REJECT"]["win_rate_pct"], v["APPROVE"]["win_rate_pct"],
                           "a reviewer this wrong has to be visibly wrong")
        self.assertEqual(v["REJECT"]["suppressed"], v["REJECT"]["resolved"],
                         "rejected signals are shadows by definition")

    def test_the_shape_holds_whatever_the_history(self):
        """
        Called on a fresh install with nothing to grade, this must return an
        answer rather than raise — the audit page asks for it unconditionally.
        """
        async def go():
            await init_db()
            async with AsyncSessionFactory() as s:
                return await Repository(s).reviewer_scorecard(days=1)
        card = _run(go())
        self.assertEqual(set(card), {"verdicts", "reviewed", "resolved"})
        self.assertIsInstance(card["verdicts"], dict)
        self.assertGreaterEqual(card["reviewed"], 0)


if __name__ == "__main__":
    unittest.main()
