"""Best deal first; a premium deal fills the book (owner's rule, 26 Sep)."""

import unittest
from types import SimpleNamespace as NS

from analysis.deal_scanner import (
    book_full,
    deal_score,
    is_premium,
    target_roe_pct,
    volatility_classes,
)


class TestScanner(unittest.TestCase):
    def test_a_wider_cleaner_target_ranks_first(self):
        rt = 0.00118
        wide = NS(current_price=100, target_price=102, confidence=0.7)     # 2%
        thin = NS(current_price=100, target_price=100.5, confidence=0.9)   # 0.5%
        self.assertGreater(deal_score(wide, rt), deal_score(thin, rt))
        self.assertEqual(deal_score(NS(current_price=0, target_price=1, confidence=1), rt), 0)

    def test_volatility_is_relative_to_the_watchlist(self):
        got = volatility_classes({"btc": 0.05, "eth": 0.07, "ltc": 0.09, "xrp": 0.12,
                                  "sol": 0.15, "doge": 0.3, "gold": 0})
        self.assertEqual(got["btc"], "steady")
        self.assertEqual(got["doge"], "volatile")
        self.assertEqual(got["gold"], "unknown")
        self.assertEqual(set(got.values()), {"steady", "normal", "volatile", "unknown"})

    def test_premium_is_target_return_on_margin(self):
        p = NS(entry_price=100, target_price=102, leverage=25)       # 2% x 25 = 50%
        self.assertAlmostEqual(target_roe_pct(p), 50.0)
        self.assertTrue(is_premium(p, 50))
        self.assertFalse(is_premium(NS(entry_price=100, target_price=101, leverage=25), 50))
        self.assertEqual(target_roe_pct(NS(entry_price=100, target_price=float("inf"),
                                           leverage=25)), 0.0)

    def test_the_book_is_full_while_a_premium_trade_is_open(self):
        small = NS(entry_price=100, target_price=100.6, leverage=20, symbol="btc")
        big = NS(entry_price=10, target_price=10.3, leverage=25, symbol="sol")   # 75%
        self.assertIsNone(book_full([small], 50))
        self.assertIs(book_full([small, big], 50), big)
        self.assertIsNone(book_full([big], 0))                        # off at 0


if __name__ == "__main__":
    unittest.main()
