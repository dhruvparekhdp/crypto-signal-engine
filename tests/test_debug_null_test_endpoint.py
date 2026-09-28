"""
GET /api/debug/null-test (Part B.2, 28 Sep review): the same comparison
scripts/null_test.py runs from a terminal, reachable from the browser.
Read-only — must never write anything — and must reuse
analysis.null_test.compare_against_random rather than duplicate it.
"""
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from aiohttp.test_utils import make_mocked_request

from analysis.null_test import Entry


def _fake_paths(symbol="btcusdt", n=400, start_price=100.0):
    times = [datetime(2026, 1, 1) + timedelta(minutes=i) for i in range(n)]
    prices = [start_price * (1 + 0.001 * i) for i in range(n)]
    return {symbol: (times, prices)}


class TestDebugNullTestEndpoint(unittest.IsolatedAsyncioTestCase):
    async def test_ok_response_reuses_compare_against_random(self):
        from scheduler import health

        entries = [Entry("btcusdt", "long", datetime(2026, 1, 1, 0, 5))]
        paths = _fake_paths()

        with patch("scripts.null_test.load", new=lambda days: _afuture((entries, paths))), \
             patch("analysis.null_test.compare_against_random") as mock_compare:
            from analysis.null_test import NullResult
            mock_compare.return_value = NullResult(
                entries=1, real_total=2.0, random_mean=1.0, random_sd=0.5,
                trials=50, beaten_by=3)
            req = make_mocked_request("GET", "/api/debug/null-test?days=30&trials=50")
            resp = await health._api_debug_null_test(None, req)

        assert resp.status == 200
        import json
        body = json.loads(resp.body)
        assert body["ok"] is True
        assert body["params"]["days"] == 30
        assert body["params"]["trials"] == 50
        assert body["entries"] == 1
        assert body["real_total_pct"] == 2.0
        assert "verdict" in body
        mock_compare.assert_called_once()

    async def test_no_signals_in_window_reports_that_instead_of_erroring(self):
        from scheduler import health

        with patch("scripts.null_test.load", new=lambda days: _afuture(([], {}))):
            req = make_mocked_request("GET", "/api/debug/null-test")
            resp = await health._api_debug_null_test(None, req)

        import json
        body = json.loads(resp.body)
        assert body["ok"] is False
        assert "no signals" in body["reason"]

    async def test_params_are_bounded_not_left_open(self):
        from scheduler import health

        entries = [Entry("btcusdt", "long", datetime(2026, 1, 1, 0, 5))]
        paths = _fake_paths()

        captured = {}

        def fake_load(days):
            captured["days"] = days
            return _afuture((entries, paths))

        with patch("scripts.null_test.load", new=fake_load), \
             patch("analysis.null_test.compare_against_random") as mock_compare:
            from analysis.null_test import NullResult
            mock_compare.return_value = NullResult(1, 1.0, 0.5, 0.2, 10, 1)
            req = make_mocked_request(
                "GET", "/api/debug/null-test?days=99999&trials=999999")
            resp = await health._api_debug_null_test(None, req)

        import json
        body = json.loads(resp.body)
        assert captured["days"] <= 365
        assert body["params"]["trials"] <= 2000


def _afuture(value):
    async def _inner(days):
        return value
    return _inner(None)


if __name__ == "__main__":
    unittest.main()
