"""
World news + sentiment engine tables (plan W8, Oct 2026).

Additive only: new tables beside market_events / news_sentiment — the legacy
briefing and event monitor stay until this engine is switched on (W3, W18).
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from storage.database import Base
from storage.models import _now_utc


class NewsCollectorState(Base):
    """Last poll outcome per collector (health for /settings and ops)."""

    __tablename__ = "news_collector_state"

    collector_id: Mapped[str] = mapped_column(String, primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String, default="idle")  # idle | ok | error | not_configured
    message: Mapped[str] = mapped_column(Text, default="")
    items_last_run: Mapped[int] = mapped_column(Integer, default=0)
    last_ok_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now_utc, onupdate=_now_utc)


class NewsDocument(Base):
    """One raw ingested item before canonical event resolution (W7)."""

    __tablename__ = "news_documents"
    __table_args__ = (UniqueConstraint("source_id", "external_id", name="uq_news_doc_source_ext"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[str] = mapped_column(String, index=True)
    external_id: Mapped[str] = mapped_column(String, index=True)
    title: Mapped[str] = mapped_column(Text, default="")
    body: Mapped[str] = mapped_column(Text, default="")
    url: Mapped[str] = mapped_column(String, default="")
    language: Mapped[str] = mapped_column(String, default="")
    countries_json: Mapped[str] = mapped_column(Text, default="[]")
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    duplicate_of: Mapped[str] = mapped_column(String, default="")
    published_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=_now_utc, index=True)


class NewsEvent(Base):
    """One canonical world event (resolver output, W7)."""

    __tablename__ = "news_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_key: Mapped[str] = mapped_column(String, unique=True, index=True)
    title: Mapped[str] = mapped_column(Text)
    event_type: Mapped[str] = mapped_column(String, index=True, default="other")
    severity: Mapped[int] = mapped_column(Integer, default=1)  # 1-5 model severity
    severity_market: Mapped[int | None] = mapped_column(Integer, nullable=True)
    countries_json: Mapped[str] = mapped_column(Text, default="[]")
    institutions_json: Mapped[str] = mapped_column(Text, default="[]")
    status: Mapped[str] = mapped_column(String, default="open", index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now_utc, onupdate=_now_utc)


class NewsEventStory(Base):
    """Links a document (or follow-up) to a canonical event."""

    __tablename__ = "news_event_stories"
    __table_args__ = (Index("ix_news_story_event_doc", "event_id", "document_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(Integer, index=True)
    document_id: Mapped[int] = mapped_column(Integer, index=True)
    is_followup: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now_utc)


class NewsCalendarRow(Base):
    """Economic calendar row with consensus / actual (W6 P1)."""

    __tablename__ = "news_calendar_rows"
    __table_args__ = (UniqueConstraint("source_id", "external_id", name="uq_news_cal_source_ext"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[str] = mapped_column(String, index=True)
    external_id: Mapped[str] = mapped_column(String, index=True)
    title: Mapped[str] = mapped_column(String, default="")
    country: Mapped[str] = mapped_column(String, default="", index=True)
    event_type: Mapped[str] = mapped_column(String, default="macro", index=True)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    consensus: Mapped[str] = mapped_column(String, default="")
    previous: Mapped[str] = mapped_column(String, default="")
    actual: Mapped[str] = mapped_column(String, default="")
    severity: Mapped[int] = mapped_column(Integer, default=2)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    event_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now_utc, onupdate=_now_utc)


class NewsOddsSnapshot(Base):
    """Prediction-market odds (Polymarket / Kalshi), W6 P1."""

    __tablename__ = "news_odds_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    venue: Mapped[str] = mapped_column(String, index=True)
    market_id: Mapped[str] = mapped_column(String, index=True)
    title: Mapped[str] = mapped_column(Text, default="")
    yes_prob: Mapped[float | None] = mapped_column(Float, nullable=True)
    volume_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    captured_at: Mapped[datetime] = mapped_column(DateTime, default=_now_utc, index=True)


class NewsLiquidationMinute(Base):
    """Binance liquidation stream aggregated per minute (W6 P1)."""

    __tablename__ = "news_liquidation_minutes"
    __table_args__ = (UniqueConstraint("symbol", "minute_at", name="uq_news_liq_sym_min"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String, index=True)
    minute_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    long_usd: Mapped[float] = mapped_column(Float, default=0.0)
    short_usd: Mapped[float] = mapped_column(Float, default=0.0)
    count: Mapped[int] = mapped_column(Integer, default=0)


class NewsCrossAssetTick(Base):
    """Cross-asset prices (DXY, yields, etc.) for context (W6 P1)."""

    __tablename__ = "news_cross_asset_ticks"
    __table_args__ = (Index("ix_news_xasset_sym_at", "symbol", "captured_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String, index=True)
    price: Mapped[float] = mapped_column(Float)
    change_24h_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    source_id: Mapped[str] = mapped_column(String, default="")
    captured_at: Mapped[datetime] = mapped_column(DateTime, default=_now_utc, index=True)


class NewsDefiSnapshot(Base):
    """DefiLlama TVL / stablecoin snapshot (W6 P1)."""

    __tablename__ = "news_defi_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chain: Mapped[str] = mapped_column(String, default="", index=True)
    tvl_usd: Mapped[float] = mapped_column(Float, default=0.0)
    stablecoin_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime, default=_now_utc, index=True)


class NewsSentimentIndex(Base):
    """Country / world / crypto sentiment indices — labels only (W9)."""

    __tablename__ = "news_sentiment_indices"
    __table_args__ = (Index("ix_news_idx_scope_at", "scope", "scope_key", "captured_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scope: Mapped[str] = mapped_column(String, index=True)  # country | world | crypto
    scope_key: Mapped[str] = mapped_column(String, index=True)  # US, world, BTC
    score: Mapped[float] = mapped_column(Float, default=0.0)  # -1 .. +1
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    components_json: Mapped[str] = mapped_column(Text, default="{}")
    captured_at: Mapped[datetime] = mapped_column(DateTime, default=_now_utc, index=True)


class NewsEventReaction(Base):
    """Measured market reaction after an event (W10)."""

    __tablename__ = "news_event_reactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(Integer, index=True)
    symbol: Mapped[str] = mapped_column(String, default="BTCUSDT", index=True)
    horizon_min: Mapped[int] = mapped_column(Integer, default=60)
    move_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    volume_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    computed_at: Mapped[datetime] = mapped_column(DateTime, default=_now_utc, index=True)


class NewsImpulseLink(Base):
    """Links a live impulse row (data/impulse_events.jsonl id) to open events at that minute (W18)."""

    __tablename__ = "news_impulse_links"
    __table_args__ = (UniqueConstraint("impulse_key", name="uq_news_impulse_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    impulse_key: Mapped[str] = mapped_column(String, index=True)
    impulse_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    event_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now_utc)


class NewsInstitution(Base):
    """Institutions seed (W4.3)."""

    __tablename__ = "news_institutions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String, unique=True, index=True)
    name: Mapped[str] = mapped_column(String)
    country: Mapped[str] = mapped_column(String, default="", index=True)
    kind: Mapped[str] = mapped_column(String, default="")
    aliases_json: Mapped[str] = mapped_column(Text, default="[]")


class NewsGdeltBatch(Base):
    """Tracks processed GDELT 15-minute files (W6 P1, low-priority worker)."""

    __tablename__ = "news_gdelt_batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    file_name: Mapped[str] = mapped_column(String, unique=True, index=True)
    status: Mapped[str] = mapped_column(String, default="pending")
    rows_ingested: Mapped[int] = mapped_column(Integer, default=0)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
