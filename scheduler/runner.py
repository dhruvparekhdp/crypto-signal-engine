"""
APScheduler-based 24/7 job runner.

Market data, in order of trust:
  - Binance klines (60s)  → real 1-minute OHLCV plus depth; everything derives from it
  - CoinDCX ticker (30s)  → keeps price fresh between kline polls
  - CoinGecko (60s)       → covers anything CoinDCX does not list

Jobs:
  - binance_klines / coindcx_poll / coingecko_poll → market data
  - binance_oi_poll (120s)      → futures open interest, for leverage build-up
  - crypto_analysis (60s)       → run the detectors, alert, queue for paper trading
  - crypto_news / sentiment     → CryptoPanic headlines, Fear & Greed
  - paper_trading_tick (30s)    → resolve open positions, then consider new ones
  - resolve_signal_outcomes     → score fired signals against what price did next
  - crypto_snapshot / db_cleanup / heartbeat

Candles are never persisted — they live in memory, capped per symbol, and are
refetched on boot. Only fired signals, snapshots and paper trades reach the DB.
"""
import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram.constants import ParseMode

from analysis.crypto_engine import CryptoEngine
from analysis.crypto_signal import CryptoSignal
from analysis.crypto_state_store import CommodityStateStore, CryptoStateStore
from analysis.multi_horizon_predictor import MultiHorizonPredictor
from analysis.paper_cycle import (
    ClosedTradeView,
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
from analysis.sentiment import SentimentAnalyzer
from collectors.binance_futures_oi import BinanceFuturesOICollector
from collectors.binance_klines import BinanceKlines
from collectors.binance_ws import BinanceWSCollector
from collectors.coindcx import CoinDCXCollector
from collectors.coingecko import CoinGeckoCollector
from collectors.cryptopanic import CryptoPanicCollector
from collectors.macro_sentinel import GroqSentinel
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
        # The last research pass, kept in memory so /api/research can serve it
        # without recomputing. It survives until restart, which is the right
        # lifetime for something regenerated weekly.
        self.last_research: dict = {}
        self.last_research_text: str = ""
        # When each symbol's open position was last put to the reviewer. The
        # tick is every 30s and no market reconsiders that often.
        self._last_position_review: dict[str, datetime] = {}
        self.oi_collector = BinanceFuturesOICollector(self.crypto_store)
        self._pending_paper_signals: list[tuple[CryptoSignal, object]] = []
        self.fear_greed = None
        # High-impact headlines seen by the news job, as blackout events.
        self._news_events: tuple = ()
        # Latest web-searched world briefing (a MarketBriefing row).
        self.latest_briefing = None
        self.multi_horizon = MultiHorizonPredictor()
        self._ws_tasks: list[asyncio.Task] = []
        # Binance-only leaves klines as the single source of candles, depth
        # and price, so the three of them cannot disagree with each other.
        binance_only = settings.binance_only_mode
        self.collector_enabled: dict[str, bool] = {
            "coindcx": not binance_only,
            "coingecko": not binance_only,
            "binance_ws": settings.binance_ws_enabled,
            "twelvedata_ws": not binance_only,
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
        # Retire any duplicate first, so a second cycle created by a second
        # worker cannot quietly take ownership of the next trade.
        closed = await repo.close_duplicate_cycles()
        if closed:
            log.error("duplicate_paper_cycles_closed", closed=closed,
                      hint="more than one worker was running the paper job")
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
            opened_at=row.opened_at.replace(tzinfo=UTC),
            entry_fee=row.entry_fee,
            signal_type=row.signal_type,
            timeframe=row.timeframe,
            confidence=row.confidence,
            expires_at=(row.expires_at.replace(tzinfo=UTC)
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
                        # The position survived the tick's exits. A losing one
                        # now has to justify staying open; a winning one has
                        # its trail set by the same confidence. Nothing here
                        # can defer an exit that already fired — by the time a
                        # stop is reached the loss is no longer bounded, so
                        # this runs only on what resolve_at_price left alive.
                        trade = await self._review_open_position(
                            pos, st, cfg, now, wallet, repo)
                    if trade is None:
                        await repo.sync_position(row.id, pos)
                        live_ids[len(live)] = row.id
                        live.append(pos)
                        continue

                    wallet = trade.wallet_after
                    await repo.record_trade(cycle.id, trade)
                    await repo.delete_position(row.id)
                    # Post-mortem runs detached: the trade is already closed and
                    # recorded, so a slow or failing reviewer must not hold up
                    # the rest of the tick or the positions still to resolve.
                    if (settings.groq_postmortem_enabled
                            and self.groq_sentinel.postmortem_available):
                        asyncio.create_task(self._review_closed_trade(trade, row))
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

                # Around FOMC / CPI / jobs releases and after confident
                # high-impact news, price spikes both ways within seconds and
                # takes out stops sized for an ordinary hour. Stand aside;
                # open positions keep their stops.
                blackout = self._blackout(now)
                if blackout is not None and pending:
                    log.info("paper_trades_blackout", event=blackout.name,
                             kind=blackout.kind, skipped=len(pending))
                    pending = []

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

    async def _log_signal(self, sig, suppressed_by: str = "") -> int:
        """
        Record a signal, whether or not it was published.

        A suppressed row carries the name of the filter that stopped it and
        is otherwise identical, so the outcome resolver scores it the same
        way. That is what makes "what did this filter cost me" answerable
        instead of a matter of opinion.
        """
        try:
            async with AsyncSessionFactory() as session:
                return await Repository(session).log_crypto_signal(
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
                    suppressed_by=suppressed_by,
                )
        except Exception:
            log.exception("crypto_signal_db_log_failed", symbol=sig.symbol)
            return 0

    async def _review_open_position(self, pos, state, cfg, now, wallet, repo):
        """
        Hold, close early, or tighten the trail — decided every tick.

        Losing positions are the ones that get reviewed against the 75%
        threshold, because they are the ones costing money. Winners are left to
        the trail, whose distance is set by the same local confidence: more
        confidence buys a looser trail, less takes what is on the table.

        Rate-limited per position, not per tick. The tick runs every thirty
        seconds and no market changes its mind that often; without this, one
        position open for two hours would be 240 reviews.
        """
        from analysis.position_review import review_position, trail_r_for_confidence

        if not settings.position_review_enabled:
            return None

        # Gross, not net: fees are already sunk at entry and would make every
        # freshly opened position look like a loser for its first few minutes,
        # which is exactly when there is least to judge.
        losing = pos.gross_pnl(state.current_price) < 0

        # Both sides are reviewed, and both scores include the model's read.
        # They are reviewed at different rates because they are asking for
        # different things: a losing position is deciding whether to exist,
        # which is worth checking often, while a winning one is only choosing
        # how much rope to give its trail — and the trail already bounds what
        # that decision can cost.
        gap = (settings.position_review_interval_seconds if losing
               else settings.position_review_interval_seconds_winning)
        last = self._last_position_review.get(pos.symbol)
        if last is not None and (now - last).total_seconds() < gap:
            return None
        self._last_position_review[pos.symbol] = now

        news, briefing_id = await self._news_context(pos.symbol)
        review = await review_position(pos, state, now, losing=losing, news=news)
        if review.asked_model:
            # Every model-backed hold/close decision is kept with the news it
            # saw, so the local model can later relate it to what happened.
            # Its own session: the tick's session must only commit what the
            # tick itself decided.
            margin = getattr(pos, "margin", 0) or 0
            async with AsyncSessionFactory() as s2:
                await Repository(s2).save_review(
                    "hold", pos.symbol, signal_type=pos.signal_type or "",
                    verdict=review.verdict, factors=review.factors, summary=review.summary,
                    confidence_delta=round(review.confidence - review.trend, 4),
                    pnl_pct=(round(pos.gross_pnl(state.current_price) / margin * 100, 4)
                             if margin else 0.0),
                    briefing_id=briefing_id, news_context=news,
                    **self._review_extras(state))

        if not losing:
            # A winner is never closed on a score — the trail does that, and
            # the score only decides how far behind price it rides.
            if cfg.trailing is not None and cfg.trailing.enabled:
                pos.trail_r_override = trail_r_for_confidence(review.confidence)
            return None

        if review.hold:
            return None

        from analysis.paper_trading import ExitReason, close_position, fees_for

        log.info("position_closed_early", symbol=pos.symbol,
                 confidence=round(review.confidence, 3), factors=review.factors,
                 summary=review.summary)
        return close_position(pos, state.current_price, ExitReason.CONVICTION_LOST,
                              now, fees_for(pos.symbol), wallet)

    async def _review_closed_trade(self, trade, row) -> None:
        """
        Ask what the trade taught, once the answer is in.

        Runs as its own task. Nothing downstream waits on it, and a review
        that fails should cost nothing but the review — the trade is already
        closed and booked by the time this starts.
        """
        try:
            pre = ""
            news, briefing_id = await self._news_context(row.symbol)
            state = await self.crypto_store.get(row.symbol)
            async with AsyncSessionFactory() as session:
                repo = Repository(session)
                review = await self.groq_sentinel.review_closed_trade(
                    ClosedTradeView(trade, row), pre_review=pre, news=news)
                if not review:
                    return
                await repo.save_review(
                    "post", row.symbol,
                    signal_type=row.signal_type,
                    verdict=review.get("verdict", ""),
                    factors=review.get("factors", ""),
                    summary=review.get("summary", ""),
                    outcome=trade.reason.value,
                    pnl_pct=round(trade.return_on_margin * 100, 4),
                    model=review.get("model", ""),
                    latency_ms=review.get("latency_ms", 0),
                    briefing_id=briefing_id, news_context=news,
                    **self._review_extras(state),
                )
        except Exception:
            log.debug("post_review_failed", symbol=getattr(row, "symbol", "?"))

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
            moved = await repo.archive_old_crypto_data()
        log.info("db_cleanup_done", archived=moved)

    async def _research_pass_job(self) -> None:
        """
        Measure the history, then ask the strongest model what to test next.

        The measuring is the valuable half and happens in code, because no
        model computes a rank correlation over tens of thousands of rows —
        asked to, it produces a number shaped like an answer. What the model
        gets is the two-kilobyte result, which is the part it is good at:
        reading a table of weak effects against a cost hurdle and saying
        which one is worth a week.
        """
        from analysis.research_report import (
            ask_for_hypotheses,
            load_and_analyse,
            render,
        )

        try:
            async with AsyncSessionFactory() as session:
                found = await load_and_analyse(
                    session, days=settings.snapshot_retention_days)
            if not found.rows:
                log.info("research_pass_skipped", reason="no labelled snapshots yet")
                return
            self.last_research_text = render(found)
            self.last_research = await ask_for_hypotheses(found)
        except Exception:
            log.exception("research_pass_failed")

    async def _label_snapshots_job(self) -> None:
        """
        Write each snapshot's forward prices, once enough time has passed.

        The columns have existed since the schema was written and nothing has
        ever filled them, so every row in the table has 0.0 for all four. The
        data to fill them was always there — the price half an hour after a
        10:00 snapshot is sitting in the 10:30 snapshot — it was simply never
        joined up. Without this the table is features with no labels, and
        nothing can be fitted on it.

        Only looks at the last few days. At 120 days of retention the table
        holds around 600,000 rows, and loading all of them four times an hour
        to write a few hundred labels would be most of the database's day
        spent on nothing. History restored in bulk needs scripts/backfill_labels.py,
        which is a one-off by design.
        """
        from analysis.snapshot_labeler import backfill_labels

        try:
            async with AsyncSessionFactory() as session:
                await backfill_labels(session)
        except Exception:
            log.exception("snapshot_labelling_failed")

    async def _heartbeat_job(self) -> None:
        # Symbols carrying a live price, not symbols merely on the watchlist:
        # a watchlist entry no feed has answered for is the failure this line
        # exists to make visible.
        states = await self.crypto_store.get_all()
        priced = sum(1 for st in states if st.current_price > 0)
        log.info(
            "heartbeat",
            symbols_tracked=len(states),
            symbols_priced=priced,
            klines_host=self.klines.host or "none",
            klines_last_success=(self.klines.last_success.isoformat()
                                 if self.klines.last_success else None),
            coindcx_failures=self.coindcx._consecutive_failures,
            coingecko_failures=self.coingecko._consecutive_failures,
        )

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

                    # Groq AI Pre-Signal Sanity Review.
                    #
                    # A voice with weight, not a veto. A ±0.04 nudge was too
                    # quiet to matter — it called a 43% gold target
                    # "mathematically impossible" and the signal published
                    # anyway. A hard veto is the other extreme: one model's
                    # bad call would silently kill good setups with no trace
                    # in the numbers. So a REJECT costs real confidence and
                    # the ordinary threshold decides, which keeps every
                    # decision in one place and visible in the logs.
                    if self.groq_sentinel.is_available and scfg.groq_signal_review_enabled:
                        news, briefing_id = await self._news_context(sig.symbol)
                        delta, ai_summary, verdict = (
                            await self.groq_sentinel.review_signal_candidate(
                                sig, state, model=scfg.groq_model, book=states, news=news)
                        )
                        if verdict == "REJECT":
                            delta = -abs(settings.groq_reject_penalty)
                        if ai_summary:
                            sig.ai_review = ai_summary
                        try:
                            async with AsyncSessionFactory() as s2:
                                await Repository(s2).save_review(
                                    "pre", sig.symbol, signal_type=sig.signal_type,
                                    verdict=verdict, summary=ai_summary,
                                    factors=self.groq_sentinel.last_factors,
                                    confidence_delta=delta,
                                    model=self.groq_sentinel.last_model or scfg.groq_model,
                                    latency_ms=self.groq_sentinel.last_latency_ms,
                                    briefing_id=briefing_id, news_context=news,
                                    **self._review_extras(state))
                        except Exception:
                            log.debug("pre_review_not_saved", symbol=sig.symbol)
                        before = sig.confidence
                        sig.confidence = max(0.50, min(0.95, round(before + delta, 4)))
                        if (scfg.crypto_min_confidence > 0
                                and sig.confidence < scfg.crypto_min_confidence):
                            log.info("crypto_signal_dropped_after_ai_review",
                                     symbol=sig.symbol, type=sig.signal_type,
                                     verdict=verdict, confidence_before=before,
                                     confidence_after=sig.confidence,
                                     threshold=scfg.crypto_min_confidence,
                                     reason=ai_summary)
                            # Logged as a shadow, not discarded. The resolver
                            # scores it like any other signal, so the cost of
                            # blocking it is measurable. A filter only ever
                            # judged on what it let through cannot be wrong.
                            await self._log_signal(sig, suppressed_by="ai_review")
                            continue

                    msg = format_crypto_signal(sig)
                    if settings.crypto_alert_telegram:
                        await self.notifier.send_text(msg, parse_mode=ParseMode.HTML)

                    if pcfg.enabled:
                        self._pending_paper_signals.append((sig, state))

                    await self._log_signal(sig)

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

    async def _news_sentiment_job(self) -> None:
        """
        Turn the scored headlines the news scorer posts into sentiment_score.

        Every five minutes: read the last twelve hours of headlines, weight
        each by confidence, source and age, and set each followed symbol's
        score from its own news plus half the macro news. A quiet feed decays
        to zero by itself. Also notes confident high-impact headlines, which
        the paper tick treats as a blackout.

        Stands down when CryptoPanic is configured, so two jobs never write
        the same field.
        """
        if not settings.news_sentiment_enabled or settings.cryptopanic_auth_token:
            return
        from analysis.event_calendar import news_events
        from analysis.news_sentiment import WINDOW_HOURS, per_symbol
        from collectors.hermes import HIGH_IMPACT

        try:
            async with AsyncSessionFactory() as session:
                rows = await Repository(session).news_sentiment_since(WINDOW_HOURS)
            now = datetime.now(UTC).replace(tzinfo=None)
            symbols = await self.crypto_store.get_symbols()
            scores = per_symbol(rows, symbols, now)
            for sym, (score, count) in scores.items():
                await self.crypto_store.set_sentiment(sym, score, count)
            self._news_events = news_events(rows, now, HIGH_IMPACT)
            log.info("news_sentiment_updated", headlines=len(rows),
                     scores={s: round(v[0], 3) for s, v in scores.items()},
                     high_impact=len(self._news_events))
        except Exception:
            log.exception("news_sentiment_job_failed")

    async def _market_briefing_job(self) -> None:
        """
        Every 30 minutes: a web-searched briefing on world events from Groq.

        Stored, fed into news_sentiment as macro headlines (so it reaches the
        sentiment score and the trading pause), and handed to every reviewer.
        """
        from collectors.llm_client import chain_for
        from collectors.market_briefing import as_news_items, fetch_briefing

        if not settings.market_briefing_enabled or not chain_for("briefing"):
            return
        try:
            got = await fetch_briefing()
            if got is None:
                return
            tone, summary, events, model, latency = got
            async with AsyncSessionFactory() as session:
                repo = Repository(session)
                await repo.save_briefing(tone, summary, events, model, latency)
                if events:
                    await repo.ingest_news_sentiment(as_news_items(events, model))
                self.latest_briefing = await repo.latest_briefing()
        except Exception:
            log.exception("market_briefing_job_failed")

    async def _move_attribution_job(self) -> None:
        """
        Every hour: ask Groq why each watchlist coin moved and whether our
        signals were on the right side of it. Moves are computed here from
        stored prices; the model only explains them.
        """
        from analysis.move_attribution import (
            ATTRIBUTION_SYSTEM,
            build_prompt,
            parse_attribution,
            signals_digest,
            summarise_moves,
        )
        from collectors.llm_client import ask_json, chain_for
        from collectors.market_briefing import context_block

        if not settings.move_attribution_enabled or not chain_for("attribution"):
            return
        hours = settings.move_attribution_window_hours
        try:
            symbols = await self.crypto_store.get_symbols()
            async with AsyncSessionFactory() as session:
                repo = Repository(session)
                points = await repo.price_points(symbols, hours + 1)
                sigs = [s for s in await repo.crypto_signals_between(
                            1, include_suppressed=True, limit=200)
                        if s.timestamp and (datetime.now(UTC).replace(tzinfo=None)
                                            - s.timestamp).total_seconds() <= hours * 3600]
                headlines = await repo.news_sentiment_since(hours, limit=200)
                briefing = self.latest_briefing or await repo.latest_briefing()
            now = datetime.now(UTC).replace(tzinfo=None)
            moves = summarise_moves(points, now)
            if not moves:
                log.info("move_attribution_skipped", reason="no stored prices yet")
                return
            digest = signals_digest(sigs)
            news = context_block(briefing, headlines, "*", limit=20)
            reply = await ask_json("attribution", ATTRIBUTION_SYSTEM,
                                   build_prompt(moves, digest, news, hours),
                                   max_tokens=2500, temperature=0.2, timeout=120.0)
            if not reply or not isinstance(reply.data, dict):
                return
            result = parse_attribution(reply.data, {m.symbol for m in moves})
            async with AsyncSessionFactory() as session:
                await Repository(session).save_move_attribution(
                    window_hours=hours, briefing_id=briefing.id if briefing else 0,
                    moves=[m.as_dict() for m in moves], signals=digest, result=result,
                    model=reply.served_by, latency_ms=reply.latency_ms)
            log.info("move_attribution_saved", coins=len(result["coins"]),
                     drivers=len(result["drivers"]), served_by=reply.served_by)
        except Exception:
            log.exception("move_attribution_job_failed")

    async def _news_context(self, symbol: str) -> tuple[str, int]:
        """(news paragraph for a reviewer, id of the briefing in it). Never raises."""
        from collectors.market_briefing import context_block
        try:
            async with AsyncSessionFactory() as session:
                repo = Repository(session)
                if self.latest_briefing is None:
                    self.latest_briefing = await repo.latest_briefing()
                headlines = await repo.news_sentiment_since(12, limit=200)
            b = self.latest_briefing
            return context_block(b, headlines, symbol), (b.id if b is not None else 0)
        except Exception:
            return "", 0

    def _review_extras(self, state) -> dict:
        """Market mood at review time, stored beside the verdict."""
        return {
            "sentiment_score": round(float(getattr(state, "sentiment_score", 0.0) or 0.0), 4),
            "fear_greed": int(self.fear_greed.value) if self.fear_greed else 0,
        }

    def _blackout(self, now: datetime):
        """The event that forbids opening a trade right now, or None."""
        if not settings.event_blackout_enabled:
            return None
        from analysis.event_calendar import active_blackout
        naive = now.replace(tzinfo=None) if now.tzinfo else now
        return active_blackout(naive, extra=self._news_events)

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
        # Every 15 minutes rather than hourly: the 30-minute horizon is the
        # shortest, and a label written an hour late is a row that spends an
        # hour looking unlabelled to anything reading the table.
        self.scheduler.add_job(
            self._label_snapshots_job,
            "interval",
            minutes=15,
            id="snapshot_labels",
            max_instances=1,
        )
        # Weekly, because the thing it measures moves on the scale of weeks.
        # Running it nightly would mostly re-measure the same fortnight and
        # invite reading noise as a change.
        self.scheduler.add_job(
            self._research_pass_job,
            "interval",
            days=7,
            id="research_pass",
            max_instances=1,
        )
        self.scheduler.add_job(
            self._heartbeat_job,
            "interval",
            minutes=10,
            id="heartbeat",
        )
        # Paper trading simulator — dynamic tick job managed via database PaperTradingConfig
        self.scheduler.add_job(
            self._paper_trading_job,
            "interval",
            seconds=settings.paper_tick_interval_seconds,
            id="paper_trading_tick",
            max_instances=1,
            next_run_time=datetime.now(UTC) + timedelta(seconds=15),
        )

        self.scheduler.add_job(
            self._resolve_signal_outcomes_job,
            "interval",
            minutes=15,
            id="resolve_signal_outcomes",
            max_instances=1,
            next_run_time=datetime.now(UTC) + timedelta(minutes=2),
        )

        if settings.sentiment_feeds_enabled and not settings.binance_only_mode:
            self.scheduler.add_job(
                self._refresh_sentiment_job,
                "interval",
                minutes=settings.fear_greed_refresh_minutes,
                id="sentiment_refresh",
                max_instances=1,
                next_run_time=datetime.now(UTC),
            )

        # Crypto & Commodities Interval Jobs
        self.scheduler.add_job(
            self._coindcx_job,
            "interval",
            seconds=settings.coindcx_poll_interval_seconds,
            id="coindcx_poll",
            max_instances=1,
            next_run_time=datetime.now(UTC),
        )
        self.scheduler.add_job(
            self._klines_job,
            "interval",
            seconds=settings.binance_klines_seconds,
            id="binance_klines",
            max_instances=1,
            next_run_time=datetime.now(UTC),
        )
        self.scheduler.add_job(
            self._coingecko_job,
            "interval",
            seconds=settings.coingecko_poll_interval_seconds,
            id="coingecko_poll",
            max_instances=1,
            next_run_time=datetime.now(UTC),
        )
        self.scheduler.add_job(
            self._crypto_analysis_job,
            "interval",
            seconds=60,
            id="crypto_analysis",
            max_instances=1,
            next_run_time=datetime.now(UTC),
        )
        self.scheduler.add_job(
            self._move_attribution_job,
            "interval",
            minutes=settings.move_attribution_minutes,
            id="move_attribution",
            max_instances=1,
            next_run_time=datetime.now(UTC) + timedelta(minutes=5),
        )
        self.scheduler.add_job(
            self._market_briefing_job,
            "interval",
            minutes=settings.market_briefing_minutes,
            id="market_briefing",
            max_instances=1,
            next_run_time=datetime.now(UTC),
        )
        self.scheduler.add_job(
            self._news_sentiment_job,
            "interval",
            minutes=5,
            id="news_sentiment",
            max_instances=1,
            next_run_time=datetime.now(UTC),
        )
        if not settings.binance_only_mode:
            self.scheduler.add_job(
                self._crypto_news_job,
                "interval",
                seconds=settings.cryptopanic_poll_interval_seconds,
                id="crypto_news",
                max_instances=1,
                next_run_time=datetime.now(UTC),
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
                next_run_time=datetime.now(UTC),
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
        if settings.binance_only_mode:
            sources = (f"Data: <b>Binance only</b> — klines every "
                       f"{settings.binance_klines_seconds}s carry candles, depth and price "
                       f"for {crypto_count} symbols\n"
                       "CoinDCX, CoinGecko, Twelve Data and news feeds: off")
        else:
            sources = (f"Crypto: CoinDCX + CoinGecko {crypto_count}-symbol watchlist "
                       f"(poll every {settings.coindcx_poll_interval_seconds}s/"
                       f"{settings.coingecko_poll_interval_seconds}s)\n"
                       f"Commodities: Twelve Data "
                       f"({'active' if settings.twelvedata_api_key else 'no key'})\n"
                       f"News Sentiment: CryptoPanic "
                       f"({'active' if settings.cryptopanic_auth_token else 'no token'})")
        await self.notifier.send_text(
            "🪙 Crypto Signal Engine started.\n"
            f"{sources}\n"
            "Paper Trading: Active in background\n"
            f"AI Sentinel: {'Active' if settings.groq_api_key else 'Disabled (no key)'}",
            parse_mode=ParseMode.HTML,
        )
        log.info("scheduler_started", crypto_symbols_count=crypto_count,
                 binance_only_mode=settings.binance_only_mode)

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
