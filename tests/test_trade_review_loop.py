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
        self.assertIn("needed a gap", self._prompt())

    def test_it_permits_a_loss_with_no_lesson(self):
        """Inventing a lesson for a clean loss teaches the wrong thing."""
        self.assertIn("loss with no lesson is allowed", self._prompt())

    def test_it_is_told_to_reason_before_answering(self):
        """A verdict with no working behind it is a guess with a label on."""
        self.assertIn("Think it through first", self._prompt())
        self.assertIn('"reasoning"', self._prompt())

    def test_it_is_forbidden_from_inventing_data_it_was_not_given(self):
        """
        Asked to weigh crude and yields it does not have, a model supplies
        them from training data months stale — and that invented number then
        moves a real confidence score. It now gets real news, and must still
        never add any that is not listed.
        """
        self.assertIn("Never invent news", self._prompt())


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
    # Provenance, not diagnostics. These rows accumulate into a dataset, and
    # one whose rows were written by three different models with no way to
    # tell which is a dataset you cannot later draw a conclusion from.
    assert out["model"].startswith("groq/")


@pytest.mark.asyncio
async def test_the_post_mortem_is_not_pinned_to_one_vendor():
    """
    The post-trade chain leads with real reasoning and falls back to the fast
    model, because nothing is waiting on a post-mortem and the quality of the
    label is the entire point of writing it down.
    """
    sentinel = GroqSentinel()
    with patch("collectors.llm_client.chain_for",
               return_value=[("anthropic", "claude-sonnet-5")]), \
         patch("collectors.llm_client._call_anthropic", new_callable=AsyncMock) as call:
        call.return_value = ('{"verdict": "GOOD_TRADE", "factors": ["worked_as_designed"],'
                             ' "lesson": "Plan held."}')
        out = await sentinel.review_closed_trade(_Trade())

    assert out["verdict"] == "GOOD_TRADE"
    assert out["model"] == "anthropic/claude-sonnet-5"


@pytest.mark.asyncio
async def test_a_reviewer_outage_does_not_reach_the_trade():
    """
    The trade is closed and booked before any of this runs. Every provider
    being down must cost the review and nothing else.
    """
    sentinel = GroqSentinel()
    with patch("collectors.llm_client.chain_for",
               return_value=[("groq", "a"), ("openrouter", "b")]), \
         patch("collectors.llm_client._call_openai_shaped",
               new_callable=AsyncMock) as call:
        call.side_effect = RuntimeError("everything is on fire")
        assert await sentinel.review_closed_trade(_Trade()) == {}


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


class TestMarketContext(unittest.TestCase):
    """
    The reviewer sees the whole book, not one chart.

    Asked to compare against crude, gold and the debt market, the honest
    answer is that two of the three are not collected: TwelveData refuses WTI
    on the current plan and nothing has ever fetched a yield. Naming them in
    the prompt would not conjure them — it would make the model supply them
    from memory. So it gets what is real: every watchlist symbol, now.
    """

    class S:
        def __init__(self, sym, price, prev, rsi, cvd):
            self.symbol, self.current_price = sym, price
            self.price_24h_ago, self.rsi_14, self.cvd_trend = prev, rsi, cvd

    def test_the_book_is_rendered_with_the_subject_marked(self):
        from collectors.macro_sentinel import market_context
        book = [self.S("btcusdt", 86500, 85000, 58, "bullish_delta"),
                self.S("ethusdt", 2740, 2800, 41, "bearish_delta")]
        out = market_context(book, "btcusdt")
        self.assertIn("BTCUSDT", out)
        self.assertIn("ETHUSDT", out)
        self.assertIn("<- this one", out)
        self.assertIn("+1.76%", out)

    def test_unpriced_symbols_are_left_out(self):
        """A symbol with no price tells the reviewer nothing except noise."""
        from collectors.macro_sentinel import market_context
        book = [self.S("btcusdt", 86500, 85000, 58, "bullish_delta"),
                self.S("xauusdt", 0.0, 0.0, 50, "neutral")]
        out = market_context(book, "btcusdt")
        self.assertNotIn("XAUUSDT", out)

    def test_an_empty_book_says_so_rather_than_rendering_nothing(self):
        from collectors.macro_sentinel import market_context
        self.assertIn("no other markets", market_context([], "btcusdt"))
