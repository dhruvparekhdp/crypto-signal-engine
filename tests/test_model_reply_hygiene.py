"""
Replies the router hands on must be safe to act on, and requests must leave
reasoning models room to answer.
"""
import asyncio
import unittest
from unittest.mock import patch

from collectors.hermes import _parse_date
from collectors.llm_client import _extract_json


class TestExtractJson(unittest.TestCase):
    def test_a_list_reply_is_no_answer(self):
        self.assertEqual(_extract_json('[{"delta": -0.15}]'), {})

    def test_an_object_still_parses(self):
        self.assertEqual(_extract_json('{"delta": 0.05}'), {"delta": 0.05})


class TestReasoningEffort(unittest.TestCase):
    def _payload(self, provider_name, model):
        from collectors import llm_client
        prov = llm_client.PROVIDERS[provider_name]
        sent = {}

        class Resp:
            status_code = 200
            text = ""

            def json(self):
                return {"choices": [{"message": {"content": "{}"}}]}

        class Client:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, json=None, headers=None):
                sent.update(json)
                return Resp()

        with patch.object(llm_client.httpx, "AsyncClient", Client), \
             patch.object(type(prov), "url", property(lambda self: "http://x/v1/chat/completions")):
            asyncio.run(llm_client._call_openai_shaped(prov, model, "s", "u", 100, 0.0, 5.0))
        return sent

    def test_ollama_is_told_not_to_think(self):
        self.assertEqual(self._payload("ollama", "qwen3:1.7b")["reasoning_effort"], "none")

    def test_gpt_oss_on_groq_thinks_little(self):
        self.assertEqual(self._payload("groq", "openai/gpt-oss-20b")["reasoning_effort"], "low")


class TestNewsDates(unittest.TestCase):
    def test_an_iso_offset_is_converted_not_dropped(self):
        self.assertEqual(_parse_date("2026-09-24T09:00:00+05:30").isoformat(),
                         "2026-09-24T03:30:00")


class TestPositionReviewGarbage(unittest.TestCase):
    """A reply that is not a usable number must read as no answer."""

    def _run(self, data):
        from datetime import UTC, datetime, timedelta
        from types import SimpleNamespace

        from analysis import position_review as pr
        from collectors.llm_client import Reply

        t0 = datetime(2026, 9, 25, tzinfo=UTC)
        pos = SimpleNamespace(symbol="ethusdt", side="long", entry_price=100.0,
                              stop_price=99.0, opened_at=t0,
                              expires_at=t0 + timedelta(hours=2),
                              signal_type="x", confidence=0.7)
        state = SimpleNamespace(current_price=99.8, rsi_14=45.0,
                                macd_histogram=0.1, cvd_trend=None)
        now = t0 + timedelta(minutes=30)

        async def fake(*a, **k):
            return Reply(data=data, provider="x", model="y")

        with patch("collectors.llm_client.ask_json", fake), \
             patch("collectors.llm_client.chain_for", lambda role: [("groq", "m")]), \
             patch.object(pr, "_needs_model", lambda t: True):
            got = asyncio.run(pr.review_position(pos, state, now))
            trend = pr.trend_confidence(pos, state, now)
            return got, pr._no_answer(trend)

    def test_nan_delta_is_no_answer_not_maximum_hold(self):
        got, expected = self._run({"delta": "nan"})
        self.assertEqual(got.verdict, expected.verdict)
        self.assertAlmostEqual(got.confidence, expected.confidence)

    def test_a_list_reply_does_not_raise(self):
        got, expected = self._run([{"delta": -0.15}])
        self.assertEqual(got.verdict, expected.verdict)


if __name__ == "__main__":
    unittest.main()
