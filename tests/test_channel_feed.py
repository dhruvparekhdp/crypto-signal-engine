"""Friends' channel of live trades: coin, direction, setup, how it ended - never prices or amounts (23 Oct 2026)."""
import re
from unittest.mock import AsyncMock

import pytest

from notifications import channel_feed as cf


def test_an_open_post_names_coin_direction_and_setup_and_no_numbers_from_the_trade():
    text = cf.open_post("xautusdt", "short", "swing_ichimoku", "4h")
    assert "Gold (XAUT)" in text and "SHORT" in text and "Ichimoku cloud trend · 4h chart" in text
    assert not re.search(r"\d+\.\d+|₹|\$|%", text)                       # no price, amount or percentage


def test_a_close_post_says_how_it_ended_and_profit_or_loss_without_amounts():
    win = cf.close_post("XRPUSDT", "long", "swing_keltner_break", "target", True)
    loss = cf.close_post("ADAUSDT", "short", "swing_donchian", "stop", False)
    assert win.startswith("✅") and "Target hit · ended in profit" in win and "Keltner channel breakout" in win
    assert loss.startswith("❌") and "Stop-loss hit · ended at a loss" in loss and "Cardano (ADA) SHORT" in loss
    assert not re.search(r"\d+\.\d+|₹|\$|%", win + loss)


def test_the_feed_posts_a_close_only_for_an_announced_trade_and_only_once(tmp_path):
    feed = cf.ChannelFeed(tmp_path / "feed.json")
    assert feed.closed("DOGEUSDT", "stop", False) is None                 # never announced: nothing to say
    feed.opened("dogeusdt", "short", "swing_vol_breakout", "8h")
    text = feed.closed("dogeusdt", "profit_lock", True)
    assert "Trailing stop hit" in text and "Dogecoin (DOGE) SHORT" in text and "Volume breakout" in text
    assert feed.closed("dogeusdt", "profit_lock", True) is None


async def test_the_notifier_posts_to_the_channel_only_when_one_is_set(monkeypatch):
    from pydantic import SecretStr

    from config.settings import settings
    from notifications.telegram_notifier import TelegramNotifier
    monkeypatch.setattr(settings, "telegram_bot_token", SecretStr("123:test-token"))   # not the machine's .env
    n = TelegramNotifier()
    bot = AsyncMock()
    n._bot = bot
    monkeypatch.setattr(settings, "telegram_channel_id", "")
    assert await n.send_channel("hi") is False and not bot.send_message.called
    monkeypatch.setattr(settings, "telegram_channel_id", "@friends_trades")
    assert await n.send_channel("hi") is True
    assert bot.send_message.call_args.kwargs["chat_id"] == "@friends_trades"


@pytest.mark.parametrize("live_orders,posted", [(True, True), (False, False)])
async def test_a_live_open_is_announced_only_when_real_orders_were_placed(tmp_path, monkeypatch, live_orders, posted):
    from config.settings import settings
    from execution.delta_book import MirrorPlan
    from scheduler.runner import AppRunner
    monkeypatch.setattr(cf, "STATE_PATH", tmp_path / "feed.json")
    monkeypatch.setattr(settings, "telegram_channel_id", "@friends_trades")
    monkeypatch.setattr(settings, "delta_india_mode", "live")
    monkeypatch.setattr(settings, "delta_testnet_mirror", False)
    monkeypatch.setattr(settings, "delta_india_live_orders", live_orders)
    r = AppRunner()
    r.notifier.send_text = AsyncMock(return_value=True)
    r.notifier.send_channel = AsyncMock(return_value=True)

    async def one(target, method, *args, **kw):
        return MirrorPlan(True, "", "live", "xrpusdt", "XRPUSD", 1, "buy", 2, 1.381, 1.315, 1.577, 2.7, 11.1, 369, 10)
    r._delta_call_one = one
    assert await r._mirror_delta_india("xrpusdt", "long", 1.381, 1.315, 1.577, "swing_keltner_break",
                                       timeframe="4h") is True
    assert r.notifier.send_channel.called is posted
    if posted:
        text = r.notifier.send_channel.call_args.args[0]
        assert "XRP" in text and "LONG" in text and "1.381" not in text and "1.315" not in text
        await r._channel_post("close", "xrpusdt", "stop", False)          # the paper close that follows
        assert "Stop-loss hit · ended at a loss" in r.notifier.send_channel.call_args.args[0]


async def test_no_channel_set_means_nothing_is_remembered_or_sent(tmp_path, monkeypatch):
    from config.settings import settings
    from scheduler.runner import AppRunner
    monkeypatch.setattr(cf, "STATE_PATH", tmp_path / "feed.json")
    monkeypatch.setattr(settings, "telegram_channel_id", "")
    r = AppRunner()
    r.notifier.send_channel = AsyncMock(return_value=True)
    await r._channel_post("open", "xrpusdt", "long", "swing_donchian", "8h")
    assert not r.notifier.send_channel.called and not (tmp_path / "feed.json").exists()
