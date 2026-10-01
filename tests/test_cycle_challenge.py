import unittest
from analysis.simulator.replay_engine import SimulatedTrade
from analysis.simulator.cycle_challenge import run_cycle_simulation


class TestCycleChallenge(unittest.TestCase):
    def test_target_reached_and_reset(self):
        # 10 winning trades each +5% with 10x leverage -> +50% on margin
        trades = []
        for i in range(15):
            trades.append(
                SimulatedTrade(
                    trade_id=i + 1,
                    symbol="BTCUSDT",
                    direction="LONG",
                    entry_time=f"2025-01-01T{i:02d}:00:00Z",
                    exit_time=f"2025-01-01T{i:02d}:30:00Z",
                    entry_price=90000.0,
                    exit_price=94500.0,
                    exit_reason="TAKE_PROFIT_2.0R",
                    pnl_pct=5.0,
                )
            )

        res = run_cycle_simulation(
            trades,
            start_balance=25.0,
            target_balance=100.0,
            margin_pct=0.30,
            leverage=10.0,
        )

        self.assertGreaterEqual(res["total_cycles"], 1)
        self.assertGreaterEqual(res["targets_hit"], 1)
        first_cycle = res["cycles"][0]
        self.assertEqual(first_cycle["status"], "TARGET_REACHED")
        self.assertGreaterEqual(first_cycle["ending_balance"], 100.0)
        self.assertEqual(first_cycle["starting_balance"], 25.0)
        self.assertGreater(len(first_cycle["transactions"]), 0)

        # Check transaction ledger structure
        first_tx = first_cycle["transactions"][0]
        self.assertEqual(first_tx["balance_before"], 25.0)
        self.assertGreater(first_tx["balance_after"], 25.0)
        self.assertGreater(first_tx["net_pnl_usdt"], 0)

    def test_busted_and_reset(self):
        # Severe losing trades
        trades = []
        for i in range(10):
            trades.append(
                SimulatedTrade(
                    trade_id=i + 1,
                    symbol="ETHUSDT",
                    direction="LONG",
                    entry_time=f"2025-01-02T{i:02d}:00:00Z",
                    exit_time=f"2025-01-02T{i:02d}:30:00Z",
                    entry_price=2600.0,
                    exit_price=2340.0,
                    exit_reason="STOP_LOSS_1.2R",
                    pnl_pct=-10.0,
                )
            )

        res = run_cycle_simulation(
            trades,
            start_balance=25.0,
            target_balance=100.0,
            margin_pct=0.50,
            leverage=10.0,
        )

        self.assertGreaterEqual(res["busted"], 1)
        busted_cycle = [c for c in res["cycles"] if c["status"] == "BUSTED"][0]
        self.assertEqual(busted_cycle["starting_balance"], 25.0)
        self.assertLess(busted_cycle["ending_balance"], 1.0)
        self.assertEqual(busted_cycle["net_profit_usdt"], -25.0)


if __name__ == "__main__":
    unittest.main()
