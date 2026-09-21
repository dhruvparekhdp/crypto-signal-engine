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
from analysis.engine import AnalysisEngine
from analysis.football_engine import FootballEngine
from analysis.football_state import FootballStateStore
from analysis.match_state import MatchState
from analysis.ml_predictor import MLPredictor
from analysis.multi_horizon_predictor import MultiHorizonPredictor
from analysis.scalping import scan_all
from analysis.sentiment import SentimentAnalyzer
from analysis.state_store import MatchStateStore
from analysis.win_probability import compute_win_probability
from collectors.api_tennis import ApiTennisCollector
from collectors.bets_api import BetsAPICollector
from collectors.binance_futures_oi import BinanceFuturesOICollector
from collectors.binance_klines import BinanceKlines
from collectors.binance_ws import BinanceWSCollector
from collectors.coindcx import CoinDCXCollector
from collectors.coingecko import CoinGeckoCollector
from collectors.macro_sentinel import GroqSentinel
from collectors.cryptopanic import CryptoPanicCollector
from collectors.espn import ESPNCollector
from collectors.flashscore import FlashscoreCollector
from collectors.football_espn import FootballESPNCollector
from collectors.football_odds_api import FootballOddsApiCollector
from collectors.historical_importer import run_import
from collectors.odds_api import OddsApiCollector
from collectors.slam_pbp_importer import run_slam_import
from collectors.sofascore import SofascoreCollector
from collectors.sportradar import SportradarCollector
from collectors.sportsdata import SportsDataCollector
from collectors.thesportsdb import TheSportsDBCollector
from collectors.sentiment_feeds import adjust_confidence, fetch_fear_greed
from collectors.twelvedata_ws import TwelveDataWSCollector
from config.settings import settings
from notifications.crypto_formatter import (
    format_crypto_signal,
    format_cycle_end,
    format_paper_trade,
)
from notifications.football_formatter import format_football_signal
from notifications.telegram_notifier import TelegramNotifier
from storage.database import AsyncSessionFactory
from storage.repository import Repository

log = structlog.get_logger()

# Save a snapshot every this many data polls (30s * 4 = ~2 minutes)
_SNAPSHOT_EVERY_N_POLLS = 4


def _infer_winner(state: MatchState) -> int | None:
    """Infer match winner from final sets score. Returns None if inconclusive."""
    if state.sets_p1 > state.sets_p2:
        return 1
    if state.sets_p2 > state.sets_p1:
        return 2
    return None


def _format_score(state: MatchState) -> str:
    return f"{state.sets_p1}-{state.sets_p2} sets ({state.games_in_set_p1}-{state.games_in_set_p2} current)"


class AppRunner:
    def __init__(self) -> None:
        self.store = MatchStateStore()
        self.flashscore = FlashscoreCollector(self.store)
        self.espn = ESPNCollector(self.store)
        self.sofascore = SofascoreCollector(self.store)
        self.thesportsdb = TheSportsDBCollector(api_key=settings.thesportsdb_api_key)
        self.odds_api = OddsApiCollector(self.store)
        self.bets_api = BetsAPICollector(self.store)
        self.ml_predictor = MLPredictor()
        self.notifier = TelegramNotifier()
        self.scheduler = AsyncIOScheduler()
        # Tennis: persistent engine so _cooldowns survive across poll cycles
        self._engine: AnalysisEngine | None = None
        # Track last-seen match states to detect completions
        self._last_states: dict[str, MatchState] = {}
        # Per-match poll counter for snapshot throttling
        self._poll_counters: dict[str, int] = {}
        # Football
        self.football_store = FootballStateStore()
        self.football_espn = FootballESPNCollector(self.football_store)
        self.football_odds = FootballOddsApiCollector(self.football_store)
        self.football_engine = FootballEngine()
        # Sportradar — covers ALL tennis (Challengers, ITF) + ALL football in one call each
        self.sportradar = SportradarCollector(self.store, self.football_store)
        # SportsData.io — live + scheduled tennis
        self.sportsdata = SportsDataCollector(self.store)
        # API-Tennis — live + scheduled, no quota limits
        self.api_tennis = ApiTennisCollector(self.store)
        # Scalping alerts — track last Telegram ping per match to avoid spam
        self._scalp_alert_times: dict[str, datetime] = {}
        # Crypto & Commodities — watchlist itself is DB-backed, loaded in start()
        self.crypto_store = CryptoStateStore()
        self.commodity_store = CommodityStateStore()
        # CoinDCX is the preferred crypto price source (free, no key, exact
        # exchange prices). CoinGecko fills in anything CoinDCX doesn't list.
        # Binance's WebSocket API returns HTTP 451 (geoblocked) from Render's
        # IPs, so it can't be relied on there — binance_ws is kept available
        # as an opt-in toggle (e.g. for a non-US deploy region) but starts
        # disabled.
        self.coindcx = CoinDCXCollector(self.crypto_store)
        self.coingecko = CoinGeckoCollector(self.crypto_store)
        self.binance_ws = BinanceWSCollector(self.crypto_store)
        # Real klines and depth over REST. The websocket is geo-blocked from
        # this region; the REST mirror generally is not.
        self.klines = BinanceKlines(self.crypto_store)
        self.twelvedata_ws = TwelveDataWSCollector(self.commodity_store)
        self.cryptopanic = CryptoPanicCollector()
        self.sentiment = SentimentAnalyzer()
        self.crypto_engine = CryptoEngine()
        self.groq_sentinel = GroqSentinel()
        self.oi_collector = BinanceFuturesOICollector(self.crypto_store)
        self._pending_paper_signals: list[tuple[CryptoSignal, object]] = []
        # Cached because it only updates daily; refreshed by its own job.
        self.fear_greed = None
        self.multi_horizon = MultiHorizonPredictor()
        self._ws_tasks: list[asyncio.Task] = []
        # Collector enable/disable toggles (runtime, not persisted across restarts)
        self.collector_enabled: dict[str, bool] = {
            "sportradar": True,
            "sportsdata": True,
            "api_tennis": True,
            "odds_api": True,
            "api_sports": True,
            "espn": True,
            "bets_api": True,
            "coindcx": True,
            "coingecko": True,
            # Driven by env: main Binance host is geo-blocked (451) from Render's
            # US IPs. Probe /api/debug/binance first, then flip BINANCE_WS_ENABLED.
            "binance_ws": settings.binance_ws_enabled,
            "twelvedata_ws": True,
        }

    async def _data_poll_job(self) -> None:
        await self.flashscore.fetch()
        await self.espn.fetch()
        await self.bets_api.fetch()

        if self.sofascore._consecutive_failures < 5:
            await self.sofascore.fetch()

        current_states = {s.match_id: s for s in await self.store.get_all()}

        # Detect matches that just completed (were live last poll, gone now)
        completed_ids = set(self._last_states) - set(current_states)
        if completed_ids:
            await self._handle_completions(completed_ids)

        # Run analysis + take snapshots
        await self._run_analysis(current_states)

        self._last_states = current_states

    async def _handle_completions(self, completed_ids: set[str]) -> None:
        """Process matches that disappeared from the live feed."""
        async with AsyncSessionFactory() as session:
            repo = Repository(session)
            for match_id in completed_ids:
                state = self._last_states[match_id]
                winner = _infer_winner(state)
                if winner is None:
                    log.info("match_completion_inconclusive", match_id=match_id,
                             sets=f"{state.sets_p1}-{state.sets_p2}")
                    continue

                total_sigs, correct_sigs = await repo.update_signal_outcomes(match_id, winner)
                await repo.label_match_snapshots(match_id, winner)
                await repo.save_match_completion(
                    match_id=match_id,
                    player1_name=state.player1_name,
                    player2_name=state.player2_name,
                    winner=winner,
                    final_sets_p1=state.sets_p1,
                    final_sets_p2=state.sets_p2,
                    final_score_str=_format_score(state),
                    tournament=state.tournament,
                    surface=state.surface,
                    total_games=state.total_games_played(),
                    total_signals=total_sigs,
                    signals_correct=correct_sigs,
                )
                await repo.mark_match_finished(match_id)

                winner_name = state.player1_name if winner == 1 else state.player2_name
                accuracy = f"{correct_sigs}/{total_sigs}" if total_sigs > 0 else "no signals"
                log.info(
                    "match_completed",
                    match_id=match_id,
                    winner=winner_name,
                    score=_format_score(state),
                    signal_accuracy=accuracy,
                )
                # Clean up poll counter
                self._poll_counters.pop(match_id, None)

    async def _run_analysis(self, current_states: dict[str, MatchState]) -> None:
        if not current_states:
            return
        async with AsyncSessionFactory() as session:
            repo = Repository(session)
            if self._engine is None:
                self._engine = AnalysisEngine(repo)
            else:
                self._engine.repository = repo

            for match_id, state in current_states.items():
                try:
                    # Run signal analysis
                    signals = await self._engine.process(state)
                    for sig in signals:
                        await self.notifier.send_signal(sig)
                        log.info(
                            "signal_fired",
                            signal_type=sig.signal_type,
                            match_id=sig.match_id,
                            player=sig.player_name,
                            confidence=sig.confidence,
                        )

                    # Take periodic snapshot (every N polls per match)
                    counter = self._poll_counters.get(match_id, 0) + 1
                    self._poll_counters[match_id] = counter
                    if counter % _SNAPSHOT_EVERY_N_POLLS == 0:
                        await self._save_snapshot(repo, state)

                except Exception:
                    log.exception("analysis_job_failed", match_id=match_id)

    async def _save_snapshot(self, repo: Repository, state: MatchState) -> None:
        """Save a periodic match state snapshot for ML training."""
        try:
            model_p1, model_p2 = compute_win_probability(state)
            # Momentum: positive = p1 streak, negative = p2 streak
            p1_streak = state.consecutive_games_won_by(1)
            p2_streak = state.consecutive_games_won_by(2)
            momentum = p1_streak if p1_streak > 0 else -p2_streak

            await repo.save_match_snapshot(
                match_id=state.match_id,
                player1_name=state.player1_name,
                player2_name=state.player2_name,
                surface=state.surface,
                tournament=state.tournament,
                sets_p1=state.sets_p1,
                sets_p2=state.sets_p2,
                games_p1=state.games_in_set_p1,
                games_p2=state.games_in_set_p2,
                current_set=state.current_set,
                total_games_played=state.total_games_played(),
                p1_momentum=momentum,
                odds_p1=state.odds_p1,
                odds_p2=state.odds_p2,
                model_win_prob_p1=round(model_p1, 4),
                model_win_prob_p2=round(model_p2, 4),
                serve_pct_p1=state.serve_stats_p1.first_serve_pct,
                serve_pct_p2=state.serve_stats_p2.first_serve_pct,
                game_log=state.game_log,
            )
        except Exception:
            log.exception("snapshot_save_failed", match_id=state.match_id)

    async def _football_poll_job(self) -> None:
        try:
            await self.football_espn.fetch()
            # Enrich with Odds API odds + upcoming matches (if key configured)
            if settings.odds_api_key:
                try:
                    await self.football_odds.fetch(settings.odds_api_key)
                except Exception:
                    log.exception("football_odds_api_failed")
            for state in await self.football_store.get_all():
                if state.is_scheduled:
                    continue  # don't run signals on upcoming matches
                try:
                    signals = self.football_engine.process(state)
                    for sig in signals:
                        msg = format_football_signal(sig)
                        await self.notifier.send_text(msg)
                except Exception:
                    log.exception("football_analysis_failed", match_id=state.match_id)
        except Exception:
            log.exception("football_poll_job_failed")

    async def _odds_job(self) -> None:
        try:
            await self.odds_api.fetch()
        except Exception:
            log.exception("odds_job_failed")

    async def _scalp_job(self) -> None:
        """Scan live tennis for sure-shot 'lock' scalps and ping Telegram (deduped)."""
        if not settings.scalp_alert_telegram:
            return
        try:
            states = await self.store.get_all()
            opps = scan_all(
                states,
                min_win_prob=settings.scalp_min_win_prob,
                lock_win_prob=settings.scalp_lock_win_prob,
                max_odds=settings.scalp_max_odds,
                lock_max_odds=settings.scalp_lock_max_odds,
            )
            now = datetime.now(timezone.utc)
            cooldown = settings.scalp_alert_cooldown_minutes * 60
            for o in opps:
                if o.tier != "lock":
                    continue
                last = self._scalp_alert_times.get(o.match_id)
                if last and (now - last).total_seconds() < cooldown:
                    continue
                self._scalp_alert_times[o.match_id] = now
                odds_txt = f"{o.market_odds:.2f}" if o.market_odds > 1.01 else "n/a"
                ev_txt = f"{o.ev_pct:+.1f}%" if o.market_odds > 1.01 else "n/a"
                reasons = ", ".join(o.reasons) if o.reasons else "decisive lead"
                window = "\n⚡ SCALP WINDOW — odds drifted up, better entry now" if o.scalp_window else ""
                await self.notifier.send_text(
                    f"🔒 SURE-SHOT SCALP\n"
                    f"Back: {o.player_name}\n"
                    f"vs {o.opponent_name}\n"
                    f"{o.tournament} ({o.surface})\n"
                    f"Score: {o.score_summary}\n"
                    f"Win prob: {o.win_prob*100:.0f}% · Odds: {odds_txt} · EV: {ev_txt}\n"
                    f"Why: {reasons}{window}"
                )
                log.info("scalp_alert_sent", match_id=o.match_id,
                         player=o.player_name, win_prob=round(o.win_prob, 3))
            # Drop stale alert-time entries for matches no longer live
            live_ids = {s.match_id for s in states}
            for mid in list(self._scalp_alert_times):
                if mid not in live_ids:
                    self._scalp_alert_times.pop(mid, None)
        except Exception:
            log.exception("scalp_job_failed")

    async def _ml_retrain_job(self) -> None:
        try:
            async with AsyncSessionFactory() as session:
                repo = Repository(session)
                await self.ml_predictor.maybe_retrain(repo)
        except Exception:
            log.exception("ml_retrain_job_failed")

    async def _schedule_job(self) -> None:
        await self.thesportsdb.fetch()

    async def _historical_import_job(self) -> None:
        # Only run match-level import on cloud — slam PBP (2.5M rows) is local-only
        # Run scripts/scrape_history.py on your laptop for slam point-by-point data
        try:
            async with AsyncSessionFactory() as session:
                await run_import(session)
        except Exception:
            log.exception("historical_import_failed_non_fatal")

    async def _sportradar_job(self) -> None:
        key = settings.sportradar_api_key
        if not key:
            return
        try:
            await self.sportradar.fetch_tennis(key)
        except Exception:
            log.exception("sportradar_tennis_job_failed")
        try:
            await self.sportradar.fetch_soccer(key)
        except Exception:
            log.exception("sportradar_soccer_job_failed")

    async def _sportsdata_job(self) -> None:
        if not settings.sportsdata_api_key:
            return
        if not self.collector_enabled.get("sportsdata", True):
            return
        try:
            await self.sportsdata.fetch()
        except Exception:
            log.exception("sportsdata_job_failed")

    async def _api_tennis_job(self) -> None:
        if not settings.api_tennis_key:
            return
        if not self.collector_enabled.get("api_tennis", True):
            return
        try:
            await self.api_tennis.fetch()
        except Exception:
            log.exception("api_tennis_job_failed")

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
        if not settings.paper_trading_enabled:
            return
        try:
            async with AsyncSessionFactory() as session:
                repo = Repository(session)
                pcfg = await repo.get_paper_config()
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
                        delta, ai_summary = await self.groq_sentinel.review_signal_candidate(sig, state)
                        if ai_summary:
                            sig.ai_review = ai_summary
                            sig.confidence = max(0.50, min(0.95, round(sig.confidence + delta, 4)))

                    msg = format_crypto_signal(sig)
                    if settings.crypto_alert_telegram:
                        await self.notifier.send_text(msg, parse_mode=ParseMode.HTML)

                    if settings.paper_trading_enabled:
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

        # ── Sports (tennis + football) — all gated behind one master switch ──
        # With SPORTS_ENABLED=false none of these are scheduled, so they spend
        # no API quota and no CPU. Crypto below is unaffected either way.
        if settings.sports_enabled:
            self._setup_sports_jobs()
        else:
            log.info("sports_jobs_disabled", hint="set SPORTS_ENABLED=true to re-enable")

        # Paper trading simulator — off unless PAPER_TRADING_ENABLED is set.
        if settings.paper_trading_enabled:
            self.scheduler.add_job(
                self._paper_trading_job,
                "interval",
                seconds=settings.paper_tick_interval_seconds,
                id="paper_trading_tick",
                max_instances=1,
                next_run_time=datetime.now(timezone.utc) + timedelta(seconds=45),
            )
            log.info("paper_trading_enabled",
                     wallet=settings.paper_starting_wallet,
                     leverage=settings.paper_leverage,
                     tick_seconds=settings.paper_tick_interval_seconds)
        else:
            log.info("paper_trading_disabled",
                     hint="set PAPER_TRADING_ENABLED=true to run a cycle")

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

    def _setup_sports_jobs(self) -> None:
        """All tennis + football jobs. Only called when settings.sports_enabled."""
        self.scheduler.add_job(
            self._data_poll_job,
            "interval",
            seconds=settings.sofascore_poll_interval,
            id="data_poll",
            max_instances=1,
            next_run_time=datetime.now(timezone.utc),  # run immediately on startup
        )
        self.scheduler.add_job(
            self._schedule_job,
            "interval",
            seconds=settings.schedule_poll_interval,
            id="schedule_poll",
            max_instances=1,
        )
        self.scheduler.add_job(
            self._odds_job,
            "interval",
            seconds=settings.odds_poll_interval_seconds,
            id="odds_poll",
            max_instances=1,
            next_run_time=datetime.now(timezone.utc),
        )
        self.scheduler.add_job(
            self._ml_retrain_job,
            "interval",
            hours=6,
            id="ml_retrain",
            max_instances=1,
        )
        if settings.sportradar_api_key:
            self.scheduler.add_job(
                self._sportradar_job,
                "interval",
                seconds=settings.sportradar_poll_interval_seconds,
                id="sportradar",
                max_instances=1,
                next_run_time=datetime.now(timezone.utc),
            )
        if settings.sportsdata_api_key:
            self.scheduler.add_job(
                self._sportsdata_job,
                "interval",
                seconds=settings.sportsdata_poll_interval_seconds,
                id="sportsdata",
                max_instances=1,
                next_run_time=datetime.now(timezone.utc),
            )
        if settings.api_tennis_key:
            self.scheduler.add_job(
                self._api_tennis_job,
                "interval",
                seconds=settings.api_tennis_poll_interval_seconds,
                id="api_tennis",
                max_instances=1,
                next_run_time=datetime.now(timezone.utc),
            )
        self.scheduler.add_job(
            self._scalp_job,
            "interval",
            seconds=60,
            id="scalp_alerts",
            max_instances=1,
            next_run_time=datetime.now(timezone.utc),
        )
        self.scheduler.add_job(
            self._football_poll_job,
            "interval",
            seconds=60,
            id="football_poll",
            max_instances=1,
            next_run_time=datetime.now(timezone.utc),
        )
        # Historical import — runs immediately on startup, then weekly
        self.scheduler.add_job(
            self._historical_import_job,
            "interval",
            weeks=1,
            id="historical_import",
            max_instances=1,
            next_run_time=datetime.now(timezone.utc),
        )
        log.info("sports_jobs_scheduled")

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
        if settings.sports_enabled:
            bets_api_status = "BetsAPI: active" if settings.bets_api_token else "BetsAPI: no token"
            sports_line = (
                f"Tennis: ESPN + Flashscore + {bets_api_status} (every {settings.sofascore_poll_interval}s)\n"
                f"Football: ESPN all leagues (every 60s)\n"
            )
        else:
            sports_line = "Sports (tennis + football): paused — no polling, no quota used\n"
        await self.notifier.send_text(
            "🪙 Crypto monitor started.\n"
            f"Crypto: CoinDCX + CoinGecko {crypto_count}-symbol watchlist "
            f"(poll every {settings.coindcx_poll_interval_seconds}s/{settings.coingecko_poll_interval_seconds}s)\n"
            f"Commodities: Twelve Data Gold/Silver/Oil ({'active' if settings.twelvedata_api_key else 'no key'})\n"
            f"News Sentiment: CryptoPanic ({'active' if settings.cryptopanic_auth_token else 'no token'})\n"
            f"{sports_line}"
            f"Min confidence: {settings.crypto_min_confidence} (Crypto)"
        )
        log.info("scheduler_started", crypto_symbols_count=crypto_count,
                 sports_enabled=settings.sports_enabled)

    def get_status(self) -> dict:
        return {
            "flashscore": {
                "http_ok": self.flashscore._consecutive_failures == 0,
                "consecutive_failures": self.flashscore._consecutive_failures,
                "consecutive_zeros": self.flashscore._consecutive_zero_matches,
            },
            "espn": {"ok": True},
            "sofascore": {
                "blocked": self.sofascore._consecutive_failures >= 5,
                "consecutive_failures": self.sofascore._consecutive_failures,
            },
            "odds_api": {
                "key_set": bool(settings.odds_api_key),
                "poll_interval_secs": settings.odds_poll_interval_seconds,
                "quota_remaining": self.odds_api.quota_remaining,
                "quota_used": self.odds_api.quota_used,
                "last_events_fetched": self.odds_api.last_events_fetched,
            },
            "bets_api": {
                "token_set": bool(settings.bets_api_token),
                "consecutive_failures": self.bets_api._consecutive_failures,
            },
            "sportradar": {
                "key_set": bool(settings.sportradar_api_key),
                "consecutive_failures": self.sportradar._consecutive_failures,
                "poll_interval_secs": settings.sportradar_poll_interval_seconds,
            },
            "sportsdata": {
                "key_set": bool(settings.sportsdata_api_key),
                "consecutive_failures": self.sportsdata._consecutive_failures,
                "poll_interval_secs": settings.sportsdata_poll_interval_seconds,
                "quota_remaining": self.sportsdata.quota_remaining,
                "quota_total": self.sportsdata.quota_total,
                "last_live": self.sportsdata.last_live_count,
                "last_scheduled": self.sportsdata.last_scheduled_count,
            },
            "api_tennis": {
                "key_set": bool(settings.api_tennis_key),
                "consecutive_failures": self.api_tennis._consecutive_failures,
                "poll_interval_secs": settings.api_tennis_poll_interval_seconds,
                "last_live": self.api_tennis.last_live_count,
                "last_scheduled": self.api_tennis.last_scheduled_count,
            },
            "football": {
                "live_matches": 0,  # filled by health.py via football_store.count()
                "signals_today": len(self.football_engine.get_recent_signals(24)),
                "odds_api_football": bool(settings.odds_api_key),
            },
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
        await self.sofascore.close()
        await self.notifier.send_text("Tennis + Football + Crypto monitor stopped.")
        log.info("scheduler_stopped")
