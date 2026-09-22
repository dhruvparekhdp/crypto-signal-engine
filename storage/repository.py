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
    CryptoSignalLog,
    CryptoSnapshot,
    CryptoWatchlistEntry,
    NewsSentiment,
    PaperCycle,
    PaperPosition,
    PaperTrade,
    PaperTradingConfig,
    StrategyConfig,
)


def _now_utc() -> datetime:
    """Current UTC time as naive datetime for TIMESTAMP WITHOUT TIME ZONE database compatibility."""
    return datetime.now(UTC).replace(tzinfo=None)


class Repository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ── Maintenance ────────────────────────────────────────────

    async def delete_old_crypto_data(self, days: int = 3) -> None:
        """Bound the growth of crypto/commodity snapshots + old signal log rows.

        These are written every crypto_snapshot_interval_seconds (default 2 min)
        for every watchlist symbol — left unbounded they would eventually fill
        the Postgres instance on their own.
        """
        cutoff = _now_utc() - timedelta(days=days)
        await self.session.execute(delete(CryptoSnapshot).where(CryptoSnapshot.timestamp < cutoff))
        await self.session.execute(delete(CommoditySnapshot).where(CommoditySnapshot.timestamp < cutoff))
        await self.session.execute(delete(CryptoSignalLog).where(CryptoSignalLog.timestamp < cutoff))
        await self.session.commit()

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
        row = PaperPosition(
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
            entry_fee=pos.entry_fee,
            signal_type=pos.signal_type,
            timeframe=pos.timeframe,
            confidence=pos.confidence,
            opened_at=pos.opened_at.replace(tzinfo=None),
            expires_at=pos.expires_at.replace(tzinfo=None) if pos.expires_at else None,
        )
        self.session.add(row)
        await self.session.commit()
        await self.session.refresh(row)
        return row

    async def sync_position(self, row_id: int, pos) -> None:
        """Persist trail movement. Called every tick, so it writes only what moves."""
        row = await self.session.get(PaperPosition, row_id)
        if row is None:
            return
        row.stop_price = pos.stop_price
        row.target_price = pos.target_price
        row.peak_price = pos.peak_price
        row.trail_active = pos.trail_active
        await self.session.commit()

    async def delete_position(self, row_id: int) -> None:
        row = await self.session.get(PaperPosition, row_id)
        if row is not None:
            await self.session.delete(row)
            await self.session.commit()

    async def record_trade(self, cycle_id: int, trade) -> None:
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
        await self.session.commit()

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
                score=max(-1.0, min(1.0, float(item.get("score") or 0.0))),
                confidence=max(0.0, min(1.0, float(item.get("confidence") or 0.5))),
                event_type=str(item.get("event_type") or "other")[:40],
                model=str(item.get("model") or "")[:80],
                published_at=_parse_dt(item.get("published_at")),
            ))
            accepted += 1
        if accepted:
            await self.session.commit()
        return accepted, duplicates

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
