"""
Settings the owner edits on /settings, stored in the database.

Precedence: database value > .env > code default. The database holds only
what was changed on the page; everything else keeps its .env or default.
`apply` writes the values onto the live `settings` object, so code that reads
`settings.x` at call time sees a change immediately. The few settings read
once at startup are marked `live=False`, and the page says "applies after
restart" for them rather than pretending.

The audit that led here (26 Sep): the collector switches lived only in
memory (lost on every restart), 8 of the 11 saved strategy settings were read
from .env instead of the database, and the "AI model" field was never used.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Field:
    key: str
    group: str
    label: str
    help: str
    kind: str = "bool"          # bool | int | float | choice | secret
    live: bool = True           # False: read once at startup
    lo: float | None = None
    hi: float | None = None
    choices: tuple = ()


GROUPS = {
    "data": "Market data",
    "signals": "Signals",
    "exits": "Exits (profit lock)",
    "protect": "Protections",
    "ai": "AI",
    "v2": "v2 strategy",
    "storage": "Storage",
    "keys": "API keys",
}

# Settings typed SecretStr on the pydantic model — coerce() must wrap the
# string in one before setattr, or pydantic rejects a plain str at assignment.
# Everything else of kind="secret" is a plain str field.
SECRET_STR_KEYS = frozenset({
    "groq_api_key", "openrouter_api_key", "gemini_api_key", "anthropic_api_key",
    "telegram_bot_token",
})

FIELDS: tuple[Field, ...] = (
    # Market data
    Field("binance_only_mode", "data", "Binance only",
          "Use Binance for all prices and candles. Turns CoinDCX, CoinGecko and Twelve Data "
          "off so sources cannot disagree. Recommended."),
    Field("binance_klines_enabled", "data", "Binance candles (main source)",
          "Real 1-minute candles, depth and price from Binance every minute. Everything "
          "else depends on it."),
    Field("binance_klines_seconds", "data", "Binance candle poll (seconds)",
          "How often candles are refreshed.", kind="int", lo=15, hi=300, live=False),
    Field("binance_ws_enabled", "data", "Binance live stream",
          "Tick-by-tick WebSocket on top of the candles. Optional."),
    Field("binance_oi_enabled", "data", "Open interest",
          "Binance futures open interest every 2 minutes.", live=False),
    Field("binance_liquidation_stream_enabled", "data", "Liquidation stream",
          "Free, no key. Watched on Why no trades? — nothing trades on it yet.",
          live=False),
    Field("binance_large_trade_stream_enabled", "data", "Large-trade stream",
          "Free, no key. Watched on Why no trades? — nothing trades on it yet.",
          live=False),
    Field("large_trade_notional_usd", "data", "Large trade threshold (USDT)",
          "A single trade at or above this is flagged as large.",
          kind="float", lo=1000, hi=1000000, live=False),
    Field("coindcx_enabled", "data", "CoinDCX prices",
          "INR exchange prices. Ignored when Binance only is on."),
    Field("coingecko_enabled", "data", "CoinGecko prices",
          "Fallback prices. Ignored when Binance only is on."),
    Field("twelvedata_enabled", "data", "Twelve Data (gold, silver, oil)",
          "Commodity prices; needs a key. Ignored when Binance only is on.", live=False),
    Field("news_sentiment_enabled", "data", "News sentiment",
          "Scored headlines feed the sentiment score and news pauses."),
    # Signals
    Field("crypto_min_confidence", "signals",
          "Minimum confidence to generate a signal at all",
          "Signals below this are dropped before they ever fire or get shown anywhere. "
          "A fired signal can still fail the separate, usually higher, paper-trading floor "
          "under Paper trading below ('Minimum confidence to open a paper trade') — that is a "
          "second, stricter check, not a duplicate of this one. Lowering this alone does not "
          "make more signals turn into trades if that other floor is still above it.",
          kind="float", lo=0.5, hi=0.95),
    Field("high_conviction_only", "signals", "High conviction only",
          "Stricter scalp and conviction gates.", live=False),
    Field("crypto_htf_filter_enabled", "signals", "1h/15m trend filter",
          "Drop signals fighting the hourly trend."),
    Field("daily_trend_filter_enabled", "signals", "Daily trend filter",
          "Drop signals against the daily SMA20 trend."),
    Field("crypto_volume_spike_enabled", "signals", "Volume spike signals", ""),
    Field("orderflow_enabled", "signals", "Order flow", "Use taker buy/sell flow."),
    Field("mirror_review_enabled", "signals", "Mirror review",
          "For every fired signal, also review the opposite direction. A candidate that "
          "isn't strong enough to open right away is tracked and re-reviewed as the market "
          "moves, instead of being decided once. Off keeps today's behaviour exactly.",
          live=False),
    Field("mirror_review_can_trade", "signals", "Mirror review: allow trading",
          "A candidate that wins its round queues a real paper trade. Off keeps mirror review "
          "watch-only: it still runs and its review trail still shows who would have won, but "
          "nothing opens. Has no effect unless Mirror review is also on.",
          live=False),
    Field("mirror_review_confidence_delta_threshold", "signals",
          "Mirror re-review: confidence move",
          "Re-review a tracked candidate only once its local confidence has moved this much.",
          kind="float", lo=0.01, hi=0.30, live=False),
    Field("mirror_review_min_elapsed_pct", "signals", "Mirror re-review: elapsed timeframe",
          "Re-review only once this fraction of the signal's own timeframe has passed.",
          kind="float", lo=0.05, hi=0.95, live=False),
    Field("mirror_review_max_rounds", "signals", "Mirror re-review: max rounds",
          "Hard cap on AI re-reviews per candidate after round 0.",
          kind="int", lo=0, hi=5, live=False),
    Field("mirror_target_jitter_pct", "signals", "Mirror target/stop jitter",
          "+/- range used to jitter the mirror candidate's target and stop away from an "
          "exact reflection of the original.", kind="float", lo=0.0, hi=0.50, live=False),
    Field("bank_size", "signals", "Bank size (for stake display)",
          "Notional wallet size used only to turn a signal's stake % into a rupee amount "
          "on its Telegram alert. Does not affect what actually opens or how much it risks "
          "- that is Paper trading's own starting wallet below.",
          kind="float", lo=100, hi=10000000),
    # Exits
    Field("profit_lock_enabled", "exits", "Profit lock",
          "Your rule: once a trade is far enough in profit, lock a small win and trail tight."),
    Field("profit_lock_at_pct", "exits", "Lock when price has moved (%)",
          "In the trade's favour, from entry.", kind="float", lo=0.1, hi=5),
    Field("profit_lock_to_pct", "exits", "Lock the stop at (% beyond entry)",
          "Never less than fees + slippage (~0.19%), whatever is set here.",
          kind="float", lo=0.0, hi=5),
    Field("profit_lock_trail_pct", "exits", "Then trail behind the best price (%)",
          "0.15% is your SOL trade. Tighter = more small wins, more early exits.",
          kind="float", lo=0.05, hi=3),
    Field("profit_lock_defers_to_trail_enabled", "exits",
          "Profit lock: let the trail go first",
          "Off (default): profit lock usually arms before the trailing stop does, so it "
          "tightens the stop first on almost every winner and the trail's own 'ride to 2R' "
          "never gets a turn. On: profit lock waits until price is past the trail's own "
          "activation distance (with margin) before it touches the stop at all. Changes real "
          "trading behaviour — leave off unless you mean to let winners run further."),
    Field("premium_fills_book", "exits", "A premium deal fills the book",
          "When an open trade's target pays at least the % below on margin, open nothing "
          "else until it closes. Signals on the same tick are always taken best first."),
    Field("premium_roe_pct", "exits", "Premium deal: target pays at least (% on margin)",
          "50% at 25x is a 2% move, typical of a volatile coin.", kind="float", lo=5, hi=500),
    Field("event_blackout_enabled", "protect", "Watch news and economic events",
          "Off: events are ignored entirely."),
    Field("event_bias_mode", "protect", "Trade through news with the AI's bias",
          "On: during an event, Groq reads the public mood (bull / bear); trades with it go "
          "ahead, only trades against a confident bias are skipped. Off: pause all new trades."),
    Field("calendar_caution_enabled", "protect", "Soften confidence around institutional flow",
          "Month-end/quarter-end rebalancing, options and futures expiry, thin weekend "
          "liquidity — a small confidence penalty, never a pause. Separate from the FOMC/"
          "CPI/NFP pause above."),
    Field("event_precedent_enabled", "protect", "Precedent-based event context",
          "Off by default. When on, an upcoming calendar event gets real historical "
          "precedent (same category, same US administration) looked up, measured against "
          "our own price history, and used only to soften confidence a little (same scale "
          "as the row above) or extend a hold-time ceiling. Never a prediction, never lowers "
          "the entry bar."),
    Field("event_precedent_lookahead_days", "protect", "Precedent research: lookahead (days)",
          "How far ahead an event has to be before it gets researched.",
          kind="int", lo=1, hi=30),
    Field("event_precedent_min_sample_size", "protect", "Precedent: min sample size",
          "A brief only counts once at least this many real historical occurrences had "
          "measured market data behind them.", kind="int", lo=1, hi=10),
    Field("event_precedent_extended_hold_enabled", "protect", "Precedent: extended hold",
          "When a usable precedent brief is active and a signal already clears the normal "
          "confidence bar, allow a longer paper-trade hold ceiling than usual. Stop-loss and "
          "sizing are never touched. Has no effect unless the row above is also on."),
    Field("event_precedent_extended_hold_minutes", "protect",
          "Precedent: extended hold ceiling (minutes)",
          "4320 = 3 days.", kind="int", lo=60, hi=20160),
    # Protections
    Field("protections_enabled", "protect", "Protections",
          "Master switch for everything in this group."),
    Field("session_filter_enabled", "protect", "Trade only in session",
          "Only in the hours below (12:30-22:30 IST by default)."),
    Field("session_start_utc", "protect", "Session start",
          "Entered as a UTC hour (Binance's clock); the IST time is shown beside it.",
          kind="int", lo=0, hi=23),
    Field("session_end_utc", "protect", "Session end",
          "Entered as a UTC hour; the IST time is shown beside it.", kind="int", lo=1, hi=24),
    Field("session_weekdays_only", "protect", "Weekdays only", ""),
    Field("daily_loss_limit_pct", "protect", "Daily loss limit (%)",
          "Stop opening trades for the day after this loss.", kind="float", lo=0.5, hi=20),
    Field("max_same_direction_positions", "protect", "Max same-direction positions",
          "All coins move with BTC; this caps correlated exposure.", kind="int", lo=1, hi=10),
    # AI
    Field("groq_signal_review_enabled", "ai", "AI review before a trade",
          "The AI can never add confidence, only take it away — and only if "
          "'AI review can block a trade' below is also on."),
    Field("ai_review_can_block_trade", "ai", "AI review can block a trade",
          "Off (default, 28 Sep): the Groq Sentinel still reviews every signal and its "
          "verdict/summary/delta are still recorded, but the delta no longer touches the "
          "confidence used for the min-confidence floor check — the AI's opinion can no "
          "longer be the reason a signal never becomes a paper trade. On: today's older "
          "behaviour, where a REJECT's penalty can push a signal below the floor. Applies "
          "immediately either way."),
    Field("position_review_enabled", "ai", "AI review of open trades",
          "Two close votes 10 minutes apart are needed."),
    Field("groq_postmortem_enabled", "ai", "AI post-mortem after a trade", ""),
    Field("pre_trade_review_cache_ttl_seconds", "ai", "Pre-trade review cache (seconds)",
          "Reuse the last review for a near-identical repeat signal (same symbol/direction/"
          "setup, price within 0.15%). 0 disables.", kind="int", lo=0, hi=900),
    Field("event_monitor_enabled", "ai", "World event monitor",
          "Web-searched news, graded 1 to 5.", live=False),
    Field("move_attribution_enabled", "ai", "Why-it-moved analysis", "Hourly, on /moves."),
    # v2
    Field("v2_shadow_enabled", "v2", "v2 live shadow",
          "Record v2 setups live without trading. Results on /v2."),
    Field("v2_backtest_enabled", "v2", "Daily v2 backtest",
          "07:47 IST every day, in a separate process."),
    Field("v2_backtest_years", "v2", "Backtest years", "", kind="float", lo=0.5, hi=5),
    # Storage
    Field("db_retention_days", "storage", "Days kept in the database",
          "Older rows move to JSON files, never deleted.", kind="int", lo=90, hi=3650),
    # API keys — editable from the phone, no server access needed. Leaving
    # the box blank and saving keeps whatever key is already set; the real
    # value is never sent back to the page once saved, only whether one exists.
    Field("groq_api_key", "keys", "Groq API key",
          "The main AI provider — free tier. console.groq.com/keys",
          kind="secret"),
    Field("openrouter_api_key", "keys", "OpenRouter API key",
          "Falls back to this when Groq is rate-limited or down. openrouter.ai/keys",
          kind="secret"),
    Field("gemini_api_key", "keys", "Google Gemini API key",
          "Quick position reviews and news scoring. aistudio.google.com/apikey",
          kind="secret"),
    Field("anthropic_api_key", "keys", "Anthropic API key",
          "Deepest research and post-mortems (Claude). console.anthropic.com",
          kind="secret"),
    Field("telegram_bot_token", "keys", "Telegram bot token",
          "Deploy, crash and trade alerts. Get one from @BotFather.", kind="secret"),
    Field("twelvedata_api_key", "keys", "Twelve Data API key",
          "Gold, silver, oil prices. Ignored when Binance only is on. twelvedata.com",
          kind="secret"),
    Field("coingecko_api_key", "keys", "CoinGecko API key",
          "Optional — raises the free rate limit. Ignored when Binance only is on.",
          kind="secret"),
    Field("cryptopanic_auth_token", "keys", "CryptoPanic token",
          "News headlines for sentiment. cryptopanic.com/developers/api",
          kind="secret"),
)
BY_KEY = {f.key: f for f in FIELDS}

# Old strategy_config columns that map onto fields, for the one-time seed.
LEGACY_STRATEGY = ("crypto_min_confidence", "high_conviction_only",
                   "crypto_volume_spike_enabled", "crypto_htf_filter_enabled",
                   "binance_klines_enabled", "binance_oi_enabled", "orderflow_enabled",
                   "groq_signal_review_enabled", "bank_size")


def coerce(field: Field, value):
    """The value in the field's type and range, or ValueError."""
    if field.kind == "bool":
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    if field.kind in ("int", "float"):
        num = float(value)
        if num != num:
            raise ValueError(f"{field.key}: not a number")
        if field.lo is not None and num < field.lo:
            raise ValueError(f"{field.key}: below {field.lo}")
        if field.hi is not None and num > field.hi:
            raise ValueError(f"{field.key}: above {field.hi}")
        return int(round(num)) if field.kind == "int" else num
    if field.kind == "choice":
        if value not in field.choices:
            raise ValueError(f"{field.key}: not one of {field.choices}")
        return value
    if field.kind == "secret":
        # "" means "leave it as it is" — a key is never cleared by accident
        # from a blank box, and the caller filters "" out before saving so a
        # blank submission never overwrites a real key with nothing.
        return "" if value is None else str(value).strip()
    return value


def apply(settings, values: dict) -> list[str]:
    """Set known keys on the live settings object. Returns the keys applied."""
    done = []
    for key, value in values.items():
        field = BY_KEY.get(key)
        if field is None:
            continue
        if field.kind == "secret" and value == "":
            continue
        try:
            coerced = coerce(field, value)
            if field.kind == "secret" and key in SECRET_STR_KEYS:
                from pydantic import SecretStr
                coerced = SecretStr(coerced)
            setattr(settings, key, coerced)
            done.append(key)
        except (TypeError, ValueError):
            continue
    return done


def _is_set(settings, key: str) -> bool:
    v = getattr(settings, key, None)
    if v is None:
        return False
    return bool(v.get_secret_value()) if hasattr(v, "get_secret_value") else bool(v)


def describe(settings, stored: dict) -> list[dict]:
    """
    Every field with its current value and where that value comes from.

    A field of kind="secret" never carries its real value here — this feeds
    a public, unauthenticated GET — only whether one is set.
    """
    return [{
        "key": f.key, "group": f.group, "group_label": GROUPS[f.group],
        "label": f.label, "help": f.help, "kind": f.kind, "live": f.live,
        "lo": f.lo, "hi": f.hi,
        "value": None if f.kind == "secret" else getattr(settings, f.key, None),
        "is_set": _is_set(settings, f.key) if f.kind == "secret" else None,
        "source": "saved" if f.key in stored else "default",
    } for f in FIELDS]
