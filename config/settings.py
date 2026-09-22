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
    snapshot_retention_days: int = 120
    signal_log_retention_days: int = 30
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
    sentiment_feeds_enabled: bool = True
    fear_greed_refresh_minutes: int = 60
    crypto_max_stake_pct: float = 0.02
    use_finbert: bool = False
    crypto_auto_execute: bool = False

    @property
    def prediction_timeframes(self) -> list[str]:
        """Return list of active prediction timeframes."""
        return [t.strip().lower() for t in self.crypto_timeframes.split(",") if t.strip()]


settings = Settings()
