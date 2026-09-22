"""
The review loop: an opinion before the trade, a verdict after it.

A pre-trade opinion nobody grades is worth very little. Paired with what
actually happened it becomes a record the reviewer can be held to, and after
enough of them a labelled dataset for deciding which setups deserve to fire.

The part that makes it usable is the closed vocabulary. "The stop was a bit
tight given the conditions" and "stop inside noise" mean the same thing and
aggregate to nothing; `stop_inside_noise` aggregates to a count. These tests
pin that discipline, because a model that invents its own labels quietly
turns the dataset back into prose.
"""
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import SecretStr

from collectors.macro_sentinel import (
    POST_FACTORS,
    PRE_FACTORS,
    GroqSentinel,
    _clean_factors,
)


class _Trade:
    symbol, side = "btcusdt", "long"
    entry_price, exit_price = 86492.46, 86200.80
    target_price, stop_price = 87367.40, 86200.80
    exit_reason, signal_type, confidence = "stop", "volume_spike", 0.79
    hours_held = 0.42
    gross_pnl, trading_fees, funding_paid, net_pnl = -28.90, 3.11, 0.12, -32.13
    return_on_margin = -0.047


class TestVocabulary(unittest.TestCase):
    def test_invented_labels_are_dropped(self):
        """A model writing its own taxonomy is prose wearing a list's clothes."""
        raw = ["stop_inside_noise", "vibes_were_off", "crowded_funding"]
        self.assertEqual(_clean_factors(raw, PRE_FACTORS),
                         "stop_inside_noise,crowded_funding")

    def test_case_and_padding_do_not_create_new_labels(self):
        self.assertEqual(_clean_factors(["  CROWDED_FUNDING "], PRE_FACTORS),
                         "crowded_funding")

    def test_a_non_list_is_not_a_crash(self):
        for junk in ("crowded_funding", None, 42, {"a": 1}):
            with self.subTest(junk=junk):
                self.assertEqual(_clean_factors(junk, PRE_FACTORS), "")

    def test_the_list_is_capped(self):
        """Four labels is a summary; twelve is an essay with commas."""
        self.assertEqual(len(_clean_factors(PRE_FACTORS, PRE_FACTORS).split(",")), 4)

    def test_the_two_vocabularies_do_not_overlap(self):
        """Pre and post ask different questions; a shared label would blur them."""
        self.assertEqual(set(PRE_FACTORS) & set(POST_FACTORS), set())


class TestPromptVoice(unittest.TestCase):
    """The prompts were rewritten because the output read like a form letter."""

    def _prompt(self):
        import inspect
        return inspect.getsource(GroqSentinel.review_closed_trade)

    def test_the_post_mortem_asks_about_winners_too(self):
        """A win that needed a gap is not an edge, and should be named as one."""
        self.assertIn("gapped in your favour", self._prompt())

    def test_it_permits_a_loss_with_no_lesson(self):
        """Inventing a lesson for a clean loss teaches the wrong thing."""
        self.assertIn("Not every loss has a lesson", self._prompt())


@pytest.mark.asyncio
async def test_a_closed_trade_produces_a_stored_verdict():
    sentinel = GroqSentinel()
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"choices": [{"message": {"content": (
        '{"verdict": "BAD_LUCK", "factors": ["stopped_by_noise", "made_up_label"],'
        ' "lesson": "Stop sat inside the ordinary range; the idea was fine."}'
    )}}]}

    with patch("collectors.macro_sentinel.settings.groq_api_key", SecretStr("k")), \
         patch("httpx.AsyncClient.post", new_callable=AsyncMock) as post:
        post.return_value = resp
        out = await sentinel.review_closed_trade(_Trade())

    assert out["verdict"] == "BAD_LUCK"
    assert out["factors"] == "stopped_by_noise"      # invented label dropped
    assert "ordinary range" in out["summary"]
    assert out["latency_ms"] >= 0


@pytest.mark.asyncio
async def test_no_api_key_means_no_review_and_no_crash():
    sentinel = GroqSentinel()
    with patch("collectors.macro_sentinel.settings.groq_api_key", None):
        assert await sentinel.review_closed_trade(_Trade()) == {}


@pytest.mark.asyncio
async def test_a_failing_reviewer_costs_only_the_review():
    """The trade is closed and booked before this runs; it must not raise."""
    sentinel = GroqSentinel()
    with patch("collectors.macro_sentinel.settings.groq_api_key", SecretStr("k")), \
         patch("httpx.AsyncClient.post", new_callable=AsyncMock) as post:
        post.side_effect = RuntimeError("groq is down")
        assert await sentinel.review_closed_trade(_Trade()) == {}
