"""
The web briefing reaches the reviewers, and every review keeps the news it
saw, so the local model can later relate decisions to what the world did.
"""
import asyncio
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from collectors.market_briefing import as_news_items, context_block, parse_briefing


class TestParse(unittest.TestCase):
    def test_a_clean_reply(self):
        tone, summary, events = parse_briefing({
            "risk_tone": -0.6, "summary": "Tariffs escalated.",
            "events": [{"headline": "US raises tariffs on EU goods", "event_type": "tariff",
                        "score": -0.7, "confidence": 0.8, "when": "", "source": "Reuters"}]})
        self.assertEqual((tone, summary), (-0.6, "Tariffs escalated."))
        self.assertEqual(events[0]["event_type"], "tariff")

    def test_garbage_is_cleaned_not_trusted(self):
        tone, _, events = parse_briefing({
            "risk_tone": "nan",
            "events": [{"headline": "x", "event_type": "made_up", "score": 9, "confidence": "high"},
                       {"headline": ""}, "not a dict"]})
        self.assertEqual(tone, 0.0)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_type"], "macro_other")
        self.assertEqual(events[0]["score"], 1.0)
        self.assertEqual(events[0]["confidence"], 0.5)


class TestNewsItems(unittest.TestCase):
    def test_the_same_event_from_two_briefings_is_one_row(self):
        e = {"headline": "Fed holds rates", "event_type": "rate_decision", "score": 0.1,
             "confidence": 0.9, "when": "", "source": "AP"}
        a = as_news_items([e], "groq/groq/compound")[0]
        b = as_news_items([dict(e, headline="FED HOLDS RATES")], "groq/groq/compound")[0]
        self.assertEqual(a["external_id"], b["external_id"])
        self.assertEqual(a["symbol"], "all")
        self.assertTrue(a["source"].startswith("web_briefing"))


class TestContext(unittest.TestCase):
    def test_briefing_then_relevant_headlines_only(self):
        made = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=10)
        briefing = SimpleNamespace(risk_tone=-0.4, summary="Risk-off after tariff news.",
                                   created_at=made)

        def h(symbol, event, score, text):
            return SimpleNamespace(symbol=symbol, event_type=event, score=score, headline=text)

        rows = [h("btcusdt", "etf_flow", 0.5, "BTC ETF inflows"),
                h("ethusdt", "hack", -0.8, "ETH bridge hack"),
                h("all", "noise", 0.0, "5 coins to 10x")]
        text = context_block(briefing, rows, "btcusdt")
        self.assertIn("Risk-off after tariff news.", text)
        self.assertIn("BTC ETF inflows", text)
        self.assertNotIn("ETH bridge hack", text)
        self.assertNotIn("10x", text)

    def test_nothing_to_say_is_said(self):
        self.assertEqual(context_block(None, [], "btcusdt"), "No news in the last 12 hours.")


class TestRouting(unittest.TestCase):
    def test_groq_leads_every_review_role(self):
        from collectors.llm_client import _parse_chain
        from config.settings import settings
        for role in ("pre_trade", "post_trade", "position_review", "briefing"):
            with self.subTest(role=role):
                chain = _parse_chain(getattr(settings, f"llm_chain_{role}"))
                self.assertEqual(chain[0][0], "groq")

    def test_the_briefing_only_uses_web_searching_models(self):
        from collectors.llm_client import _parse_chain
        from config.settings import settings
        for _, model in _parse_chain(settings.llm_chain_briefing):
            self.assertTrue(model.startswith("groq/compound") or model.endswith("+search"), model)

    def test_compound_gets_no_structured_output_knobs(self):
        from collectors import llm_client
        prov = llm_client.PROVIDERS["groq"]
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

        with patch.object(llm_client.httpx, "AsyncClient", Client):
            asyncio.run(llm_client._call_openai_shaped(
                prov, "groq/compound", "s", "u", 100, 0.0, 5.0))
        self.assertNotIn("response_format", sent)
        self.assertNotIn("reasoning_effort", sent)


class TestWiring(unittest.TestCase):
    SRC = Path(__file__).resolve().parent.parent.joinpath("scheduler", "runner.py").read_text()

    def test_the_briefing_job_is_scheduled(self):
        self.assertIn("self._market_briefing_job,", self.SRC)

    def test_every_review_is_saved_with_its_news(self):
        self.assertEqual(self.SRC.count("briefing_id=briefing_id, news_context=news"), 3)


if __name__ == "__main__":
    unittest.main()
