"""Strategy Lab engine: fills, costs, wallet cycles, no look-ahead, no AI."""
import subprocess
import sys
import unittest

import numpy as np
import pandas as pd

from analysis.lab.costs import CostModel
from analysis.lab.data import INTERVAL_MS, Bars
from analysis.lab.simulate import ExitModel, simulate_symbol
from analysis.lab.wallet import WalletConfig, cycle_odds, run_wallet

NO_COST = CostModel(taker_fee=0, maker_fee=0, gst=0, slip_bps=0, stop_slip_bps=0, funding=False)
MIN = INTERVAL_MS["1m"]


def bars(rows, symbol="TESTUSDT", interval="1m"):
    """rows: (open, high, low, close) per bar."""
    a = np.array(rows, float)
    n = len(a)
    return Bars(symbol, interval, np.arange(n, dtype=np.int64) * INTERVAL_MS[interval] + 1_700_000_000_000,
                a[:, 0], a[:, 1], a[:, 2], a[:, 3], np.full(n, 100.0), np.full(n, 50.0))


def flat(n, px=100.0):
    return [(px, px + 0.05, px - 0.05, px)] * n


def run(rows, side=1, exit=None, cost=NO_COST, sig_at=60):
    b = bars(rows)
    sig = np.zeros(len(b), np.int8)
    sig[sig_at] = side
    # ATR of flat 0.1-range bars is 0.1; use a wide stop multiple so stop distance is ~1%
    ex = exit or ExitModel(stop_atr=10.0, rr=2.0, min_stop_pct=0.0, max_stop_pct=1.0)
    return simulate_symbol(b, sig, b, ex, cost)


class TestFills(unittest.TestCase):
    def test_entry_is_next_bar_open_not_signal_close(self):
        rows = flat(61) + [(101.0, 101.2, 100.9, 101.1)] + flat(10, 101.0)
        t = run(rows)[0]
        self.assertAlmostEqual(t[4], 101.0)          # entry price = open of bar 61

    def test_stop_hit_gives_minus_one_r_without_costs(self):
        rows = flat(61) + [(100.0, 100.05, 100.0, 100.0)] + [(100.0, 100.0, 98.0, 98.5)] + flat(5, 98.5)
        t = run(rows)[0]
        self.assertEqual(t[16], "stop")
        self.assertAlmostEqual(t[11], -1.0, places=2)

    def test_target_hit_gives_plus_rr(self):
        rows = flat(61) + [(100.0, 100.05, 100.0, 100.0)] + [(100.0, 103.0, 99.99, 102.9)] + flat(5, 102.9)
        t = run(rows)[0]
        self.assertEqual(t[16], "target")
        self.assertAlmostEqual(t[11], 2.0, places=2)

    def test_same_bar_stop_and_target_resolves_to_the_stop(self):
        rows = flat(61) + [(100.0, 100.05, 100.0, 100.0)] + [(100.0, 105.0, 95.0, 100.0)] + flat(5)
        self.assertEqual(run(rows)[0][16], "stop")
        opt = ExitModel(stop_atr=10.0, rr=2.0, min_stop_pct=0.0, max_stop_pct=1.0, optimistic_ties=True)
        self.assertEqual(run(rows, exit=opt)[0][16], "target")

    def test_gap_through_the_stop_fills_at_the_open(self):
        rows = flat(61) + [(100.0, 100.05, 100.0, 100.0)] + [(95.0, 95.5, 94.0, 94.5)] + flat(5, 94.5)
        t = run(rows)[0]
        self.assertAlmostEqual(t[5], 95.0)
        self.assertLess(t[11], -3.0)                  # worse than -1R: the gap cost extra

    def test_short_mirrors_long(self):
        rows = flat(61) + [(100.0, 100.05, 100.0, 100.0)] + [(100.0, 103.0, 99.99, 102.9)] + flat(5, 102.9)
        t = run(rows, side=-1)[0]
        self.assertEqual(t[16], "stop")

    def test_costs_reduce_net_return(self):
        rows = flat(61) + [(100.0, 100.05, 100.0, 100.0)] + [(100.0, 103.0, 99.99, 102.9)] + flat(5, 102.9)
        free = run(rows)[0]
        paid = run(rows, cost=CostModel(funding=False))[0]
        self.assertLess(paid[10], free[10])
        self.assertGreater(paid[8], 0)

    def test_breakeven_stop_is_booked_as_breakeven_not_a_stop(self):
        ex = ExitModel(stop_atr=10.0, rr=5.0, min_stop_pct=0.0, max_stop_pct=1.0, be_trigger_r=1.0)
        rows = flat(61) + [(100.0, 100.05, 100.0, 100.0)] + [(100.0, 102.0, 99.99, 101.9)] + [(101.9, 101.9, 99.0, 99.5)] + flat(5, 99.5)
        t = run(rows, exit=ex)[0]
        self.assertEqual(t[16], "breakeven")
        self.assertAlmostEqual(t[11], 0.0, places=1)

    def test_breakeven_does_not_apply_inside_the_bar_that_armed_it(self):
        ex = ExitModel(stop_atr=10.0, rr=5.0, min_stop_pct=0.0, max_stop_pct=1.0, be_trigger_r=1.0)
        # one bar spikes to +1R and also dips to the entry: the stop must still be the original one
        rows = flat(61) + [(100.0, 100.05, 100.0, 100.0)] + [(100.0, 102.0, 99.5, 101.0)] + flat(5, 101.0)
        t = run(rows, exit=ex)[0]
        self.assertNotIn(t[16], ("breakeven", "stop"))

    def test_one_open_trade_per_symbol(self):
        b = bars(flat(300))
        sig = np.zeros(300, np.int8)
        sig[[60, 61, 62]] = 1
        out = simulate_symbol(b, sig, b, ExitModel(stop_atr=10.0, max_stop_pct=1.0, min_stop_pct=0.0, max_hold_min=100), NO_COST)
        self.assertEqual(len(out), 1)


class TestWallet(unittest.TestCase):
    def tr(self, rets, sf=0.01, gap_ms=60_000 * 10):
        n = len(rets)
        return pd.DataFrame({"entry_t": np.arange(n) * gap_ms, "exit_t": np.arange(n) * gap_ms + 60_000,
                             "symbol": "A", "side": 1, "net_ret": rets, "stop_frac": sf, "mae": 0.0,
                             "fee_frac": 0.0, "reason": "target"})

    def test_target_resets_wallet_to_start(self):
        cfg = WalletConfig(sizing="margin_pct", margin_pct=1.0, leverage=2.0, min_notional=1.0, max_margin_use=1.0)
        res = run_wallet(self.tr([0.5] * 4), cfg)           # +100% per trade at 2x: 25->50->100
        self.assertEqual(res.cycles[0]["status"], "TARGET")
        self.assertEqual(res.cycles[0]["trades"], 2)
        self.assertEqual(res.cycles[1]["start"], 25.0)

    def test_bust_resets_wallet(self):
        cfg = WalletConfig(sizing="margin_pct", margin_pct=1.0, leverage=2.0, min_notional=1.0, max_margin_use=1.0)
        res = run_wallet(self.tr([-0.5] * 3), cfg)
        self.assertEqual(res.cycles[0]["status"], "BUST")
        self.assertEqual(res.cycles[1]["start"], 25.0)

    def test_risk_sizing_loses_exactly_the_risk_at_the_stop(self):
        cfg = WalletConfig(risk_pct=0.02, leverage=10.0)
        res = run_wallet(self.tr([-0.01]), cfg)             # a -1R trade
        self.assertAlmostEqual(res.ledger[0]["pnl"], -0.5, places=6)   # 2% of 25

    def test_liquidation_loses_the_margin(self):
        cfg = WalletConfig(sizing="margin_pct", margin_pct=0.5, leverage=25.0)
        df = self.tr([-0.05], sf=0.05)
        df["mae"] = 0.06
        res = run_wallet(df, cfg)
        self.assertTrue(res.ledger[0]["liquidated"])
        self.assertAlmostEqual(res.ledger[0]["pnl"], -res.ledger[0]["margin"])

    def test_below_minimum_notional_is_skipped(self):
        res = run_wallet(self.tr([0.01]), WalletConfig(risk_pct=0.0001))
        self.assertEqual(len(res.ledger), 0)
        self.assertEqual(res.skipped.get("below_min_notional"), 1)

    def test_overlapping_trades_respect_max_concurrent(self):
        df = pd.DataFrame({"entry_t": [0, 1000], "exit_t": [100_000, 100_000], "symbol": ["A", "B"], "side": 1,
                           "net_ret": 0.01, "stop_frac": 0.01, "mae": 0.0, "fee_frac": 0.0})
        res = run_wallet(df, WalletConfig(max_concurrent=1))
        self.assertEqual(len(res.ledger), 1)
        self.assertEqual(res.skipped["max_concurrent"], 1)

    def test_loss_streak_pause_skips_entries(self):
        cfg = WalletConfig(loss_streak_pause=2, pause_hours=1.0)
        res = run_wallet(self.tr([-0.001] * 4), cfg)
        self.assertGreaterEqual(res.skipped.get("loss_streak_pause", 0), 1)

    def test_fair_coin_reaches_target_near_the_fair_baseline(self):
        o = cycle_odds(np.array([0.015, -0.015]), np.array([0.015, 0.015]),
                       WalletConfig(risk_pct=0.25, leverage=10.0), n_cycles=6000, seed=3)
        self.assertTrue(0.15 < o["p_target"] < 0.30)

    def test_positive_edge_beats_the_baseline_and_negative_edge_loses_to_it(self):
        cfg = WalletConfig(risk_pct=0.10, leverage=10.0)
        up = cycle_odds(np.array([0.03] * 6 + [-0.015] * 4), np.full(10, 0.015), cfg, n_cycles=4000)
        dn = cycle_odds(np.array([0.03] * 3 + [-0.015] * 7), np.full(10, 0.015), cfg, n_cycles=4000)
        self.assertGreater(up["p_target"], 0.5)
        self.assertLess(dn["p_target"], 0.1)


class TestSafety(unittest.TestCase):
    def test_lab_never_imports_an_llm_or_network_client(self):
        code = ("import sys, analysis.lab.runner, analysis.lab.strategies, analysis.lab.wallet; "
                "bad=[m for m in sys.modules if m.startswith(('collectors.llm','httpx','aiohttp','requests','openai','anthropic','groq'))]; "
                "print(bad); sys.exit(1 if bad else 0)")
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_data_loader_never_fabricates_candles(self):
        from analysis.lab.data import MissingData, load_bars
        with self.assertRaises(MissingData):
            load_bars("NOSUCHCOINUSDT", "1m", root="/nonexistent")


if __name__ == "__main__":
    unittest.main()


class TestNoLookAhead(unittest.TestCase):
    """A signal at bar i must not change when bars after i are removed."""

    @classmethod
    def setUpClass(cls):
        from pathlib import Path
        if not (Path("data/lake/um/klines/BTCUSDT/5m").exists() or Path("data/lake/um/klines/BTCUSDT/1m").exists()):
            raise unittest.SkipTest("lake not available")

    def test_every_strategy_is_causal(self):
        from analysis.lab.data import load_bars
        from analysis.lab.strategies import REGISTRY
        end_full = int(pd.Timestamp("2025-04-01", tz="UTC").timestamp() * 1000)
        end_cut = int(pd.Timestamp("2025-03-10", tz="UTC").timestamp() * 1000)
        start = int(pd.Timestamp("2024-12-01", tz="UTC").timestamp() * 1000)
        for sid, st in REGISTRY.items():
            with self.subTest(strategy=sid):
                full = load_bars("BTCUSDT", st.tf, start_ms=start, end_ms=end_full)
                cut = load_bars("BTCUSDT", st.tf, start_ms=start, end_ms=end_cut)
                s_full = st.signals(full)[:len(cut)]
                s_cut = st.signals(cut)
                self.assertEqual(int((s_full != s_cut).sum()), 0, f"{sid} changed {int((s_full != s_cut).sum())} signals")


class TestLoaderCoverage(unittest.TestCase):
    def test_a_short_stored_interval_does_not_truncate_history(self):
        import tempfile
        from pathlib import Path
        from analysis.lab.data import load_bars
        with tempfile.TemporaryDirectory() as d:
            def write(interval, month, step_min, n):
                p = Path(d) / "um" / "klines" / "ZZZUSDT" / interval
                p.mkdir(parents=True, exist_ok=True)
                t0 = int(pd.Timestamp(f"{month}-01", tz="UTC").timestamp() * 1000)
                t = t0 + np.arange(n, dtype=np.int64) * step_min * 60_000
                pd.DataFrame({"open_time": t, "open": 1.0, "high": 1.1, "low": 0.9, "close": 1.0,
                              "volume": 1.0, "taker_buy_volume": 0.5}).to_parquet(p / f"{month}.parquet")
            write("1m", "2021-10", 1, 60 * 24 * 60)          # 60 days of 1m starting 2021-10
            write("15m", "2024-08", 15, 96 * 10)             # 10 days of 15m starting 2024-08
            b = load_bars("ZZZUSDT", "15m", root=d)
            self.assertEqual(pd.to_datetime(b.t[0], unit="ms").year, 2021)


class TestAIReviewIsSeparate(unittest.TestCase):
    def test_lab_modules_do_not_import_the_ai_package(self):
        code = ("import sys, analysis.lab.runner, analysis.lab.strategies, analysis.lab.simulate; "
                "bad=[m for m in sys.modules if m.startswith('analysis.lab_ai')]; "
                "print(bad); sys.exit(1 if bad else 0)")
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_cloud_models_are_refused(self):
        r = subprocess.run([sys.executable, "-m", "scripts.run_lab_ai", "--models", "kimi-k2.6:cloud"],
                           capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("refusing cloud models", r.stdout + r.stderr)


class TestAIGate(unittest.TestCase):
    def test_gate_context_hides_every_outcome_field(self):
        from unittest import mock
        from analysis.lab_ai import gate
        fake = {"symbol": "BTCUSDT", "at_entry": {"rsi14_15m": 50},
                "outcome": {"r_net": 9.9, "exit_reason": "target"},
                "next_16_closes_15m_pct_in_trade_direction": [1, 2, 3],
                "last_16_closes_15m_pct_vs_entry": [0.1, 0.2]}
        with mock.patch("analysis.lab_ai.context.build", return_value=dict(fake)):
            ctx = gate.pretrade_context(pd.Series({"symbol": "BTCUSDT"}))
        self.assertNotIn("outcome", ctx)
        self.assertNotIn("next_16_closes_15m_pct_in_trade_direction", ctx)
        self.assertIn("last_16_closes_15m_pct_vs_entry", ctx)        # the past is allowed
        self.assertNotIn("9.9", gate.prompt(ctx))

    def test_prompt_never_mentions_results(self):
        from analysis.lab_ai import gate
        self.assertIn("result is hidden", gate.SYSTEM)
        for word in ("r_net", "mfe_r", "mae_r", "move_after"):
            self.assertNotIn(word, gate.SYSTEM)

    def test_compare_rewards_a_model_that_actually_predicts(self):
        from analysis.lab_ai import gate
        rng = np.random.default_rng(0)
        rows = []
        for _ in range(300):
            score = int(rng.integers(1, 6))
            rows.append({"ok": True, "score": score, "decision": "take" if score >= 3 else "skip",
                         "r_net": float(rng.normal(0.4 * (score - 3), 1.2))})
        c = gate.compare(rows)
        self.assertGreater(c["take_exp_r"], c["skip_exp_r"])
        self.assertGreater(c["auc"], 0.6)
        self.assertGreater(c["take_minus_skip_ci"][0], 0)

    def test_compare_finds_nothing_in_a_coin_flip_model(self):
        from analysis.lab_ai import gate
        rng = np.random.default_rng(1)
        rows = [{"ok": True, "score": int(rng.integers(1, 6)), "decision": "take" if rng.random() < 0.5 else "skip",
                 "r_net": float(rng.normal(0.1, 1.2))} for _ in range(300)]
        c = gate.compare(rows)
        self.assertLess(c["take_minus_skip_ci"][0], 0.05)
        self.assertGreater(c["take_minus_skip_ci"][1], -0.05)
        self.assertTrue(0.4 < c["auc"] < 0.6)


class TestReviewPrompt(unittest.TestCase):
    def test_hypotheses_may_only_use_fields_known_before_entry(self):
        from analysis.lab_ai.review import system_x
        text = system_x("k")
        self.assertIn("ONLY fields known", text)
        self.assertIn("Never use anything under outcome", text)

    def test_cause_order_is_shuffled_per_trade(self):
        from analysis.lab_ai.review import system_x
        firsts = {system_x(f"trade{i}").split("no particular order:\n")[1].split(":")[0].strip() for i in range(12)}
        self.assertGreater(len(firsts), 2)


class TestOddTimeframes(unittest.TestCase):
    def test_8h_and_12h_bars_are_built_from_complete_lower_bars_only(self):
        import tempfile
        from pathlib import Path
        from analysis.lab.data import load_bars
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "um" / "klines" / "ZZZUSDT" / "1m"
            p.mkdir(parents=True)
            t0 = int(pd.Timestamp("2024-01-01", tz="UTC").timestamp() * 1000)
            n = 60 * 24 * 3 - 90                       # three days minus 90 minutes: the last 8h bar is incomplete
            t = t0 + np.arange(n, dtype=np.int64) * 60_000
            pd.DataFrame({"open_time": t, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 1.0,
                          "taker_buy_volume": 0.5}).to_parquet(p / "2024-01.parquet")
            b8 = load_bars("ZZZUSDT", "8h", root=d)
            self.assertEqual(len(b8), 8)                # 9 slots, the partial one is dropped
            self.assertTrue(((b8.t - t0) % (8 * 3_600_000) == 0).all())
            self.assertEqual(len(load_bars("ZZZUSDT", "12h", root=d)), 5)


class TestMirrorsAndVotes(unittest.TestCase):
    def test_every_strategy_has_an_exact_mirror(self):
        from analysis.lab.strategies import REGISTRY
        base = [k for k in REGISTRY if not k.startswith(("mirror_", "vote", "first_")) and k != "random"]
        self.assertGreaterEqual(len(base), 30)
        for k in base:
            self.assertIn(f"mirror_{k}", REGISTRY)

    def test_a_mirror_is_the_exact_negation_of_its_strategy(self):
        from analysis.lab.data import Bars
        from analysis.lab.strategies import REGISTRY
        rng = np.random.default_rng(0)
        n = 800
        c = 100 + np.cumsum(rng.normal(0, 1, n))
        b = Bars("TESTUSDT", "15m", np.arange(n, dtype=np.int64) * 900_000, c - 0.1, c + 1, c - 1, c, np.abs(rng.normal(100, 20, n)), np.abs(rng.normal(50, 10, n)))
        for sid in ("donchian", "rsi2", "bb_reversion", "ema_cross", "supertrend"):
            a = REGISTRY[sid].signals(b)
            m = REGISTRY[f"mirror_{sid}"].signals(b)
            self.assertTrue((a == -m).all(), sid)
            self.assertGreater(int((a != 0).sum()), 0, sid)

    def test_an_ensemble_only_fires_when_enough_members_agree(self):
        from analysis.lab import strategies as S
        from analysis.lab.data import Bars
        n = 300
        b = Bars("TESTUSDT", "4h", np.arange(n, dtype=np.int64), *(np.ones(n),) * 4, np.ones(n), np.ones(n))
        stub = {"a": np.r_[1, 1, 0, -1, 0], "b": np.r_[1, 0, 0, -1, 1], "c": np.r_[0, 1, 0, 1, 0]}
        old = dict(S.REGISTRY)
        try:
            for k, v in stub.items():
                S.REGISTRY[k] = S.Strategy(k, k, "x", "x", "", (lambda bb, p, _v=v: np.r_[np.zeros(100), _v, np.zeros(n - 105)].astype(np.int8)), {}, {}, "4h")
            fn = S.combine(["a", "b", "c"], "vote", 2)
            out = fn(b, {})[100:105]                          # after the 60-bar warm-up that zeroes early signals
            self.assertEqual(list(out), [1, 1, 0, -1, 0])     # needs two on the same side
        finally:
            S.REGISTRY.clear()
            S.REGISTRY.update(old)
