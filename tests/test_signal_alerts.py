"""18 Oct 2026, owner: 'too many signals, no signal converted to trade' - 167 of 172 signals since live started were
intraday (switched off on 14-15 Oct, kept as research), and their Telegram alerts never became trades."""
from types import SimpleNamespace

from config.settings import settings
from scheduler.runner import _signal_alert_due


def _sig(kind, conf=0.9, tf="4h"):
    return SimpleNamespace(signal_type=kind, confidence=conf, timeframe=tf)


def test_only_signals_that_can_trade_reach_telegram(monkeypatch):
    monkeypatch.setattr(settings, "crypto_alert_telegram", True)
    monkeypatch.setattr(settings, "paper_open_families", "swing")
    monkeypatch.setattr(settings, "shadow_signal_alerts", False)
    assert _signal_alert_due(_sig("swing_keltner_break")) is True
    assert _signal_alert_due(_sig("volume_spike")) is False            # intraday is research-only
    monkeypatch.setattr(settings, "shadow_signal_alerts", True)
    assert _signal_alert_due(_sig("volume_spike")) is True             # opt back in
    monkeypatch.setattr(settings, "crypto_alert_telegram", False)
    assert _signal_alert_due(_sig("swing_keltner_break")) is False     # the master switch still wins


def test_intraday_alerts_return_when_intraday_trades_again(monkeypatch):
    monkeypatch.setattr(settings, "crypto_alert_telegram", True)
    monkeypatch.setattr(settings, "shadow_signal_alerts", False)
    monkeypatch.setattr(settings, "paper_open_families", "swing,intraday")
    assert _signal_alert_due(_sig("confluence", conf=0.9, tf="1h")) is True


def test_a_telegram_timeout_is_retried_not_dropped(monkeypatch):
    import asyncio

    from telegram.error import TimedOut

    from notifications import telegram_notifier as tn
    calls = []

    class Bot:
        async def send_message(self, chat_id, text, parse_mode=None):
            calls.append(text)
            if len(calls) == 1:
                raise TimedOut("Timed out")
    n = tn.TelegramNotifier.__new__(tn.TelegramNotifier)
    monkeypatch.setattr(tn.TelegramNotifier, "_bot", property(lambda self: Bot()))
    monkeypatch.setattr(settings, "telegram_chat_id", "123")
    real_sleep = asyncio.sleep
    monkeypatch.setattr(tn.asyncio, "sleep", lambda s: real_sleep(0))
    assert asyncio.run(n.send_text("LIVE order placed")) is True and calls == ["LIVE order placed"] * 2
