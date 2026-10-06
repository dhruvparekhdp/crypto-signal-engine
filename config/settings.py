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
    # A CAUTION verdict used to cost nothing unless the model's own
    # confidence_delta happened to be negative — a diplomatic "proceed with
    # caution" could pass through at delta 0.0. This guarantees a minimum
    # cost: applied as delta = min(delta, -groq_caution_min_penalty), so it
    # only ever makes the penalty MORE negative, never overrides a larger
    # self-assessed one. Deliberately smaller than groq_reject_penalty —
    # CAUTION is not REJECT.
    groq_caution_min_penalty: float = 0.01

    # A second signal on the same symbol/direction/setup, fired minutes
    # apart with the price barely moved, is not a new question — it is the
    # same question asked twice. Cache the last real pre-trade review
    # answer and reuse it while BOTH hold: within this many seconds, and
    # within a small relative move of the price that was actually reviewed
    # (collectors/macro_sentinel.py's _PreTradeReviewCache). Short on
    # purpose — this is a safety-relevant AI review, not a read-heavy
    # endpoint, so the window stays tight even though it is configurable.
    # 0 disables the cache outright.
    pre_trade_review_cache_ttl_seconds: int = 180

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

    # Free models only on OpenRouter (the account has no credits: paid models and the :online search
    # plugin return HTTP 402). Chains that must search therefore end on Groq's own +search tool.
    # Groq first, as the owner asked, and every chain that does not need web search ends on OpenRouter: a
    # Groq free-tier 429 is org-wide, so consecutive Groq entries all die
    # together and only a different provider at the tail survives it.
    # qwen/qwen3.8-27b is deliberately not a Groq lead (live 1 Oct logs: 400
    # JSON-validation failures and 429 "request too large" on every call).
    # Web search is gpt-oss with Groq's
    # built-in browser_search tool ("+search"): groq/compound and
    # compound-mini were decommissioned on 21 Sep 2026 and now return errors.
    # The reviews it writes are stored with their news context so the local
    # model can study them later.
    llm_chain_pre_trade: str = (
        "groq:openai/gpt-oss-120b, groq:openai/gpt-oss-20b, "
        "hf:meta-llama/Llama-3.1-8B-Instruct, openrouter:google/gemma-4-31b-it:free")
    llm_chain_post_trade: str = (
        "groq:openai/gpt-oss-120b+search, groq:openai/gpt-oss-120b, "
        "hf:meta-llama/Llama-3.1-8B-Instruct, openrouter:nvidia/nemotron-3-ultra-550b-a55b:free")
    # The world-events briefing. Every entry can actually search.
    llm_chain_briefing: str = (
        "groq:openai/gpt-oss-120b+search, groq:openai/gpt-oss-20b+search, "
        "openrouter:google/gemma-4-31b-it:free+search, "
        "hf:analyst+search")
    market_briefing_enabled: bool = True
    market_briefing_minutes: int = 60          # Groq's free daily token quota ran out by midday at 30
    # After a briefing that found nothing, wait this long before the next one unless an event is near.
    market_briefing_quiet_minutes: int = 120
    # Hourly: why each watchlist coin moved, and how our signals fared.
    llm_chain_attribution: str = (
        "groq:openai/gpt-oss-120b+search, groq:openai/gpt-oss-20b+search, "
        "openrouter:google/gemma-4-31b-it:free+search, "
        "hf:analyst+search")
    move_attribution_enabled: bool = True
    # The event monitor: adaptive, jittered web checks with a daily cap.
    event_monitor_enabled: bool = True
    event_monitor_daily_cap: int = 30           # 104 searches/day found nothing new and spent Groq's daily quota
    llm_chain_briefing_calm: str = (
        "groq:openai/gpt-oss-20b+search, groq:openai/gpt-oss-120b+search, "
        "openrouter:google/gemma-4-31b-it:free+search, "
        "hf:analyst+search")
    # Labelling past moves from the Binance lake (scripts/review_history.py).
    # Local first: it is bulk work and the free Groq requests are shared with
    # live trading. The biggest moves use web search to find that day's news.
    llm_chain_history: str = (
        "ollama:qwen3:8b, groq:openai/gpt-oss-120b, "
        "openrouter:nvidia/nemotron-3-ultra-550b-a55b:free")
    llm_chain_history_search: str = (
        "groq:openai/gpt-oss-120b+search, groq:openai/gpt-oss-20b+search")
    # Entry protections (analysis/protections.py): session window, daily
    # loss limit, losing-streak brake, pair cooldown, correlated exposure,
    # stop-vs-fee floor and liquidation distance.
    # Profit lock (the owner's exit): once price moves this % in favour, the
    # stop jumps to lock_to % beyond entry (never less than costs), then
    # trails trail % behind the best price. analysis.paper_trading.ProfitLock.
    # Swing book (analysis/swing_book.py): the 4h strategies that survived the 5-year test, traded with
    # exactly the tested exits (3xATR stop, 3R target, 7 days). paper_open_families decides which signal
    # families may open paper trades; the rest are logged as shadow signals only.
    swing_enabled: bool = True
    swing_strategies: str = ("4h@vol_breakout:z=3.0,4h@keltner_break:k=2.5,4h@donchian:n=100,4h@ichimoku,"
                             "8h@keltner_break:k=2.0,8h@vol_breakout:z=3.0,8h@donchian:n=100,8h@ichimoku")
    swing_scan_seconds: int = 300
    swing_risk_pct: float = 0.03
    # Backtest (scripts/portfolio_wallet, fixed vs adaptive): cutting risk in drawdowns barely reduced the
    # worst drawdown but halved the profit, so fixed risk is the default.
    swing_adaptive_risk: bool = False
    swing_max_open: int = 0                   # 0 = no limit: free margin and leverage decide how many fit
    swing_max_leverage: float = 10.0          # a ceiling: each trade uses the lowest leverage its margin needs
    swing_hold_minutes: int = 10080
    swing_signal_max_age_minutes: int = 60
    # Regime check (analysis/regime_gate.py): skip swing signals when Bitcoin's 30-day volatility is in the top
    # third of its past year, or the coin's daily ADX is above 30. "shadow" tags every signal and still trades it,
    # so the live book can confirm the backtest before the filter is allowed to block anything; "on" skips.
    swing_regime_filter: str = "on"
    swing_regime_vol_rank_max: float = 0.67
    swing_regime_adx_max: float = 0.0          # 0 = trend check off: it blocked 10 of 12 coins in a trending week
    # Crypto signals arrive in same-direction clusters (97% of multi-signal bars), so several open trades are one
    # bet. Capping trades per direction did more for the backtested wallet than any risk level.
    swing_max_same_side: int = 0              # 0 = no limit (2 was the best backtest setting; see design notes)
    # Five years show no edge on these: original and mirror both average about zero.
    swing_exclude_symbols: str = "BCHUSDT,LTCUSDT"
    # Live swing book (execution/live_book.py): copies swing entries to a small Binance USD-M futures account.
    # "off" | "testnet" (testnet.binancefuture.com, fake money) | "live". Keys come from the environment only:
    # futures-enabled, withdrawals disabled, IP-restricted to this server.
    live_trading_mode: str = "off"
    binance_api_key: SecretStr | None = None
    binance_api_secret: SecretStr | None = None
    binance_testnet_api_key: SecretStr | None = None
    binance_testnet_api_secret: SecretStr | None = None
    live_max_balance_usdt: float = 20.0       # the bot treats the account as at most this, whatever it holds
    live_risk_pct: float = 0.015              # risk per trade
    live_max_risk_pct: float = 0.025          # Binance's minimum order may force more; refused above this
    live_max_open: int = 0                    # 0 = no limit
    live_max_leverage: int = 10
    live_daily_loss_pct: float = 0.06         # no new entries after losing this share of the capped balance today
    live_respect_regime_filter: bool = True   # real money skips wild-market / strong-trend signals even in shadow
    paper_open_families: str = "swing"
    # The 60-minute "flat or losing -> close" rule cut trades at small losses; off unless asked for.
    smart_60m_enabled: bool = False
    candle_refresh_enabled: bool = True
    profit_lock_enabled: bool = True
    profit_lock_at_pct: float = 0.5
    profit_lock_to_pct: float = 0.35
    profit_lock_trail_pct: float = 0.15
    # Default OFF — this changes real paper-trading risk behaviour, unlike
    # everything else in this block. Review finding (28 Sep): a flat
    # profit_lock_at_pct usually arms BEFORE the runner-trail's own
    # activation (activate_at_r, in R), so profit-lock tightens the stop
    # first every time and the trail's "ride to 2R" branch never fires —
    # almost every winner gets walked down to a small locked gain instead
    # of being allowed to run. On: profit-lock only arms once price has
    # passed max(profit_lock_at_pct, 1.3x the trail's own activation
    # distance for that position), so the trail gets first look. Off
    # (default): behaviour is byte-for-byte identical to before this
    # setting existed. See Position.apply_profit_lock.
    profit_lock_defers_to_trail_enabled: bool = False
    # Deal scanner (analysis/deal_scanner.py): signals on the same tick are
    # taken best first; an open trade whose target pays >= premium_roe_pct on
    # margin fills the book until it closes.
    premium_roe_pct: float = 50.0
    premium_fills_book: bool = True
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
    v2_backtest_enabled: bool = False           # timed out at 90 min every day; analysis/lab replaced it
    v2_backtest_years: float = 2.0
    v2_backtest_hour_utc: int = 2
    v2_lake_dir: str = "data/lake"
    v2_reports_dir: str = "data/reports"
    v2_backtest_timeout_minutes: int = 90   # 7 coins x 2 years x 12 variants at nice 19
    v2_backtest_download: bool = False     # Use existing lake data without stalling on remote download
    # The database keeps this many days; older rows go to gzipped JSON files
    # under cold_storage_dir (storage/cold_storage.py), never deleted outright.
    db_retention_days: int = 365
    cold_storage_dir: str = "data/archive"
    move_attribution_minutes: int = 180         # hourly searching calls exhausted Groq's daily quota
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
    # Hugging Face Space endpoint (e.g. https://user-space.hf.space) and optional token.
    hf_base_url: str = ""
    # Our own Hugging Face Space (huggingface_space/, Gradio + ZeroGPU, free): the last fallback for the search
    # roles. Called with the saved HF token (a private Space needs it); hf_space_api_key only if the Space sets
    # its own SPACE_API_KEY secret.
    hf_space_api_key: SecretStr | None = None
    hf_space_timeout_seconds: int = 240
    # JSON overrides for collectors/llm_budget.py's free-tier limits, e.g.
    # {"groq/openai/gpt-oss-120b": {"rpm": 30, "rpd": 1000, "tpm": 8000, "tpd": 200000}}
    llm_limits: str = ""
    hf_api_token: SecretStr | None = None

    # Scoring a headline is a short structured classification — a number, a
    # confidence and one tag from a closed list. That is the shape of work a
    # small local model does well and a frontier model is wasted on, and at
    # a few hundred headlines a day it is also the one that would cost the
    # most through a metered API. Local first, and the free tiers behind it.
    llm_chain_news_scoring: str = (
        "groq:qwen/qwen3.8-27b, groq:openai/gpt-oss-20b, "
        "hf:meta-llama/Llama-3.1-8B-Instruct, openrouter:google/gemma-4-31b-it:free")

    # Groq leads, not the laptop: this call sits inside the 30-second paper
    # tick with a 12-second budget, and an 8B model on the i5 needs 10-25 s
    # just to read the prompt. gemini-2.5-* is being shut down in October.
    llm_chain_position_review: str = (
        "groq:openai/gpt-oss-20b, hf:meta-llama/Llama-3.1-8B-Instruct, "
        "openrouter:google/gemma-4-31b-it:free")

    llm_chain_research: str = (
        "anthropic:claude-opus-5-5, anthropic:claude-opus-5, gemini:gemini-3.1-pro-preview")

    # Precedent-based event context (28 Sep) — NOT a price-direction
    # forecaster; the owner was told plainly no detector stack here can
    # promise that, and agreed. For an upcoming calendar event, a web-search
    # model looks up real historical precedent (same category, same US
    # administration), OUR OWN Binance Parquet lake measures what actually
    # happened around those precedent dates, and a second AI call
    # characterises the pattern grounded in those real numbers. Advisory
    # only: clamped through the exact same penalty-bucket scale as
    # calendar_caution above (see analysis/event_precedent.py), never
    # raises confidence, never lowers the entry bar. Genuinely rare — a
    # handful of AI calls a year, cached per event occurrence. Off by
    # default, shadow-first like every other AI-touching feature here.
    event_precedent_enabled: bool = False
    # How far ahead an event has to be before the job researches it.
    event_precedent_lookahead_days: int = 7
    # A brief may only touch anything live once at least this many
    # precedent occurrences had real measured market data behind them — one
    # data point is an anecdote, not a precedent.
    event_precedent_min_sample_size: int = 2
    # The "take longer time trades" half of the ask: when a usable brief is
    # active and a signal already clears the normal confidence bar on its
    # own, the paper engine may use this hold-time ceiling instead of
    # paper_max_hold_minutes for that one trade. Stop-loss and position
    # sizing are never touched — only the hold-time ceiling changes. Off
    # by default even when event_precedent_enabled is on, so turning the
    # research pipeline on does not by itself change trading behaviour.
    event_precedent_extended_hold_enabled: bool = False
    event_precedent_extended_hold_minutes: int = 4320   # 3 days
    llm_chain_event_precedent: str = (
        "groq:openai/gpt-oss-120b+search, groq:openai/gpt-oss-20b+search")

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
        "btcusdt,ethusdt,solusdt,bnbusdt,dogeusdt,xrpusdt,adausdt,avaxusdt,linkusdt,suiusdt,bchusdt"
    )
    crypto_kline_interval: str = "1m"
    crypto_timeframes: str = "15m,30m,1h,4h,1d"
    binance_ws_enabled: bool = False
    # Per-collector switches, editable on /settings (config/overrides.py).
    # binance_only_mode overrides all three to off.
    coindcx_enabled: bool = False               # matched 0 watchlist symbols: polling for nothing
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
    counter_trend_short_min_confidence: float = 0.80
    crypto_signal_cooldown_minutes: int = 45
    crypto_snapshot_interval_seconds: int = 120
    crypto_alert_telegram: bool = True
    crypto_volume_spike_enabled: bool = True
    crypto_htf_filter_enabled: bool = True
    binance_oi_enabled: bool = True
    orderflow_enabled: bool = True
    high_conviction_only: bool = False
    # Research phases 2-3 (27 Sep): Binance's free public liquidation and
    # trade streams, subscribed alongside the klines the socket already
    # carries — no extra connection, no key. Purely observational for
    # now: nothing in the signal engine reads state.liquidations or
    # state.large_trades yet, only /api/pipeline shows them. Off does not
    # take effect until the next reconnect, same as the kline interval.
    binance_liquidation_stream_enabled: bool = False
    binance_large_trade_stream_enabled: bool = False
    # Mirror review (27 Sep): for every fired signal, also synthesise the
    # opposite-direction candidate and give the AI reviewer a second look at
    # it in parallel. A candidate that isn't already strong enough to open
    # on round 0 is tracked and re-reviewed as the market moves, up to
    # mirror_review_max_rounds more times, before it wins, is rejected, or
    # times out against its own timeframe. Off by default — this changes
    # trading behaviour (a signal can now be held instead of opened
    # immediately), unlike the purely observational phases above.
    mirror_review_enabled: bool = False
    # Two separate switches on purpose. mirror_review_enabled turns on the
    # generation/tracking/review machinery itself (mirrors get built,
    # reviewed, and their trail shows up on the Signals page); this one
    # separately decides whether a candidate that wins its round is allowed
    # to actually queue a paper trade. Off (default): a winning candidate is
    # logged and its review trail shows "Not traded — mirror trading switch
    # is off" instead of opening anything — lets the feature run and be
    # watched with zero effect on real paper trades until this is flipped
    # on too. Has no effect while mirror_review_enabled is False.
    mirror_review_can_trade: bool = False
    # A tracked candidate is only re-reviewed once its local confidence has
    # moved by at least this much since its last AI review.
    mirror_review_confidence_delta_threshold: float = 0.05
    # A tracked candidate is only re-reviewed once at least this fraction of
    # its own timeframe has elapsed (e.g. 0.33 of a 1h signal is 20 minutes).
    mirror_review_min_elapsed_pct: float = 0.33
    # Hard cap on re-reviews per candidate after the initial round-0 review
    # (so at most 1 + mirror_review_max_rounds AI calls per candidate).
    mirror_review_max_rounds: int = 2
    # +/- range used to jitter the mirror candidate's target/stop distances
    # away from an exact mechanical reflection of the original.
    mirror_target_jitter_pct: float = 0.20
    # A single trade at or above this notional (USDT) is flagged as
    # "large" — a free, crude proxy for a big (possibly institutional)
    # participant. BTCUSDT trades often several times a second; this
    # threshold is what keeps almost all of them from ever reaching the
    # store's lock, let alone memory.
    large_trade_notional_usd: float = 50000.0

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

    # Dual-Mode Trading Engine (Intraday Scalp vs Delivery Swing)
    intraday_leverage: float = 10.0                 # 5x to 15x leverage
    delivery_wallet_allocation_pct: float = 8.0     # 5% to 10% of shared wallet
    delivery_leverage: float = 2.0                  # 1x to 3x swing leverage
    max_concurrent_intraday: int = 3                # Max simultaneous active intraday trades
    consecutive_loss_cooldown_minutes: int = 60     # Adaptive cooldown on 2 losses (60m-180m)
    anti_flip_cooldown_minutes: int = 90

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
    # During an event window, trade with the AI's bull/bear read of it
    # (analysis/event_bias.py) instead of pausing everything.
    event_bias_mode: bool = True
    # Research phase 1 (27 Sep): soften confidence during structural
    # institutional-flow windows — rebalancing, options/futures expiry,
    # thin weekend liquidity (analysis/event_calendar.caution). Never a
    # veto, and separate from event_blackout_enabled above, which is
    # FOMC/CPI/NFP specifically.
    calendar_caution_enabled: bool = True
    fear_greed_refresh_minutes: int = 60
    crypto_max_stake_pct: float = 0.02
    use_finbert: bool = False
    crypto_auto_execute: bool = False

    @property
    def prediction_timeframes(self) -> list[str]:
        """Return list of active prediction timeframes."""
        return [t.strip().lower() for t in self.crypto_timeframes.split(",") if t.strip()]


settings = Settings()
