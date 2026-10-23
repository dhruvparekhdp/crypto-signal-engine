"""
Public channel feed of LIVE (real-money) trades, for friends (owner, 23 Oct 2026: "what live trades we have created get
posted in there ... no trade values, only trade signals type").

A post says which coin, which direction, which setup, and later how it ended (target / stop / ...) and whether it
ended in profit or at a loss - never a price, size, amount or percentage. Only live Delta India trades are posted
(not paper, not the demo account). The open trades already posted are remembered in data/channel_feed.json, so each
close is posted once whichever way it closed (by the engine, or by Delta's own stop / target with paper following).
"""
from __future__ import annotations

import html
import json
import time
from pathlib import Path

STATE_PATH = Path("data/channel_feed.json")

COINS = {"BTC": "Bitcoin", "ETH": "Ethereum", "SOL": "Solana", "XRP": "XRP", "ADA": "Cardano", "DOGE": "Dogecoin",
         "AVAX": "Avalanche", "SUI": "Sui", "TIA": "Celestia", "LINK": "Chainlink", "BNB": "BNB", "LTC": "Litecoin",
         "BCH": "Bitcoin Cash", "XAUT": "Gold", "PAXG": "Gold"}
SETUPS = {"vol_breakout": "Volume breakout", "keltner_break": "Keltner channel breakout",
          "donchian": "Donchian channel breakout", "ichimoku": "Ichimoku cloud trend", "pivots": "Pivot breakout"}
EXITS = {"target": "🎯 Target hit", "stop": "🛑 Stop-loss hit", "profit_lock": "🔒 Trailing stop hit",
         "expiry": "⏱ Time limit reached", "stagnant_timeout": "⏱ Time limit reached", "cycle_end": "⏱ Closed at cycle end",
         "signal_flip": "↩️ Setup reversed", "conviction_lost": "↩️ Setup faded", "market_shock": "⚠️ Emergency exit",
         "manual": "✋ Closed manually", "liquidation": "Closed by the exchange"}
FOOTER = "<i>Personal experiment with a small wallet · not financial advice</i>"


def coin_label(symbol: str) -> tuple[str, str]:
    """('XAUT', 'Gold (XAUT)') from 'xautusdt' / 'XAUTUSD'."""
    base = symbol.upper()
    for q in ("USDT", "USD"):
        if base.endswith(q):
            base = base[: -len(q)]
            break
    name = COINS.get(base)
    return base, (f"{name} ({base})" if name and name != base else base)


def setup_label(signal_type: str, timeframe: str = "") -> str:
    key = signal_type.removeprefix("swing_")
    name = SETUPS.get(key, key.replace("_", " ").capitalize() or "Swing setup")
    return f"{name} · {timeframe} chart" if timeframe else name


def _side(side_word: str) -> str:
    return "LONG 📈" if side_word.lower() in ("long", "buy") else "SHORT 📉"


def open_post(symbol: str, side_word: str, signal_type: str, timeframe: str = "") -> str:
    base, coin = coin_label(symbol)
    return (f"🟢 <b>NEW TRADE · {html.escape(coin)}</b>\n"
            f"Direction: <b>{_side(side_word)}</b>\n"
            f"Setup: {html.escape(setup_label(signal_type, timeframe))} (swing, usually held for days)\n"
            f"Stop-loss and target are set on the exchange.\n"
            f"#{base} #swing\n{FOOTER}")


def close_post(symbol: str, side_word: str, signal_type: str, reason: str, in_profit: bool | None) -> str:
    base, coin = coin_label(symbol)
    head = "✅" if in_profit else "❌" if in_profit is False else "⚪"
    result = "in profit" if in_profit else "at a loss" if in_profit is False else ""
    how = EXITS.get(reason, reason.replace("_", " ").capitalize() or "Closed")
    return (f"{head} <b>CLOSED · {html.escape(coin)} {_side(side_word).split()[0]}</b>\n"
            f"{how}{' · ended ' + result if result else ''}\n"
            f"Setup was: {html.escape(setup_label(signal_type))}\n"
            f"#{base}\n{FOOTER}")


class ChannelFeed:
    """Remembers which live trades were posted, so each close is posted exactly once."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or STATE_PATH

    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {"open": {}}

    def _save(self, state: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state))
        tmp.replace(self.path)

    def opened(self, symbol: str, side_word: str, signal_type: str, timeframe: str = "") -> str:
        state = self._load()
        state["open"][symbol.upper()] = {"side": side_word, "signal_type": signal_type, "t": int(time.time() * 1000)}
        self._save(state)
        return open_post(symbol, side_word, signal_type, timeframe)

    def closed(self, symbol: str, reason: str, in_profit: bool | None) -> str | None:
        """The close post for a trade this feed announced, or None (never posted -> nothing to close)."""
        state = self._load()
        rec = state["open"].pop(symbol.upper(), None)
        if rec is None:
            return None
        self._save(state)
        return close_post(symbol, rec["side"], rec["signal_type"], reason, in_profit)
