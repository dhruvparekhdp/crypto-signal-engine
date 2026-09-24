"""
The router in front of four model providers.

Groq was hardcoded into the sentinel. That was fine while it was the only
account; it stopped being fine once the pre-trade check — which sits in the
signal path — could be silenced by a free-tier rate limit with nothing louder
than a debug line to show for it.

These tests pin the three properties that make the router worth having: a
failing provider costs a slower answer rather than a missing one, a caller
never learns which vendor served it, and every reply records who did.
"""
import unittest
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import SecretStr

from collectors.llm_client import (
    PROVIDERS,
    Reply,
    _extract_json,
    _parse_chain,
    ask_json,
    chain_for,
)


class TestChainParsing(unittest.TestCase):
    def test_a_chain_is_ordered_preference(self):
        self.assertEqual(
            _parse_chain("groq:a, openrouter:b/c"),
            [("groq", "a"), ("openrouter", "b/c")])

    def test_a_model_name_may_contain_a_slash(self):
        """Most of them do — `openai/gpt-oss-20b`, `qwen/qwen3-32b`."""
        self.assertEqual(_parse_chain("groq:openai/gpt-oss-20b"),
                         [("groq", "openai/gpt-oss-20b")])

    def test_a_typo_in_a_fallback_does_not_take_out_the_primary(self):
        self.assertEqual(_parse_chain("groq:a, gorq:b"), [("groq", "a")])

    def test_an_entry_with_no_model_is_dropped(self):
        self.assertEqual(_parse_chain("groq:a, openrouter"), [("groq", "a")])

    def test_whitespace_and_empty_entries_are_tolerated(self):
        self.assertEqual(_parse_chain("  groq:a ,, "), [("groq", "a")])

    def test_providers_with_no_key_are_skipped(self):
        """
        A chain may name a provider you have not signed up for yet. Trying it
        anyway spends the timeout on a guaranteed 401 before reaching one that
        would have worked.
        """
        with patch("collectors.llm_client.settings") as st:
            st.llm_chain_pre_trade = "anthropic:m1, groq:m2"
            st.anthropic_api_key = None
            st.groq_api_key = SecretStr("k")
            self.assertEqual(chain_for("pre_trade"), [("groq", "m2")])


class TestRolesAreConfiguredSeparately(unittest.TestCase):
    """
    One chain for every job gets at most one job right: a pre-trade check has
    seconds, a post-mortem has none of that pressure and is building a dataset.
    """

    def test_the_three_roles_are_distinct(self):
        from config.settings import settings

        chains = {r: getattr(settings, f"llm_chain_{r}")
                  for r in ("pre_trade", "position_review", "post_trade", "research")}
        self.assertEqual(len(set(chains.values())), 4)

    def test_the_latency_critical_role_leads_with_the_fast_provider(self):
        from config.settings import settings

        first = _parse_chain(settings.llm_chain_pre_trade)[0]
        self.assertEqual(first[0], "groq")

    def test_every_role_has_a_fallback(self):
        from config.settings import settings

        for role in ("pre_trade", "position_review", "post_trade", "research"):
            with self.subTest(role=role):
                self.assertGreater(
                    len(_parse_chain(getattr(settings, f"llm_chain_{role}"))), 1)

    def test_every_named_provider_is_one_the_router_knows(self):
        from config.settings import settings

        for role in ("pre_trade", "position_review", "post_trade", "research"):
            raw = getattr(settings, f"llm_chain_{role}")
            named = [e.strip().partition(":")[0] for e in raw.split(",") if e.strip()]
            for provider in named:
                with self.subTest(role=role, provider=provider):
                    self.assertIn(provider, PROVIDERS)


class TestReplyParsing(unittest.TestCase):
    def test_plain_json(self):
        self.assertEqual(_extract_json('{"a": 1}'), {"a": 1})

    def test_a_fenced_block(self):
        self.assertEqual(_extract_json('```json\n{"a": 1}\n```'), {"a": 1})

    def test_an_object_after_a_sentence_of_preamble(self):
        """Not every provider honours a response_format that forbids this."""
        self.assertEqual(_extract_json('Sure, here you go:\n{"a": 1}'), {"a": 1})

    def test_prose_with_no_object_is_not_a_reply(self):
        self.assertEqual(_extract_json("I cannot help with that."), {})

    def test_an_empty_reply_is_falsey(self):
        self.assertFalse(Reply())
        self.assertTrue(Reply(data={"a": 1}, provider="groq", model="m"))


@pytest.mark.asyncio
async def test_a_dead_primary_costs_latency_not_the_answer():
    """The whole reason the router exists."""
    with patch("collectors.llm_client.chain_for",
               return_value=[("groq", "fast"), ("openrouter", "slow")]), \
         patch("collectors.llm_client._call_openai_shaped",
               new_callable=AsyncMock) as call:
        call.side_effect = [RuntimeError("groq returned 429"), '{"verdict": "OK"}']
        reply = await ask_json("pre_trade", "sys", "user")

    assert reply.data == {"verdict": "OK"}
    assert reply.provider == "openrouter"
    assert reply.attempts == ["groq/fast", "openrouter/slow"]


@pytest.mark.asyncio
async def test_a_reachable_provider_returning_prose_does_not_end_the_search():
    """
    An empty dict from a reachable provider is indistinguishable downstream
    from an outage, so it must not be accepted as the answer.
    """
    with patch("collectors.llm_client.chain_for",
               return_value=[("groq", "a"), ("gemini", "b")]), \
         patch("collectors.llm_client._call_openai_shaped",
               new_callable=AsyncMock) as call:
        call.side_effect = ["I'd rather not.", '{"verdict": "BAD_LUCK"}']
        reply = await ask_json("post_trade", "sys", "user")

    assert reply.provider == "gemini"


@pytest.mark.asyncio
async def test_everything_failing_is_an_empty_reply_and_not_an_exception():
    """
    Every caller is past the point of decision — the trade is placed or
    closed. An unavailable reviewer costs the review and nothing else.
    """
    with patch("collectors.llm_client.chain_for",
               return_value=[("groq", "a"), ("openrouter", "b")]), \
         patch("collectors.llm_client._call_openai_shaped",
               new_callable=AsyncMock) as call:
        call.side_effect = RuntimeError("down")
        reply = await ask_json("pre_trade", "sys", "user")

    assert not reply
    assert reply.data == {}
    assert len(reply.attempts) == 2


@pytest.mark.asyncio
async def test_no_configured_provider_is_also_not_an_exception():
    with patch("collectors.llm_client.chain_for", return_value=[]):
        assert not await ask_json("research", "sys", "user")


@pytest.mark.asyncio
async def test_the_reply_records_who_answered():
    """
    Post-mortems accumulate into a dataset. Rows written by three different
    models with no way to tell which is a dataset you cannot later trust, so
    provenance is a column rather than a diagnostic.
    """
    with patch("collectors.llm_client.chain_for",
               return_value=[("anthropic", "claude-sonnet-5")]), \
         patch("collectors.llm_client._call_anthropic",
               new_callable=AsyncMock) as call:
        call.return_value = '{"verdict": "WORKED"}'
        reply = await ask_json("post_trade", "sys", "user")

    assert reply.served_by == "anthropic/claude-sonnet-5"
    assert reply.latency_ms >= 0
