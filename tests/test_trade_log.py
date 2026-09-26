"""The per-trade change log: what moved, in words a trader would write."""

import unittest
from datetime import datetime
from types import SimpleNamespace

from scheduler.runner import _event, _trade_changes, _trade_snapshot

T0 = datetime(2026, 9, 26, 8, 0)


def pos(**kw):
    base = dict(symbol="SOLUSDT", side=SimpleNamespace(value="long"), entry_price=120.69,
                stop_price=119.0, target_price=122.13, trail_active=False,
                trail_r_override=None, locked_roe=None, opened_at=T0)
    base.update(kw)
    return SimpleNamespace(**base)


class TestTradeLog(unittest.TestCase):
    def test_a_stop_moved_into_profit_is_logged_with_the_locked_amount(self):
        p = pos()
        before = _trade_snapshot(p)
        p.stop_price, p.trail_active = 121.15, True
        ev = _trade_changes(before, p, 1, T0, 121.31)
        kinds = [e["kind"] for e in ev]
        self.assertEqual(kinds, ["trail", "lock"])          # crossed into profit
        stop = ev[1]
        self.assertEqual((stop["old"], stop["new"]), ("119", "121.15"))
        self.assertIn("profit locked: stop +0.38% beyond entry", stop["note"])
        self.assertEqual(stop["symbol"], "solusdt")
        self.assertEqual(stop["opened_at"], T0)

    def test_a_later_move_in_profit_is_a_plain_stop_move(self):
        p = pos(trail_active=True, stop_price=121.15)
        before = _trade_snapshot(p)
        p.stop_price = 121.45
        (e,) = _trade_changes(before, p, 1, T0, 121.6)
        self.assertEqual(e["kind"], "stop")

    def test_tiny_trail_ticks_are_not_logged(self):
        p = pos(trail_active=True, stop_price=121.15)
        before = _trade_snapshot(p)
        p.stop_price = 121.16                            # < 0.02% of price
        self.assertEqual(_trade_changes(before, p, 1, T0, 121.3), [])

    def test_target_release_and_ai_trail_distance(self):
        p = pos()
        before = _trade_snapshot(p)
        p.target_price, p.trail_r_override = 0.0, 0.8
        ev = _trade_changes(before, p, 1, T0, 121.0)
        self.assertEqual([e["kind"] for e in ev], ["target", "trail"])
        self.assertEqual(ev[0]["new"], "released")
        self.assertEqual(ev[1]["new"], "0.80R")

    def test_event_shape(self):
        e = _event(pos(), 3, T0, "closed", "exit", new="121.2", note="stop")
        self.assertEqual(set(e), {"cycle_id", "symbol", "opened_at", "at", "kind", "field",
                                  "old", "new", "note"})


if __name__ == "__main__":
    unittest.main()
