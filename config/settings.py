from __future__ import annotations

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Application environment configuration.
    Non-secret parameters and toggles are managed dynamically in the database
    via the /settings admin UI.
    """
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Core Server & Storage
    database_url: str = "sqlite+aiosqlite:///./crypto_engine.db"
    # Path to the CA certificate the database server's certificate is signed
    # by. Aiven issues each project its own CA rather than using a public one,
    # so `sslmode=verify-full` cannot work without pointing at that file —
    # which is why the URL has always said `require`, i.e. encrypt but do not
    # check who is on the other end. Download the project CA from the Aiven
    # console, put it on the box, set this, and change the URL to verify-full.
    database_ssl_ca: str = ""

    # How long each kind of row is kept.
    #
    # These were one hardcoded `days=3` covering snapshots, commodity snapshots
    # and the signal log alike. Three days is right for a log you read when
    # something breaks. It is fatal for a training set: crypto_snapshots is the
    # only table with features AND forward-looking labels, and a cleanup job
    # every six hours meant no model could ever be fitted on more than three
    # days of it. Nothing about the plan to learn from history works until
    # history survives.
    #
    # The cost of keeping it is small. Seven symbols at one snapshot every two
    # minutes is ~5,000 rows a day; at roughly 150 bytes a row, 120 days is
    # about 90 MB — well inside a free managed-Postgres tier.
    # How long a snapshot stays in the LIVE table. Older rows are moved to
    # crypto_snapshots_archive / commodity_snapshots_archive, never deleted:
    # all history is kept for later analysis. 0 = never move.
    # The signal log is never trimmed at all — it is the evaluation record.
    snapshot_retention_days: int = 120
    port: int = 8080

    # Telegram Notifications (optional in dev, required in prod)
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None

    # Groq AI Sentinel (Key in env; Model chosen via Admin UI in DB)
    groq_api_key: SecretStr | None = None
    groq_model: str = "qwen/qwen3.8-27b"
    groq_signal_review_enabled: bool = True

    # What a REJECT verdict costs the signal's confidence. Sized to sink a
    # typical 0.70-0.75 setup below the threshold while leaving a strong one
    # standing: the reviewer gets a real say without a unilateral veto, and
    # the confidence threshold stays the single place a signal is refused.
    groq_reject_penalty: float = 0.15

    # Two jobs, two models. The pre-trade check holds a signal up while it
    # runs, so it buys speed. The post-mortem runs after the money is already
    # decided and nothing waits on it, so it buys judgement instead.
    groq_postmortem_model: str = "openai/gpt-oss-120b"
    groq_postmortem_enabled: bool = True

    # Only the gpt-oss models take this. Anything else 400s on it, so the
    # client retries once without it rather than treating an unsupported
    # parameter as an unsupported model and silently downgrading.
    groq_reasoning_effort: str = "high"

    # ── Model providers ──────────────────────────────────────────────────
    #
    # Four accounts, three jobs, and the jobs want different things.
    #
    # A pre-trade check sits in the signal path: if it has not answered in a
    # couple of seconds the price it was asked about is gone, so it wants the
    # fastest model that follows instructions. Groq at ~1000 tok/s is that,
    # and OpenRouter behind it turns a free-tier rate limit — which arrives at
    # the worst moment, by definition — into a slower answer rather than a
    # silently missing one.
    #
    # A post-mortem on a closed trade has no deadline whatsoever. The trade is
    # booked; nothing waits on it. What it produces is a labelled dataset that
    # only becomes worth having if the labels are any good, so it gets real
    # reasoning. At roughly 600 reviews a month this is a couple of dollars.
    #
    # The weekly research pass reads aggregated statistics — a few kilobytes,
    # never raw candles — and proposes what to test next. Four calls a month,
    # so it gets the strongest model available and the cost is a rounding
    # error.
    #
    # Format: "provider:model, provider:model" in preference order. Unknown
    # providers and entries whose key is unset are skipped, so a chain can
    # name a provider you have not signed up for yet without breaking.
    openrouter_api_key: SecretStr | None = None
    gemini_api_key: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None

    # Groq first, as the owner asked. Web search is gpt-oss with Groq's
    # built-in browser_search tool ("+search"): groq/compound and
    # compound-mini were decommissioned on 21 Sep 2026 and now return errors.
    # The reviews it writes are stored with their news context so the local
    # model can study them later.
    llm_chain_pre_trade: str = (
        "groq:openai/gpt-oss-120b, groq:openai/gpt-oss-20b, openrouter:qwen/qwen3-32b")
    llm_chain_post_trade: str = (
        "groq:openai/gpt-oss-120b+search, groq:openai/gpt-oss-120b, anthropic:claude-sonnet-5")
    # The world-events briefing. Only web-searching models: a model without
    # search asked for "today's news" invents it.
    llm_chain_briefing: str = (
        "groq:openai/gpt-oss-120b+search, groq:openai/gpt-oss-20b+search")
    market_briefing_enabled: bool = True
    market_briefing_minutes: int = 30
    # Hourly: why each watchlist coin moved, and how our signals fared.
    llm_chain_attribution: str = (
        "groq:openai/gpt-oss-120b+search, groq:openai/gpt-oss-120b")
    move_attribution_enabled: bool = True
    # The event monitor: adaptive, jittered web checks with a daily cap. The
    # free tier's daily requests are shared by every role, so the monitor
    # takes at most 120 of them. Shadow mode only.
    event_monitor_enabled: bool = True
    event_monitor_daily_cap: int = 120
    llm_chain_briefing_calm: str = (
        "groq:openai/gpt-oss-20b+search, groq:openai/gpt-oss-120b+search")
    # Labelling past moves from the Binance lake (scripts/review_history.py).
    # Local first: it is bulk work and the free Groq requests are shared with
    # live trading. The biggest moves use web search to find that day's news.
    llm_chain_history: str = "ollama:qwen3:8b, groq:openai/gpt-oss-120b"
    llm_chain_history_search: str = "groq:openai/gpt-oss-120b+search"
    # Entry protections (analysis/protections.py): session window, daily
    # loss limit, losing-streak brake, pair cooldown, correlated exposure,
    # stop-vs-fee floor and liquidation distance.
    protections_enabled: bool = True
    session_filter_enabled: bool = True
    session_start_utc: int = 7
    session_end_utc: int = 17
    session_weekdays_only: bool = True
    daily_loss_limit_pct: float = 3.0
    max_same_direction_positions: int = 2
    # v2 setups (analysis/v2_setups.py) run live in shadow every 5 minutes:
    # recorded and resolved with the backtest's fill rules, never traded.
    v2_shadow_enabled: bool = True
    v2_shadow_minutes: int = 5
    # The server downloads the v2 test data from data.binance.vision itself
    # (it can reach Binance; the owner's laptop is not needed) and re-runs
    # the v2 backtest daily at this UTC hour. Results on /v2.
    v2_backtest_enabled: bool = True
    v2_backtest_years: float = 2.0
    v2_backtest_hour_utc: int = 2
    v2_lake_dir: str = "data/lake"
    v2_reports_dir: str = "data/reports"
    v2_backtest_timeout_minutes: int = 90   # 7 coins x 2 years x 12 variants at nice 19
    v2_backtest_download: bool = True     # fetch missing data before each run
    # The database keeps this many days; older rows go to gzipped JSON files
    # under cold_storage_dir (storage/cold_storage.py), never deleted outright.
    db_retention_days: int = 365
    cold_storage_dir: str = "data/archive"
    move_attribution_minutes: int = 60
    move_attribution_window_hours: int = 12
    # Reviewing what is already open.
    #
    # A losing position has to keep earning the right to stay open: the local
    # read of the market plus a bounded model adjustment must clear 75%, or it
    # closes now rather than waiting for the stop. That can only ever close a
    # trade EARLIER, never hold one past an exit that already fired.
    #
    # Thirty seconds is the tick; five minutes is the review. No market
    # reconsiders itself twice a minute, and without the gap a single position
    # open for two hours would be 240 reviews.
    position_review_enabled: bool = True
    position_review_interval_seconds: int = 300

    # What to do with a losing position when no model answers.
    #
    # Two defensible answers and they fail in opposite directions. Falling
    # back to the local read keeps trading through a provider outage, at the
    # cost of holding positions a model might have closed. Closing honours
    # "keep the loss to a minimum" literally, at the cost of booking real
    # trades because of an API problem — a Groq hiccup at 3am shuts the book.
    #
    # Set to close, on instruction. The local read is still what decides
    # everything else; this is only the tie-break for the case where the half
    # that was asked for cannot be obtained.
    position_review_close_on_outage: bool = True
    # A model-driven close must be said twice in a row (the next review, about
    # 5 min later). The 22-25 Sep log flipped hold/close 18 times in 52 reviews;
    # LLM verdicts flip between adjacent steps, one vote is noise. The stop
    # still protects the position while the second vote is pending.
    position_review_confirm_close: bool = True
    # The two votes must be at least this far apart, and no model-driven
    # close happens in a position's first minutes (research: 2 votes >= 15 min
    # apart, minimum hold 2 x 15m bars before any discretionary close).
    position_review_confirm_gap_minutes: int = 10
    position_review_min_hold_minutes: int = 30

    # A winning position is reviewed less often than a losing one. It is not
    # deciding whether to exist — the trail already bounds what it can give
    # back — it is only choosing how much rope that trail gets, and a trail
    # distance that is roughly right is worth very little less than one that
    # is exactly right. Fifteen minutes keeps the model in the loop on every
    # trade, as instructed, without paying loser rates for it.
    position_review_interval_seconds_winning: int = 900

    # Reviewing a position that is still open is its own job, and a cheaper
    # one than it looks. The score is already 75% settled by the local read of
    # price, time and distance to the stop; the model supplies a bounded
    # +0.10/-0.15 nudge on top. That is a quick market read, not deep
    # reasoning, and putting it on the post-mortem chain would cost about
    # Rs 2,000 a month on its own — the whole budget, before the post-mortems
    # and the research pass it would be sharing with. Flash does the same job
    # for about Rs 400, with the free tier behind it.
    # Where a self-hosted model answers, if there is one. Empty means there
    # is not, and every chain that names ollama simply skips it — so this one
    # setting is the whole on/off switch and nothing else has to change.
    #
    # The engine runs on EC2 and the model runs at home, so this is a
    # Tailscale hostname rather than localhost. That the laptop might be
    # asleep is not a problem to solve: it is the first entry in a chain, and
    # an unreachable first entry is what the rest of the chain is for.
    ollama_base_url: str = ""

    # Scoring a headline is a short structured classification — a number, a
    # confidence and one tag from a closed list. That is the shape of work a
    # small local model does well and a frontier model is wasted on, and at
    # a few hundred headlines a day it is also the one that would cost the
    # most through a metered API. Local first, and the free tiers behind it.
    llm_chain_news_scoring: str = (
        "ollama:qwen3:1.7b, groq:openai/gpt-oss-20b, gemini:gemini-3.1-flash-lite")

    # Groq leads, not the laptop: this call sits inside the 30-second paper
    # tick with a 12-second budget, and an 8B model on the i5 needs 10-25 s
    # just to read the prompt. gemini-2.5-* is being shut down in October.
    llm_chain_position_review: str = (
        "groq:openai/gpt-oss-120b, gemini:gemini-3.1-flash-lite, gemini:gemini-3.6-flash")

    llm_chain_research: str = (
        "anthropic:claude-opus-5-5, anthropic:claude-opus-5, gemini:gemini-3.1-pro-preview")

    # Market Data & External APIs
    twelvedata_api_key: str | None = None
    twelvedata_symbols: str = "XAU/USD,XAG/USD,WTI/USD"
    coindcx_poll_interval_seconds: int = 30
    coingecko_api_key: str | None = None
    coingecko_poll_interval_seconds: int = 60
    cryptopanic_auth_token: str | None = None
    cryptopanic_poll_interval_seconds: int = 300

    # Crypto & Commodities Watchlist
    crypto_watchlist_seed: str = (
        "btcusdt,ethusdt,bnbusdt,solusdt,xrpusdt,dogeusdt,adausdt,linkusdt,ltcusdt,dotusdt"
    )
    crypto_kline_interval: str = "1m"
    crypto_timeframes: str = "15m,30m,1h,4h,1d"
    binance_ws_enabled: bool = False
    # Per-collector switches, editable on /settings (config/overrides.py).
    # binance_only_mode overrides all three to off.
    coindcx_enabled: bool = True
    coingecko_enabled: bool = True
    twelvedata_enabled: bool = True
    binance_klines_enabled: bool = True
    binance_klines_seconds: int = 60

    # One source of truth, for measuring the engine rather than the plumbing.
    # Binance klines carry candles, depth and — in this mode — the price too,
    # so price and candles cannot disagree. A gold signal once published a 43%
    # target because a ticker feed wrote $0.00004 over a $4,341 price and
    # poisoned the candle the ATR was measured from; one source removes that
    # whole class of failure. Turns off CoinDCX, CoinGecko, Twelve Data and
    # the news feeds.
    binance_only_mode: bool = False

    # A tick further than this from the running price is a feed fault, not a
    # move. Nothing legitimate gaps 60% between two polls of a liquid pair,
    # and the cost of believing one bad print is a signal built on it.
    max_price_jump_pct: float = 0.60

    # Ceiling on a published target. The level policy derives distance from
    # ATR and has a floor but no roof, so a corrupted ATR produced a 43.5%
    # scalp target with a 36-minute horizon. Past this the setup is not
    # improbable, it is evidence something upstream is broken: refuse it.
    max_target_pct: float = 0.15

    # Strategy & Conviction defaults (Managed in DB via /settings)
    min_confidence: float = 0.65
    max_stake_pct: float = 0.03
    bank_size: float = 10000.0
    signal_cooldown_minutes: int = 10
    crypto_min_confidence: float = 0.60
    crypto_signal_cooldown_minutes: int = 15
    crypto_snapshot_interval_seconds: int = 120
    crypto_alert_telegram: bool = True
    crypto_volume_spike_enabled: bool = True
    crypto_htf_filter_enabled: bool = True
    binance_oi_enabled: bool = True
    orderflow_enabled: bool = True
    high_conviction_only: bool = False

    # Paper Trading defaults (Dynamically managed in DB via PaperTradingConfig)
    paper_trading_enabled: bool = True
    paper_starting_wallet: float = 3000.0
    paper_target_wallet: float = 20000.0
    paper_leverage: float = 10.0
    paper_stop_pct_of_margin: float = 0.20
    paper_reward_risk: float = 2.0
    paper_min_confidence: float = 0.70
    paper_max_concurrent: int = 3
    paper_max_hold_minutes: int = 240
    paper_scaled_sizing: bool = True
    paper_trailing_enabled: bool = True
    paper_scaled_leverage: bool = False
    paper_ladder_enabled: bool = False
    paper_ladder_tight: bool = False
    paper_max_leverage: float = 25.0
    paper_tick_interval_seconds: int = 30
    paper_usdt_inr: float = 102.0
    paper_alert_telegram: bool = True

    # Ingest / API Auth & Security
    api_auth_token: str = ""
    api_rate_limit_requests: int = 120
    api_rate_limit_window_seconds: int = 60
    api_auth_rate_limit_requests: int = 10
    api_auth_rate_limit_window_seconds: int = 300
    sentiment_ingest_token: str = ""
    # Comma-separated proxy addresses whose X-Forwarded-For is believed.
    # Empty = none, which is right while nothing sits in front of :8080.
    trusted_proxy_ips: str = ""
    sentiment_feeds_enabled: bool = True
    # Headlines from the news scorer -> CryptoState.sentiment_score, every 5 min.
    news_sentiment_enabled: bool = True
    # RSI divergence detector. It never fired before (window too short); now
    # that it can, it trades against trends. Off until it passes the null test.
    rsi_divergence_enabled: bool = False
    # Longs only above the 20-day average, shorts only below it.
    daily_trend_filter_enabled: bool = True
    # No new paper trades around FOMC / CPI / NFP / PCE / PPI releases, or for
    # an hour after confidently scored high-impact news (war, tariffs, rates).
    event_blackout_enabled: bool = True
    fear_greed_refresh_minutes: int = 60
    crypto_max_stake_pct: float = 0.02
    use_finbert: bool = False
    crypto_auto_execute: bool = False

    @property
    def prediction_timeframes(self) -> list[str]:
        """Return list of active prediction timeframes."""
        return [t.strip().lower() for t in self.crypto_timeframes.split(",") if t.strip()]


settings = Settings()
