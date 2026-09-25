"""
The confirmed bugs from the 25 Sep code review, each pinned so it cannot come
back quietly.
"""
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
RUNNER = ROOT.joinpath("scheduler", "runner.py").read_text()


class TestTrailOverrideSurvives(unittest.TestCase):
    """The reviewer's trail was set, then lost when the row was reloaded."""

    def test_it_is_a_column(self):
        from storage.models import PaperPosition
        cols = PaperPosition.__table__.columns.keys()
        self.assertIn("trail_r_override", cols)
        self.assertIn("locked_roe", cols)

    def test_it_is_written_and_read_back(self):
        import inspect

        from storage.repository import Repository
        self.assertIn("row.trail_r_override = pos.trail_r_override",
                      inspect.getsource(Repository.sync_position))
        self.assertIn('pos.trail_r_override = getattr(row, "trail_r_override", None)', RUNNER)

    def test_it_applies_on_the_same_tick(self):
        after = RUNNER[RUNNER.index("pos.trail_r_override = trail_r_for_confidence"):]
        self.assertLess(after.index("pos.update_trail("), after.index("return None"))


class TestTheTickIsAtomic(unittest.TestCase):
    def test_closes_and_opens_move_the_wallet_in_the_same_commit(self):
        self.assertIn("close_position_atomic(cycle.id, row.id, trade, wallet)", RUNNER)
        self.assertIn("open_position_atomic(cycle.id, pos, wallet)", RUNNER)
        self.assertNotIn("await repo.record_trade(cycle.id, trade)", RUNNER)


class TestLossBudgetIncludesCosts(unittest.TestCase):
    def test_costs_lower_the_leverage(self):
        from analysis.paper_cycle import CycleConfig
        cfg = CycleConfig()
        bare = cfg.leverage_for_stop(10, 100.0, 99.0)
        costed = cfg.leverage_for_stop(10, 100.0, 99.0, costs_pct=0.002)
        self.assertAlmostEqual(bare, 0.04 / 0.01)
        self.assertAlmostEqual(costed, 0.04 / 0.012)

    def test_stop_out_costs_are_fee_spread_and_stop_slippage(self):
        from analysis.paper_cycle import CycleConfig, fees_for, stop_out_costs
        cfg = CycleConfig()
        want = (fees_for("btcusdt").round_trip_pct() + 2 * cfg.slippage.spread_pct
                + cfg.slippage.stop_extra_pct)
        self.assertAlmostEqual(stop_out_costs("btcusdt", cfg), want)


class TestQueuedSignalsAreRepriced(unittest.TestCase):
    NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)

    def sig(self, **kw):
        base = dict(symbol="btcusdt", direction="long", current_price=100.0,
                    target_price=102.0, stop_loss=99.0, timestamp=self.NOW - timedelta(seconds=20))
        base.update(kw)
        return SimpleNamespace(**base)

    def test_fills_at_the_live_price(self):
        from analysis.paper_cycle import reprice_signal
        got, why = reprice_signal(self.sig(), 100.4, self.NOW)
        self.assertEqual((got.current_price, why), (100.4, "ok"))

    def test_stale_target_passed_stop_passed_and_chased_are_dropped(self):
        from analysis.paper_cycle import reprice_signal
        self.assertEqual(reprice_signal(self.sig(timestamp=self.NOW - timedelta(minutes=5)),
                                        100.1, self.NOW)[1], "stale")
        self.assertEqual(reprice_signal(self.sig(), 102.5, self.NOW)[1], "target_passed")
        self.assertEqual(reprice_signal(self.sig(), 98.9, self.NOW)[1], "stop_passed")
        self.assertEqual(reprice_signal(self.sig(), 101.3, self.NOW)[1], "chased")

    def test_shorts_read_every_level_the_other_way(self):
        from analysis.paper_cycle import reprice_signal
        s = self.sig(direction="short", target_price=98.0, stop_loss=101.0)
        self.assertEqual(reprice_signal(s, 97.5, self.NOW)[1], "target_passed")
        self.assertEqual(reprice_signal(s, 99.7, self.NOW)[1], "ok")


class TestIndicators(unittest.TestCase):
    def test_macd_signal_and_histogram_are_computed(self):
        from analysis.crypto_state_store import _macd
        closes = [100 + i * 0.1 + (i % 7) * 0.05 for i in range(120)]
        line, signal, hist = _macd(closes)
        self.assertNotEqual(signal, 0.0)
        self.assertAlmostEqual(hist, line - signal)

    def test_rsi_divergence_is_off_until_it_passes_the_null_test(self):
        from config.settings import settings
        self.assertFalse(settings.rsi_divergence_enabled)


class TestDroppedSignalsAreForgotten(unittest.TestCase):
    def test_forget_clears_live_and_cooldown(self):
        from analysis.crypto_engine import CryptoEngine
        eng = CryptoEngine()
        sig = SimpleNamespace(symbol="btcusdt", direction="long", signal_type="confluence")
        eng._live[("btcusdt", "long")] = sig
        eng._cooldowns[("btcusdt", "confluence")] = datetime.now(UTC)
        eng.forget(sig)
        self.assertNotIn(("btcusdt", "long"), eng._live)
        self.assertNotIn(("btcusdt", "confluence"), eng._cooldowns)

    def test_both_drop_points_call_it(self):
        self.assertEqual(RUNNER.count("self.crypto_engine.forget(sig)"), 2)


class TestSmallerFixes(unittest.TestCase):
    def test_the_table_viewer_escapes_quotes(self):
        import scheduler.health as h
        self.assertIn("'\"':'&quot;'", h._DATA_HTML)

    def test_backfill_never_stores_the_forming_bar_and_retries_rate_limits(self):
        src = ROOT.joinpath("collectors", "binance_history.py").read_text()
        self.assertIn('c["open_time"] + step <= now', src)
        self.assertIn("(418, 429)", src)

    def test_no_provider_is_not_an_outage(self):
        import inspect

        from analysis.position_review import review_position
        self.assertIn('if not chain_for("position_review")', inspect.getsource(review_position))


if __name__ == "__main__":
    unittest.main()
