"""Delta India client: signature vector from official docs + symbol/sizing helpers."""
from __future__ import annotations

import unittest

from execution.delta_book import size_contracts
from execution.delta_india import Product, binance_to_delta_symbol, sign


class SignTests(unittest.TestCase):
    def test_docs_prehash_matches_openssl(self):
        # Prehash format from https://docs.delta.exchange/ (method+ts+path+query+body).
        # Their published hex digest is stale vs the sample secret; openssl agrees with us.
        secret = "7b6f39dcf660ec1c7c664f612c60410a2bd0c258416b498bf0311f94228f"
        sig = sign(secret, "GET", "1542110948", "/v2/orders",
                   "?product_id=1&state=open", "")
        self.assertEqual(sig, "4e38dda3e6477092f360ba70399266d8145630b22bcc34c0ec7f804d5746877a")

    def test_symbol_map(self):
        self.assertEqual(binance_to_delta_symbol("XRPUSDT"), "XRPUSD")
        self.assertEqual(binance_to_delta_symbol("btcusdt"), "BTCUSD")
        self.assertEqual(binance_to_delta_symbol("SOLUSD"), "SOLUSD")


class SizeTests(unittest.TestCase):
    def test_xrp_sizing(self):
        # XRPUSD: contract_value=1 XRP per contract
        p = Product(14969, "XRPUSD", 1.0, 0.0001, 1.0, "XRP")
        entry, stop = 1.42, 1.49          # short, ~4.9% stop
        contracts, notional, risk = size_contracts(p, entry, stop, equity_inr=5000,
                                                   risk_pct=0.01, usd_inr=83.0)
        self.assertGreaterEqual(contracts, 1)
        self.assertGreater(notional, 0)
        # risk should be near 1% of 5000 = 50 INR (within rounding of whole contracts)
        self.assertLess(abs(risk - 50.0) / 50.0, 0.25)

    def test_below_one_contract(self):
        p = Product(27, "BTCUSD", 0.001, 0.5, 0.5, "BTC")
        # tiny equity can't afford one BTC contract at ~80k
        contracts, _, _ = size_contracts(p, 80000, 82000, equity_inr=100,
                                         risk_pct=0.01, usd_inr=83.0)
        self.assertEqual(contracts, 0)


if __name__ == "__main__":
    unittest.main()
