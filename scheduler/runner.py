"""
APScheduler-based 24/7 job runner.

Data collection strategy:
  - ESPN (primary)     → always works from cloud IPs, covers ATP + WTA live scores
  - Sofascore (enrich) → attempted for serve stats only; silently skipped if blocked

Jobs:
  - data_poll:      every 30s  → ESPN fetch + optional Sofascore + analysis + snapshots
  - schedule_poll:  every 5min → TheSportsDB schedule
  - db_cleanup:     daily      → delete old odds/crypto/commodity snapshots
  - heartbeat:      every 10m  → log status

Data storage:
  - MatchSnapshot: saved every ~2 minutes per match (every 4 polls)
  - MatchCompletion: saved when a match disappears from the live feed
  - SignalLog: updated with outcome (won/lost) when match completes
"""
import json
import os
import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram.constants import ParseMode

from dataclasses import replace

from analysis.crypto_engine import CryptoEngine
from analysis.paper_cycle import (
    CycleState,
    config_for_cycle,
    cycle_outcome,
    now_utc,
    open_from_signal,
    resolve_at_price,
    should_open,
    summarise,
)
from analysis.paper_trading import Position, Side
from analysis.crypto_state_store import CommodityStateStore, CryptoStateStore
from analysis.football_state import FootballStateStore
from analysis.match_state import MatchState
from analysis.multi_horizon_predictor import MultiHorizonPredictor
from analysis.sentiment import SentimentAnalyzer
from analysis.state_store import MatchStateStore
from collectors.binance_futures_oi import BinanceFuturesOICollector
from collectors.binance_klines import BinanceKlines
from collectors.binance_ws import BinanceWSCollector
from collectors.coindcx import CoinDCXCollector
from collectors.coingecko import CoinGeckoCollector
from collectors.macro_sentinel import GroqSentinel
from collectors.cryptopanic import CryptoPanicCollector
from collectors.historical_importer import run_import
from collectors.sentiment_feeds import adjust_confidence, fetch_fear_greed
from collectors.twelvedata_ws import TwelveDataWSCollector
from config.settings import settings
from notifications.crypto_formatter import (
    format_crypto_signal,
    format_cycle_end,
    format_paper_trade,
)
from notifications.telegram_notifier import TelegramNotifier
from storage.database import AsyncSessionFactory
from storage.repository import Repository

log = structlog.get_logger()


class AppRunner:
    def __init__(self) -> None:
        self.store = MatchStateStore()
        self.football_store = FootballStateStore()
        self.notifier = TelegramNotifier()
        self.scheduler = AsyncIOScheduler()
        # Crypto & Commodities — watchlist itself is DB-backed, loaded in start()
        self.crypto_store = CryptoStateStore()
        self.commodity_store = CommodityStateStore()
        self.coindcx = CoinDCXCollector(self.crypto_store)
        self.coingecko = CoinGeckoCollector(self.crypto_store)
        self.binance_ws = BinanceWSCollector(self.crypto_store)
        self.klines = BinanceKlines(self.crypto_store)
        self.twelvedata_ws = TwelveDataWSCollector(self.commodity_store)
        self.cryptopanic = CryptoPanicCollector()
        self.sentiment = SentimentAnalyzer()
        self.crypto_engine = CryptoEngine()
        self.groq_sentinel = GroqSentinel()
        self.oi_collector = BinanceFuturesOICollector(self.crypto_store)
        self._pending_paper_signals: list[tuple[CryptoSignal, object]] = []
        self.fear_greed = None
        self.multi_horizon = MultiHorizonPredictor()
        self._ws_tasks: list[asyncio.Task] = []
        self.collector_enabled: dict[str, bool] = {
            "coindcx": True,
            "coingecko": True,
            "binance_ws": settings.binance_ws_enabled,
            "twelvedata_ws": True,
        }


    async def _coindcx_job(self) -> None:
        if not self.collector_enabled.get("coindcx", True):
            return
        try:
            await self.coindcx.fetch()
        except Exception:
            log.exception("coindcx_job_failed")

    async def _klines_job(self) -> None:
        """
        Pull real 1-minute bars and depth, replacing the poll-aggregated ones.

        This is the job that decides whether any signal means anything. With
        sampled bars the ATR came out roughly a third of the real figure, the
        cost floor beat the volatility term every time, and every card on the
        board showed the same 0.505% target — a constant wearing the costume
        of a forecast.
        """
        if not settings.binance_klines_enabled:
            return
        try:
            await self.klines.fetch()
        except Exception:
            log.exception("klines_job_failed")

    async def _coingecko_job(self) -> None:
        if not self.collector_enabled.get("coingecko", True):
            return
        try:
            await self.coingecko.fetch()
        except Exception:
            log.exception("coingecko_job_failed")


    # ── Paper trading simulator ───────────────────────────────────────────

    async def _refresh_sentiment_job(self) -> None:
        """Fetch the Fear & Greed index. Cached, because it only moves daily."""
        if not settings.sentiment_feeds_enabled:
            return
        try:
            self.fear_greed = await fetch_fear_greed()
            if self.fear_greed:
                log.info("fear_greed", value=self.fear_greed.value,
                         classification=self.fear_greed.classification)
        except Exception:
            log.exception("fear_greed_job_failed")

    async def _ensure_cycle(self, repo) -> object | None:
        """Return the running cycle, starting one if none exists."""
        cycle = await repo.get_running_cycle()
        if cycle is not None:
            return cycle
        pcfg = await repo.get_paper_config()
        cycle = await repo.start_cycle(
            starting_wallet=pcfg.starting_wallet,
            target_wallet=pcfg.target_wallet,
            leverage=(pcfg.max_leverage if pcfg.scaled_leverage
                      else pcfg.leverage),
            stop_pct_of_margin=pcfg.stop_pct_of_margin,
            reward_risk=pcfg.reward_risk,
            min_confidence=pcfg.min_confidence,
            trailing_enabled=pcfg.trailing_enabled,
            scaled_sizing=pcfg.scaled_sizing,
            scaled_leverage=pcfg.scaled_leverage,
            ladder_enabled=pcfg.ladder_enabled,
            ladder_tight=pcfg.ladder_tight,
        )
        log.info("paper_cycle_started", cycle_id=cycle.id,
                 wallet=cycle.starting_wallet, leverage=cycle.leverage)
        return cycle

    def _restore_position(self, row) -> Position:
        """Rebuild an in-memory Position from its database row."""
        pos = Position(
            symbol=row.symbol,
            side=Side.LONG if row.side == "long" else Side.SHORT,
            entry_price=row.entry_price,
            margin=row.margin,
            leverage=row.leverage,
            stop_price=row.stop_price,
            target_price=row.target_price,
            liq_price=row.liq_price,
            opened_at=row.opened_at.replace(tzinfo=timezone.utc),
            entry_fee=row.entry_fee,
            signal_type=row.signal_type,
            timeframe=row.timeframe,
            confidence=row.confidence,
            expires_at=(row.expires_at.replace(tzinfo=timezone.utc)
                        if row.expires_at else None),
            usdt_inr=row.usdt_inr,
            signal_price=row.signal_price,
            initial_stop_price=row.initial_stop_price,
            peak_price=row.peak_price,
            trail_active=row.trail_active,
        )
        # Size was fixed at fill time, so it is restored rather than re-derived:
        # recomputing it from the current wallet would silently resize the
        # position every time the process restarts.
        pos._coin_qty = row.coin_qty
        return pos

    async def _paper_trading_job(self) -> None:
        """
        One tick of the live paper-trading cycle.

        Resolves open positions against current prices first, then considers
        new ones — so a position can never be opened and closed on the same
        tick using the same information.
        """
        try:
            async with AsyncSessionFactory() as session:
                repo = Repository(session)
                pcfg = await repo.get_paper_config()
                if not pcfg.enabled:
                    return
                cycle = await self._ensure_cycle(repo)
                if cycle is None:
                    return

                cfg = config_for_cycle(cycle)
                cfg = replace(cfg, max_concurrent=pcfg.max_concurrent,
                              max_hold_minutes=pcfg.max_hold_minutes)
                wallet = cycle.wallet
                now = now_utc()

                rows = await repo.get_open_positions(cycle.id)
                states = {st.symbol: st for st in await self.crypto_store.get_all()}

                # 1. Resolve what is already open.
                live: list[Position] = []
                live_ids: dict[int, int] = {}
                for row in rows:
                    pos = self._restore_position(row)
                    st = states.get(row.symbol)
                    if st is None or st.current_price <= 0:
                        live_ids[len(live)] = row.id
                        live.append(pos)
                        continue

                    trade = resolve_at_price(pos, st.current_price, now, cfg, wallet)
                    if trade is None:
                        await repo.sync_position(row.id, pos)
                        live_ids[len(live)] = row.id
                        live.append(pos)
                        continue

                    wallet = trade.wallet_after
                    await repo.record_trade(cycle.id, trade)
                    await repo.delete_position(row.id)
                    log.info("paper_trade_closed", symbol=pos.symbol,
                             reason=trade.reason.value, net=round(trade.net_pnl, 2),
                             wallet=round(wallet, 2))
                    if pcfg.alert_telegram:
                        await self.notifier.send_text(
                            format_paper_trade(trade, wallet), parse_mode=ParseMode.HTML)

                cstate = CycleState(cycle_id=cycle.id, wallet=wallet,
                                    peak_wallet=cycle.peak_wallet,
                                    positions=live, position_ids=live_ids)

                # 2. Consider new positions from queued signals.
                pending = list(self._pending_paper_signals)
                self._pending_paper_signals.clear()

                for sig, st in pending:
                    if st.current_price <= 0:
                        continue
                    sig = self._apply_sentiment(sig, st.funding_rate_per_8h)
                    ok, _why = should_open(sig, cfg, cstate, now)
                    if not ok:
                        log.info("paper_trade_skipped", symbol=sig.symbol, reason=_why)
                        continue
                    atr_pct = (st.atr_14 / st.current_price
                               if st.current_price > 0 and st.atr_14 > 0 else None)
                    pos = open_from_signal(sig, cfg, cstate, now,
                                           pcfg.usdt_inr, atr_pct)
                    if pos is None:
                        continue
                    cstate.wallet -= pos.margin
                    wallet = cstate.wallet
                    row = await repo.save_position(cycle.id, pos)
                    cstate.position_ids[len(cstate.positions)] = row.id
                    cstate.positions.append(pos)
                    log.info("paper_trade_opened", symbol=pos.symbol,
                             side=pos.side.value, margin=round(pos.margin, 2),
                             confidence=pos.confidence)
                    if pcfg.alert_telegram:
                        arrow = "LONG 🟢" if pos.side.value == "long" else "SHORT 🔴"
                        await self.notifier.send_text(
                            f"📝 <b>Paper Trade Opened</b>\n"
                            f"<b>{pos.symbol.upper()}</b> · {arrow}\n"
                            f"Entry <b>${pos.entry_price:,.4f}</b> · Margin <b>₹{pos.margin:,.0f}</b> ({pos.leverage:.0f}x)\n"
                            f"Target <b>${pos.target_price:,.4f}</b> · Stop <b>${pos.stop_price:,.4f}</b>",
                            parse_mode=ParseMode.HTML,
                        )

                await repo.update_cycle_wallet(cycle.id, wallet)

                # 3. Has the cycle finished?
                outcome = cycle_outcome(wallet + sum(p.margin for p in cstate.positions),
                                        cfg, len(cstate.positions))
                if outcome:
                    await repo.end_cycle(cycle.id, outcome)
                    trades = await repo.get_cycle_trades(cycle.id)
                    log.info("paper_cycle_ended", cycle_id=cycle.id,
                             outcome=outcome, trades=len(trades),
                             wallet=round(wallet, 2))
                    if pcfg.alert_telegram:
                        await self.notifier.send_text(
                            format_cycle_end(cycle, outcome, summarise(trades, wallet, cfg)),
                            parse_mode=ParseMode.HTML)
        except Exception:
            log.exception("paper_trading_job_failed")

    def _apply_sentiment(self, sig, funding: float | None = None):
        """Let sentiment nudge confidence — it never creates or blocks a signal."""
        if not settings.sentiment_feeds_enabled:
            return sig
        adjusted, reasons = adjust_confidence(
            sig.confidence, sig.direction, self.fear_greed, funding)
        if adjusted == sig.confidence:
            return sig
        return replace(sig, confidence=adjusted,
                       indicators_summary=" | ".join([sig.indicators_summary, *reasons]))


    async def _resolve_signal_outcomes_job(self) -> None:
        """
        Decide whether past signals reached target or stop.

        Resolution walks the stored candle window forward from the signal.
        We check all pending signals (older than 5 minutes to avoid 1-tick noise),
        so that signals hitting target early (e.g. in 10-30 mins) are captured
        immediately before rolling in-memory candles are evicted.

        Unresolved signals remain 'pending' until either target/stop is hit
        or until the holding horizon has elapsed, at which point they are marked 'expired'.
        """
        try:
            async with AsyncSessionFactory() as session:
                repo = Repository(session)
                pending = await repo.pending_crypto_signals(older_than_minutes=5)
                if not pending:
                    return

                pcfg = await repo.get_paper_config()
                states = {st.symbol: st for st in await self.crypto_store.get_all()}
                resolved = 0
                for sig in pending:
                    if sig.current_price <= 0.001 or sig.target_price <= 0 or sig.stop_loss <= 0:
                        await repo.resolve_crypto_signal(sig.id, "expired", 0.0)
                        resolved += 1
                        continue

                    st = states.get(sig.symbol.lower())
                    if st is None or not st.candles_1m:
                        continue
                    sig_ts = sig.timestamp.replace(tzinfo=None) if sig.timestamp.tzinfo else sig.timestamp
                    after = [c for c in st.candles_1m
                             if (c.timestamp.replace(tzinfo=None) if c.timestamp.tzinfo else c.timestamp) > sig_ts]
                    if not after:
                        continue

                    long_ = sig.direction == "long"
                    outcome, pnl = None, 0.0
                    for c in after:
                        hit_stop = c.low <= sig.stop_loss if long_ else c.high >= sig.stop_loss
                        hit_tgt = c.high >= sig.target_price if long_ else c.low <= sig.target_price

                        if hit_tgt and hit_stop:
                            # Both hit on same bar: check if bar moved in trade direction
                            favorable = (c.close >= c.open) if long_ else (c.close <= c.open)
                            if favorable:
                                outcome = "won"
                                pnl = (sig.target_price - sig.current_price) / sig.current_price * 100
                            else:
                                outcome = "lost"
                                pnl = (sig.stop_loss - sig.current_price) / sig.current_price * 100
                            break
                        elif hit_tgt:
                            outcome = "won"
                            pnl = (sig.target_price - sig.current_price) / sig.current_price * 100
                            break
                        elif hit_stop:
                            outcome = "lost"
                            pnl = (sig.stop_loss - sig.current_price) / sig.current_price * 100
                            break

                        # Signal is still running. Only expire if past maximum hold time
                        last_c_ts = after[-1].timestamp.replace(tzinfo=None) if after[-1].timestamp.tzinfo else after[-1].timestamp
                        span = last_c_ts - sig_ts
                        paper_max_hold_minutes = pcfg.max_hold_minutes
                        if span.total_seconds() / 60 < paper_max_hold_minutes:
                            continue
                        outcome = "expired"
                        pnl = (after[-1].close - sig.current_price) / sig.current_price * 100

                    await repo.resolve_crypto_signal(
                        sig.id, outcome, round(pnl * (1 if long_ else -1), 4))
                    resolved += 1

                if resolved:
                    log.info("signal_outcomes_resolved", resolved=resolved,
                             still_pending=len(pending) - resolved)
        except Exception:
            log.exception("resolve_signal_outcomes_failed")

    async def _cleanup_job(self) -> None:
        async with AsyncSessionFactory() as session:
            repo = Repository(session)
            await repo.delete_old_odds_snapshots(days=3)
            await repo.delete_old_crypto_data(days=3)
        log.info("db_cleanup_done")

    async def _heartbeat_job(self) -> None:
        count = await self.store.count()
        sofascore_ok = self.sofascore._consecutive_failures == 0
        flashscore_ok = self.flashscore._consecutive_failures == 0
        log.info(
            "heartbeat",
            matches_tracked=count,
            sofascore_available=sofascore_ok,
            flashscore_http_ok=flashscore_ok,
            flashscore_consecutive_zeros=self.flashscore._consecutive_zero_matches,
        )

    def _self_ping_url(self) -> tuple[str, bool]:
        """
        Where to ping, and whether it actually counts as traffic.

        This has to leave the container and come back through Render's router.
        A request to http://localhost never reaches the load balancer, so it
        does not reset the idle timer — the job logged self_ping_ok every five
        minutes for weeks while the service went right on spinning down.

        RENDER_EXTERNAL_URL is injected by Render automatically. The loopback
        fall-back is kept only as a liveness check, and says so.
        """
        external = (os.environ.get("RENDER_EXTERNAL_URL")
                    or settings.self_ping_url
                    or "").strip().rstrip("/")
        if not external:
            host = os.environ.get("RENDER_EXTERNAL_HOSTNAME", "").strip()
            if host:
                external = f"https://{host}"
        if external:
            return f"{external}/health", True
        port = int(os.environ.get("PORT", 8080))
        return f"http://localhost:{port}/health", False

    async def _self_ping_job(self) -> None:
        url, is_external = self._self_ping_url()
        try:
            async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
                resp = await client.get(url)
            if is_external:
                log.debug("self_ping_ok", status=resp.status_code, url=url)
            else:
                # Worth a warning rather than a debug line: this is the state
                # in which the service will sleep despite the job "working".
                log.warning("self_ping_loopback_only",
                            hint="set RENDER_EXTERNAL_URL or SELF_PING_URL; "
                                 "a localhost ping does not keep the service awake")
        except Exception as exc:
            log.warning("self_ping_failed", url=url, error=str(exc))

    async def _crypto_analysis_job(self) -> None:
        """Run crypto signal detection across all symbols in the watchlist."""
        try:
            async with AsyncSessionFactory() as session:
                repo = Repository(session)
                scfg = await repo.get_strategy_config()
                pcfg = await repo.get_paper_config()

            states = await self.crypto_store.get_all()
            for state in states:
                if state.current_price <= 0:
                    continue

                signals = self.crypto_engine.process(state)
                for sig in signals:
                    if scfg.crypto_min_confidence > 0 and sig.confidence < scfg.crypto_min_confidence:
                        continue

                    # Groq AI Pre-Signal Sanity Review (advisory sanity check)
                    if self.groq_sentinel.is_available and scfg.groq_signal_review_enabled:
                        delta, ai_summary = await self.groq_sentinel.review_signal_candidate(
                            sig, state, model=scfg.groq_model
                        )
                        if ai_summary:
                            sig.ai_review = ai_summary
                            sig.confidence = max(0.50, min(0.95, round(sig.confidence + delta, 4)))

                    msg = format_crypto_signal(sig)
                    if settings.crypto_alert_telegram:
                        await self.notifier.send_text(msg, parse_mode=ParseMode.HTML)

                    if pcfg.enabled:
                        self._pending_paper_signals.append((sig, state))

                    # Log to DB
                    try:
                        async with AsyncSessionFactory() as session:
                            repo = Repository(session)
                            await repo.log_crypto_signal(
                                symbol=sig.symbol,
                                signal_type=sig.signal_type,
                                direction=sig.direction,
                                trigger_description=sig.trigger_description,
                                confidence=sig.confidence,
                                current_price=sig.current_price,
                                target_price=sig.target_price,
                                stop_loss=sig.stop_loss,
                                edge_pct=sig.edge_pct,
                                stake_pct=sig.stake_pct,
                                timeframe=sig.timeframe,
                                sentiment_score=sig.sentiment_score,
                                indicators_summary=sig.indicators_summary,
                            )
                    except Exception:
                        log.exception("crypto_signal_db_log_failed", symbol=sig.symbol)

                    log.info(
                        "crypto_signal_fired",
                        symbol=sig.symbol,
                        type=sig.signal_type,
                        direction=sig.direction,
                        price=sig.current_price,
                        confidence=sig.confidence,
                    )
        except Exception:
            log.exception("crypto_analysis_job_failed")

    async def _crypto_news_job(self) -> None:
        """Poll CryptoPanic for news and compute global & coin-specific sentiment."""
        if not settings.cryptopanic_auth_token:
            return

        try:
            news_items = await self.cryptopanic.fetch()
            if not news_items:
                return

            headlines = [n.title for n in news_items]
            global_sentiment = self.sentiment.score(headlines)

            # Update overall sentiment
            await self.crypto_store.update_sentiment("ALL", global_sentiment, len(news_items))

            # Update coin-specific sentiment
            coin_headlines: dict[str, list[str]] = {}
            for item in news_items:
                for cur in item.currencies:
                    coin_headlines.setdefault(cur.upper(), []).append(item.title)

            for cur, titles in coin_headlines.items():
                cur_score = self.sentiment.score(titles)
                await self.crypto_store.update_sentiment(cur, cur_score, len(titles))

            log.info("crypto_news_sentiment_updated", global_score=global_sentiment, items_analyzed=len(news_items))
        except Exception:
            log.exception("crypto_news_job_failed")

    async def _crypto_snapshot_job(self) -> None:
        """Save periodic crypto & commodity snapshots for ML training and backtesting."""
        try:
            async with AsyncSessionFactory() as session:
                repo = Repository(session)
                # Crypto snapshots
                for state in await self.crypto_store.get_all():
                    if state.current_price > 0:
                        await repo.save_crypto_snapshot(
                            symbol=state.symbol,
                            price=state.current_price,
                            volume_24h=state.volume_24h,
                            rsi_14=state.rsi_14,
                            macd_line=state.macd_line,
                            macd_signal=state.macd_signal,
                            bollinger_upper=state.bollinger_upper,
                            bollinger_lower=state.bollinger_lower,
                            atr_14=state.atr_14,
                            sentiment_score=state.sentiment_score,
                        )

                # Commodity snapshots
                for cstate in await self.commodity_store.get_all():
                    if cstate.current_price > 0:
                        await repo.save_commodity_snapshot(
                            symbol=cstate.symbol,
                            price=cstate.current_price,
                            rsi_14=cstate.rsi_14,
                            atr_14=cstate.atr_14,
                        )
        except Exception:
            log.exception("crypto_snapshot_job_failed")

    async def _binance_oi_job(self) -> None:
        """Poll Binance Futures Open Interest for crypto perpetuals."""
        if not getattr(settings, "binance_oi_enabled", True):
            return
        try:
            await self.oi_collector.fetch_all()
        except Exception:
            log.exception("binance_oi_job_failed")

    # ── Crypto watchlist (DB-backed) ────────────────────────────────────────

    async def _load_crypto_watchlist(self) -> None:
        """Load the watchlist from the DB, seeding a small default the first
        time the table is empty (e.g. a brand-new deploy)."""
        try:
            async with AsyncSessionFactory() as session:
                repo = Repository(session)
                symbols = await repo.seed_crypto_watchlist_if_empty(
                    settings.crypto_watchlist_seed.split(",")
                )
            await self.crypto_store.seed(symbols)
            log.info("crypto_watchlist_loaded", count=len(symbols))
        except Exception:
            log.exception("crypto_watchlist_load_failed")

    async def add_crypto_symbol(self, symbol: str) -> None:
        sym = symbol.strip().lower()
        async with AsyncSessionFactory() as session:
            repo = Repository(session)
            await repo.add_crypto_watchlist_symbol(sym)
        await self.crypto_store.add_symbol(sym)

    async def remove_crypto_symbol(self, symbol: str) -> None:
        sym = symbol.strip().lower()
        async with AsyncSessionFactory() as session:
            repo = Repository(session)
            await repo.remove_crypto_watchlist_symbol(sym)
        await self.crypto_store.remove_symbol(sym)

    def setup_jobs(self) -> None:
        # ── Always-on infrastructure jobs ────────────────────────────────────
        self.scheduler.add_job(
            self._cleanup_job,
            "interval",
            hours=6,
            id="db_cleanup",
        )
        self.scheduler.add_job(
            self._heartbeat_job,
            "interval",
            minutes=10,
            id="heartbeat",
        )
        self.scheduler.add_job(
            self._self_ping_job,
            "interval",
            minutes=5,
            id="self_ping",
            max_instances=1,
        )

        # Paper trading simulator — dynamic tick job managed via database PaperTradingConfig
        self.scheduler.add_job(
            self._paper_trading_job,
            "interval",
            seconds=settings.paper_tick_interval_seconds,
            id="paper_trading_tick",
            max_instances=1,
            next_run_time=datetime.now(timezone.utc) + timedelta(seconds=15),
        )

        self.scheduler.add_job(
            self._resolve_signal_outcomes_job,
            "interval",
            minutes=15,
            id="resolve_signal_outcomes",
            max_instances=1,
            next_run_time=datetime.now(timezone.utc) + timedelta(minutes=2),
        )

        if settings.sentiment_feeds_enabled:
            self.scheduler.add_job(
                self._refresh_sentiment_job,
                "interval",
                minutes=settings.fear_greed_refresh_minutes,
                id="sentiment_refresh",
                max_instances=1,
                next_run_time=datetime.now(timezone.utc),
            )

        # Crypto & Commodities Interval Jobs
        self.scheduler.add_job(
            self._coindcx_job,
            "interval",
            seconds=settings.coindcx_poll_interval_seconds,
            id="coindcx_poll",
            max_instances=1,
            next_run_time=datetime.now(timezone.utc),
        )
        self.scheduler.add_job(
            self._klines_job,
            "interval",
            seconds=settings.binance_klines_seconds,
            id="binance_klines",
            max_instances=1,
            next_run_time=datetime.now(timezone.utc),
        )
        self.scheduler.add_job(
            self._coingecko_job,
            "interval",
            seconds=settings.coingecko_poll_interval_seconds,
            id="coingecko_poll",
            max_instances=1,
            next_run_time=datetime.now(timezone.utc),
        )
        self.scheduler.add_job(
            self._crypto_analysis_job,
            "interval",
            seconds=60,
            id="crypto_analysis",
            max_instances=1,
            next_run_time=datetime.now(timezone.utc),
        )
        self.scheduler.add_job(
            self._crypto_news_job,
            "interval",
            seconds=settings.cryptopanic_poll_interval_seconds,
            id="crypto_news",
            max_instances=1,
            next_run_time=datetime.now(timezone.utc),
        )
        self.scheduler.add_job(
            self._crypto_snapshot_job,
            "interval",
            seconds=settings.crypto_snapshot_interval_seconds,
            id="crypto_snapshot",
            max_instances=1,
        )
        if getattr(settings, "binance_oi_enabled", True):
            self.scheduler.add_job(
                self._binance_oi_job,
                "interval",
                seconds=120,
                id="binance_oi_poll",
                max_instances=1,
                next_run_time=datetime.now(timezone.utc),
            )

    async def start(self) -> None:
        await self.notifier.verify()

        # Crypto watchlist lives in the DB — load/seed it before the WS collector
        # picks up symbols, so the very first connection already has the right set.
        await self._load_crypto_watchlist()

        self.setup_jobs()
        self.scheduler.start()

        # Launch continuous WebSocket tasks concurrently (non-blocking)
        if self.collector_enabled.get("binance_ws", True):
            task = asyncio.create_task(self.binance_ws.run_forever(), name="binance_ws")
            self._ws_tasks.append(task)
            log.info("binance_ws_task_spawned")

        if self.collector_enabled.get("twelvedata_ws", True) and settings.twelvedata_api_key:
            task = asyncio.create_task(self.twelvedata_ws.run_forever(), name="twelvedata_ws")
            self._ws_tasks.append(task)
            log.info("twelvedata_ws_task_spawned")

        # Preload bundled historical candles into in-memory state store (zero-DB requirement)
        from collectors.historical_data_service import HistoricalDataService
        await HistoricalDataService.preload_states(self.crypto_store)

        crypto_count = await self.crypto_store.count()
        await self.notifier.send_text(
            "🪙 Crypto Signal Engine started.\n"
            f"Crypto: CoinDCX + CoinGecko {crypto_count}-symbol watchlist "
            f"(poll every {settings.coindcx_poll_interval_seconds}s/{settings.coingecko_poll_interval_seconds}s)\n"
            f"Commodities: Twelve Data Gold/Silver/Oil ({'active' if settings.twelvedata_api_key else 'no key'})\n"
            f"News Sentiment: CryptoPanic ({'active' if settings.cryptopanic_auth_token else 'no token'})\n"
            f"Paper Trading: Active in background\n"
            f"AI Sentinel: {'Active' if settings.groq_api_key else 'Disabled (no key)'}"
        )
        log.info("scheduler_started", crypto_symbols_count=crypto_count)

    def get_status(self) -> dict:
        return {
            "crypto": {
                "coindcx_consecutive_failures": self.coindcx._consecutive_failures,
                "coindcx_matched_symbols": len(self.coindcx.last_matched_symbols),
                "coingecko_consecutive_failures": self.coingecko._consecutive_failures,
                "binance_ws_connected": self.binance_ws._running and self.binance_ws._consecutive_failures == 0,
                "binance_messages_received": self.binance_ws._total_messages_received,
                "signals_today": len(self.crypto_engine.get_recent_signals(24)),
                "sentiment_mode": self.sentiment._mode,
                "cryptopanic_token": bool(settings.cryptopanic_auth_token),
                "twelvedata_key": bool(settings.twelvedata_api_key),
            },
        }

    async def stop(self) -> None:
        self.scheduler.shutdown(wait=False)
        self.binance_ws.stop()
        self.twelvedata_ws.stop()
        for task in self._ws_tasks:
            task.cancel()
        await self.notifier.send_text("Crypto signal engine stopped.")
        log.info("scheduler_stopped")
