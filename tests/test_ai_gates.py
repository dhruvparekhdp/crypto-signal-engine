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


def test_on_a_calm_half_hour_the_biggest_movers_are_still_explained():
    # 28 Oct 2026: the Moves page was 1 day 6 h old because no coin had moved 3% in 4 h
    m = [SimpleNamespace(symbol=s, change_4h=c) for s, c in (("a", 0.4), ("b", -1.9), ("c", None), ("d", 1.1))]
    assert big_movers(m, 3.0) == []
    assert [x.symbol for x in big_movers(m, 3.0, top_n=2)] == ["b", "d"]
    m.append(SimpleNamespace(symbol="e", change_4h=-3.5))
    assert [x.symbol for x in big_movers(m, 3.0, top_n=2)] == ["e"]          # a real move still comes alone
