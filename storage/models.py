from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, Float, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from storage.database import Base


def _now_utc() -> datetime:
    """Return current UTC time as naive datetime for TIMESTAMP WITHOUT TIME ZONE compatibility."""
    return datetime.now(UTC).replace(tzinfo=None)


class Match(Base):
    __tablename__ = "matches"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    player1: Mapped[str] = mapped_column(String)
    player2: Mapped[str] = mapped_column(String)
    tournament: Mapped[str] = mapped_column(String)
    surface: Mapped[str] = mapped_column(String)
    first_seen: Mapped[datetime] = mapped_column(default=_now_utc)
    last_updated: Mapped[datetime] = mapped_column(default=_now_utc)
    is_finished: Mapped[bool] = mapped_column(default=False)


class OddsSnapshot(Base):
    __tablename__ = "odds_snapshots"
    __table_args__ = (Index("ix_odds_match_ts", "match_id", "timestamp"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    match_id: Mapped[str] = mapped_column(String, index=True)
    odds_p1: Mapped[float] = mapped_column(Float)
    odds_p2: Mapped[float] = mapped_column(Float)
    timestamp: Mapped[datetime] = mapped_column(index=True)


class SignalLog(Base):
    __tablename__ = "signal_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    match_id: Mapped[str] = mapped_column(String, index=True)
    signal_type: Mapped[str] = mapped_column(String)
    player_to_back: Mapped[int] = mapped_column(Integer)
    player_name: Mapped[str] = mapped_column(String, default="")
    opponent_name: Mapped[str] = mapped_column(String, default="")
    tournament: Mapped[str] = mapped_column(String, default="")
    surface: Mapped[str] = mapped_column(String, default="hard")
    trigger_description: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float)
    recommended_market: Mapped[str] = mapped_column(String)
    current_odds: Mapped[float] = mapped_column(Float)
    fair_odds: Mapped[float] = mapped_column(Float)
    edge_pct: Mapped[float] = mapped_column(Float)
    stake_pct: Mapped[float] = mapped_column(Float)
    # Model state at signal time
    model_win_prob: Mapped[float] = mapped_column(Float, default=0.0)
    score_at_signal: Mapped[str] = mapped_column(String, default="")   # e.g. "1-0, 3-2"
    sets_p1_at_signal: Mapped[int] = mapped_column(Integer, default=0)
    sets_p2_at_signal: Mapped[int] = mapped_column(Integer, default=0)
    games_p1_at_signal: Mapped[int] = mapped_column(Integer, default=0)
    games_p2_at_signal: Mapped[int] = mapped_column(Integer, default=0)
    # Filled in when match completes
    outcome: Mapped[str] = mapped_column(String, default="pending")    # pending/won/lost/void
    match_winner: Mapped[int] = mapped_column(Integer, default=0)      # 1 or 2, 0 = unknown
    timestamp: Mapped[datetime] = mapped_column(index=True)


class PlayerStats(Base):
    """Historical player statistics loaded from Tennis Abstract CSV data."""

    __tablename__ = "player_stats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    player_name: Mapped[str] = mapped_column(String, index=True)
    surface: Mapped[str] = mapped_column(String)
    matches_played: Mapped[int] = mapped_column(Integer, default=0)
    matches_won: Mapped[int] = mapped_column(Integer, default=0)
    first_set_losses: Mapped[int] = mapped_column(Integer, default=0)
    first_set_loss_wins: Mapped[int] = mapped_column(Integer, default=0)
    avg_first_serve_pct: Mapped[float] = mapped_column(Float, default=0.60)
    avg_aces_per_game: Mapped[float] = mapped_column(Float, default=0.5)
    avg_dfs_per_game: Mapped[float] = mapped_column(Float, default=0.2)


class MatchRecord(Base):
    """
    Individual historical match from Sackmann ATP/WTA CSVs.
    One row per match — full stats for both players.
    Used for ML training and pattern analysis.
    """

    __tablename__ = "match_records"
    __table_args__ = (
        Index("ix_mr_winner", "winner_name"),
        Index("ix_mr_loser", "loser_name"),
        Index("ix_mr_year_surface", "year", "surface"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tour: Mapped[str] = mapped_column(String)           # atp / wta
    year: Mapped[int] = mapped_column(Integer)
    tourney_id: Mapped[str] = mapped_column(String, default="")
    tourney_name: Mapped[str] = mapped_column(String)
    surface: Mapped[str] = mapped_column(String)
    tourney_level: Mapped[str] = mapped_column(String, default="")  # G=slam, M=masters, etc.
    round: Mapped[str] = mapped_column(String, default="")
    best_of: Mapped[int] = mapped_column(Integer, default=3)
    winner_name: Mapped[str] = mapped_column(String)
    loser_name: Mapped[str] = mapped_column(String)
    winner_rank: Mapped[int] = mapped_column(Integer, default=0)
    loser_rank: Mapped[int] = mapped_column(Integer, default=0)
    score: Mapped[str] = mapped_column(String, default="")
    minutes: Mapped[int] = mapped_column(Integer, default=0)
    # Winner serve stats
    w_ace: Mapped[int] = mapped_column(Integer, default=0)
    w_df: Mapped[int] = mapped_column(Integer, default=0)
    w_svpt: Mapped[int] = mapped_column(Integer, default=0)
    w_1st_in: Mapped[int] = mapped_column(Integer, default=0)
    w_1st_won: Mapped[int] = mapped_column(Integer, default=0)
    w_2nd_won: Mapped[int] = mapped_column(Integer, default=0)
    w_svc_games: Mapped[int] = mapped_column(Integer, default=0)
    w_bp_saved: Mapped[int] = mapped_column(Integer, default=0)
    w_bp_faced: Mapped[int] = mapped_column(Integer, default=0)
    # Loser serve stats
    l_ace: Mapped[int] = mapped_column(Integer, default=0)
    l_df: Mapped[int] = mapped_column(Integer, default=0)
    l_svpt: Mapped[int] = mapped_column(Integer, default=0)
    l_1st_in: Mapped[int] = mapped_column(Integer, default=0)
    l_1st_won: Mapped[int] = mapped_column(Integer, default=0)
    l_2nd_won: Mapped[int] = mapped_column(Integer, default=0)
    l_svc_games: Mapped[int] = mapped_column(Integer, default=0)
    l_bp_saved: Mapped[int] = mapped_column(Integer, default=0)
    l_bp_faced: Mapped[int] = mapped_column(Integer, default=0)


class SlamPoint(Base):
    """
    Individual point from Jeff Sackmann's slam point-by-point data.
    Covers Australian Open, Roland Garros, Wimbledon, US Open from 2011.
    Primary source for momentum / psychological pattern analysis.
    """

    __tablename__ = "slam_points"
    __table_args__ = (
        Index("ix_sp_match", "match_id"),
        Index("ix_sp_slam_year", "slam", "year"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    slam: Mapped[str] = mapped_column(String)           # ausopen/frenchopen/wimbledon/usopen
    year: Mapped[int] = mapped_column(Integer)
    match_id: Mapped[str] = mapped_column(String)
    player1: Mapped[str] = mapped_column(String, default="")
    player2: Mapped[str] = mapped_column(String, default="")
    set_no: Mapped[int] = mapped_column(Integer)
    game_no: Mapped[int] = mapped_column(Integer)
    point_no: Mapped[int] = mapped_column(Integer)
    server: Mapped[int] = mapped_column(Integer)        # 1 or 2
    point_winner: Mapped[int] = mapped_column(Integer)  # 1 or 2
    p1_score: Mapped[str] = mapped_column(String, default="")  # "0","15","30","40","A"
    p2_score: Mapped[str] = mapped_column(String, default="")
    p1_games: Mapped[int] = mapped_column(Integer, default=0)
    p2_games: Mapped[int] = mapped_column(Integer, default=0)
    p1_sets: Mapped[int] = mapped_column(Integer, default=0)
    p2_sets: Mapped[int] = mapped_column(Integer, default=0)
    is_break_point: Mapped[bool] = mapped_column(default=False)
    is_set_point: Mapped[bool] = mapped_column(default=False)
    is_match_point: Mapped[bool] = mapped_column(default=False)
    p1_ace: Mapped[bool] = mapped_column(default=False)
    p2_ace: Mapped[bool] = mapped_column(default=False)
    p1_double_fault: Mapped[bool] = mapped_column(default=False)
    p2_double_fault: Mapped[bool] = mapped_column(default=False)
    serve_no: Mapped[int] = mapped_column(Integer, default=1)   # 1 = first, 2 = second
    rally_length: Mapped[int] = mapped_column(Integer, default=0)
    game_winner: Mapped[int] = mapped_column(Integer, default=0)    # 0 = game ongoing
    set_winner: Mapped[int] = mapped_column(Integer, default=0)
    match_winner: Mapped[int] = mapped_column(Integer, default=0)


class MatchSnapshot(Base):
    """
    Periodic snapshot of live match state — primary source for ML training.
    One row every ~2 minutes per match while live.
    winner column is NULL during play, filled in retroactively when match completes.
    """

    __tablename__ = "match_snapshots"
    __table_args__ = (Index("ix_snap_match_ts", "match_id", "timestamp"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    match_id: Mapped[str] = mapped_column(String, index=True)
    player1_name: Mapped[str] = mapped_column(String)
    player2_name: Mapped[str] = mapped_column(String)
    surface: Mapped[str] = mapped_column(String)
    tournament: Mapped[str] = mapped_column(String)
    # Score state
    sets_p1: Mapped[int] = mapped_column(Integer)
    sets_p2: Mapped[int] = mapped_column(Integer)
    games_p1: Mapped[int] = mapped_column(Integer)
    games_p2: Mapped[int] = mapped_column(Integer)
    current_set: Mapped[int] = mapped_column(Integer)
    total_games_played: Mapped[int] = mapped_column(Integer)
    # Momentum: positive = p1 winning streak, negative = p2 winning streak
    p1_momentum: Mapped[int] = mapped_column(Integer, default=0)
    # Odds
    odds_p1: Mapped[float] = mapped_column(Float)
    odds_p2: Mapped[float] = mapped_column(Float)
    # Model predictions at this moment
    model_win_prob_p1: Mapped[float] = mapped_column(Float, default=0.0)
    model_win_prob_p2: Mapped[float] = mapped_column(Float, default=0.0)
    # Serve stats (0.0 if unavailable)
    serve_pct_p1: Mapped[float] = mapped_column(Float, default=0.0)
    serve_pct_p2: Mapped[float] = mapped_column(Float, default=0.0)
    # Full game sequence as JSON array, e.g. [1,2,1,1,2]
    game_log_json: Mapped[str] = mapped_column(Text, default="[]")
    # Outcome — NULL until match completes, then set to 1 or 2
    winner: Mapped[int] = mapped_column(Integer, default=0)    # 0 = not yet known
    timestamp: Mapped[datetime] = mapped_column(DateTime, index=True)


class MatchCompletion(Base):
    """
    Final result of a match — used to label MatchSnapshot and SignalLog rows.
    Created when a match disappears from the live feed.
    """

    __tablename__ = "match_completions"

    match_id: Mapped[str] = mapped_column(String, primary_key=True)
    player1_name: Mapped[str] = mapped_column(String)
    player2_name: Mapped[str] = mapped_column(String)
    winner: Mapped[int] = mapped_column(Integer)               # 1 or 2
    final_sets_p1: Mapped[int] = mapped_column(Integer)
    final_sets_p2: Mapped[int] = mapped_column(Integer)
    final_score_str: Mapped[str] = mapped_column(String)       # "6-3, 7-5"
    tournament: Mapped[str] = mapped_column(String)
    surface: Mapped[str] = mapped_column(String)
    total_games: Mapped[int] = mapped_column(Integer)
    total_signals_fired: Mapped[int] = mapped_column(Integer, default=0)
    signals_correct: Mapped[int] = mapped_column(Integer, default=0)
    completed_at: Mapped[datetime] = mapped_column(DateTime, index=True)


class MatchResult(Base):
    """Training data row for the ML win predictor, recorded at match completion."""

    __tablename__ = "match_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    match_id: Mapped[str] = mapped_column(String, index=True)
    p1_sets_lead: Mapped[int] = mapped_column(Integer)
    p1_games_lead: Mapped[int] = mapped_column(Integer)
    current_set: Mapped[int] = mapped_column(Integer)
    p1_momentum: Mapped[int] = mapped_column(Integer)
    p1_serve_pct: Mapped[float] = mapped_column(Float)
    p2_serve_pct: Mapped[float] = mapped_column(Float)
    surface_clay: Mapped[int] = mapped_column(Integer)
    surface_grass: Mapped[int] = mapped_column(Integer)
    surface_indoor: Mapped[int] = mapped_column(Integer)
    match_progress: Mapped[float] = mapped_column(Float)
    p1_opening_implied: Mapped[float] = mapped_column(Float)
    winner: Mapped[int] = mapped_column(Integer)
    recorded_at: Mapped[datetime] = mapped_column(DateTime, default=_now_utc)


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
    sports_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
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
