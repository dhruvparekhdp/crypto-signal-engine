import os
import ssl
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from config.settings import settings


def _make_url(raw: str) -> tuple[str, dict]:
    """
    Normalise a database URL for SQLAlchemy asyncpg:
    - Convert postgres:// / postgresql:// → postgresql+asyncpg://
    - Strip sslmode= query param (asyncpg rejects it) and configure connect_args ssl
    Returns (url, connect_args).
    """
    connect_args: dict = {}

    if raw.startswith("postgres://"):
        raw = raw.replace("postgres://", "postgresql+asyncpg://", 1)
    elif raw.startswith("postgresql://") and "+asyncpg" not in raw:
        raw = raw.replace("postgresql://", "postgresql+asyncpg://", 1)

    # asyncpg doesn't accept sslmode — strip it and pass ssl via connect_args
    if "sslmode=" in raw or "ssl=" in raw:
        parsed = urlparse(raw)
        params = parse_qs(parsed.query, keep_blank_values=True)
        ssl_val = (params.pop("sslmode", None) or params.pop("ssl", ["require"]))[0].lower()
        if ssl_val in ("verify-ca", "verify-full"):
            # Verify properly. A managed provider that issues its own CA —
            # Aiven does — needs that CA named, or every connection fails
            # with an unknown-issuer error rather than falling back quietly.
            ca = (settings.database_ssl_ca or "").strip()
            ctx = ssl.create_default_context(cafile=ca or None)
            ctx.check_hostname = ssl_val == "verify-full"
            connect_args["ssl"] = ctx
        elif ssl_val in ("require", "prefer", "allow", "no-verify", "true", "1"):
            # libpq's `require` means encrypted, NOT authenticated, so this is
            # faithful to the URL rather than a bug — but it does mean anyone
            # who can answer for the database's address gets the credentials.
            # Say so loudly once at startup instead of leaving it implicit.
            print("WARNING: database TLS is encrypted but unverified "
                  f"(sslmode={ssl_val}). Set DATABASE_SSL_CA to the provider's "
                  "CA file and change the URL to sslmode=verify-full.")
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            connect_args["ssl"] = ctx
        new_query = urlencode({k: v[0] for k, v in params.items()})
        raw = urlunparse(parsed._replace(query=new_query))

    return raw, connect_args


_url, _connect_args = _make_url(settings.database_url)

engine = create_async_engine(
    _url,
    echo=False,
    pool_pre_ping=True,
    connect_args=_connect_args,
)
AsyncSessionFactory = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def init_db() -> None:
    import storage.models  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with engine.connect() as conn:
        await _migrate_columns(conn)

    # Optional initial seed for fresh databases if ADMIN_PASSWORD is provided in env
    admin_pwd = os.getenv("ADMIN_PASSWORD")
    if admin_pwd and admin_pwd.strip():
        from storage.models import AdminAuth
        from storage.repository import Repository
        async with AsyncSessionFactory() as session:
            auth = await session.get(AdminAuth, 1)
            if auth is None:
                repo = Repository(session)
                await repo.set_admin_password(admin_pwd.strip())


async def _migrate_columns(conn) -> None:
    """
    Idempotent column migrations — ADD COLUMN IF NOT EXISTS for every column
    added after the initial table creation. Safe to run on every startup.
    PostgreSQL 9.6+ supports IF NOT EXISTS on ADD COLUMN.
    """
    migrations = [
        # crypto_snapshots table
        """CREATE TABLE IF NOT EXISTS crypto_snapshots (
            id SERIAL PRIMARY KEY,
            symbol VARCHAR, price FLOAT, volume_24h FLOAT DEFAULT 0.0,
            rsi_14 FLOAT DEFAULT 50.0, macd_line FLOAT DEFAULT 0.0, macd_signal FLOAT DEFAULT 0.0,
            bollinger_upper FLOAT DEFAULT 0.0, bollinger_lower FLOAT DEFAULT 0.0,
            atr_14 FLOAT DEFAULT 0.0, sentiment_score FLOAT DEFAULT 0.0,
            price_30m_later FLOAT DEFAULT 0.0, price_1h_later FLOAT DEFAULT 0.0,
            price_4h_later FLOAT DEFAULT 0.0, price_1d_later FLOAT DEFAULT 0.0,
            timestamp TIMESTAMP
        )""",
        "CREATE INDEX IF NOT EXISTS ix_cs_symbol_ts ON crypto_snapshots (symbol, timestamp)",
        # commodity_snapshots table
        """CREATE TABLE IF NOT EXISTS commodity_snapshots (
            id SERIAL PRIMARY KEY,
            symbol VARCHAR, price FLOAT, rsi_14 FLOAT DEFAULT 50.0, atr_14 FLOAT DEFAULT 0.0,
            timestamp TIMESTAMP
        )""",
        "CREATE INDEX IF NOT EXISTS ix_comms_symbol_ts ON commodity_snapshots (symbol, timestamp)",
        # crypto_signal_log table
        """CREATE TABLE IF NOT EXISTS crypto_signal_log (
            id SERIAL PRIMARY KEY,
            symbol VARCHAR, signal_type VARCHAR, direction VARCHAR,
            trigger_description TEXT, confidence FLOAT, current_price FLOAT,
            target_price FLOAT DEFAULT 0.0, stop_loss FLOAT DEFAULT 0.0,
            edge_pct FLOAT, stake_pct FLOAT, timeframe VARCHAR,
            sentiment_score FLOAT DEFAULT 0.0, indicators_summary VARCHAR DEFAULT '',
            outcome VARCHAR DEFAULT 'pending', pnl_pct FLOAT DEFAULT 0.0,
            timestamp TIMESTAMP
        )""",
        # crypto_watchlist table — symbols to stream, DB-backed instead of an env var
        """CREATE TABLE IF NOT EXISTS crypto_watchlist (
            symbol VARCHAR PRIMARY KEY,
            added_at TIMESTAMP
        )""",
        # paper_cycles columns for dynamic leverage scaling and profit ladders
        "ALTER TABLE paper_cycles ADD COLUMN IF NOT EXISTS scaled_leverage BOOLEAN DEFAULT FALSE",
        "ALTER TABLE paper_cycles ADD COLUMN IF NOT EXISTS ladder_enabled BOOLEAN DEFAULT FALSE",
        "ALTER TABLE paper_cycles ADD COLUMN IF NOT EXISTS ladder_tight BOOLEAN DEFAULT FALSE",
        # paper_trading_config table — user configurable paper trading settings in DB
        """CREATE TABLE IF NOT EXISTS paper_trading_config (
            id INTEGER PRIMARY KEY,
            enabled BOOLEAN DEFAULT TRUE,
            starting_wallet FLOAT DEFAULT 3000.0,
            target_wallet FLOAT DEFAULT 20000.0,
            leverage FLOAT DEFAULT 10.0,
            stop_pct_of_margin FLOAT DEFAULT 0.20,
            reward_risk FLOAT DEFAULT 2.0,
            min_confidence FLOAT DEFAULT 0.70,
            max_concurrent INTEGER DEFAULT 3,
            max_hold_minutes INTEGER DEFAULT 240,
            scaled_sizing BOOLEAN DEFAULT TRUE,
            trailing_enabled BOOLEAN DEFAULT TRUE,
            scaled_leverage BOOLEAN DEFAULT FALSE,
            ladder_enabled BOOLEAN DEFAULT FALSE,
            ladder_tight BOOLEAN DEFAULT FALSE,
            max_leverage FLOAT DEFAULT 25.0,
            usdt_inr FLOAT DEFAULT 102.0,
            alert_telegram BOOLEAN DEFAULT TRUE
        )""",
        "ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS usdt_inr FLOAT DEFAULT 102.0",
        "ALTER TABLE crypto_signal_log ADD COLUMN IF NOT EXISTS suppressed_by VARCHAR DEFAULT ''",
        "CREATE INDEX IF NOT EXISTS ix_csl_suppressed ON crypto_signal_log (suppressed_by)",
        """CREATE TABLE IF NOT EXISTS signal_reviews (
            id SERIAL PRIMARY KEY,
            phase VARCHAR, symbol VARCHAR, signal_type VARCHAR DEFAULT '',
            signal_log_id INTEGER DEFAULT 0, trade_id INTEGER DEFAULT 0,
            verdict VARCHAR DEFAULT '', factors VARCHAR DEFAULT '',
            summary TEXT DEFAULT '', confidence_delta FLOAT DEFAULT 0.0,
            outcome VARCHAR DEFAULT '', pnl_pct FLOAT DEFAULT 0.0,
            model VARCHAR DEFAULT '', latency_ms INTEGER DEFAULT 0,
            created_at TIMESTAMP
        )""",
        "CREATE INDEX IF NOT EXISTS ix_review_symbol_phase ON signal_reviews (symbol, phase)",
        "CREATE INDEX IF NOT EXISTS ix_review_created ON signal_reviews (created_at)",
        # News context stored with every review. Postgres form first; the
        # plain form is for an existing SQLite file, and whichever does not
        # apply fails harmlessly.
        "ALTER TABLE signal_reviews ADD COLUMN IF NOT EXISTS briefing_id INTEGER DEFAULT 0",
        "ALTER TABLE signal_reviews ADD COLUMN IF NOT EXISTS news_context TEXT DEFAULT ''",
        "ALTER TABLE signal_reviews ADD COLUMN IF NOT EXISTS sentiment_score FLOAT DEFAULT 0.0",
        "ALTER TABLE signal_reviews ADD COLUMN IF NOT EXISTS fear_greed INTEGER DEFAULT 0",
        "ALTER TABLE signal_reviews ADD COLUMN briefing_id INTEGER DEFAULT 0",
        "ALTER TABLE signal_reviews ADD COLUMN news_context TEXT DEFAULT ''",
        "ALTER TABLE signal_reviews ADD COLUMN sentiment_score FLOAT DEFAULT 0.0",
        "ALTER TABLE signal_reviews ADD COLUMN fear_greed INTEGER DEFAULT 0",
        "CREATE INDEX IF NOT EXISTS ix_signal_reviews_briefing_id ON signal_reviews (briefing_id)",
        "INSERT INTO paper_trading_config (id) VALUES (1) ON CONFLICT (id) DO NOTHING",
        "ALTER TABLE paper_trading_config ADD COLUMN IF NOT EXISTS enabled BOOLEAN DEFAULT TRUE",
        # admin_auth table — password hash + salt + active session
        """CREATE TABLE IF NOT EXISTS admin_auth (
            id INTEGER PRIMARY KEY,
            password_hash VARCHAR NOT NULL,
            salt VARCHAR NOT NULL,
            session_token VARCHAR,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
        # strategy_config table — strategy & AI runtime settings
        """CREATE TABLE IF NOT EXISTS strategy_config (
            id INTEGER PRIMARY KEY,
            crypto_min_confidence FLOAT DEFAULT 0.70,
            high_conviction_only BOOLEAN DEFAULT TRUE,
            crypto_volume_spike_enabled BOOLEAN DEFAULT TRUE,
            crypto_htf_filter_enabled BOOLEAN DEFAULT TRUE,
            binance_klines_enabled BOOLEAN DEFAULT TRUE,
            binance_oi_enabled BOOLEAN DEFAULT TRUE,
            orderflow_enabled BOOLEAN DEFAULT TRUE,
            groq_signal_review_enabled BOOLEAN DEFAULT TRUE,
            groq_model VARCHAR DEFAULT 'qwen/qwen3.8-27b',
            bank_size FLOAT DEFAULT 10000.0,
            min_confidence FLOAT DEFAULT 0.65
        )""",
        "INSERT INTO strategy_config (id) VALUES (1) ON CONFLICT (id) DO NOTHING",
        # There used to be a DELETE here, purging "corrupted" signals — any row
        # with a target more than 2x away from entry — on every single boot. It
        # was written to clean up after the gold 43%-target bug. That bug is
        # fixed at the source now, and what the DELETE actually did in the
        # meantime was destroy the evidence: the exact rows worth studying were
        # gone before anyone could read them, on a schedule nobody remembered.
        # Bad rows are a diagnosis problem, not a startup chore.
    ]
    for sql in migrations:
        try:
            await conn.execute(__import__("sqlalchemy").text(sql))
            await conn.commit()
        except Exception:
            await conn.rollback()


async def get_session() -> AsyncSession:
    async with AsyncSessionFactory() as session:
        yield session
