"""18 Oct 2026: AI jobs run only when something happened (23 days: 75-95% of briefings said 'nothing new')."""
from datetime import datetime, timedelta
from types import SimpleNamespace

from analysis.ai_gates import big_movers, btc_shock, event_near, fresh_news_count

NOW = datetime(2026, 10, 18, 3, 0)


def _row(mins_ago, event="crypto_other", source="coindesk"):
    return SimpleNamespace(published_at=NOW - timedelta(minutes=mins_ago), event_type=event, source=source)


def test_fresh_news_counts_only_real_new_headlines():
    rows = [_row(10), _row(30, event="noise"), _row(20, source="web_briefing:x"), _row(-120), _row(400)]
    assert fresh_news_count(rows, NOW - timedelta(hours=1), NOW) == 1


def test_event_near_uses_the_calendar():
    ev = [SimpleNamespace(at=NOW + timedelta(minutes=90))]
    assert event_near(NOW, 2, ev) and not event_near(NOW, 1, ev)


def test_btc_shock():
    assert btc_shock([100.0] * 61 + [102.5])
    assert not btc_shock([100.0] * 61 + [101.0])


def test_big_movers():
    m = [SimpleNamespace(symbol="a", change_4h=3.4), SimpleNamespace(symbol="b", change_4h=-1.2),
         SimpleNamespace(symbol="c", change_4h=None)]
    assert [x.symbol for x in big_movers(m)] == ["a"]
