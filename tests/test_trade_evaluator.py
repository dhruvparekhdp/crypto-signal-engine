import unittest
from analysis.trade_evaluator import evaluate_signal_trade_path

class TestTradeEvaluator(unittest.TestCase):
    def test_trade_evaluator_won(self):
        sig = {
            "id": 1,
            "symbol": "ETHUSDT",
            "direction": "long",
            "current_price": 2600.0,
            "target_price": 2650.0,  # +1.92%
            "stop_loss": 2580.0,     # -0.77%
            "outcome": "pending",
            "pnl_pct": 0.0,
        }
        candles = [
            {"high": 2610.0, "low": 2595.0, "close": 2605.0},
            {"high": 2630.0, "low": 2600.0, "close": 2625.0},
            {"high": 2652.0, "low": 2620.0, "close": 2650.0},  # Hit target
        ]
        res = evaluate_signal_trade_path(sig, candles)
        self.assertEqual(res["status"], "won")
        self.assertTrue(res["worked"])
        self.assertTrue(res["hit_target"])
        self.assertEqual(res["target_pct_reached"], 100.0)
        self.assertGreaterEqual(res["peak_gain_pct"], 1.92)
        self.assertIn("🏆 Worked 100%", res["worked_desc"])

    def test_trade_evaluator_partial(self):
        sig = {
            "id": 2,
            "symbol": "BTCUSDT",
            "direction": "short",
            "current_price": 60000.0,
            "target_price": 58800.0,  # -2.0% target
            "stop_loss": 60600.0,     # +1.0% stop
            "outcome": "pending",
            "pnl_pct": 0.0,
        }
        # Price drops to 59200 (covered 66.7% of target), then spikes up to hit stop loss 60700
        candles = [
            {"high": 60050.0, "low": 59500.0, "close": 59600.0},
            {"high": 59700.0, "low": 59200.0, "close": 59300.0},  # Peak gain +1.33% (66.7% of 2% TP)
            {"high": 60700.0, "low": 59300.0, "close": 60650.0},  # Stopped out
        ]
        res = evaluate_signal_trade_path(sig, candles)
        self.assertEqual(res["status"], "partial")
        self.assertTrue(res["worked"])
        self.assertTrue(res["hit_stop"])
        self.assertGreaterEqual(res["target_pct_reached"], 65.0)
        self.assertIn("⚡ Worked Partially", res["worked_desc"])
        self.assertTrue(res["tp1_hit"])

    def test_trade_evaluator_direct_stop(self):
        sig = {
            "id": 3,
            "symbol": "SOLUSDT",
            "direction": "long",
            "current_price": 150.0,
            "target_price": 156.0,
            "stop_loss": 147.0,
            "outcome": "pending",
            "pnl_pct": 0.0,
        }
        # Drops straight into stop loss
        candles = [
            {"high": 150.1, "low": 148.5, "close": 148.6},
            {"high": 148.7, "low": 146.5, "close": 146.8},  # Hit stop
        ]
        res = evaluate_signal_trade_path(sig, candles)
        self.assertEqual(res["status"], "stopped")
        self.assertFalse(res["worked"])
        self.assertTrue(res["hit_stop"])
        self.assertLess(res["target_pct_reached"], 20.0)
        self.assertIn("🛑 Direct Stop Out", res["worked_desc"])

    def test_trade_evaluator_running(self):
        sig = {
            "id": 4,
            "symbol": "ETHUSDT",
            "direction": "long",
            "current_price": 2660.0,
            "target_price": 2700.0,
            "stop_loss": 2640.0,
            "outcome": "pending",
            "pnl_pct": 0.0,
        }
        candles = [
            {"high": 2675.0, "low": 2658.0, "close": 2670.0},
        ]
        res = evaluate_signal_trade_path(sig, candles, current_price=2670.0)
        self.assertEqual(res["status"], "running")
        self.assertFalse(res["hit_target"])
        self.assertFalse(res["hit_stop"])
        self.assertGreater(res["current_pnl_pct"], 0)
        self.assertIn("🔵 Live Trade Active", res["worked_desc"])

if __name__ == "__main__":
    unittest.main()
