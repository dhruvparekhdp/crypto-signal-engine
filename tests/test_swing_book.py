"""Swing book: live code must trade exactly what the 5-year backtest validated."""
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from analysis import swing_book as sb
from analysis.lab.data import Bars
from analysis.lab.simulate import ExitModel
from analysis.lab.strategies import REGISTRY

LAKE = Path("data/lake/um/klines/BTCUSDT/1m")


class TestSpecs(unittest.TestCase):
    def test_default_specs_are_the_validated_4h_and_8h_sets_in_priority_order(self):
        specs = sb.parse_specs(sb.DEFAULT_SPECS)
        self.assertEqual([(tf, s) for tf, s, _ in specs[:4]],
                         [("4h", "vol_breakout"), ("4h", "keltner_break"), ("4h", "donchian"), ("4h", "ichimoku")])
        self.assertEqual([(tf, s) for tf, s, _ in specs[4:]],
                         [("8h", "keltner_break"), ("8h", "vol_breakout"), ("8h", "donchian"), ("8h", "ichimoku")])
        self.assertEqual(specs[0][2], {"z": 3.0})
        self.assertEqual(specs[4][2], {"k": 2.0})
        self.assertEqual(sb.timeframes(specs), ["4h", "8h"])
        self.assertNotIn("12h", sb.timeframes(specs))          # Ichimoku failed its 12h null test

    def test_unknown_strategies_are_ignored_and_bare_specs_are_4h(self):
        self.assertEqual(sb.parse_specs("nope,donchian:n=55,9q@ichimoku"), [("4h", "donchian", {"n": 55})])

    def test_settings_default_matches(self):
        from config.settings import Settings
        self.assertEqual(Settings.model_fields["swing_strategies"].default, sb.DEFAULT_SPECS)
        self.assertEqual(Settings.model_fields["paper_open_families"].default, "swing")
        self.assertFalse(Settings.model_fields["smart_60m_enabled"].default)
        self.assertEqual(Settings.model_fields["swing_hold_minutes"].default, 10080)


class TestKlines(unittest.TestCase):
    def test_the_forming_bar_is_dropped(self):
        t0 = 1_700_000_000_000 - (1_700_000_000_000 % sb.TF_MS)
        rows = [[t0 + i * sb.TF_MS, "1", "2", "0.5", "1.5", "10", 0, 0, 0, "5"] for i in range(200)]
        now = t0 + 199 * sb.TF_MS + 1000              # the last bar has just started
        b = sb.bars_from_klines("btcusdt", rows, now)
        self.assertEqual(len(b), 199)
        b8 = sb.bars_from_klines("btcusdt", [[t0 + i * 2 * sb.TF_MS, "1", "2", "0.5", "1.5", "10", 0, 0, 0, "5"] for i in range(200)],
                                 t0 + 199 * 2 * sb.TF_MS + 1000, "8h")
        self.assertEqual((len(b8), b8.interval), (199, "8h"))
        self.assertEqual(int(b.t[-1]), t0 + 198 * sb.TF_MS)
        self.assertEqual(b.symbol, "BTCUSDT")
        self.assertEqual(float(b.tb[0]), 5.0)          # taker-buy volume is column 9

    def test_too_little_history_returns_none(self):
        rows = [[i * sb.TF_MS, "1", "1", "1", "1", "1", 0, 0, 0, "1"] for i in range(100)]
        self.assertIsNone(sb.bars_from_klines("x", rows, 10**15))


class TestParityWithBacktest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not LAKE.exists():
            raise unittest.SkipTest("lake not available")

    def test_live_window_signals_equal_backtest_signals_bar_for_bar(self):
        from analysis.lab.data import load_bars
        specs = sb.parse_specs(sb.DEFAULT_SPECS)
        for tf in ("4h", "8h"):
            mine = [(t, sid, p) for t, sid, p in specs if t == tf]
            for sym in ("BTCUSDT", "SOLUSDT"):
                b = load_bars(sym, tf)
                full = {sid: REGISTRY[sid].signals(b, p) for _, sid, p in mine}
                n = len(b)
                mismatches = fired = 0
                for i in range(n - 250, n):
                    w = Bars(sym, tf, b.t[i - 499:i + 1], b.o[i - 499:i + 1], b.h[i - 499:i + 1], b.l[i - 499:i + 1],
                             b.c[i - 499:i + 1], b.v[i - 499:i + 1], b.tb[i - 499:i + 1])
                    live = sb.evaluate(w, specs)
                    expect = next(((sid, int(full[sid][i])) for _, sid, _ in mine if full[sid][i] != 0), None)
                    got = (live.strategy, live.side) if live else None
                    mismatches += int(got != expect)
                    fired += int(expect is not None)
                self.assertEqual(mismatches, 0, f"{sym} {tf}")
                self.assertGreater(fired, 0, f"{sym} {tf}")


class TestLevels(unittest.TestCase):
    def test_stop_and_target_match_the_backtest_exit_model(self):
        ex = ExitModel(stop_atr=3.0, rr=3.0, max_hold_min=10080)
        self.assertEqual((sb.STOP_ATR, sb.REWARD_RISK, sb.MIN_STOP_PCT, sb.MAX_STOP_PCT),
                         (ex.stop_atr, ex.rr, ex.min_stop_pct, ex.max_stop_pct))
        stop, target = sb.levels(100.0, +1, 2.0)
        self.assertAlmostEqual(stop, 94.0)
        self.assertAlmostEqual(target, 118.0)
        stop, target = sb.levels(100.0, -1, 2.0)
        self.assertAlmostEqual(stop, 106.0)
        self.assertAlmostEqual(target, 82.0)

    def test_tiny_atr_uses_the_minimum_stop_and_huge_atr_is_refused(self):
        stop, _ = sb.levels(100.0, +1, 0.01)
        self.assertAlmostEqual(stop, 99.7)
        self.assertIsNone(sb.levels(100.0, +1, 5.0))     # 15% stop: wider than the 8% the test allowed

    def test_signal_carries_swing_mode_and_levels(self):
        setup = sb.SwingSetup("BTCUSDT", "keltner_break", -1, 0, 1000.0, 60000.0)
        sig = sb.to_signal(setup, 60000.0, datetime(2026, 10, 5, tzinfo=UTC))
        self.assertEqual((sig.trade_mode, sig.signal_type, sig.direction, sig.timeframe), ("swing", "swing_keltner_break", "short", "4h"))
        self.assertAlmostEqual(sig.stop_loss, 63000.0)
        self.assertAlmostEqual(sig.target_price, 51000.0)


class TestSizing(unittest.TestCase):
    def test_a_stop_out_costs_exactly_the_risk_share(self):
        margin, lev = sb.size(wallet=2000, free=2000, risk_pct=0.01, entry=100, stop=94, costs=0.002, max_leverage=3)
        loss = margin * lev * (0.06 + 0.002)
        self.assertAlmostEqual(loss, 20.0, places=6)
        self.assertEqual(lev, 1.0)                     # plenty of free margin: no leverage needed

    def test_leverage_rises_only_when_free_margin_is_short_and_is_capped(self):
        margin, lev = sb.size(wallet=2000, free=200, risk_pct=0.01, entry=100, stop=99, costs=0.002, max_leverage=3)
        self.assertLessEqual(lev, 3.0)
        self.assertLessEqual(margin, 180.0 + 1e-9)

    def test_nothing_to_fund(self):
        self.assertIsNone(sb.size(2000, 0, 0.01, 100, 94, 0.002, 3))

    def test_adaptive_risk_cuts_in_drawdowns_and_streaks(self):
        self.assertAlmostEqual(sb.adaptive_risk(0.01, 0.0, 0), 0.01)
        self.assertAlmostEqual(sb.adaptive_risk(0.01, 0.12, 0), 0.0075)
        self.assertAlmostEqual(sb.adaptive_risk(0.01, 0.25, 0), 0.005)
        self.assertAlmostEqual(sb.adaptive_risk(0.01, 0.25, 3), 0.0025)
        self.assertAlmostEqual(sb.adaptive_risk(0.01, 0.0, 3), 0.005)

    def test_book_state_reads_drawdown_and_streak_from_its_own_trades(self):
        T = lambda pnl, w: SimpleNamespace(net_pnl=pnl, wallet_after=w)
        dd, streak = sb.book_state([T(100, 2100), T(-50, 2050), T(-50, 2000)])
        self.assertEqual(streak, 2)
        self.assertAlmostEqual(dd, 100 / 2100)
        self.assertEqual(sb.book_state([]), (0.0, 0))


class TestEngineRules(unittest.TestCase):
    def test_old_families_are_shadow_only_by_default(self):
        from scheduler.runner import _family_may_trade
        self.assertTrue(_family_may_trade(SimpleNamespace(signal_type="swing_donchian")))
        self.assertFalse(_family_may_trade(SimpleNamespace(signal_type="confluence")))
        self.assertFalse(_family_may_trade(SimpleNamespace(signal_type="volume_spike")))

    def test_a_swing_positions_stop_is_never_moved_by_lock_trail_or_ladder(self):
        from analysis.paper_cycle import resolve_at_price
        from analysis.paper_trading import Side
        pos = SimpleNamespace(trade_mode="swing", symbol="btcusdt", side=Side.LONG, stop_price=94.0, target_price=118.0)
        called = []
        import analysis.paper_cycle as pc
        orig = pc.resolve_candle
        pc.resolve_candle = lambda *a, **k: None
        try:
            for m in ("apply_ladder", "update_trail", "apply_profit_lock"):
                setattr(pos, m, lambda *a, _m=m, **k: called.append(_m))
            out = resolve_at_price(pos, 110.0, datetime(2026, 10, 5, tzinfo=UTC), SimpleNamespace(slippage=None), 1000.0,
                                   lock=object())
        finally:
            pc.resolve_candle = orig
        self.assertIsNone(out)
        self.assertEqual(called, [])
        self.assertEqual(pos.stop_price, 94.0)


if __name__ == "__main__":
    unittest.main()
