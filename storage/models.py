from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, Float, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from storage.database import Base


def _now_utc() -> datetime:
    """Return current UTC time as naive datetime for TIMESTAMP WITHOUT TIME ZONE compatibility."""
    return datetime.now(UTC).replace(tzinfo=None)


class CryptoSnapshot(Base):
    """
    Periodic snapshot of crypto state (price, technical indicators, sentiment)
    used for multi-horizon model training and feature store.
    """

    __tablename__ = "crypto_snapshots"
    __table_args__ = (Index("ix_cs_symbol_ts", "symbol", "timestamp"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String, index=True)
    price: Mapped[float] = mapped_column(Float)
    volume_24h: Mapped[float] = mapped_column(Float, default=0.0)
    rsi_14: Mapped[float] = mapped_column(Float, default=50.0)
    macd_line: Mapped[float] = mapped_column(Float, default=0.0)
    macd_signal: Mapped[float] = mapped_column(Float, default=0.0)
    bollinger_upper: Mapped[float] = mapped_column(Float, default=0.0)
    bollinger_lower: Mapped[float] = mapped_column(Float, default=0.0)
    atr_14: Mapped[float] = mapped_column(Float, default=0.0)
    sentiment_score: Mapped[float] = mapped_column(Float, default=0.0)
    price_30m_later: Mapped[float] = mapped_column(Float, default=0.0)
    price_1h_later: Mapped[float] = mapped_column(Float, default=0.0)
    price_4h_later: Mapped[float] = mapped_column(Float, default=0.0)
    price_1d_later: Mapped[float] = mapped_column(Float, default=0.0)
    timestamp: Mapped[datetime] = mapped_column(DateTime, index=True)


class CommoditySnapshot(Base):
    """Periodic snapshot of commodity spot prices (Gold, Silver, Oil)."""

    __tablename__ = "commodity_snapshots"
    __table_args__ = (Index("ix_comms_symbol_ts", "symbol", "timestamp"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String, index=True)
    price: Mapped[float] = mapped_column(Float)
    rsi_14: Mapped[float] = mapped_column(Float, default=50.0)
    atr_14: Mapped[float] = mapped_column(Float, default=0.0)
    timestamp: Mapped[datetime] = mapped_column(DateTime, index=True)


class CryptoSignalLog(Base):
    """Log of all fired cryptocurrency trade signals with performance tracking."""

    __tablename__ = "crypto_signal_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String, index=True)
    signal_type: Mapped[str] = mapped_column(String)
    direction: Mapped[str] = mapped_column(String)                  # "long" | "short"
    trigger_description: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float)
    current_price: Mapped[float] = mapped_column(Float)
    target_price: Mapped[float] = mapped_column(Float, default=0.0)
    stop_loss: Mapped[float] = mapped_column(Float, default=0.0)
    edge_pct: Mapped[float] = mapped_column(Float)
    stake_pct: Mapped[float] = mapped_column(Float)
    timeframe: Mapped[str] = mapped_column(String)                  # "30m", "1h", "4h", "1d"
    sentiment_score: Mapped[float] = mapped_column(Float, default=0.0)
    indicators_summary: Mapped[str] = mapped_column(String, default="")
    outcome: Mapped[str] = mapped_column(String, default="pending") # pending/won/lost/expired
    pnl_pct: Mapped[float] = mapped_column(Float, default=0.0)
    timestamp: Mapped[datetime] = mapped_column(DateTime, index=True)


class CryptoWatchlistEntry(Base):
    """
    Crypto symbols the Binance WS collector streams. Lives in the DB — not an
    env var — so it can be edited at runtime from the dashboard's Crypto tab
    without a redeploy.
    """

    __tablename__ = "crypto_watchlist"

    symbol: Mapped[str] = mapped_column(String, primary_key=True)   # e.g. "btcusdt"
    added_at: Mapped[datetime] = mapped_column(DateTime, default=_now_utc)


class PaperCycle(Base):
    """
    One run of the paper-trading simulator, start to finish.

    A cycle ends when the wallet reaches the target or is exhausted, and then
    a fresh one starts. Keeping cycles as rows rather than a single running
    balance is what makes "restart and compare with tighter logic" possible —
    each cycle carries the configuration it ran under.
    """

    __tablename__ = "paper_cycles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    starting_wallet: Mapped[float] = mapped_column(Float)
    target_wallet: Mapped[float] = mapped_column(Float)
    wallet: Mapped[float] = mapped_column(Float)
    peak_wallet: Mapped[float] = mapped_column(Float)

    leverage: Mapped[float] = mapped_column(Float)
    stop_pct_of_margin: Mapped[float] = mapped_column(Float)
    reward_risk: Mapped[float] = mapped_column(Float)
    min_confidence: Mapped[float] = mapped_column(Float)
    trailing_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    scaled_sizing: Mapped[bool] = mapped_column(Boolean, default=True)
    # Leverage scaled by confidence too, bounded by volatility. Stored per
    # cycle so an old cycle still says how it was actually run.
    scaled_leverage: Mapped[bool] = mapped_column(Boolean, default=False)
    # Profit ladder: ratchet the stop as ROE crosses rungs.
    ladder_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    ladder_tight: Mapped[bool] = mapped_column(Boolean, default=False)

    # running | hit_target | busted | stopped
    status: Mapped[str] = mapped_column(String, default="running", index=True)
    note: Mapped[str] = mapped_column(String, default="")


class PaperTradingConfig(Base):
    """
    User-configurable parameters for the paper trading engine stored in DB.
    Allows runtime editing from /settings without touching environment files.
    """

    __tablename__ = "paper_trading_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    starting_wallet: Mapped[float] = mapped_column(Float, default=3000.0)
    target_wallet: Mapped[float] = mapped_column(Float, default=20000.0)
    leverage: Mapped[float] = mapped_column(Float, default=10.0)
    stop_pct_of_margin: Mapped[float] = mapped_column(Float, default=0.20)
    reward_risk: Mapped[float] = mapped_column(Float, default=2.0)
    min_confidence: Mapped[float] = mapped_column(Float, default=0.70)
    max_concurrent: Mapped[int] = mapped_column(Integer, default=3)
    max_hold_minutes: Mapped[int] = mapped_column(Integer, default=240)
    scaled_sizing: Mapped[bool] = mapped_column(Boolean, default=True)
    trailing_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    scaled_leverage: Mapped[bool] = mapped_column(Boolean, default=False)
    ladder_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    ladder_tight: Mapped[bool] = mapped_column(Boolean, default=False)
    max_leverage: Mapped[float] = mapped_column(Float, default=25.0)
    usdt_inr: Mapped[float] = mapped_column(Float, default=102.0)
    alert_telegram: Mapped[bool] = mapped_column(Boolean, default=True)


class AdminAuth(Base):
    """
    Administrator authentication hash and active session token.
    Stores salted PBKDF2 hash so no plaintext credentials ever exist in DB or code.
    """

    __tablename__ = "admin_auth"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    password_hash: Mapped[str] = mapped_column(String)
    salt: Mapped[str] = mapped_column(String)
    session_token: Mapped[str | None] = mapped_column(String, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now_utc)


class StrategyConfig(Base):
    """
    Runtime strategy parameters editable via /settings without redeploying.
    """

    __tablename__ = "strategy_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    crypto_min_confidence: Mapped[float] = mapped_column(Float, default=0.70)
    high_conviction_only: Mapped[bool] = mapped_column(Boolean, default=True)
    crypto_volume_spike_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    crypto_htf_filter_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    binance_klines_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    binance_oi_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    orderflow_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    groq_signal_review_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    groq_model: Mapped[str] = mapped_column(String, default="qwen/qwen3.8-27b")
    bank_size: Mapped[float] = mapped_column(Float, default=10000.0)
    min_confidence: Mapped[float] = mapped_column(Float, default=0.65)


class PaperPosition(Base):
    """
    An open paper position. Deleted on close — the record lives on as a
    PaperTrade. Persisted rather than held in memory so a redeploy does not
    silently abandon open positions mid-cycle.
    """

    __tablename__ = "paper_positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    cycle_id: Mapped[int] = mapped_column(Integer, index=True)
    symbol: Mapped[str] = mapped_column(String, index=True)
    side: Mapped[str] = mapped_column(String)                # long | short

    signal_price: Mapped[float] = mapped_column(Float)       # quoted
    entry_price: Mapped[float] = mapped_column(Float)        # filled
    margin: Mapped[float] = mapped_column(Float)
    leverage: Mapped[float] = mapped_column(Float)
    coin_qty: Mapped[float] = mapped_column(Float)
    usdt_inr: Mapped[float] = mapped_column(Float)

    stop_price: Mapped[float] = mapped_column(Float)
    initial_stop_price: Mapped[float] = mapped_column(Float)
    target_price: Mapped[float] = mapped_column(Float)
    liq_price: Mapped[float] = mapped_column(Float)
    peak_price: Mapped[float] = mapped_column(Float, default=0.0)
    trail_active: Mapped[bool] = mapped_column(Boolean, default=False)

    entry_fee: Mapped[float] = mapped_column(Float)
    signal_type: Mapped[str] = mapped_column(String, default="")
    timeframe: Mapped[str] = mapped_column(String, default="")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)

    opened_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class PaperTrade(Base):
    """
    A closed paper trade, with every cost broken out.

    Fees and funding are stored separately from gross rather than netted, so
    the question "did the strategy work, or did the costs eat it" can still be
    answered after the fact.
    """

    __tablename__ = "paper_trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    cycle_id: Mapped[int] = mapped_column(Integer, index=True)
    symbol: Mapped[str] = mapped_column(String, index=True)
    side: Mapped[str] = mapped_column(String)

    signal_price: Mapped[float] = mapped_column(Float, default=0.0)
    entry_price: Mapped[float] = mapped_column(Float)
    exit_price: Mapped[float] = mapped_column(Float)
    coin_qty: Mapped[float] = mapped_column(Float, default=0.0)
    margin: Mapped[float] = mapped_column(Float)
    leverage: Mapped[float] = mapped_column(Float)
    # The rate this trade was actually booked at. Money on the row is INR;
    # reading it back through today's rate would silently restate history
    # every time the rate moves.
    usdt_inr: Mapped[float] = mapped_column(Float, default=102.0)

    stop_price: Mapped[float] = mapped_column(Float, default=0.0)
    target_price: Mapped[float] = mapped_column(Float, default=0.0)
    exit_reason: Mapped[str] = mapped_column(String, index=True)

    gross_pnl: Mapped[float] = mapped_column(Float)
    trading_fees: Mapped[float] = mapped_column(Float, default=0.0)
    funding_paid: Mapped[float] = mapped_column(Float, default=0.0)
    net_pnl: Mapped[float] = mapped_column(Float)
    return_on_margin: Mapped[float] = mapped_column(Float, default=0.0)
    wallet_after: Mapped[float] = mapped_column(Float)

    signal_type: Mapped[str] = mapped_column(String, default="")
    timeframe: Mapped[str] = mapped_column(String, default="")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    entry_slippage_pct: Mapped[float] = mapped_column(Float, default=0.0)
    hours_held: Mapped[float] = mapped_column(Float, default=0.0)

    opened_at: Mapped[datetime] = mapped_column(DateTime)
    closed_at: Mapped[datetime] = mapped_column(DateTime, index=True)


class NewsSentiment(Base):
    """
    One scored news item, pushed in by an external analyser.

    Kept as rows rather than a single per-symbol score so a reading can be
    audited back to what produced it — a score with no headline behind it is
    impossible to argue with when it turns out to be wrong.
    """

    __tablename__ = "news_sentiment"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Idempotency key from the sender, so a retry cannot double-count a story.
    external_id: Mapped[str] = mapped_column(String, unique=True, index=True)
    symbol: Mapped[str] = mapped_column(String, index=True)      # "xauusdt", or "ALL"

    headline: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String, default="")
    url: Mapped[str] = mapped_column(String, default="")

    score: Mapped[float] = mapped_column(Float)                  # -1 bearish .. +1 bullish
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    # "cpi" | "fed" | "geopolitical" | "earnings" | "regulation" | "other".
    # More useful than sentiment for gold: a scheduled event is a reason to
    # stand aside, which has no direction at all.
    event_type: Mapped[str] = mapped_column(String, default="other", index=True)
    model: Mapped[str] = mapped_column(String, default="")       # what scored it

    published_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    received_at: Mapped[datetime] = mapped_column(DateTime, default=_now_utc)
