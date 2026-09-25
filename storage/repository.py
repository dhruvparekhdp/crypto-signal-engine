from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from storage.models import (
    AdminAuth,
    CommoditySnapshot,
    CommoditySnapshotArchive,
    CryptoSignalLog,
    CryptoSnapshot,
    CryptoSnapshotArchive,
    CryptoWatchlistEntry,
    MarketCandle,
    NewsSentiment,
    PaperCycle,
    PaperPosition,
    PaperTrade,
    PaperTradingConfig,
    StrategyConfig,
)


def _bounded(value, lo: float, hi: float, default: float) -> float:
    """
    A float clamped to [lo, hi]. Missing, unparseable or non-finite -> default.

    NaN must not be clamped: max(lo, min(hi, nan)) returns hi, so a garbage
    score became the most bullish reading possible.
    """
    import math
    if value is None or value == "":
        return default
    try:
        x = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(x):
        return default
    return max(lo, min(hi, x))


def _now_utc() -> datetime:
    """Current UTC time as naive datetime for TIMESTAMP WITHOUT TIME ZONE database compatibility."""
    return datetime.now(UTC).replace(tzinfo=None)


class Repository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ── Maintenance ────────────────────────────────────────────

    async def archive_old_crypto_data(self, snapshot_days: int | None = None) -> dict[str, int]:
        """
        Move snapshots older than the live window into the archive tables.

        Nothing is deleted outright. The owner's rule is to keep every row —
        old signals and snapshots are what later AI analysis runs on — so
        rows leave the live table only by being copied to the archive first,
        in the same transaction. If the copy fails, nothing is removed.

        The signal log is not touched at all. It is the evaluation record the
        null test, the accuracy page and the audit read, and at ~30 signals a
        day a decade of it is smaller than a week of snapshots.

        The copy and the delete use the same predicate — older than the
        cutoff AND no newer than the highest id seen before starting — inside
        one transaction. A row written meanwhile has a higher id and is left
        alone; a row that was copied is exactly a row that is deleted. Because
        both halves commit together, a re-run never finds an archived row
        still in the live table, so nothing is copied twice.
        """
        from sqlalchemy import insert

        from config.settings import settings

        days = snapshot_days if snapshot_days is not None else settings.snapshot_retention_days
        moved = {"crypto_snapshots": 0, "commodity_snapshots": 0}
        if not days or days <= 0:
            return moved

        cutoff = _now_utc() - timedelta(days=days)
        for live, archive, key in (
            (CryptoSnapshot, CryptoSnapshotArchive, "crypto_snapshots"),
            (CommoditySnapshot, CommoditySnapshotArchive, "commodity_snapshots"),
        ):
            ceiling = (await self.session.execute(select(func.max(live.id)))).scalar()
            if ceiling is None:
                continue
            which = (live.timestamp < cutoff, live.id <= ceiling)
            data_cols = [c.name for c in live.__table__.columns if c.name != "id"]
            source = select(live.id, *[live.__table__.c[c] for c in data_cols]).where(*which)
            await self.session.execute(
                insert(archive.__table__).from_select(["source_id", *data_cols], source))
            result = await self.session.execute(
                delete(live).where(*which).execution_options(synchronize_session=False))
            moved[key] = result.rowcount or 0
        await self.session.commit()
        return moved

    # ── Historical candles ──────────────────────────────────────

    async def save_candles(self, candles: list[dict]) -> int:
        """
        Insert bars, skipping any already held. Returns how many were new.

        The duplicate check is the unique constraint, not a SELECT first.
        Reading before writing would be both slower and wrong: two backfills
        running at once would each see the row absent and each insert it, and
        the window between the read and the write is exactly where a retry
        after a timeout lands. ON CONFLICT DO NOTHING makes the second write
        a no-op inside the database, where the race cannot happen.

        DO NOTHING rather than DO UPDATE because a bar that is already stored
        is finished. Binance does not revise closed candles, so a second copy
        carries the same numbers, and treating it as an update would rewrite
        half a million rows on every re-run to change nothing.

        SQLite is handled separately only because its dialect spells the same
        clause differently; the semantics are identical, which matters since
        the tests run there and production runs on Postgres.
        """
        if not candles:
            return 0

        dialect = self.session.bind.dialect.name if self.session.bind else ""
        if dialect == "postgresql":
            from sqlalchemy.dialects.postgresql import insert as _insert
        elif dialect == "sqlite":
            from sqlalchemy.dialects.sqlite import insert as _insert
        else:
            from sqlalchemy import insert as _insert

        statement = _insert(MarketCandle.__table__)
        if dialect in ("postgresql", "sqlite"):
            # index_elements rather than the constraint name: Postgres accepts
            # either, SQLite only this one, and the tests run on SQLite while
            # production runs on Postgres. Naming the columns works on both.
            statement = statement.on_conflict_do_nothing(
                index_elements=["symbol", "interval", "open_time"])

        before = await self.count_candles(candles[0]["symbol"], candles[0]["interval"])
        # Chunked: a single statement with half a million rows exceeds the
        # parameter limit on both backends and holds one long lock on neither's
        # behalf.
        for i in range(0, len(candles), 1000):
            await self.session.execute(statement, candles[i:i + 1000])
        await self.session.commit()
        after = await self.count_candles(candles[0]["symbol"], candles[0]["interval"])
        return after - before

    async def count_candles(self, symbol: str, interval: str) -> int:
        return int(await self.session.scalar(
            select(func.count()).select_from(MarketCandle).where(
                MarketCandle.symbol == symbol.lower(),
                MarketCandle.interval == interval,
            )) or 0)

    async def candle_coverage(self, symbol: str, interval: str) -> dict:
        """
        What is actually stored for this series, and how much of it is missing.

        `gaps` is the honest number: bars the exchange should have between the
        first and last stored, that are not there. A backfill that reports
        success while silently holding two thirds of a range is the failure
        this exists to make visible.
        """
        row = (await self.session.execute(
            select(func.min(MarketCandle.open_time), func.max(MarketCandle.open_time),
                   func.count()).where(
                MarketCandle.symbol == symbol.lower(),
                MarketCandle.interval == interval,
            ))).first()
        first, last, held = (row or (None, None, 0))
        if not held or first is None:
            return {"symbol": symbol.lower(), "interval": interval, "held": 0,
                    "first": None, "last": None, "expected": 0, "gaps": 0}

        from collectors.binance_history import interval_delta

        expected = int((last - first) / interval_delta(interval)) + 1
        return {"symbol": symbol.lower(), "interval": interval, "held": held,
                "first": first.isoformat(), "last": last.isoformat(),
                "expected": expected, "gaps": max(0, expected - held)}

    # ── Crypto & Commodities ────────────────────────────────────

    async def save_crypto_snapshot(
        self,
        symbol: str,
        price: float,
        volume_24h: float,
        rsi_14: float,
        macd_line: float,
        macd_signal: float,
        bollinger_upper: float,
        bollinger_lower: float,
        atr_14: float,
        sentiment_score: float,
    ) -> None:
        self.session.add(CryptoSnapshot(
            symbol=symbol,
            price=price,
            volume_24h=volume_24h,
            rsi_14=rsi_14,
            macd_line=macd_line,
            macd_signal=macd_signal,
            bollinger_upper=bollinger_upper,
            bollinger_lower=bollinger_lower,
            atr_14=atr_14,
            sentiment_score=sentiment_score,
            timestamp=_now_utc(),
        ))
        await self.session.commit()

    async def save_commodity_snapshot(
        self,
        symbol: str,
        price: float,
        rsi_14: float,
        atr_14: float,
    ) -> None:
        self.session.add(CommoditySnapshot(
            symbol=symbol,
            price=price,
            rsi_14=rsi_14,
            atr_14=atr_14,
            timestamp=_now_utc(),
        ))
        await self.session.commit()

    async def log_crypto_signal(
        self,
        symbol: str,
        signal_type: str,
        direction: str,
        trigger_description: str,
        confidence: float,
        current_price: float,
        target_price: float | None,
        stop_loss: float | None,
        edge_pct: float,
        stake_pct: float,
        timeframe: str,
        sentiment_score: float = 0.0,
        indicators_summary: str = "",
        suppressed_by: str = "",
    ) -> int:
        row = CryptoSignalLog(
            symbol=symbol,
            signal_type=signal_type,
            direction=direction,
            trigger_description=trigger_description,
            confidence=confidence,
            current_price=current_price,
            target_price=target_price or 0.0,
            stop_loss=stop_loss or 0.0,
            edge_pct=edge_pct,
            stake_pct=stake_pct,
            timeframe=timeframe,
            sentiment_score=sentiment_score,
            indicators_summary=indicators_summary,
            outcome="pending",
            pnl_pct=0.0,
            suppressed_by=suppressed_by,
            timestamp=_now_utc(),
        )
        self.session.add(row)
        await self.session.commit()
        await self.session.refresh(row)
        return row.id

    async def get_recent_crypto_signals(self, hours: int = 24) -> list[CryptoSignalLog]:
        since = _now_utc() - timedelta(hours=hours)
        result = await self.session.execute(
            select(CryptoSignalLog)
            .where(CryptoSignalLog.timestamp >= since)
            .order_by(CryptoSignalLog.timestamp.desc())
            .limit(50)
        )
        return list(result.scalars())

    # ── Crypto watchlist (DB-backed, editable at runtime without a redeploy) ─

    async def get_crypto_watchlist(self) -> list[str]:
        result = await self.session.execute(select(CryptoWatchlistEntry.symbol))
        return [row[0] for row in result.all()]

    async def add_crypto_watchlist_symbol(self, symbol: str) -> None:
        sym = symbol.strip().lower()
        existing = await self.session.get(CryptoWatchlistEntry, sym)
        if existing is None:
            self.session.add(CryptoWatchlistEntry(symbol=sym, added_at=_now_utc()))
            await self.session.commit()

    async def remove_crypto_watchlist_symbol(self, symbol: str) -> None:
        sym = symbol.strip().lower()
        existing = await self.session.get(CryptoWatchlistEntry, sym)
        if existing is not None:
            await self.session.delete(existing)
            await self.session.commit()

    async def seed_crypto_watchlist_if_empty(self, default_symbols: list[str]) -> list[str]:
        """First-run only: populate the table from the small seed list.

        Returns the active watchlist either way, so the caller can always use
        the return value regardless of whether seeding happened.
        """
        existing = await self.get_crypto_watchlist()
        if existing:
            return existing
        symbols = [s.strip().lower() for s in default_symbols if s.strip()]
        for sym in symbols:
            self.session.add(CryptoWatchlistEntry(symbol=sym, added_at=_now_utc()))
        await self.session.commit()
        return symbols

    # ── Paper trading ─────────────────────────────────────────────────────

    async def get_running_cycle(self) -> PaperCycle | None:
        res = await self.session.execute(
            select(PaperCycle).where(PaperCycle.status == "running")
            .order_by(PaperCycle.id.desc()).limit(1)
        )
        return res.scalar_one_or_none()

    async def running_cycles(self) -> list[PaperCycle]:
        """Every cycle claiming to be running. There should only ever be one."""
        res = await self.session.execute(
            select(PaperCycle).where(PaperCycle.status == "running")
            .order_by(PaperCycle.id))
        return list(res.scalars())

    async def close_duplicate_cycles(self) -> int:
        """
        Keep the oldest running cycle and retire the rest.

        _ensure_cycle checks then creates with nothing held between, so two
        workers — two processes after a restart that left the old one alive —
        can both see no cycle and both start one. Trades then land on
        whichever cycle their worker holds while the dashboard reads
        `ORDER BY id DESC LIMIT 1` and shows the other, so a trade reported on
        Telegram is missing from the page and the wallets disagree.

        The oldest survives because it owns the earlier trades; the newer one
        is the accident.
        """
        rows = await self.running_cycles()
        if len(rows) < 2:
            return 0
        for extra in rows[1:]:
            extra.status = "superseded"
            extra.ended_at = _now_utc()
            extra.note = (extra.note or "") + " closed as a duplicate running cycle"
        await self.session.commit()
        return len(rows) - 1

    async def start_cycle(
        self,
        starting_wallet: float,
        target_wallet: float,
        leverage: float,
        stop_pct_of_margin: float,
        reward_risk: float,
        min_confidence: float,
        trailing_enabled: bool,
        scaled_sizing: bool,
        scaled_leverage: bool = False,
        ladder_enabled: bool = False,
        ladder_tight: bool = False,
    ) -> PaperCycle:
        """
        Begin a cycle, recording the configuration it runs under.

        Storing the settings on the row rather than reading them from the
        environment is what makes cycles comparable: a cycle you re-read next
        month still knows the leverage and risk it was actually run with.
        """
        cycle = PaperCycle(
            started_at=_now_utc(),
            starting_wallet=starting_wallet,
            target_wallet=target_wallet,
            wallet=starting_wallet,
            peak_wallet=starting_wallet,
            leverage=leverage,
            stop_pct_of_margin=stop_pct_of_margin,
            reward_risk=reward_risk,
            min_confidence=min_confidence,
            trailing_enabled=trailing_enabled,
            scaled_sizing=scaled_sizing,
            scaled_leverage=scaled_leverage,
            ladder_enabled=ladder_enabled,
            ladder_tight=ladder_tight,
            status="running",
        )
        self.session.add(cycle)
        await self.session.commit()
        await self.session.refresh(cycle)
        return cycle

    async def update_cycle_wallet(self, cycle_id: int, wallet: float) -> None:
        cycle = await self.session.get(PaperCycle, cycle_id)
        if cycle is None:
            return
        cycle.wallet = wallet
        cycle.peak_wallet = max(cycle.peak_wallet, wallet)
        await self.session.commit()

    async def end_cycle(self, cycle_id: int, status: str, note: str = "") -> None:
        cycle = await self.session.get(PaperCycle, cycle_id)
        if cycle is None:
            return
        cycle.status = status
        cycle.note = note
        cycle.ended_at = _now_utc()
        await self.session.commit()

    async def get_open_positions(self, cycle_id: int) -> list[PaperPosition]:
        res = await self.session.execute(
            select(PaperPosition).where(PaperPosition.cycle_id == cycle_id)
            .order_by(PaperPosition.id)
        )
        return list(res.scalars().all())

    async def save_position(self, cycle_id: int, pos) -> PaperPosition:
        row = self._position_row(cycle_id, pos)
        self.session.add(row)
        await self.session.commit()
        await self.session.refresh(row)
        return row

    def _position_row(self, cycle_id: int, pos) -> PaperPosition:
        return PaperPosition(
            cycle_id=cycle_id,
            symbol=pos.symbol,
            side=pos.side.value,
            signal_price=pos.signal_price,
            entry_price=pos.entry_price,
            margin=pos.margin,
            leverage=pos.leverage,
            coin_qty=pos.coin_qty,
            usdt_inr=pos.usdt_inr,
            stop_price=pos.stop_price,
            initial_stop_price=pos.initial_stop_price,
            target_price=pos.target_price,
            liq_price=pos.liq_price,
            peak_price=pos.peak_price,
            trail_active=pos.trail_active,
            trail_r_override=pos.trail_r_override,
            locked_roe=pos.locked_roe,
            entry_fee=pos.entry_fee,
            signal_type=pos.signal_type,
            timeframe=pos.timeframe,
            confidence=pos.confidence,
            opened_at=pos.opened_at.replace(tzinfo=None),
            expires_at=pos.expires_at.replace(tzinfo=None) if pos.expires_at else None,
        )

    async def sync_position(self, row_id: int, pos) -> None:
        """Persist trail movement. Called every tick, so it writes only what moves."""
        row = await self.session.get(PaperPosition, row_id)
        if row is None:
            return
        row.stop_price = pos.stop_price
        row.target_price = pos.target_price
        row.peak_price = pos.peak_price
        row.trail_active = pos.trail_active
        row.trail_r_override = pos.trail_r_override
        row.locked_roe = pos.locked_roe
        await self.session.commit()

    async def delete_position(self, row_id: int) -> None:
        row = await self.session.get(PaperPosition, row_id)
        if row is not None:
            await self.session.delete(row)
            await self.session.commit()

    async def close_position_atomic(self, cycle_id: int, row_id: int, trade,
                                    wallet: float) -> None:
        """
        Record the trade, remove the position and credit the wallet in ONE commit.

        These used to be three commits with the wallet written once at the end
        of the tick, so a deploy or an exception in between lost the margin and
        profit of a trade that the dashboard already showed as closed.
        """
        self._add_trade(cycle_id, trade)
        row = await self.session.get(PaperPosition, row_id)
        if row is not None:
            await self.session.delete(row)
        await self._set_wallet(cycle_id, wallet)
        await self.session.commit()

    async def open_position_atomic(self, cycle_id: int, pos, wallet: float) -> PaperPosition:
        """Store a new position and debit its margin in one commit."""
        row = self._position_row(cycle_id, pos)
        self.session.add(row)
        await self._set_wallet(cycle_id, wallet)
        await self.session.commit()
        await self.session.refresh(row)
        return row

    async def _set_wallet(self, cycle_id: int, wallet: float) -> None:
        cycle = await self.session.get(PaperCycle, cycle_id)
        if cycle is not None:
            cycle.wallet = wallet
            cycle.peak_wallet = max(cycle.peak_wallet, wallet)

    async def record_trade(self, cycle_id: int, trade) -> None:
        self._add_trade(cycle_id, trade)
        await self.session.commit()

    def _add_trade(self, cycle_id: int, trade) -> None:
        pos = trade.position
        self.session.add(PaperTrade(
            cycle_id=cycle_id,
            symbol=pos.symbol,
            side=pos.side.value,
            signal_price=pos.signal_price,
            entry_price=pos.entry_price,
            exit_price=trade.exit_price,
            coin_qty=pos.coin_qty,
            margin=pos.margin,
            leverage=pos.leverage,
            usdt_inr=pos.usdt_inr,
            stop_price=pos.stop_price,
            target_price=pos.target_price,
            exit_reason=trade.reason.value,
            gross_pnl=trade.gross_pnl,
            # Fees and funding stay separate so "was it the strategy or the
            # costs" is still answerable after the fact.
            trading_fees=trade.fees_paid - trade.funding_paid,
            funding_paid=trade.funding_paid,
            net_pnl=trade.net_pnl,
            return_on_margin=trade.return_on_margin,
            wallet_after=trade.wallet_after,
            signal_type=pos.signal_type,
            timeframe=pos.timeframe,
            confidence=pos.confidence,
            entry_slippage_pct=trade.entry_slippage_pct,
            hours_held=trade.hours_held,
            opened_at=pos.opened_at.replace(tzinfo=None),
            closed_at=trade.closed_at.replace(tzinfo=None),
        ))

    async def get_cycle_trades(self, cycle_id: int, limit: int = 500) -> list[PaperTrade]:
        res = await self.session.execute(
            select(PaperTrade).where(PaperTrade.cycle_id == cycle_id)
            .order_by(PaperTrade.closed_at.desc()).limit(limit)
        )
        return list(res.scalars().all())

    async def get_recent_cycles(self, limit: int = 20) -> list[PaperCycle]:
        res = await self.session.execute(
            select(PaperCycle).order_by(PaperCycle.id.desc()).limit(limit)
        )
        return list(res.scalars().all())

    async def get_paper_config(self) -> PaperTradingConfig:
        cfg = await self.session.get(PaperTradingConfig, 1)
        if cfg is None:
            cfg = PaperTradingConfig(id=1)
            self.session.add(cfg)
            await self.session.commit()
            await self.session.refresh(cfg)
        return cfg

    async def update_paper_config(self, **kwargs) -> PaperTradingConfig:
        cfg = await self.get_paper_config()
        for k, v in kwargs.items():
            if hasattr(cfg, k) and v is not None:
                setattr(cfg, k, v)
        await self.session.commit()
        await self.session.refresh(cfg)
        return cfg

    # ── Strategy & Runtime Config ──────────────────────────────────────────

    async def get_strategy_config(self) -> StrategyConfig:
        cfg = await self.session.get(StrategyConfig, 1)
        if cfg is None:
            cfg = StrategyConfig(id=1)
            self.session.add(cfg)
            await self.session.commit()
            await self.session.refresh(cfg)
        return cfg

    async def update_strategy_config(self, **kwargs) -> StrategyConfig:
        cfg = await self.get_strategy_config()
        for k, v in kwargs.items():
            if hasattr(cfg, k) and v is not None:
                setattr(cfg, k, v)
        await self.session.commit()
        await self.session.refresh(cfg)
        return cfg

    # ── Admin Auth ─────────────────────────────────────────────────────────

    async def verify_admin_password(self, candidate: str) -> tuple[bool, str | None]:
        auth = await self.session.get(AdminAuth, 1)
        if auth is None:
            return False, None
        salt_bytes = bytes.fromhex(auth.salt)
        computed = hashlib.pbkdf2_hmac("sha256", candidate.encode("utf-8"), salt_bytes, 100_000).hex()
        if not hmac.compare_digest(computed, auth.password_hash):
            return False, None
        token = secrets.token_hex(32)
        auth.session_token = token
        auth.updated_at = _now_utc()
        await self.session.commit()
        return True, token

    async def validate_session_token(self, token: str) -> bool:
        if not token or not token.strip():
            return False
        auth = await self.session.get(AdminAuth, 1)
        if auth is None or not auth.session_token:
            return False
        return hmac.compare_digest(auth.session_token, token.strip())

    async def invalidate_session_token(self, token: str) -> None:
        auth = await self.session.get(AdminAuth, 1)
        if auth and auth.session_token and hmac.compare_digest(auth.session_token, token.strip()):
            auth.session_token = None
            await self.session.commit()

    async def set_admin_password(self, new_pwd: str) -> None:
        salt = secrets.token_hex(16)
        salt_bytes = bytes.fromhex(salt)
        p_hash = hashlib.pbkdf2_hmac("sha256", new_pwd.encode("utf-8"), salt_bytes, 100_000).hex()
        auth = await self.session.get(AdminAuth, 1)
        if auth is None:
            auth = AdminAuth(id=1, password_hash=p_hash, salt=salt, session_token=None)
            self.session.add(auth)
        else:
            auth.password_hash = p_hash
            auth.salt = salt
            auth.session_token = None
            auth.updated_at = _now_utc()
        await self.session.commit()

    # ── External news sentiment ───────────────────────────────────────────

    async def ingest_news_sentiment(self, items: list[dict]) -> tuple[int, int]:
        """
        Store scored headlines. Returns (accepted, duplicates).

        Deduplicated on external_id rather than on content, so the sender can
        retry a failed batch freely — a news pipeline that double-counts one
        story on a retry produces a sentiment spike that never happened.
        """
        accepted = duplicates = 0
        for item in items:
            ext = str(item.get("external_id") or "").strip()
            if not ext:
                continue
            existing = await self.session.execute(
                select(NewsSentiment).where(NewsSentiment.external_id == ext).limit(1))
            if existing.scalar_one_or_none() is not None:
                duplicates += 1
                continue
            self.session.add(NewsSentiment(
                external_id=ext,
                symbol=str(item.get("symbol") or "ALL").lower(),
                headline=str(item.get("headline") or "")[:2000],
                source=str(item.get("source") or "")[:200],
                url=str(item.get("url") or "")[:500],
                score=_bounded(item.get("score"), -1.0, 1.0, 0.0),
                # 0.0 is a real answer ("I don't know"); only a missing value
                # takes the default. `or 0.5` used to turn 0.0 into 0.5.
                confidence=_bounded(item.get("confidence"), 0.0, 1.0, 0.5),
                event_type=str(item.get("event_type") or "other")[:40],
                model=str(item.get("model") or "")[:80],
                published_at=_parse_dt(item.get("published_at")) or _now_utc(),
            ))
            accepted += 1
        if accepted:
            await self.session.commit()
        return accepted, duplicates

    async def save_briefing(self, risk_tone: float, summary: str, events: list,
                            model: str, latency_ms: int) -> int:
        """Store a market briefing and return its id."""
        import json

        from storage.models import MarketBriefing
        row = MarketBriefing(risk_tone=risk_tone, summary=summary[:4000],
                             events=json.dumps(events)[:20000], model=model,
                             latency_ms=latency_ms, created_at=_now_utc())
        self.session.add(row)
        await self.session.commit()
        return row.id

    # ── World events (the event monitor) ─────────────────────────────────

    async def recent_events(self, days: float = 14) -> list:
        from storage.models import MarketEvent
        cutoff = _now_utc() - timedelta(days=days)
        res = await self.session.execute(
            select(MarketEvent).where(MarketEvent.happened_at >= cutoff)
            .order_by(MarketEvent.happened_at.desc()))
        return list(res.scalars().all())

    async def upsert_event(self, key: str, **fields):
        """Insert an event, or refresh last_seen if the key is already known."""
        from storage.models import MarketEvent
        res = await self.session.execute(select(MarketEvent).where(MarketEvent.key == key))
        row = res.scalar_one_or_none()
        if row is None:
            level = fields.get("level", 2)
            row = MarketEvent(key=key, title=fields.get("title", "")[:1000],
                              category=fields.get("category", "other"),
                              level_initial=level, level_current=level,
                              direction=fields.get("direction", "mixed"),
                              source=fields.get("source", ""),
                              happened_at=fields.get("happened_at") or _now_utc(),
                              first_seen=_now_utc(), last_seen=_now_utc())
            self.session.add(row)
        else:
            row.last_seen = _now_utc()
        await self.session.commit()
        return row

    async def update_event(self, event_id: int, level: int | None = None,
                           note: str = "", resolved: bool = False) -> None:
        from storage.models import MarketEvent
        row = await self.session.get(MarketEvent, event_id)
        if row is None:
            return
        if level:
            row.level_current = level
        if note:
            row.notes = ((row.notes + " | ") if row.notes else "") + note[:300]
        if resolved:
            row.status = "resolved"
        row.last_seen = _now_utc()
        await self.session.commit()

    async def save_shadow(self, event_id: int, results) -> None:
        from storage.models import EventShadowTrade, MarketEvent
        for r in results:
            self.session.add(EventShadowTrade(
                event_id=event_id, book=r.book, symbol=r.symbol, side=r.side,
                entry=r.entry, exit=r.exit, wallet_pct=r.wallet_pct, note=r.note[:500]))
        row = await self.session.get(MarketEvent, event_id)
        if row is not None:
            row.shadow_done = True
        await self.session.commit()

    async def shadow_trades(self, days: float = 30) -> list:
        from storage.models import EventShadowTrade
        cutoff = _now_utc() - timedelta(days=days)
        res = await self.session.execute(
            select(EventShadowTrade).where(EventShadowTrade.created_at >= cutoff)
            .order_by(EventShadowTrade.created_at.desc()))
        return list(res.scalars().all())

    async def price_points(self, symbols: list[str], hours: int = 13) -> dict:
        """symbol -> [(timestamp, price)] from the live snapshots."""
        cutoff = _now_utc() - timedelta(hours=hours)
        res = await self.session.execute(
            select(CryptoSnapshot.symbol, CryptoSnapshot.timestamp, CryptoSnapshot.price)
            .where(CryptoSnapshot.timestamp >= cutoff)
            .where(CryptoSnapshot.symbol.in_([s.lower() for s in symbols])))
        out: dict = {}
        for sym, ts, price in res.all():
            out.setdefault(sym, []).append((ts, price))
        return out

    async def save_move_attribution(self, **kw) -> int:
        import json

        from storage.models import MoveAttribution
        row = MoveAttribution(
            window_hours=kw.get("window_hours", 12), briefing_id=kw.get("briefing_id", 0) or 0,
            moves=json.dumps(kw.get("moves", [])), signals=json.dumps(kw.get("signals", [])),
            result=json.dumps(kw.get("result", {})), model=kw.get("model", ""),
            latency_ms=kw.get("latency_ms", 0) or 0, created_at=_now_utc())
        self.session.add(row)
        await self.session.commit()
        return row.id

    async def recent_move_attributions(self, limit: int = 24) -> list:
        from storage.models import MoveAttribution
        res = await self.session.execute(
            select(MoveAttribution).order_by(MoveAttribution.created_at.desc()).limit(limit))
        return list(res.scalars().all())

    async def latest_briefing(self):
        from storage.models import MarketBriefing
        res = await self.session.execute(
            select(MarketBriefing).order_by(MarketBriefing.created_at.desc()).limit(1))
        return res.scalar_one_or_none()

    async def news_sentiment_since(self, hours: int = 12, limit: int = 2000) -> list[NewsSentiment]:
        """Every scored headline in the window, all symbols, newest first."""
        cutoff = _now_utc() - timedelta(hours=hours)
        res = await self.session.execute(
            select(NewsSentiment)
            .where(NewsSentiment.published_at >= cutoff)
            .order_by(NewsSentiment.published_at.desc()).limit(limit))
        return list(res.scalars().all())

    async def recent_news_sentiment(self, symbol: str, hours: int = 6) -> list[NewsSentiment]:
        cutoff = _now_utc() - timedelta(hours=hours)
        res = await self.session.execute(
            select(NewsSentiment)
            .where(NewsSentiment.published_at >= cutoff)
            .where(NewsSentiment.symbol.in_([symbol.lower(), "all"]))
            .order_by(NewsSentiment.published_at.desc()).limit(200))
        return list(res.scalars().all())

    # ── Signal history and accuracy ───────────────────────────────────────

    async def crypto_signals_between(self, newer_than_days: int,
                                     older_than_days: int = 0,
                                     limit: int = 400,
                                     include_suppressed: bool = False,
                                     ) -> list[CryptoSignalLog]:
        """
        Signals inside a window. `older_than_days` carves out the recent end,
        which is what splits the live dashboard from the archive.

        Suppressed signals are excluded by default: they never fired, so
        counting them would misreport what the engine actually published.
        The scorecard asks for them on purpose.
        """
        now = _now_utc()
        q = select(CryptoSignalLog).where(
            CryptoSignalLog.timestamp >= now - timedelta(days=newer_than_days))
        if not include_suppressed:
            q = q.where(CryptoSignalLog.suppressed_by == "")
        if older_than_days:
            q = q.where(CryptoSignalLog.timestamp < now - timedelta(days=older_than_days))
        res = await self.session.execute(
            q.order_by(CryptoSignalLog.timestamp.desc()).limit(limit))
        return list(res.scalars().all())

    async def pending_crypto_signals(self, older_than_minutes: int = 240,
                                     limit: int = 200) -> list[CryptoSignalLog]:
        """
        Signals old enough to have resolved but still marked pending.

        The age floor matters: resolving a signal the moment it fires would
        record whatever the first tick did, which is noise rather than outcome.
        """
        cutoff = _now_utc() - timedelta(minutes=older_than_minutes)
        res = await self.session.execute(
            select(CryptoSignalLog)
            .where(CryptoSignalLog.outcome == "pending")
            .where(CryptoSignalLog.timestamp <= cutoff)
            .order_by(CryptoSignalLog.timestamp).limit(limit))
        return list(res.scalars().all())

    async def resolve_crypto_signal(self, signal_id: int, outcome: str,
                                    pnl_pct: float) -> None:
        row = await self.session.get(CryptoSignalLog, signal_id)
        if row is None:
            return
        row.outcome = outcome
        row.pnl_pct = pnl_pct
        await self.session.commit()

    async def crypto_signal_counts(self) -> dict:
        async def count(model, where=None):
            q = select(func.count()).select_from(model)
            if where is not None:
                q = q.where(where)
            return int((await self.session.execute(q)).scalar() or 0)
        return {
            "signals": await count(CryptoSignalLog),
            "trades": await count(PaperTrade),
            "snapshots": await count(CryptoSnapshot),
            "cycles": await count(PaperCycle),
            "pending": await count(CryptoSignalLog, CryptoSignalLog.outcome == "pending"),
        }


    # ── Signal reviews ────────────────────────────────────────────────────

    async def save_review(self, phase: str, symbol: str, **kw) -> None:
        """Record one review. Never raises into the caller: a lost post-mortem
        is not worth failing a trade close over."""
        from storage.models import SignalReview
        try:
            self.session.add(SignalReview(
                phase=phase, symbol=symbol, created_at=_now_utc(),
                signal_type=kw.get("signal_type", ""),
                signal_log_id=kw.get("signal_log_id", 0) or 0,
                trade_id=kw.get("trade_id", 0) or 0,
                verdict=kw.get("verdict", "") or "",
                factors=kw.get("factors", "") or "",
                summary=kw.get("summary", "") or "",
                confidence_delta=kw.get("confidence_delta", 0.0) or 0.0,
                outcome=kw.get("outcome", "") or "",
                pnl_pct=kw.get("pnl_pct", 0.0) or 0.0,
                model=kw.get("model", "") or "",
                latency_ms=kw.get("latency_ms", 0) or 0,
                briefing_id=kw.get("briefing_id", 0) or 0,
                news_context=(kw.get("news_context", "") or "")[:4000],
                sentiment_score=kw.get("sentiment_score", 0.0) or 0.0,
                fear_greed=kw.get("fear_greed", 0) or 0,
            ))
            await self.session.commit()
        except Exception:
            await self.session.rollback()

    async def reviewer_scorecard(self, days: int = 14) -> dict:
        """
        Was the reviewer right? Graded against what price actually did.

        Only answerable because suppressed signals are still logged and still
        resolved. A filter measured solely on the trades it let through cannot
        be wrong by construction — every rejection it got wrong is invisible.
        Here a REJECT that would have won shows up as exactly that.

        Read it as: if REJECT has a higher win rate than APPROVE, the reviewer
        is costing money and the penalty should come down or go.
        """
        from storage.models import SignalReview
        since = _now_utc() - timedelta(days=days)

        res = await self.session.execute(
            select(SignalReview).where(SignalReview.phase == "pre",
                                       SignalReview.created_at >= since))
        reviews = list(res.scalars())
        if not reviews:
            return {"verdicts": {}, "reviewed": 0, "resolved": 0}

        sig_res = await self.session.execute(
            select(CryptoSignalLog).where(CryptoSignalLog.timestamp >= since))
        # Match on (symbol, setup) within the window: a pre-review is written
        # in the same breath as its signal, so the pairing is unambiguous in
        # practice and does not need a foreign key it never had.
        by_key: dict[tuple, list] = {}
        for row in sig_res.scalars():
            by_key.setdefault((row.symbol, row.signal_type), []).append(row)

        out: dict[str, dict] = {}
        resolved = 0
        for rv in reviews:
            verdict = (rv.verdict or "NONE").upper()
            slot = out.setdefault(verdict, {
                "seen": 0, "resolved": 0, "won": 0, "lost": 0,
                "suppressed": 0, "net_pnl_pct": 0.0})
            slot["seen"] += 1
            candidates = by_key.get((rv.symbol, rv.signal_type), [])
            match = min(
                (c for c in candidates
                 if abs((c.timestamp - rv.created_at).total_seconds()) < 120),
                key=lambda c: abs((c.timestamp - rv.created_at).total_seconds()),
                default=None)
            if match is None or match.outcome not in ("won", "lost", "expired"):
                continue
            slot["resolved"] += 1
            resolved += 1
            if match.suppressed_by:
                slot["suppressed"] += 1
            if match.outcome == "won":
                slot["won"] += 1
            elif match.outcome == "lost":
                slot["lost"] += 1
            slot["net_pnl_pct"] += match.pnl_pct or 0.0

        for slot in out.values():
            decided = slot["won"] + slot["lost"]
            slot["win_rate_pct"] = (round(slot["won"] / decided * 100, 1)
                                    if decided else None)
            slot["net_pnl_pct"] = round(slot["net_pnl_pct"], 2)
        return {"verdicts": out, "reviewed": len(reviews), "resolved": resolved}

    async def review_factor_counts(self, phase: str = "post", days: int = 30) -> dict:
        """
        How often each label appears, split by whether the trade made money.

        This is the whole point of a closed vocabulary: the answer to "what
        keeps killing my trades" is a count, not a pile of prose.
        """
        from storage.models import SignalReview
        since = _now_utc() - timedelta(days=days)
        res = await self.session.execute(
            select(SignalReview).where(SignalReview.phase == phase,
                                       SignalReview.created_at >= since))
        out: dict[str, dict] = {}
        for row in res.scalars():
            won = row.pnl_pct > 0
            for f in (row.factors or "").split(","):
                f = f.strip()
                if not f:
                    continue
                slot = out.setdefault(f, {"total": 0, "wins": 0, "losses": 0})
                slot["total"] += 1
                slot["wins" if won else "losses"] += 1
        return dict(sorted(out.items(), key=lambda kv: -kv[1]["total"]))


def _parse_dt(value) -> datetime:
    """Accept an ISO string or a datetime; fall back to now rather than fail."""
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, str) and value.strip():
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            pass
    return _now_utc()
