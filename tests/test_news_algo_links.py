"""18 Oct 2026 research: news pauses stay (entries in release windows lost), the AI-bias override is off (its tone
pointed the wrong way), and swing signals now carry the news score so the link can be measured."""
from pathlib import Path

from config.settings import Settings


def test_ai_bias_override_off_and_pause_on():
    s = Settings()
    assert s.event_bias_mode is False and s.event_blackout_enabled is True and s.swing_news_guard is True


def test_swing_signal_records_the_news_score():
    src = Path("scheduler/runner.py").read_text()
    i = src.index("sig = sb.to_signal(setup, price, now)")
    assert 'sig.sentiment_score = float(getattr(st, "sentiment_score"' in src[i:i + 500]
