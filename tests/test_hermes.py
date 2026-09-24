"""
Hermes — the news pipeline that had an endpoint and never had a sender.

`sentiment_score` is 0.0 on all 35,637 snapshots in the archive. The field was
wired to CryptoPanic, which needs a token that was never issued, so every
signal this system has ever produced was generated with no idea what the world
was doing.

The week of 12-19 September makes that concrete: the two moves that decided it
were a Fed rate decision and a short squeeze, and the engine traded through
both on an RSI reading. So the feed list leads with the Federal Reserve's own
press releases — free, keyless, and where a rate decision appears first.
"""
import unittest
from datetime import datetime

from collectors.hermes import (
    EVENT_TYPES,
    FEEDS,
    HIGH_IMPACT,
    SCORING_SYSTEM,
    Batch,
    Headline,
    guess_symbol,
    parse_feed,
    stable_id,
)

ATOM = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>Federal Reserve issues FOMC statement</title>
<link href="https://federalreserve.gov/a.htm"/><updated>2026-09-16T18:00:00Z</updated></entry>
</feed>"""

RSS = """<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>Bitcoin blasts past $80K</title><link>https://coindesk.com/a</link>
<pubDate>Fri, 18 Sep 2026 14:20:00 GMT</pubDate></item>
</channel></rss>"""


class TestParsingBothFormats(unittest.TestCase):
    """
    The Fed publishes Atom and the crypto press publishes RSS. A reader that
    handles one of them silently drops the feed that matters most.
    """

    def test_atom(self):
        out = parse_feed(ATOM, "federal_reserve")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].headline, "Federal Reserve issues FOMC statement")
        self.assertEqual(out[0].url, "https://federalreserve.gov/a.htm")

    def test_rss(self):
        out = parse_feed(RSS, "coindesk")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].url, "https://coindesk.com/a")

    def test_rss_dates_are_not_iso_and_still_parse(self):
        self.assertEqual(parse_feed(RSS, "x")[0].published_at,
                         datetime(2026, 9, 18, 14, 20))

    def test_atom_dates_parse_too(self):
        self.assertEqual(parse_feed(ATOM, "x")[0].published_at,
                         datetime(2026, 9, 16, 18, 0))

    def test_broken_xml_costs_that_feed_and_nothing_else(self):
        self.assertEqual(parse_feed("<not xml", "x"), [])

    def test_an_entry_missing_a_link_is_skipped(self):
        self.assertEqual(parse_feed(
            '<?xml version="1.0"?><rss><channel><item><title>t</title></item></channel></rss>',
            "x"), [])


class TestTheDedupKey(unittest.TestCase):
    """
    The ingest endpoint deduplicates on this, so it has to be a property of
    the story rather than of the fetch — otherwise a retried batch
    double-counts one headline and produces a sentiment spike that never
    happened.
    """

    def test_the_same_story_gives_the_same_id(self):
        self.assertEqual(stable_id("http://x/1", "Title"), stable_id(" http://x/1 ", "Title "))

    def test_different_stories_do_not_collide(self):
        self.assertNotEqual(stable_id("http://x/1", "A"), stable_id("http://x/2", "A"))
        self.assertNotEqual(stable_id("http://x/1", "A"), stable_id("http://x/1", "B"))

    def test_it_carries_nothing_about_when_it_was_fetched(self):
        """
        Asserted by calling it, not by reading it. A source grep for "random"
        matches the docstring explaining why there is no randomness in it.
        """
        first = stable_id("http://x/1", "Title")
        self.assertEqual({stable_id("http://x/1", "Title") for _ in range(50)}, {first})


class TestSymbolMatching(unittest.TestCase):
    """
    A regex rather than the model. It cannot invent a symbol that is not on
    the watchlist, and it costs nothing.
    """

    def test_it_finds_the_coin(self):
        for title, expected in (("Bitcoin blasts past $80K", "btcusdt"),
                                ("Ethereum upgrade ships", "ethusdt"),
                                ("Solana ETF filing", "solusdt"),
                                ("Gold hits a record high", "xauusdt")):
            with self.subTest(title=title):
                self.assertEqual(guess_symbol(title), expected)

    def test_a_headline_naming_no_coin_is_macro(self):
        """And macro is the one that matters for a rate decision."""
        self.assertEqual(guess_symbol("Federal Reserve issues FOMC statement"), "all")

    def test_it_matches_whole_words_only(self):
        """'ethics' is not Ethereum; 'solar' is not Solana."""
        self.assertEqual(guess_symbol("New ethics rules for brokers"), "all")
        self.assertEqual(guess_symbol("Solar stocks rally"), "all")


class TestTheVocabulary(unittest.TestCase):
    def test_the_events_that_moved_the_week_have_tags(self):
        """A rate decision and a tariff are what this exists to catch."""
        for event in ("rate_decision", "war", "tariff", "inflation_data"):
            with self.subTest(event=event):
                self.assertIn(event, EVENT_TYPES)

    def test_high_impact_is_a_subset_of_the_vocabulary(self):
        self.assertTrue(HIGH_IMPACT.issubset(set(EVENT_TYPES)))

    def test_noise_is_available_and_is_the_default(self):
        self.assertIn("noise", EVENT_TYPES)
        self.assertEqual(Headline("i", "h", "s", "u").event_type, "noise")

    def test_the_prompt_says_a_hike_is_negative(self):
        """
        The one directional fact the scorer must not get backwards, and the
        one a small model is most likely to.
        """
        self.assertIn("rate HIKE is negative", SCORING_SYSTEM)

    def test_the_prompt_says_most_headlines_are_noise(self):
        """A scorer that finds meaning in everything is one nobody can act on."""
        self.assertIn("Most headlines are noise", SCORING_SYSTEM)


class TestTheFeedList(unittest.TestCase):
    def test_the_central_bank_comes_first(self):
        self.assertEqual(FEEDS[0][0], "federal_reserve")

    def test_every_feed_is_keyless(self):
        """No signup, no quota, nothing to expire quietly at 3am."""
        for name, url in FEEDS:
            with self.subTest(feed=name):
                self.assertTrue(url.startswith("https://"))
                for marker in ("apikey", "api_key", "token=", "auth"):
                    self.assertNotIn(marker, url.lower())


class TestTheBatch(unittest.TestCase):
    def test_high_impact_picks_out_the_blackout_candidates(self):
        batch = Batch(headlines=[
            Headline("1", "FOMC", "fed", "u", event_type="rate_decision"),
            Headline("2", "10x altcoins", "cd", "u", event_type="noise"),
            Headline("3", "Strikes reported", "yf", "u", event_type="war"),
        ])
        self.assertEqual({h.event_type for h in batch.high_impact},
                         {"rate_decision", "war"})

    def test_the_payload_matches_what_the_endpoint_stores(self):
        from pathlib import Path

        repo = (Path(__file__).resolve().parent.parent / "storage/repository.py").read_text()
        block = repo[repo.index("async def ingest_news_sentiment"):]
        block = block[:block.index("accepted += 1")]
        for key in Headline("i", "h", "s", "u").as_payload():
            with self.subTest(key=key):
                self.assertIn(f'"{key}"', block)


class TestScoringRunsLocally(unittest.TestCase):
    def test_it_has_its_own_chain_led_by_the_local_model(self):
        """
        Hundreds of headlines a day through a metered API is a bill; through
        a model on hardware you own it is electricity.
        """
        from collectors.llm_client import _parse_chain
        from config.settings import settings

        chain = _parse_chain(settings.llm_chain_news_scoring)
        self.assertEqual(chain[0][0], "ollama")
        self.assertGreater(len(chain), 1)

    def test_it_asks_for_the_smallest_model(self):
        """A score, a confidence and one tag does not need an 8B."""
        from collectors.llm_client import _parse_chain
        from config.settings import settings

        self.assertIn("1.7b", _parse_chain(settings.llm_chain_news_scoring)[0][1])
