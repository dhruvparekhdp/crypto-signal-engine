"""18 Oct 2026 research: news pauses stay (entries in release windows lost), the AI-bias override is off (its tone
pointed the wrong way), and swing signals now carry the news score so the link can be measured."""
from pathlib import Path

from config.settings import Settings


def test_ai_bias_override_off_pause_on_for_intraday_and_off_for_swing():
    # 27 Oct 2026, owner: "news should never go blackout" - the swing book follows its 5-year backtest (no blackout);
    # the blackout stays for the intraday research book
    s = Settings()
    assert s.event_bias_mode is False and s.event_blackout_enabled is True and s.swing_news_guard is False


def test_the_swing_book_only_checks_the_blackout_when_its_guard_is_on():
    src = Path("scheduler/runner.py").read_text()
    i = src.index("async def _open_swing_signals")
    assert "blackout = self._blackout(now) if settings.swing_news_guard else None" in src[i:i + 6000]


def test_swing_signal_records_the_news_score():
    src = Path("scheduler/runner.py").read_text()
    i = src.index("sig = sb.to_signal(setup, price, now)")
    assert 'sig.sentiment_score = float(getattr(st, "sentiment_score"' in src[i:i + 500]
