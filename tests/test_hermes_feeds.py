"""15 Oct 2026 audit: yahoo's rssindex feed was dead (404 every pass) and news came from 3 crypto sites only."""
from collectors.hermes import FEEDS


def test_dead_yahoo_feed_is_gone_and_names_are_unique():
    urls = [u for _, u in FEEDS]
    assert "https://finance.yahoo.com/news/rssindex" not in urls
    assert len({n for n, _ in FEEDS}) == len(FEEDS)


def test_more_than_three_crypto_sources():
    names = {n for n, _ in FEEDS}
    assert {"coindesk", "cointelegraph", "theblock", "decrypt", "cryptoslate"} <= names
