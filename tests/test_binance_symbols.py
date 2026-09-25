"""
The watchlist is the one list the engine reads, so what goes on it must be a
pair Binance lists — found by search, not typed from memory.
"""
import unittest

from collectors.binance_symbols import rank

SPOT = {
    "BTCUSDT": "BTC", "ETHUSDT": "ETH", "SOLUSDT": "SOL", "SOLVUSDT": "SOLV",
    "PAXGUSDT": "PAXG", "XAUTUSDT": "XAUT", "PEPEUSDT": "PEPE",
    "BCHUSDT": "BCH", "LTCUSDT": "LTC",
}


class TestRank(unittest.TestCase):
    def test_gold_finds_the_tokenised_gold_pairs(self):
        """Binance has no XAUUSDT; searching the word is how you find what it does list."""
        self.assertEqual(set(rank("gold", SPOT)), {"PAXGUSDT", "XAUTUSDT"})

    def test_an_exact_ticker_comes_first(self):
        self.assertEqual(rank("sol", SPOT)[0], "SOLUSDT")

    def test_the_full_pair_name_works(self):
        self.assertEqual(rank("paxgusdt", SPOT)[0], "PAXGUSDT")

    def test_a_name_finds_its_ticker(self):
        self.assertEqual(rank("bitcoin", SPOT)[0], "BTCUSDT")

    def test_nothing_listed_means_nothing_returned(self):
        self.assertEqual(rank("xau", SPOT), ["XAUTUSDT"])
        self.assertEqual(rank("zzzz", SPOT), [])

    def test_an_empty_query_returns_nothing(self):
        self.assertEqual(rank("  ", SPOT), [])


class TestNewsFollowsTheWatchlist(unittest.TestCase):
    def tearDown(self):
        from collectors.hermes import DEFAULT_WATCHLIST, set_watchlist
        set_watchlist(DEFAULT_WATCHLIST)

    def test_gold_news_goes_to_whichever_gold_pair_is_followed(self):
        from collectors.hermes import guess_symbol, set_watchlist
        set_watchlist(["btcusdt", "paxgusdt"])
        self.assertEqual(guess_symbol("Gold hits a record high"), "paxgusdt")

    def test_a_coin_not_on_the_watchlist_is_macro(self):
        from collectors.hermes import guess_symbol, set_watchlist
        set_watchlist(["btcusdt"])
        self.assertEqual(guess_symbol("Solana ETF filing"), "all")

    def test_bitcoin_cash_is_not_bitcoin(self):
        from collectors.hermes import guess_symbol, set_watchlist
        set_watchlist(["btcusdt", "bchusdt"])
        self.assertEqual(guess_symbol("Bitcoin Cash jumps 12%"), "bchusdt")

    def test_tickers_count_only_in_capitals(self):
        """'SUI surges' is a coin; 'sui generis' is not."""
        from collectors.hermes import guess_symbol, set_watchlist
        set_watchlist(["suiusdt"])
        self.assertEqual(guess_symbol("SUI surges after upgrade"), "suiusdt")
        self.assertEqual(guess_symbol("A sui generis ruling"), "all")

    def test_an_empty_answer_keeps_the_last_list(self):
        from collectors.hermes import guess_symbol, set_watchlist
        set_watchlist(["paxgusdt"])
        set_watchlist([])
        self.assertEqual(guess_symbol("Gold slips"), "paxgusdt")


if __name__ == "__main__":
    unittest.main()
