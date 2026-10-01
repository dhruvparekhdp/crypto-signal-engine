"""
Unit tests for SteppedMarketReplayEngine and clock-paced simulation.
"""
import unittest
from datetime import UTC, datetime, timedelta
import pandas as pd

from analysis.simulator.stepped_replay import SteppedMarketReplayEngine
from analysis.simulator.replay_engine import SimulatedTrade


class TestSteppedReplay(unittest.TestCase):
    def test_process_symbol_step_generates_trades(self):
        engine = SteppedMarketReplayEngine(
            symbols=["BTCUSDT"],
            window_years=0.01,
            market_step_hours=1.0,
            step_budget_seconds=1.0,
            tp_r=2.0,
            sl_r=1.2,
        )

        now = datetime(2024, 3, 15, 12, 0, 0, tzinfo=UTC)
        times = [now + timedelta(minutes=5 * i) for i in range(12)]
        df = pd.DataFrame({
            "open_time": times,
            "open": [65000, 65050, 65100, 65300, 65500, 65700, 66000, 66300, 66500, 66700, 67000, 67200],
            "high": [65100, 65200, 65350, 65600, 65800, 66100, 66400, 66600, 66800, 67100, 67300, 68500],
            "low":  [64900, 65000, 65050, 65250, 65400, 65600, 65900, 66200, 66400, 66600, 66900, 67100],
            "close": [65050, 65150, 65300, 65550, 65750, 66050, 66350, 66550, 66750, 67050, 67250, 68450],
            "volume": [100.0] * 12,
        })

        closed, active_pos, last_time, last_dir = engine._process_symbol_step(
            symbol="BTCUSDT",
            df=df,
            active_trade=None,
            last_trade_time=None,
            last_trade_dir=0,
        )

        self.assertGreaterEqual(len(closed), 1)
        t = closed[0]
        self.assertEqual(t.symbol, "BTCUSDT")
        self.assertEqual(t.direction, "LONG")
        self.assertTrue("TAKE_PROFIT" in t.exit_reason or "STOP_LOSS" in t.exit_reason)
        self.assertNotEqual(t.pnl_r, 0)


if __name__ == "__main__":
    unittest.main()
