"""Structural rules every LLM chain must keep (regression: Oct-2026 rewrite)."""
import unittest

from collectors.llm_client import PROVIDERS, _parse_chain
from config.settings import Settings

ROLES = [n[len("llm_chain_"):] for n in Settings.model_fields if n.startswith("llm_chain_")]
SEARCH_ROLES = ("briefing", "briefing_calm", "attribution", "history_search", "event_precedent")


def _chain(role):
    return _parse_chain(Settings.model_fields[f"llm_chain_{role}"].default)


class TestChainShape(unittest.TestCase):
    def test_roles_were_discovered(self):
        self.assertGreaterEqual(len(ROLES), 10)

    def test_non_search_chains_end_on_openrouter(self):
        # A Groq 429 is org-wide, so the tail must be a different provider. Chains that must
        # search cannot: OpenRouter's search plugin is a paid feature (HTTP 402 without credits).
        for role in ROLES:
            if role == "research" or role in SEARCH_ROLES:
                continue
            with self.subTest(role=role):
                self.assertEqual(_chain(role)[-1][0], "openrouter")

    def test_openrouter_entries_are_free_models_only(self):
        # The account has no credits. A paid model there is a guaranteed 402 and a wasted delay.
        for role in ROLES:
            for provider, model in _chain(role):
                if provider == "openrouter":
                    with self.subTest(role=role, model=model):
                        self.assertTrue(model.removesuffix("+search").endswith(":free"), model)

    def test_no_three_consecutive_groq_entries(self):
        for role in ROLES:
            with self.subTest(role=role):
                run = best = 0
                for provider, _ in _chain(role):
                    run = run + 1 if provider == "groq" else 0
                    best = max(best, run)
                self.assertLess(best, 3)

    def test_search_roles_keep_search_on_every_entry(self):
        # Groq searches with its own tool; a free OpenRouter model gets DuckDuckGo results put in its prompt
        # (collectors/llm_client.py), never OpenRouter's paid search plugin.
        for role in SEARCH_ROLES:
            with self.subTest(role=role):
                for provider, model in _chain(role):
                    self.assertIn(provider, ("groq", "openrouter"))
                    self.assertTrue(model.endswith("+search"), model)
                    if provider == "openrouter":
                        self.assertTrue(model.endswith(":free+search"), model)

    def test_every_provider_is_known(self):
        for role in ROLES:
            for provider, _ in _chain(role):
                self.assertIn(provider, PROVIDERS)


if __name__ == "__main__":
    unittest.main()
