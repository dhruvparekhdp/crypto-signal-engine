"""Wallet passbook: every line says what was traded, by which strategy and bar size, at what prices, and why it closed."""
import unittest

import pandas as pd

from analysis.lab.wallet import WalletConfig, run_wallet
from scripts import portfolio_wallet as pw

H = 3_600_000


def trades():
    rows = []
    for i, (st, tf, ret, r) in enumerate([("donchian", "4h", 0.06, 2.9), ("ichimoku", "8h", -0.021, -1.03)]):
        rows.append({"symbol": "BTCUSDT", "side": 1 if i == 0 else -1, "entry_t": i * 10 * H, "exit_t": i * 10 * H + 5 * H,
                     "entry": 100.0 + i, "exit": 106.0 + i, "stop_frac": 0.02, "net_ret": ret, "r_net": r, "mae": 0.01,
                     "fee_frac": 0.001, "reason": "target" if r > 0 else "stop", "strategy": st, "tf": tf})
    return pd.DataFrame(rows)


class TestPassbookLines(unittest.TestCase):
    def setUp(self):
        res = run_wallet(trades(), WalletConfig(risk_pct=0.01, leverage=5, max_concurrent=4, reset=False))
        self.lines = [pw.ledger_row(r) for r in res.ledger]

    def test_each_line_carries_prices_strategy_bar_size_and_result(self):
        a, b = self.lines
        self.assertEqual((a["strategy"], a["tf"], a["side"], a["exit"]), ("donchian", "4h", "long", "target"))
        self.assertEqual((b["strategy"], b["tf"], b["side"], b["exit"]), ("ichimoku", "8h", "short", "stop"))
        self.assertEqual((a["entry"], a["exit_px"], a["r"]), (100.0, 106.0, 2.9))
        self.assertEqual(a["opened"], "1970-01-01 00:00")
        self.assertEqual(a["t"], "1970-01-01 05:00")
        self.assertGreater(a["fee"], 0)

    def test_balances_chain_from_25(self):
        a, b = self.lines
        self.assertEqual(a["before"], 25.0)
        self.assertAlmostEqual(a["balance"], round(25 + a["pnl"], 2), places=2)
        self.assertEqual(b["before"], a["balance"])

    def test_universe_lists_every_distinct_trade_once(self):
        u = pw.universe(trades())
        self.assertEqual([(t["st"], t["tf"], t["s"], t["d"]) for t in u], [("donchian", "4h", "BTC", 1), ("ichimoku", "8h", "BTC", -1)])

    def test_bar_size_comes_from_the_run_spec(self):
        self.assertEqual(pw._tf_of("data/lab/runs/null_8h/trades.parquet"), "8h")


class TestDashboardStrategySummary(unittest.TestCase):
    def test_per_start_strategy_totals(self):
        from scripts import dash
        out = dash._by_strategy([{"strategy": "donchian", "tf": "4h", "pnl": 0.5}, {"strategy": "donchian", "tf": "4h", "pnl": -0.2},
                                 {"strategy": "ichimoku", "tf": "8h", "pnl": -0.1}])
        self.assertEqual(out, {"donchian|4h": [2, 1, 0.3], "ichimoku|8h": [1, 0, -0.1]})


if __name__ == "__main__":
    unittest.main()


class TestCorrelationCaps(unittest.TestCase):
    """Four longs opened together on four coins are one bet on one market move; the caps treat them so."""

    def cluster(self):
        rows = [{"symbol": s, "side": 1, "entry_t": i * 60_000, "exit_t": 10 * H, "entry": 100.0, "exit": 94.0,
                 "stop_frac": 0.05, "net_ret": -0.05, "r_net": -1.0, "mae": 0.05, "fee_frac": 0.0, "reason": "stop",
                 "strategy": "donchian", "tf": "4h"} for i, s in enumerate(["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"])]
        return pd.DataFrame(rows)

    def test_without_caps_all_four_open(self):
        res = run_wallet(self.cluster(), WalletConfig(start=1000, target=10_000, risk_pct=0.05, leverage=5, max_concurrent=4, reset=False))
        self.assertEqual(len(res.ledger), 4)

    def test_same_side_cap_limits_the_cluster(self):
        res = run_wallet(self.cluster(), WalletConfig(start=1000, target=10_000, risk_pct=0.05, leverage=5, max_concurrent=4,
                                                      max_same_side=2, reset=False))
        self.assertEqual(len(res.ledger), 2)
        self.assertEqual(res.skipped["same_side_cap"], 2)

    def test_open_risk_cap_bounds_the_total_loss(self):
        res = run_wallet(self.cluster(), WalletConfig(start=1000, target=10_000, risk_pct=0.05, leverage=5, max_concurrent=4,
                                                      max_open_risk=0.08, reset=False))
        lost = 1000 - res.cycles[0]["end"]
        self.assertLessEqual(lost, 80 + 1e-6)          # 8% cap, not 4 x 5% = 20%
