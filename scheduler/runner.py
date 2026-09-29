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
import math
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram.constants import ParseMode

from analysis.crypto_engine import CryptoEngine
from analysis.crypto_signal import CryptoSignal, make_mirror_signal
from analysis.crypto_state_store import CommodityStateStore, CryptoStateStore
from analysis.mirror_review import (
    TrackedCandidate,
    TrackedPair,
    breach_reason,
    elapsed_pct,
    local_confidence_estimate,
)
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
from collectors.llm_client import should_call_again
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


def _trade_snapshot(pos) -> dict:
    return {"stop": pos.stop_price, "target": pos.target_price, "trail": pos.trail_active,
            "trail_r": pos.trail_r_override, "lock": pos.locked_roe}


def _event(pos, cycle_id: int, now: datetime, kind: str, field: str = "", old: str = "",
           new: str = "", note: str = "") -> dict:
    return {"cycle_id": cycle_id, "symbol": pos.symbol.lower(),
            "opened_at": pos.opened_at.replace(tzinfo=None) if pos.opened_at.tzinfo
            else pos.opened_at,
            "at": now.replace(tzinfo=None) if now.tzinfo else now,
            "kind": kind, "field": field, "old": old, "new": new, "note": note}


def _trade_changes(before: dict, pos, cycle_id: int, now: datetime, price: float) -> list[dict]:
    """
    What moved on this tick, as log rows. A trailing stop moves a little on
    most ticks, so a stop move is logged only when it is at least 0.02% of
    price (or it is the trail's first move): the log should read like a
    trader's notes, not a tick stream.
    """
    out = []
    ref = price or pos.entry_price
    if before["trail"] != pos.trail_active and pos.trail_active:
        out.append(_event(pos, cycle_id, now, "trail", "trail", "off", "on",
                          f"trail armed at price {price:.6g}"))
    if before["stop"] != pos.stop_price and (
            abs(pos.stop_price - before["stop"]) / ref >= 0.0002
            or (pos.trail_active and not before["trail"])):
        side = 1 if str(getattr(pos.side, "value", pos.side)) == "long" else -1
        locked = side * (pos.stop_price - pos.entry_price) / pos.entry_price * 100
        was = side * (before["stop"] - pos.entry_price) / pos.entry_price * 100
        kind = "lock" if was <= 0 < locked else "stop"
        note = (f"profit locked: stop {locked:+.2f}% beyond entry at price {price:.6g}"
                if kind == "lock" else f"price {price:.6g} · stop now {locked:+.2f}% from entry")
        out.append(_event(pos, cycle_id, now, kind, "stop", f"{before['stop']:.6g}",
                          f"{pos.stop_price:.6g}", note))
    if before["target"] != pos.target_price:
        released = not pos.target_price or not math.isfinite(pos.target_price)
        out.append(_event(pos, cycle_id, now, "target", "target", f"{before['target']:.6g}",
                          "released" if released else f"{pos.target_price:.6g}",
                          "target released: the trail decides the exit" if released else ""))
    if before["trail_r"] != pos.trail_r_override and pos.trail_r_override is not None:
        old = f"{before['trail_r']:.2f}R" if before["trail_r"] is not None else "default"
        out.append(_event(pos, cycle_id, now, "trail", "trail distance", old,
                          f"{pos.trail_r_override:.2f}R", "set by the AI confidence"))
    return out


def _code_version() -> tuple[str, str]:
    """(short commit, subject) of the running code, or ('unknown', '')."""
    import html
    import subprocess
    try:
        out = subprocess.run(
            ["git", "-C", str(Path(__file__).resolve().parent.parent), "log", "-1",
             "--format=%h%x09%s"], capture_output=True, text=True, timeout=5).stdout.strip()
        sha, _, subject = out.partition("\t")
        return (sha or "unknown"), html.escape(subject[:120])
    except Exception:
        return "unknown", ""


def _version_changed(path: Path, sha: str) -> bool:
    """True when this start runs different code from the last one (a deploy)."""
    try:
        previous = path.read_text().strip() if path.exists() else ""
    except OSError:
        previous = ""
    if sha == "unknown" or sha == previous:
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(sha)
    except OSError:
        pass
    return True


def _record_start(path: Path, window_minutes: int = 60) -> list[str]:
    """Append this start to the file; return the starts within the window, this one included."""
    import json
    now = datetime.now(UTC)
    try:
        starts = json.loads(path.read_text()) if path.exists() else []
    except (OSError, ValueError):
        starts = []
    recent = [t for t in starts
              if (now - datetime.fromisoformat(t)).total_seconds() < window_minutes * 60]
    recent.append(now.isoformat())
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(recent))
    except OSError:
        pass
    return recent


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
        # Why the last event-monitor call fell through its chain, for /moves.
        self._monitor_failures: list[str] = []
        self._v2_bt_state: dict = {}
        # symbol -> time of an unconfirmed model "close" vote.
        self._close_votes: dict[str, datetime] = {}
        self._tick_notes: list[str] = []
        # Groq's bull/bear read per news or calendar event (event_bias.py).
        self._event_biases: dict[str, dict] = {}
        self._event_bias_pending: set[str] = set()
        self.oi_collector = BinanceFuturesOICollector(self.crypto_store)
        self._pending_paper_signals: list[tuple[CryptoSignal, object]] = []
        # Mirror review (27 Sep): primary+mirror candidate pairs held between
        # AI reviews while settings.mirror_review_enabled is on, keyed by
        # the primary candidate's round-0 crypto_signal_log id. Empty and
        # untouched when the feature is off.
        self._tracked_signal_pairs: dict[int, TrackedPair] = {}
        self.fear_greed = None
        # High-impact headlines seen by the news job, as blackout events.
        self._news_events: tuple = ()
        # Latest web-searched world briefing (a MarketBriefing row).
        self.latest_briefing = None
        # Event monitor bookkeeping: calls today (for the free-tier cap) and
        # when it last ran (so a fast-move trigger cannot fire it twice).
        self._monitor_day = None
        self._monitor_calls = 0
        self._monitor_last_run = None
        self.multi_horizon = MultiHorizonPredictor()
        self._ws_tasks: list[asyncio.Task] = []
        # Binance-only leaves klines as the single source of candles, depth
        # and price, so the three of them cannot disagree with each other.
        self.collector_enabled: dict[str, bool] = {}
        self._binance_ws_task: asyncio.Task | None = None
        self.sync_collectors()


    def sync_collectors(self) -> None:
        """Collector switches from settings. Binance-only turns the others off."""
        only = settings.binance_only_mode
        self.collector_enabled.update({
            "coindcx": settings.coindcx_enabled and not only,
            "coingecko": settings.coingecko_enabled and not only,
            "binance_ws": settings.binance_ws_enabled,
            "twelvedata_ws": settings.twelvedata_enabled and not only,
        })

    def _sync_binance_ws(self) -> None:
        """Start or stop the Binance stream now, not at the next restart."""
        running = self._binance_ws_task is not None and not self._binance_ws_task.done()
        if self.collector_enabled.get("binance_ws") and not running:
            self._binance_ws_task = asyncio.create_task(self.binance_ws.run_forever(),
                                                        name="binance_ws")
            self._ws_tasks.append(self._binance_ws_task)
            log.info("binance_ws_task_spawned")
        elif not self.collector_enabled.get("binance_ws") and running:
            self._binance_ws_task.cancel()
            log.info("binance_ws_task_stopped")

    async def load_settings_overrides(self) -> list[str]:
        """Apply the settings saved on /settings (database beats .env)."""
        from config.overrides import apply
        try:
            async with AsyncSessionFactory() as session:
                stored = await Repository(session).get_app_settings()
        except Exception:
            log.exception("settings_overrides_load_failed")
            return []
        applied = apply(settings, stored)
        self.sync_collectors()
        log.info("settings_overrides_applied", keys=applied)
        return applied

    def on_settings_changed(self, keys: list[str]) -> None:
        """React to a save on /settings without a restart where possible."""
        self.sync_collectors()
        self._sync_binance_ws()
        log.info("settings_changed", keys=keys)

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
            sizing_floor_pct=pcfg.sizing_floor_pct,
            sizing_ceiling_pct=pcfg.sizing_ceiling_pct,
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
        pos.trail_r_override = getattr(row, "trail_r_override", None)
        pos.locked_roe = getattr(row, "locked_roe", None)
        pos.stop_moved_by_profit_lock = getattr(row, "stop_moved_by_profit_lock", False)
        pos.trade_mode = getattr(row, "trade_mode", "intraday")
        pos.tp1_price = getattr(row, "tp1_price", 0.0)
        pos.tp2_price = getattr(row, "tp2_price", 0.0)
        pos.partial_closed = getattr(row, "partial_closed", False)
        pos.partial_pnl = getattr(row, "partial_pnl", 0.0)
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

                # live=pcfg: every risk/behaviour knob (confidence floor,
                # leverage, stop size, sizing, trailing, ladder...) comes
                # from the current Settings-page config, not the 16 Sep
                # snapshot the cycle started under — see config_for_cycle's
                # docstring. Only starting_wallet/target_wallet stay the
                # cycle's own.
                cfg = config_for_cycle(cycle, live=pcfg)
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
                    current_price = st.current_price if (st and st.current_price > 0) else None
                    if current_price is None and hasattr(self, "commodity_store"):
                        # Fallback for commodities (e.g. xau/usd, gold)
                        for cs in await self.commodity_store.get_all():
                            if (cs.symbol.lower() == row.symbol.lower()
                                    or cs.symbol.replace("/", "").lower() == row.symbol.lower()
                                    or ("xau" in row.symbol.lower() and "xau" in cs.symbol.lower())):
                                if cs.current_price > 0:
                                    current_price = cs.current_price
                                    break

                    # If the position has timed out, force-close it even if price feed is missing
                    is_pos_expired = (
                        pos.is_expired(now)
                        if hasattr(pos, "is_expired")
                        else (
                            pos.expires_at is not None
                            and (
                                (now.replace(tzinfo=UTC) if now.tzinfo is None else now)
                                >= (pos.expires_at.replace(tzinfo=UTC) if pos.expires_at.tzinfo is None else pos.expires_at)
                            )
                        )
                    )
                    if is_pos_expired:
                        mark_price = current_price if (current_price and current_price > 0) else row.entry_price
                        trade = resolve_at_price(pos, mark_price, now, cfg, wallet,
                                                 lock=self._profit_lock())
                        if trade is None:
                            from analysis.paper_cycle import fees_for
                            from analysis.paper_trading import ExitReason, close_position
                            trade = close_position(pos, mark_price, ExitReason.EXPIRY, now, fees_for(pos.symbol), wallet)
                        wallet = trade.wallet_after
                        await repo.close_position_atomic(cycle.id, row.id, trade, wallet)
                        events = [_event(
                            pos, cycle.id, now, "closed", "exit", new=f"{trade.exit_price:.6g}",
                            note=(f"{trade.reason.value} · net ₹{trade.net_pnl:+.2f} "
                                  f"({trade.return_on_margin * 100:+.1f}% on margin) · "
                                  f"fees ₹{trade.fees_paid:.2f}"))]
                        await repo.add_trade_events(events)
                        log.info("paper_trade_expired_closed", symbol=pos.symbol,
                                 reason=trade.reason.value, net=round(trade.net_pnl, 2),
                                 wallet=round(wallet, 2))
                        if pcfg.alert_telegram:
                            await self.notifier.send_text(
                                format_paper_trade(trade, wallet), parse_mode=ParseMode.HTML)
                        continue

                    if current_price is None or current_price <= 0:
                        live_ids[len(live)] = row.id
                        live.append(pos)
                        continue

                    before = _trade_snapshot(pos)
                    self._tick_notes = []
                    # Dynamic Runner Extension:
                    # If trade reaches >= 1.8R, extend target dynamically into runner mode (+1.5R)
                    # and lock in at least +1.2R with trailing stop before resolve_at_price evaluates.
                    if current_price is not None and current_price > 0 and pos.extend_runner(current_price):
                        self._tick_notes.append(
                            f"Runner extension activated at {pos.r_multiple(current_price):.1f}R: "
                            f"target extended to {pos.target_price:.6g}, stop ratcheted to {pos.stop_price:.6g}"
                        )
                        log.info("paper_trade_runner_extended", symbol=pos.symbol,
                                 r=round(pos.r_multiple(current_price), 2),
                                 new_target=pos.target_price, new_stop=pos.stop_price)

                    trade = resolve_at_price(pos, current_price, now, cfg, wallet,
                                             lock=self._profit_lock())
                    if trade is None:
                        # 1. Dual Mode Trailing Ladder (TP1 partial 50% scale-out + R-ladder)
                        from analysis.paper_cycle import fees_for
                        fees = fees_for(pos.symbol)
                        trail_moved, credit = pos.apply_dual_mode_trailing(current_price, fees)
                        if credit > 0:
                            wallet += credit
                            await repo._set_wallet(cycle.id, wallet)
                            self._tick_notes.append(f"TP1 partial 50% scale-out (+₹{credit:.2f} credited)")
                            log.info("paper_trade_tp1_scale_out", symbol=pos.symbol, credit=round(credit, 2), wallet=round(wallet, 2))

                        # 2. AI Sentiment Emergency Stop Ratchet
                        if st is not None:
                            ratcheted = pos.apply_ai_sentiment_stop_ratchet(current_price, st.sentiment_score, fees)
                            if ratcheted:
                                self._tick_notes.append(f"AI Sentiment stop ratchet applied (sentiment {st.sentiment_score:+.2f})")
                                log.info("paper_trade_sentiment_ratchet", symbol=pos.symbol, sentiment=st.sentiment_score, new_stop=pos.stop_price)

                        # The position survived the tick's exits. A losing one
                        # now has to justify staying open; a winning one has
                        # its trail set by the same confidence. Nothing here
                        # can defer an exit that already fired — by the time a
                        # stop is reached the loss is no longer bounded, so
                        # this runs only on what resolve_at_price left alive.
                        if st is not None:
                            trade = await self._review_open_position(
                                pos, st, cfg, now, wallet, repo)
                    events = _trade_changes(before, pos, cycle.id, now, current_price)
                    events += [_event(pos, cycle.id, now, "ai", note=n) for n in self._tick_notes]
                    if trade is None:
                        await repo.sync_position(row.id, pos)
                        await repo.add_trade_events(events)
                        live_ids[len(live)] = row.id
                        live.append(pos)
                        continue

                    wallet = trade.wallet_after
                    await repo.close_position_atomic(cycle.id, row.id, trade, wallet)
                    events.append(_event(
                        pos, cycle.id, now, "closed", "exit", new=f"{trade.exit_price:.6g}",
                        note=(f"{trade.reason.value} · net ₹{trade.net_pnl:+.2f} "
                              f"({trade.return_on_margin * 100:+.1f}% on margin) · "
                              f"fees ₹{trade.fees_paid:.2f}")))
                    await repo.add_trade_events(events)
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
                event_bias = None
                if blackout is not None and pending and not settings.event_bias_mode:
                    log.info("paper_trades_blackout", event_name=blackout.name,
                             kind=blackout.kind, skipped=len(pending))
                    for _sig, _st in pending:
                        await self._mark_skipped(_sig.log_id, "news_blackout")
                    pending = []
                elif blackout is not None:
                    # Trade through it with the crowd's bias (owner, 26 Sep):
                    # only signals against a confident bias are skipped.
                    event_bias = self._event_bias_for(blackout, states)

                from analysis.paper_cycle import reprice_signal
                from analysis.protections import RecentTrade, check_entry
                from analysis.scalp_levels import ScalpConfig
                max_age = ScalpConfig().max_signal_age_seconds
                protect = self._protection_config()
                recent: list = []
                day_start_wallet = 0.0
                if pending and protect.enabled:
                    rows_t = await repo.get_cycle_trades(cycle.id, 50)
                    recent = [RecentTrade(t.symbol, t.closed_at, t.net_pnl)
                              for t in rows_t if t.closed_at is not None]
                    today0 = now.replace(tzinfo=None, hour=0, minute=0, second=0,
                                         microsecond=0)
                    pnl_today = sum(t.net_pnl for t in recent if t.closed_at >= today0)
                    equity = wallet + sum(p.margin for p in cstate.positions)
                    day_start_wallet = equity - pnl_today
                # Best deal first: on a tick with several signals, the one
                # most worth its costs gets the slot (owner's rule, 26 Sep).
                from analysis.deal_scanner import (
                    book_full,
                    deal_score,
                    target_roe_pct,
                    volatility_classes,
                )
                from analysis.paper_cycle import fees_for as _fees_for
                pending.sort(key=lambda p: -deal_score(p[0], _fees_for(p[0].symbol)
                                                        .round_trip_pct()))
                vol_class = volatility_classes({
                    sym: (s_.atr_14 / s_.current_price * 100 if s_.current_price > 0 else 0)
                    for sym, s_ in states.items()})
                for sig, st in pending:
                    log_id = sig.log_id   # captured before reprice_signal can null sig out
                    if st.current_price <= 0:
                        await self._mark_skipped(log_id, "no_live_price")
                        continue
                    if event_bias is not None:
                        from analysis.event_bias import allows
                        if not allows(sig.direction, event_bias):
                            log.info("paper_trade_skipped", symbol=sig.symbol,
                                     reason="against_news_bias", bias=event_bias["bias"],
                                     event_name=blackout.name)
                            await self._mark_skipped(log_id, "against_news_bias")
                            continue
                    full = (book_full(cstate.positions, settings.premium_roe_pct)
                            if settings.premium_fills_book else None)
                    if full is not None:
                        log.info("paper_trade_skipped", symbol=sig.symbol,
                                 reason="book_full_premium", holding=full.symbol)
                        await self._mark_skipped(log_id, "book_full_premium")
                        continue
                    max_intraday = getattr(settings, "max_concurrent_intraday", 3)
                    active_intraday = sum(1 for p in cstate.positions if getattr(p, "trade_mode", "intraday") == "intraday")
                    if getattr(sig, "trade_mode", "intraday") == "intraday" and active_intraday >= max_intraday:
                        log.info("paper_trade_skipped", symbol=sig.symbol, reason="max_concurrent_intraday_reached",
                                 active=active_intraday, limit=max_intraday)
                        await self._mark_skipped(log_id, f"max_intraday_trades_{active_intraday}/{max_intraday}")
                        continue

                    sig, why_not = reprice_signal(sig, st.current_price, now, max_age)
                    if sig is None:
                        log.info("paper_trade_skipped", symbol=st.symbol, reason=why_not)
                        await self._mark_skipped(log_id, why_not)
                        continue
                    sig = self._apply_sentiment(sig, st.funding_rate_per_8h)
                    ok, _why = should_open(sig, cfg, cstate, now)
                    if ok:
                        from analysis.instruments import spec_for
                        ok, _why = check_entry(
                            now.replace(tzinfo=None), sig.symbol, sig.direction, recent,
                            day_start_wallet, cstate.positions, protect,
                            is_crypto=spec_for(sig.symbol).kind == "crypto",
                            sentiment_score=st.sentiment_score)
                    if not ok:
                        log.info("paper_trade_skipped", symbol=sig.symbol, reason=_why)
                        await self._mark_skipped(log_id, _why)
                        continue
                    atr_pct = (st.atr_14 / st.current_price
                               if st.current_price > 0 and st.atr_14 > 0 else None)
                    # The event-precedent brief's extended hold: only for a
                    # trade the engine already flagged, and only while the
                    # setting that turns extension on is still on (it may
                    # have been flipped off between detection and open).
                    extended_hold = (
                        settings.event_precedent_extended_hold_minutes
                        if (settings.event_precedent_extended_hold_enabled
                            and getattr(sig, "precedent_extended_hold", False))
                        else None)
                    pos = open_from_signal(sig, cfg, cstate, now, pcfg.usdt_inr, atr_pct,
                                           protect=protect, extended_hold_minutes=extended_hold)
                    if pos is None:
                        # open_from_signal's own gates (stop_inside_fees,
                        # liquidation_too_near, below_one_lot, target_not_
                        # viable) already log a reason; margin<=0 is the one
                        # silent case, and the only one left once this is
                        # reached — see analysis/paper_cycle.open_from_signal.
                        await self._mark_skipped(log_id, "no_free_margin")
                        continue
                    cstate.wallet -= pos.margin
                    wallet = cstate.wallet
                    row = await repo.open_position_atomic(cycle.id, pos, wallet)
                    risk = abs(pos.entry_price - pos.stop_price) / pos.entry_price * 100
                    roe = target_roe_pct(pos)
                    premium = (settings.premium_fills_book
                               and roe >= settings.premium_roe_pct)
                    await repo.add_trade_events([_event(
                        pos, cycle.id, now, "opened", "entry", new=f"{pos.entry_price:.6g}",
                        note=(f"{pos.side.value} {sig.signal_type} at {sig.confidence:.0%} · "
                              f"stop {pos.stop_price:.6g} ({risk:.2f}% away) · target "
                              f"{pos.target_price:.6g} · {pos.leverage:.0f}x · margin "
                              f"₹{pos.margin:.0f} · quoted {sig.current_price:.6g} · "
                              f"{vol_class.get(pos.symbol, 'unknown')} coin · target pays "
                              f"{roe:.0f}% on margin"
                              + (" · PREMIUM: book full until this closes" if premium
                                 else "")
                              + (f" · news: {blackout.name[:60]} → {event_bias['bias']} "
                                 f"({event_bias['confidence']:.2f}, {event_bias['source']})"
                                 if event_bias else "")
                              + (f" · extended hold: {extended_hold:.0f}m (event precedent)"
                                 if extended_hold else "")))])
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
                            f"Entry <b>${pos.entry_price:,.4f}</b> · "
                            f"Margin <b>₹{pos.margin:,.0f}</b> ({pos.leverage:.0f}x)\n"
                            f"Target <b>${pos.target_price:,.4f}</b> · "
                            f"Stop <b>${pos.stop_price:,.4f}</b>",
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
            # A just-opened or just-closed trade should show up the moment
            # this tick commits, not after /api/paper's or /api/pipeline's
            # cache expires.
            from scheduler import cache
            cache.invalidate("paper_db_snapshot", "pipeline_db_read")
        except Exception:
            log.exception("paper_trading_job_failed")

    async def _log_signal(self, sig, suppressed_by: str = "", **extra) -> int:
        """
        Record a signal, whether or not it was published.

        A suppressed row carries the name of the filter that stopped it and
        is otherwise identical, so the outcome resolver scores it the same
        way. That is what makes "what did this filter cost me" answerable
        instead of a matter of opinion.

        `**extra` passes through mirror-review lineage columns
        (mirror_of_log_id, review_round, parent_signal_id) — empty/zero for
        every call site that predates that feature, so this is a no-op
        unless a caller opts in.
        """
        trade_mode = extra.pop("trade_mode", getattr(sig, "trade_mode", "intraday"))
        veto_reason = extra.pop("veto_reason", getattr(sig, "veto_reason", ""))
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
                    candidate_role=getattr(sig, "candidate_role", "primary"),
                    trade_mode=trade_mode,
                    veto_reason=veto_reason,
                    **extra,
                )
        except Exception:
            log.exception("crypto_signal_db_log_failed", symbol=sig.symbol)
            return 0

    async def _mark_skipped(self, log_id: int, reason: str) -> None:
        """Never let a failure here interrupt the tick that called it — an
        unexplained Signals card is a worse night than a missing one."""
        try:
            async with AsyncSessionFactory() as session:
                await Repository(session).mark_signal_skipped(log_id, reason)
        except Exception:
            log.debug("signal_skip_reason_not_saved", log_id=log_id, reason=reason)

    async def _mark_rejected(self, log_id: int, reason: str) -> None:
        """A mirror-review candidate stopped tracking without a trade."""
        try:
            async with AsyncSessionFactory() as session:
                await Repository(session).mark_signal_rejected(log_id, reason)
        except Exception:
            log.debug("signal_rejection_reason_not_saved", log_id=log_id, reason=reason)

    async def _mark_traded(self, log_id: int) -> None:
        """A mirror-review candidate won and was queued as a paper trade."""
        try:
            async with AsyncSessionFactory() as session:
                await Repository(session).mark_signal_traded_with_review(log_id)
        except Exception:
            log.debug("signal_traded_mark_not_saved", log_id=log_id)

    async def _ai_review_candidate(self, sig, state, states, scfg) -> tuple[str, str]:
        """
        Run one Groq AI review round on `sig` in place (mutates sig.confidence
        and sig.ai_review) and returns (verdict, ai_summary).

        The same call the original single-candidate path made, pulled out so
        mirror review can run it independently for a primary, a mirror, and
        any later re-review round, without duplicating the Groq/save_review
        plumbing three times over.
        """
        if not (self.groq_sentinel.is_available and settings.groq_signal_review_enabled):
            return "", ""
        news, briefing_id = await self._news_context(sig.symbol)
        delta, ai_summary, verdict = await self.groq_sentinel.review_signal_candidate(
            sig, state, model=scfg.groq_model, book=states, news=news)
        if verdict == "REJECT":
            delta = -abs(settings.groq_reject_penalty)
        elif verdict == "CAUTION":
            # Guaranteed minimum cost: takes whichever is more negative, the
            # model's own delta or this floor, so a CAUTION that came back
            # with delta 0.0 still costs something.
            delta = min(delta, -settings.groq_caution_min_penalty)
        if ai_summary:
            sig.ai_review = ai_summary
        try:
            async with AsyncSessionFactory() as s2:
                await Repository(s2).save_review(
                    "pre", sig.symbol, signal_type=sig.signal_type,
                    verdict=verdict, summary=ai_summary,
                    factors=self.groq_sentinel.last_factors,
                    confidence_delta=delta,
                    model=self.groq_sentinel.last_model or "no_answer",
                    latency_ms=self.groq_sentinel.last_latency_ms,
                    briefing_id=briefing_id, news_context=news,
                    **self._review_extras(state))
        except Exception:
            log.debug("mirror_review_not_saved", symbol=sig.symbol,
                      role=getattr(sig, "candidate_role", "primary"))
        before = sig.confidence
        sig.confidence = max(0.50, min(0.95, round(before + delta, 4)))
        return verdict, ai_summary

    async def _ai_review_pair(
        self, sig, mirror, state, states, scfg,
    ) -> tuple[tuple[str, str], tuple[str, str]]:
        """
        Round-0 counterpart to _ai_review_candidate: one combined Groq call
        (GroqSentinel.review_signal_pair) judges the primary and the mirror
        together instead of two separate calls back to back — same
        information the two separate calls used to produce, half the AI
        calls. Runs the same per-candidate penalty / save_review /
        confidence-update logic _ai_review_candidate runs, just fed from
        one shared reply instead of making its own call per candidate.

        Only round 0 uses this: the live re-review loop (_mirror_review_job)
        does not always have both sides still tracking to pair up, so it
        keeps calling _ai_review_candidate one at a time.
        """
        if not (self.groq_sentinel.is_available and settings.groq_signal_review_enabled):
            return ("", ""), ("", "")
        news, briefing_id = await self._news_context(sig.symbol)
        (delta_p, summary_p, verdict_p), (delta_m, summary_m, verdict_m) = (
            await self.groq_sentinel.review_signal_pair(
                sig, mirror, state, model=scfg.groq_model, book=states, news=news))
        factors_p, factors_m = self.groq_sentinel.last_factors_pair

        for cand_sig, verdict, delta, summary, factors in (
            (sig, verdict_p, delta_p, summary_p, factors_p),
            (mirror, verdict_m, delta_m, summary_m, factors_m),
        ):
            if verdict == "REJECT":
                delta = -abs(settings.groq_reject_penalty)
            elif verdict == "CAUTION":
                delta = min(delta, -settings.groq_caution_min_penalty)
            if summary:
                cand_sig.ai_review = summary
            try:
                async with AsyncSessionFactory() as s2:
                    await Repository(s2).save_review(
                        "pre", cand_sig.symbol, signal_type=cand_sig.signal_type,
                        verdict=verdict, summary=summary, factors=factors,
                        confidence_delta=delta,
                        model=self.groq_sentinel.last_model or "no_answer",
                        latency_ms=self.groq_sentinel.last_latency_ms,
                        briefing_id=briefing_id, news_context=news,
                        **self._review_extras(state))
            except Exception:
                log.debug("mirror_review_not_saved", symbol=cand_sig.symbol,
                          role=getattr(cand_sig, "candidate_role", "primary"))
            before = cand_sig.confidence
            cand_sig.confidence = max(0.50, min(0.95, round(before + delta, 4)))

        return (verdict_p, summary_p), (verdict_m, summary_m)

    async def _settle_mirror_round(self, pair: TrackedPair, round_winners: list[TrackedCandidate],
                                    pcfg, states: dict) -> None:
        """
        One or two candidates just cleared pcfg.min_confidence on the same
        tick. Higher confidence wins outright; the loser (and anything else
        still tracking in the pair) is marked rejected — only one direction
        per symbol ever opens.
        """
        winner = max(round_winners, key=lambda c: c.signal.confidence)
        winner.state = "traded"
        state = states.get(winner.signal.symbol)
        if not settings.mirror_review_can_trade:
            # Won its round, but the trading switch is off: log it exactly
            # like a normal "why no trade" skip, so the review trail reads
            # "Not traded — mirror trading switch is off" instead of
            # "Traded" — the whole point of the second switch is that this
            # branch is indistinguishable from a real trade in every way
            # except that nothing opens.
            await self._mark_skipped(winner.log_id, "mirror_trading_disabled")
        elif pcfg.enabled:
            self._pending_paper_signals.append((winner.signal, state))
            await self._mark_traded(winner.log_id)
        else:
            await self._mark_skipped(winner.log_id, "paper_trading_off")
        log.info("mirror_candidate_won", symbol=winner.signal.symbol,
                 role=winner.signal.candidate_role, direction=winner.signal.direction,
                 confidence=winner.signal.confidence, round=winner.review_round,
                 would_trade_only=not settings.mirror_review_can_trade)

        for cand in pair.candidates():
            if cand is winner or cand.state != "tracking":
                continue
            cand.state = "rejected"
            cand.rejection_reason = "opposite_side_won"
            await self._mark_rejected(cand.log_id, "opposite_side_won")

    async def _handle_mirror_candidates(self, sig, state, states, scfg, pcfg) -> None:
        """
        Round-0 of the mirror-review feature (settings.mirror_review_enabled):
        build the opposite-direction candidate, review both together in one
        combined Groq call (_ai_review_pair — independent verdicts on each,
        half the calls two separate reviews used to cost), and either open
        the stronger one immediately (today's behaviour, preserved for
        signals already confident enough) or hold both for the live
        re-review loop (_mirror_review_job).
        """
        mirror = make_mirror_signal(sig)
        now = datetime.now(UTC)

        (verdict_p, summary_p), (verdict_m, summary_m) = (
            await self._ai_review_pair(sig, mirror, state, states, scfg))

        primary_log_id = await self._log_signal(sig)
        sig.log_id = primary_log_id
        mirror_log_id = await self._log_signal(mirror, mirror_of_log_id=primary_log_id)
        mirror.log_id = mirror_log_id

        if verdict_p != "REJECT":
            msg = format_crypto_signal(sig)
            if settings.crypto_alert_telegram:
                await self.notifier.send_text(msg, parse_mode=ParseMode.HTML)

        pair = TrackedPair()
        for cand_sig, verdict, summary, log_id in (
            (sig, verdict_p, summary_p, primary_log_id),
            (mirror, verdict_m, summary_m, mirror_log_id),
        ):
            if verdict == "REJECT":
                await self._mark_rejected(log_id, summary or "ai_review")
                continue
            cand = TrackedCandidate(signal=cand_sig, log_id=log_id, root_log_id=log_id,
                                     last_ai_review_at=now,
                                     last_reviewed_confidence=cand_sig.confidence)
            if cand_sig is sig:
                pair.primary = cand
            else:
                pair.mirror = cand

        states_by_symbol = {st.symbol: st for st in states}
        round_winners = [c for c in pair.candidates()
                        if c.signal.confidence >= pcfg.min_confidence]
        if round_winners:
            await self._settle_mirror_round(pair, round_winners, pcfg, states_by_symbol)

        for cand in pair.candidates():
            if cand.state == "tracking":
                # Sentinel, not a real "why no trade" reason: distinguishes
                # a row still being tracked from one that opened (empty)
                # on the Signals page's review trail. Cleared the moment
                # this candidate wins or is rejected.
                await self._mark_skipped(cand.log_id, "mirror_review_tracking")

        if any(c.state == "tracking" for c in pair.candidates()):
            self._tracked_signal_pairs[primary_log_id] = pair

        log.info("mirror_candidates_processed", symbol=sig.symbol, type=sig.signal_type,
                 primary_confidence=sig.confidence, mirror_confidence=mirror.confidence,
                 primary_verdict=verdict_p, mirror_verdict=verdict_m)

    async def _mirror_review_job(self) -> None:
        """
        Live re-review loop (every crypto_analysis tick): for every
        candidate still "tracking", cheaply recompute a local confidence
        estimate and only spend an AI call on it once enough of its own
        timeframe has elapsed AND that local estimate has moved enough to
        be worth a second opinion — capped at mirror_review_max_rounds
        re-reviews per candidate (3 AI calls total, including round 0).
        """
        if not settings.mirror_review_enabled or not self._tracked_signal_pairs:
            return
        try:
            async with AsyncSessionFactory() as session:
                repo = Repository(session)
                scfg = await repo.get_strategy_config()
                pcfg = await repo.get_paper_config()

            all_states = await self.crypto_store.get_all()
            states_by_symbol = {st.symbol: st for st in all_states}
            now = datetime.now(UTC)

            finished_keys = []
            for pair_key, pair in list(self._tracked_signal_pairs.items()):
                round_winners: list[TrackedCandidate] = []

                for cand in pair.candidates():
                    if cand.state != "tracking":
                        continue
                    st = states_by_symbol.get(cand.signal.symbol)
                    if st is None or st.current_price <= 0:
                        continue

                    ep = elapsed_pct(cand.signal, now)

                    breach = breach_reason(cand.signal, st.current_price)
                    if breach:
                        cand.state = "rejected"
                        cand.rejection_reason = breach
                        await self._mark_rejected(cand.log_id, breach)
                        continue

                    if ep >= 1.0:
                        cand.state = "rejected"
                        cand.rejection_reason = "ai_review_timed_out"
                        await self._mark_rejected(cand.log_id, "ai_review_timed_out")
                        continue

                    local_conf = local_confidence_estimate(cand.signal, st)
                    if not should_call_again(
                            elapsed_ratio=ep,
                            min_elapsed_ratio=settings.mirror_review_min_elapsed_pct,
                            current_value=local_conf,
                            last_value=cand.last_reviewed_confidence,
                            min_delta=settings.mirror_review_confidence_delta_threshold):
                        continue
                    if cand.review_round >= settings.mirror_review_max_rounds:
                        # Budget exhausted — keep tracking silently until it
                        # either times out (ep >= 1.0, above) or the other
                        # side confirms first.
                        continue

                    cand.signal.current_price = st.current_price
                    cand.signal.confidence = local_conf
                    verdict, summary = await self._ai_review_candidate(
                        cand.signal, st, list(states_by_symbol.values()), scfg)
                    cand.review_round += 1
                    cand.last_ai_review_at = now
                    cand.last_reviewed_confidence = cand.signal.confidence

                    new_log_id = await self._log_signal(
                        cand.signal,
                        review_round=cand.review_round,
                        parent_signal_id=cand.root_log_id,
                        mirror_of_log_id=(pair_key if cand is pair.mirror else 0),
                    )
                    cand.log_id = new_log_id or cand.log_id

                    if verdict == "REJECT":
                        cand.state = "rejected"
                        cand.rejection_reason = summary or "ai_review"
                        await self._mark_rejected(cand.log_id, summary or "ai_review")
                        continue

                    if cand.signal.confidence >= pcfg.min_confidence:
                        round_winners.append(cand)
                    else:
                        await self._mark_skipped(cand.log_id, "mirror_review_tracking")

                if round_winners:
                    await self._settle_mirror_round(pair, round_winners, pcfg, states_by_symbol)

                if pair.all_settled():
                    finished_keys.append(pair_key)

            for key in finished_keys:
                self._tracked_signal_pairs.pop(key, None)
        except Exception:
            log.exception("mirror_review_job_failed")

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
                    model=review.model, latency_ms=review.latency_ms,
                    briefing_id=briefing_id, news_context=news,
                    **self._review_extras(state))

        if not losing:
            # A winner is never closed on a score — the trail does that, and
            # the score only decides how far behind price it rides.
            if cfg.trailing is not None and cfg.trailing.enabled:
                pos.trail_r_override = trail_r_for_confidence(review.confidence)
                # Apply it on this tick. The trail already ran inside
                # resolve_at_price with the old distance; without this the
                # new one would wait a tick, and before it was persisted it
                # never applied at all.
                from analysis.paper_cycle import fees_for
                pos.update_trail(state.current_price, state.current_price,
                                 cfg.trailing, fees_for(pos.symbol))
            if review.confidence >= 0.70 and pos.extend_runner(state.current_price, r_extension=2.0):
                self._tick_notes.append(
                    f"AI Runner Extension: target extended to {pos.target_price:.6g}, "
                    f"stop {pos.stop_price:.6g} (confidence {review.confidence:.2f})"
                )
            return None

        if review.hold:
            self._close_votes.pop(pos.symbol, None)
            return None

        if review.asked_model:
            self._tick_notes.append(
                f"AI review: {review.verdict} · confidence {review.confidence:.2f} "
                f"(chart read {review.trend:.2f}) · {review.summary[:140]}")

        # Unconditional minimum hold guard: NO position may be closed early for conviction loss
        # before the minimum hold time has elapsed (default 30 min, minimum 15 min).
        held = (now - pos.opened_at).total_seconds() / 60.0
        if held < max(15.0, float(settings.position_review_min_hold_minutes)):
            log.info("position_close_too_early", symbol=pos.symbol,
                     held_minutes=round(held, 1),
                     min_required=max(15.0, float(settings.position_review_min_hold_minutes)))
            return None
        if review.asked_model and settings.position_review_confirm_close:
            first = self._close_votes.get(pos.symbol)
            gap = settings.position_review_confirm_gap_minutes * 60
            window = max(settings.position_review_interval_seconds * 3, gap * 3)
            age = (now - first).total_seconds() if first is not None else None
            if age is None or age > window:
                self._close_votes[pos.symbol] = now
                self._tick_notes.append("AI close vote 1 of 2: waiting for confirmation")
                log.info("position_close_pending_confirmation", symbol=pos.symbol,
                         confidence=round(review.confidence, 3))
                return None
            if age < gap:
                return None
        self._close_votes.pop(pos.symbol, None)

        from analysis.paper_trading import ExitReason, close_position
        from analysis.paper_cycle import fees_for

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
                    sig_ts = (sig.timestamp.replace(tzinfo=None) if sig.timestamp.tzinfo
                             else sig.timestamp)
                    after = [c for c in st.candles_1m
                             if (c.timestamp.replace(tzinfo=None) if c.timestamp.tzinfo
                                 else c.timestamp) > sig_ts]
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
                                pnl = ((sig.target_price - sig.current_price)
                                      / sig.current_price * 100)
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
                        last_c_ts = (after[-1].timestamp.replace(tzinfo=None)
                                    if after[-1].timestamp.tzinfo else after[-1].timestamp)
                        span = last_c_ts - sig_ts
                        paper_max_hold_minutes = pcfg.max_hold_minutes
                        if span.total_seconds() / 60 < paper_max_hold_minutes:
                            continue
                        outcome = "expired"
                        pnl = (after[-1].close - sig.current_price) / sig.current_price * 100

                    if outcome is None:
                        # Still genuinely running: no bar hit target or stop,
                        # and the newest one hasn't reached max hold yet
                        # either. Nothing to record — leave it pending for
                        # the next pass. Falling through used to call
                        # resolve_crypto_signal(sig.id, None, ...), which
                        # the database rejects (outcome is NOT NULL) and
                        # crashed this job on the same signal every run
                        # until it aged past its own history window.
                        continue

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
        # Past a year, rows leave the database for JSON files (owner's rule:
        # at most one year in the DB, nothing deleted).
        if settings.db_retention_days > 0:
            from storage.cold_storage import offload
            try:
                async with AsyncSessionFactory() as session:
                    to_files = await offload(session, Path(settings.cold_storage_dir),
                                             days=settings.db_retention_days)
                log.info("db_cold_storage_done", moved=to_files)
            except Exception:
                log.exception("db_cold_storage_failed")

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
            self._maybe_trigger_monitor(states)
            from scheduler import pipeline
            priced = sum(1 for st in states if st.current_price > 0)
            if priced < len(states):
                pipeline.no_setup(f"{len(states) - priced} coin(s) have no price yet")
            try:
                await self._analyse_states(states, scfg, pcfg)
            finally:
                pipeline.end_scan(priced)
        except Exception:
            log.exception("crypto_analysis_job_failed")

    async def _analyse_states(self, states, scfg, pcfg) -> None:
        """Run the detectors over every priced coin and hand on what fires."""
        try:
            btc_state = next(
                (s for s in states if s.symbol.lower() in ("btcusdt", "btc") or getattr(s, "base_asset", "").upper() == "BTC"),
                None,
            )
            for state in states:
                if state.current_price <= 0:
                    continue

                signals = self.crypto_engine.process(state, btc_state=btc_state)
                for sig in signals:
                    if (settings.crypto_min_confidence > 0
                            and sig.confidence < settings.crypto_min_confidence):
                        log.info("crypto_signal_below_min_confidence", symbol=sig.symbol,
                                 type=sig.signal_type, confidence=sig.confidence,
                                 threshold=settings.crypto_min_confidence)
                        self.crypto_engine.forget(sig)
                        continue

                    if settings.mirror_review_enabled:
                        await self._handle_mirror_candidates(sig, state, states, scfg, pcfg)
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
                    if self.groq_sentinel.is_available and settings.groq_signal_review_enabled:
                        news, briefing_id = await self._news_context(sig.symbol)
                        delta, ai_summary, verdict = (
                            await self.groq_sentinel.review_signal_candidate(
                                sig, state, model=scfg.groq_model, book=states, news=news)
                        )
                        if verdict in ("REJECT", "HARD_VETO"):
                            delta = -abs(settings.groq_reject_penalty)
                        elif verdict == "CAUTION":
                            delta = min(delta, -settings.groq_caution_min_penalty)
                        if ai_summary:
                            sig.ai_review = ai_summary
                        try:
                            async with AsyncSessionFactory() as s2:
                                await Repository(s2).save_review(
                                    "pre", sig.symbol, signal_type=sig.signal_type,
                                    verdict=verdict, summary=ai_summary,
                                    factors=self.groq_sentinel.last_factors,
                                    confidence_delta=delta,
                                    model=self.groq_sentinel.last_model or "no_answer",
                                    latency_ms=self.groq_sentinel.last_latency_ms,
                                    briefing_id=briefing_id, news_context=news,
                                    **self._review_extras(state))
                        except Exception:
                            log.debug("pre_review_not_saved", symbol=sig.symbol)

                        # Hard Veto: AI detected funding squeeze, news trap, or structural conflict
                        if verdict in ("REJECT", "HARD_VETO"):
                            log.info("crypto_signal_hard_vetoed_by_ai",
                                     symbol=sig.symbol, type=sig.signal_type,
                                     verdict=verdict, reason=ai_summary)
                            sig.veto_reason = f"AI Hard Veto: {ai_summary}"
                            await self._log_signal(sig, suppressed_by="ai_hard_veto", veto_reason=sig.veto_reason)
                            self.crypto_engine.forget(sig)
                            continue

                        before = sig.confidence
                        sig.confidence = max(0.50, min(0.95, round(before + delta, 4)))
                        if (settings.crypto_min_confidence > 0
                                and sig.confidence < settings.crypto_min_confidence):
                            log.info("crypto_signal_dropped_after_ai_review",
                                     symbol=sig.symbol, type=sig.signal_type,
                                     verdict=verdict, confidence_before=before,
                                     confidence_after=sig.confidence,
                                     threshold=settings.crypto_min_confidence,
                                     reason=ai_summary)
                            sig.veto_reason = f"AI Caution: {ai_summary}"
                            await self._log_signal(sig, suppressed_by="ai_review", veto_reason=sig.veto_reason)
                            self.crypto_engine.forget(sig)
                            continue

                    msg = format_crypto_signal(sig)
                    if settings.crypto_alert_telegram:
                        await self.notifier.send_text(msg, parse_mode=ParseMode.HTML)

                    # Set before queuing: _paper_trading_job writes its
                    # decision back onto this exact row (opened, or skipped
                    # and why), which is what lets the Signals page explain
                    # "why no trade" per signal instead of a silent card.
                    sig.log_id = await self._log_signal(sig)

                    if pcfg.enabled:
                        self._pending_paper_signals.append((sig, state))
                    else:
                        await self._mark_skipped(sig.log_id, "paper_trading_off")

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

    _CATEGORY_TO_EVENT_TYPE = {
        "central_bank": "rate_decision", "inflation": "inflation_data", "jobs": "jobs_data",
        "geopolitics_war": "war", "sanctions": "war", "trade_tariffs": "tariff",
        "crypto_regulation": "regulation", "crypto_etf_flows": "etf_flow",
        "liquidations_funding": "liquidation", "exchange_hack_insolvency": "hack",
        "stablecoin": "hack", "corporate_treasury": "adoption",
    }

    def _schedule_monitor(self, minutes: float) -> None:
        """Book the next event-monitor run, replacing any already booked."""
        self.scheduler.add_job(
            self._event_monitor_job, "date",
            run_date=datetime.now(UTC) + timedelta(minutes=max(1.0, minutes)),
            id="event_monitor", replace_existing=True, max_instances=1)

    async def _event_monitor_job(self) -> None:
        """
        Adaptive world watch. Replaces the fixed 30-minute briefing.

        Each run: calendar context + the events already tracked go to Groq's
        web-search model, which reports new events (graded 1-5), updates and
        resolutions. The next run is booked from the biggest live event —
        about 45 minutes when calm, 5 during a level-5 shock, with jitter —
        and pulled forward to just after any scheduled release. A daily cap
        keeps it inside Groq's free tier. Shadow mode: nothing here changes a
        paper trade.
        """
        import hashlib

        from analysis.event_calendar import EVENTS_2026, calendar_text
        from analysis.event_monitor import (
            MONITOR_SYSTEM,
            ActiveEvent,
            active_window,
            monitor_model_role,
            monitor_prompt,
            next_delay_minutes,
            parse_monitor,
        )
        from collectors.llm_client import ask_json, chain_for
        from collectors.market_briefing import as_news_items

        now = datetime.now(UTC).replace(tzinfo=None)
        max_level = 0
        try:
            if not settings.event_monitor_enabled or not chain_for("briefing"):
                return
            if self._monitor_day != now.date():
                self._monitor_day, self._monitor_calls = now.date(), 0
            if self._monitor_calls >= settings.event_monitor_daily_cap:
                log.info("event_monitor_daily_cap_reached", calls=self._monitor_calls)
                return
            async with AsyncSessionFactory() as session:
                rows = await Repository(session).recent_events(14)
            active = active_window([
                ActiveEvent(r.id, r.title, r.category, r.level_current, r.happened_at, r.direction)
                for r in rows if r.status == "active"], now)
            max_level = max((e.level for e in active), default=0)
            self._monitor_calls += 1
            self._monitor_last_run = now
            reply = await ask_json(monitor_model_role(max_level), MONITOR_SYSTEM,
                                   monitor_prompt(calendar_text(now), active),
                                   max_tokens=1800, temperature=0.2, timeout=90.0)
            self._monitor_failures = reply.failures[:4]
            if not reply or not isinstance(reply.data, dict):
                log.warning("event_monitor_no_answer", failures=reply.failures)
                return
            got = parse_monitor(reply.data, {e.id for e in active})
            news = []
            async with AsyncSessionFactory() as session:
                repo = Repository(session)
                for e in got["new"]:
                    key = "news:" + hashlib.sha256(e["title"].lower().encode()).hexdigest()[:24]
                    try:
                        when = datetime.fromisoformat(e["when"].replace("Z", "+00:00"))
                        when = when.astimezone(UTC).replace(tzinfo=None) if when.tzinfo else when
                    except ValueError:
                        when = now
                    await repo.upsert_event(key, title=e["title"], category=e["category"],
                                            level=e["level"], direction=e["direction"],
                                            source=e["source"], happened_at=when)
                    max_level = max(max_level, e["level"])
                    news.append({"headline": e["title"], "score": e["score"],
                                 "confidence": e["confidence"], "when": e["when"],
                                 "source": e["source"],
                                 "event_type": self._CATEGORY_TO_EVENT_TYPE.get(
                                     e["category"], "macro_other")})
                for u in got["updates"]:
                    await repo.update_event(u["id"], level=u["level"], note=u["note"])
                    if u["level"]:
                        max_level = max(max_level, u["level"])
                for rid in got["resolved"]:
                    await repo.update_event(rid, resolved=True)
                await repo.save_briefing(got["risk_tone"], got["summary"], news,
                                         reply.served_by, reply.latency_ms)
                if news:
                    await repo.ingest_news_sentiment(as_news_items(news, reply.served_by))
                self.latest_briefing = await repo.latest_briefing()
            log.info("event_monitor_ran", new=len(got["new"]), updates=len(got["updates"]),
                     resolved=len(got["resolved"]), max_level=max_level,
                     served_by=reply.served_by, calls_today=self._monitor_calls)
        except Exception:
            log.exception("event_monitor_failed")
        finally:
            delay = next_delay_minutes(max_level)
            # Just after a scheduled release, look at what it did.
            for ev in EVENTS_2026:
                for after in (2, 15):
                    mins = (ev.at + timedelta(minutes=after) - now).total_seconds() / 60
                    if 0 < mins < delay:
                        delay = mins
            try:
                self._schedule_monitor(delay)
            except Exception:
                log.exception("event_monitor_reschedule_failed")

    async def _daily_trend_job(self) -> None:
        """
        Hourly: 60 daily candles per watchlist coin -> 20-day average on its
        state.

        One REST round trip per coin, fetched concurrently (bounded, same cap
        as the minute-candle poll in BinanceKlines.fetch) rather than one
        after another — each coin's daily bars are independent of every
        other's, so there is no ordering to preserve, and sequentially this
        job paid one round trip per watchlist coin back to back.
        """
        from analysis.daily_trend import sma
        try:
            states = await self.crypto_store.get_all()
            sem = asyncio.Semaphore(self.klines.FETCH_CONCURRENCY)

            async def _one(st) -> None:
                async with sem:
                    bars = await self.klines.fetch_candles(st.symbol, interval="1d", limit=60)
                    # The last daily bar is still forming; average closed days only.
                    closes = [b["close"] for b in (bars or [])][:-1]
                    st.daily_sma20 = sma(closes, 20)
                    st.daily_trend_at = datetime.now(UTC)
                    if st.daily_sma20 is None:
                        log.info("daily_trend_unavailable", symbol=st.symbol,
                                bars=len(bars or []))

            await asyncio.gather(*(_one(st) for st in states))
        except Exception:
            log.exception("daily_trend_job_failed")

    def _maybe_trigger_monitor(self, states) -> None:
        """A fast BTC move means something probably just happened: check now."""
        btc = next((s for s in states if s.symbol == "btcusdt"), None)
        if btc is None or len(btc.candles_1m) < 6:
            return
        a, b = btc.candles_1m[-6].close, btc.candles_1m[-1].close
        if a <= 0 or abs(b - a) / a < 0.012:
            return
        last = self._monitor_last_run
        now = datetime.now(UTC).replace(tzinfo=None)
        if last is not None and (now - last).total_seconds() < 300:
            return
        log.info("event_monitor_triggered_by_move", move_pct=round((b - a) / a * 100, 2))
        self._schedule_monitor(0)

    async def _event_evaluation_job(self) -> None:
        """
        Every 30 minutes: file scheduled releases as events, confirm each
        event's level from how far BTC actually moved in the 2 hours after it,
        and run the four shadow books on level 4-5 events once 6 hours of
        prices have followed.
        """
        from analysis.event_calendar import EVENTS_2026
        from analysis.event_monitor import (
            confirmed_level,
            max_abs_move,
            simulate_books,
        )

        now = datetime.now(UTC).replace(tzinfo=None)
        try:
            async with AsyncSessionFactory() as session:
                repo = Repository(session)
                for ev in EVENTS_2026:
                    if now - timedelta(days=14) <= ev.at <= now:
                        level = {"fomc": 5 if "projections" in ev.name else 4,
                                 "cpi": 4, "nfp": 4}.get(ev.kind, 3)
                        await repo.upsert_event(
                            f"sched:{ev.kind}:{ev.at.isoformat()}", title=ev.name,
                            category={"fomc": "central_bank", "cpi": "inflation",
                                      "nfp": "jobs"}.get(ev.kind, "inflation"),
                            level=level, source="scheduled", happened_at=ev.at)
                events = await repo.recent_events(14)
                if not events:
                    return
                points = await repo.price_points(["btcusdt", "ethusdt", "solusdt"], 14 * 24)
                btc = sorted(points.get("btcusdt", []))
                for e in events:
                    if e.level_confirmed is None and now - e.happened_at >= timedelta(hours=2):
                        move = max_abs_move(btc, e.happened_at)
                        if move is not None:
                            e.btc_move_2h_pct = round(move, 3)
                            e.level_confirmed = confirmed_level(move)
                    big = max(e.level_current or 0, e.level_confirmed or 0) >= 4
                    if big and not e.shadow_done and now - e.happened_at >= timedelta(hours=6):
                        level = max(e.level_current or 0, e.level_confirmed or 0)
                        paths = {s: sorted(p) for s, p in points.items()}
                        await repo.save_shadow(e.id, simulate_books(e.happened_at, level, paths))
                        log.info("event_shadow_books_saved", event_name=e.title[:60])
                await session.commit()
        except Exception:
            log.exception("event_evaluation_failed")

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

    async def _v2_shadow_job(self) -> None:
        """
        Every 5 minutes: run the v2 setups on fresh Binance frames for each
        crypto on the watchlist, record new candidates, and resolve the open
        ones with the backtest's own fill rules. Shadow only — nothing trades.

        One DB session to read every symbol's open rows, then step() for all
        seven symbols with no session open at all, then one DB session to
        write everything. The old version opened a session per symbol and
        held it — checked out from the pool, doing nothing — for the whole
        of that symbol's step() computation, seven times a run. On a remote
        database that starved every other page and job of a free connection;
        it is why this job's own 17-20s showed up as 1-4s pings everywhere.
        """
        if not settings.v2_shadow_enabled:
            return
        import pandas as pd

        from analysis.instruments import spec_for
        from analysis.v2_setups import V2Config
        from analysis.v2_shadow import step
        from collectors.v2_feed import fetch_frames, fetch_funding

        cfg = V2Config(session_filter=settings.session_filter_enabled,
                       session_start_utc=settings.session_start_utc,
                       session_end_utc=settings.session_end_utc,
                       weekdays_only=settings.session_weekdays_only)
        now = pd.Timestamp.now("UTC").tz_localize(None)
        new_total = closed = 0
        try:
            symbols = [s for s in await self.crypto_store.get_symbols()
                       if spec_for(s).kind == "crypto"]

            async with AsyncSessionFactory() as session:
                open_by_symbol: dict[str, list] = {}
                for row in await Repository(session).open_v2_shadows():
                    open_by_symbol.setdefault(row.symbol, []).append(row)

            all_cands: list = []
            all_updates: list[tuple[int, dict]] = []
            for sym in symbols:
                frames = await fetch_frames(sym)
                if frames["5m"].empty:
                    continue
                funding = await fetch_funding(sym)
                # CPU work; off the event loop so the paper tick and the web
                # pages never wait on it. No DB session is open while it runs.
                cands, updates = await asyncio.to_thread(
                    step, sym, frames, funding, open_by_symbol.get(sym, []), now, cfg)
                all_cands.extend(cands)
                all_updates.extend((u.row_id, u.fields) for u in updates)

            if all_cands or all_updates:
                async with AsyncSessionFactory() as session:
                    repo = Repository(session)
                    for row_id, fields in all_updates:
                        await repo.update_v2_shadow(row_id, commit=False, **fields)
                        closed += fields.get("status") == "closed"
                    new_total = await repo.save_v2_candidates(all_cands, commit=False)
                    await session.commit()
            log.info("v2_shadow_step", symbols=len(symbols), new=new_total, closed=closed)
        except Exception:
            log.exception("v2_shadow_failed")

    async def _v2_backtest_job(self) -> None:
        """
        Download the v2 test data from data.binance.vision and run the v2
        backtest — in a SEPARATE process, never inside the engine.

        The first version ran it in a worker thread of the engine itself.
        Two years of 5m bars for seven coins was more than the box's memory,
        the kernel killed the whole engine, systemd restarted it, the restart
        found no report and scheduled the test again: a restart every ~10
        minutes. Now the child runs at the lowest CPU priority with its OOM
        score raised to the maximum, so if memory runs out the kernel kills
        the test, not the engine; it has a time limit; and an attempt file
        stops any retry until the next daily slot.
        """
        if not settings.v2_backtest_enabled or self._v2_bt_state.get("running"):
            return
        import json
        import os
        import sys

        from analysis.instruments import spec_for

        out = Path(settings.v2_reports_dir)
        out.mkdir(parents=True, exist_ok=True)
        attempt = out / "backtest_v2_attempt.json"
        attempt.write_text(json.dumps({"started": datetime.now(UTC).isoformat()}))
        self._v2_bt_state = {"running": True, "started": datetime.now(UTC).isoformat(),
                             "stage": "downloading and testing"}
        symbols = [s.upper() for s in await self.crypto_store.get_symbols()
                   if spec_for(s).kind == "crypto"]

        def _sacrificial():
            # Runs in the child just before exec: lowest priority, first to
            # be killed on memory pressure. Raising one's own score needs no
            # privilege.
            os.nice(19)
            try:
                with open("/proc/self/oom_score_adj", "w") as fh:
                    fh.write("1000")
            except OSError:
                pass

        cmd = [sys.executable, "-m", "scripts.backtest_v2", "--latest",
               "--symbols", ",".join(symbols),
               "--root", settings.v2_lake_dir, "--reports", settings.v2_reports_dir]
        if settings.v2_backtest_years > 2.0:
            cmd.extend(["--total-years", str(settings.v2_backtest_years), "--chunk-years", "2.0"])
        else:
            cmd.extend(["--years", str(settings.v2_backtest_years)])
        if settings.v2_backtest_download:
            cmd.append("--download")
        if not settings.session_filter_enabled:
            cmd.append("--no-session")
        env = dict(os.environ, OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1",
                   MKL_NUM_THREADS="1")
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                preexec_fn=_sacrificial, env=env)
            try:
                output, _ = await asyncio.wait_for(
                    proc.communicate(), timeout=settings.v2_backtest_timeout_minutes * 60)
            except TimeoutError:
                proc.kill()
                await proc.wait()
                raise RuntimeError(f"timed out after {settings.v2_backtest_timeout_minutes} min")
            tail = output.decode(errors="replace")[-1500:]
            if proc.returncode != 0:
                why = ("killed by the kernel (out of memory?)" if proc.returncode == -9
                       else f"exit code {proc.returncode}")
                raise RuntimeError(f"{why}: {tail[-300:]}")
            log.info("v2_backtest_done", output=tail[-600:])
            self._v2_bt_state = {"running": False, "finished": datetime.now(UTC).isoformat()}
        except Exception as exc:
            log.error("v2_backtest_failed", error=str(exc)[:400])
            self._v2_bt_state = {"running": False, "error": str(exc)[:300],
                                 "finished": datetime.now(UTC).isoformat()}

    async def _move_attribution_job(self) -> None:
        """
        Every hour: ask Groq why each watchlist coin moved and whether our
        signals were on the right side of it. Moves are computed here from
        stored prices; the model only explains them.
        """
        from analysis.move_attribution import (
            ATTRIBUTION_SYSTEM,
            build_prompt,
            coin_facts,
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
            btc = next((m for m in moves if m.symbol == "btcusdt"), None)
            facts = {}
            for m in moves:
                try:
                    facts[m.symbol] = coin_facts(await self.crypto_store.get(m.symbol), m, btc)
                except Exception:
                    facts[m.symbol] = {}
            reply = await ask_json("attribution", ATTRIBUTION_SYSTEM,
                                   build_prompt(moves, digest, news, hours, facts),
                                   max_tokens=3000, temperature=0.2, timeout=120.0)
            if not reply or not isinstance(reply.data, dict):
                log.warning("move_attribution_no_answer", failures=reply.failures)
                return
            result = parse_attribution(reply.data, {m.symbol for m in moves})
            # Why the web-search model was skipped, shown on the page.
            result["skipped"] = reply.failures[:4]
            move_rows = [dict(m.as_dict(), facts=facts.get(m.symbol, {})) for m in moves]
            async with AsyncSessionFactory() as session:
                await Repository(session).save_move_attribution(
                    window_hours=hours, briefing_id=briefing.id if briefing else 0,
                    moves=move_rows, signals=digest, result=result,
                    model=reply.served_by, latency_ms=reply.latency_ms)
            log.info("move_attribution_saved", coins=len(result["coins"]),
                     drivers=len(result["drivers"]), served_by=reply.served_by)
        except Exception:
            log.exception("move_attribution_job_failed")

    async def _research_precedent(self, name: str, at: datetime, level: int,
                                  symbols: list[str]) -> dict | None:
        """
        Steps A-C of analysis/event_precedent.py for one calendar event:
        find real historical precedent, measure our own Binance lake data
        for it, then synthesise. Returns None — never raises — whenever
        there is nothing usable to cache: zero real precedents found (the
        expected, correct outcome for a genuinely novel event), or none of
        the precedents found had measured market data behind them. Nothing
        is stored for a None; the next run tries fresh.
        """
        from analysis.event_precedent import (
            PRECEDENT_FIND_SYSTEM,
            PRECEDENT_SYNTH_SYSTEM,
            confidence_bucket,
            find_prompt,
            measure_window,
            parse_precedents,
            parse_synthesis,
            synth_prompt,
        )
        from collectors.llm_client import ask_json

        when = at.strftime("%A %d %B %Y, %H:%M UTC")
        try:
            found = await ask_json("event_precedent", PRECEDENT_FIND_SYSTEM,
                                   find_prompt(name, when), max_tokens=1200,
                                   temperature=0.2, timeout=90.0)
            if not found:
                log.info("event_precedent_no_answer", event_name=name, failures=found.failures)
                return None
            precedents = parse_precedents(found.data)
            if not precedents:
                log.info("event_precedent_none_found", event_name=name)
                return None

            measurements: dict[str, dict] = {}
            for p in precedents:
                try:
                    start = datetime.fromisoformat(p["start_date"])
                    end = datetime.fromisoformat(p["end_date"])
                except ValueError:
                    continue
                per_symbol = {}
                for sym in symbols:
                    m = await asyncio.to_thread(
                        measure_window, sym, start, end, settings.v2_lake_dir)
                    if m is not None:
                        per_symbol[sym] = m
                if per_symbol:
                    measurements[p["name"]] = per_symbol
            usable_precedents = [p for p in precedents if p["name"] in measurements]
            if not usable_precedents:
                log.info("event_precedent_no_usable_data", event_name=name,
                         precedents_found=len(precedents))
                return None

            synthesised = await ask_json("event_precedent", PRECEDENT_SYNTH_SYSTEM,
                                         synth_prompt(name, precedents, measurements),
                                         max_tokens=900, temperature=0.2, timeout=90.0)
            if not synthesised:
                log.info("event_precedent_synthesis_no_answer", event_name=name,
                         failures=synthesised.failures)
                return None
            result = parse_synthesis(synthesised.data, len(usable_precedents))
            log.info("event_precedent_saved", event_name=name, sample_size=result["sample_size"],
                     direction_bias=result["direction_bias"],
                     served_by=f"{found.served_by} / {synthesised.served_by}")
            return {
                "event_name": name, "event_at": at, "level": level,
                "precedents": precedents, "measurements": measurements,
                "direction_bias": result["direction_bias"],
                "typical_magnitude_pct": result["typical_magnitude_pct"],
                "typical_duration_days": result["typical_duration_days"],
                "sample_size": result["sample_size"], "summary": result["summary"],
                "confidence_real": confidence_bucket(usable_precedents),
                "model": f"{found.served_by} / {synthesised.served_by}",
                "latency_ms": found.latency_ms + synthesised.latency_ms,
            }
        except Exception:
            log.exception("event_precedent_research_failed", event_name=name)
            return None

    async def _event_precedent_job(self) -> None:
        """
        Every few hours: for calendar events coming up within the lookahead
        window, make sure there is cached precedent research for this exact
        occurrence (scheduler.runner._research_precedent), then refresh the
        small in-process cache analysis.event_precedent.current_brief()
        reads from — crypto_engine.process() runs on every tick and is
        synchronous, so the DB read has to have already happened.

        Genuinely rare: most runs find every candidate event already
        researched (or find none in the lookahead window at all) and do
        nothing but refresh the cache.
        """
        if not settings.event_precedent_enabled:
            return
        from collectors.llm_client import chain_for
        if not chain_for("event_precedent"):
            return
        from analysis.event_calendar import EVENTS_2026, calendar_context, upcoming
        now = datetime.now(UTC).replace(tzinfo=None)
        lookahead = settings.event_precedent_lookahead_days

        # EVENTS_2026 entries carry an exact timestamp, so they key cleanly
        # by name + date. calendar_context()'s recurring structural items
        # (rebalancing, expiry, the yearly list) do not carry a machine
        # date — only calendar_context() itself knows when they are near —
        # so they are keyed by name + year instead: still a fresh row each
        # time the item's date rolls around, at the coarser granularity
        # calendar_context() already operates at. Only level >= 3 items are
        # considered; the weekly/intraday ones (funding settlement, weekend
        # liquidity, the US market open) are too frequent for "precedent"
        # research to mean anything and would defeat "genuinely rare".
        candidates: dict[str, tuple[str, datetime, int]] = {}
        for ev in upcoming(now, days=lookahead, events=EVENTS_2026):
            level = {"fomc": 5, "cpi": 4, "nfp": 4}.get(ev.kind, 3)
            candidates[f"{ev.kind}:{ev.at.date().isoformat()}"] = (ev.name, ev.at, level)
        for item in calendar_context(now):
            if item.level >= 3:
                key = f"{item.name}:{now.year}"
                candidates.setdefault(key, (item.name, now, item.level))
        if not candidates:
            return
        try:
            async with AsyncSessionFactory() as session:
                repo = Repository(session)
                existing = {}
                for key in candidates:
                    row = await repo.get_event_precedent(key)
                    if row is not None:
                        existing[key] = row
            missing = {k: v for k, v in candidates.items() if k not in existing}
            symbols = list(await self.crypto_store.get_symbols())
            for key, (name, at, level) in missing.items():
                built = await self._research_precedent(name, at, level, symbols)
                if built is None:
                    continue
                async with AsyncSessionFactory() as session:
                    await Repository(session).save_event_precedent(event_key=key, **built)
                async with AsyncSessionFactory() as session:
                    row = await Repository(session).get_event_precedent(key)
                if row is not None:
                    existing[key] = row

            from analysis.event_precedent import brief_from_row, set_active_briefs
            set_active_briefs({key: brief_from_row(row, lookahead)
                               for key, row in existing.items()})
            log.info("event_precedent_job_done", candidates=len(candidates),
                     researched=len(missing), active=len(existing))
        except Exception:
            log.exception("event_precedent_job_failed")

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

    def _profit_lock(self):
        from analysis.paper_trading import ProfitLock
        return ProfitLock(enabled=settings.profit_lock_enabled,
                          at_pct=settings.profit_lock_at_pct,
                          to_pct=settings.profit_lock_to_pct,
                          trail_pct=settings.profit_lock_trail_pct,
                          defers_to_trail=settings.profit_lock_defers_to_trail_enabled)

    def _protection_config(self):
        from analysis.protections import ProtectionConfig
        return ProtectionConfig(
            enabled=settings.protections_enabled,
            session_filter=settings.session_filter_enabled,
            session_start_utc=settings.session_start_utc,
            session_end_utc=settings.session_end_utc,
            weekdays_only=settings.session_weekdays_only,
            daily_loss_pct=settings.daily_loss_limit_pct,
            max_same_direction=settings.max_same_direction_positions,
        )

    def _review_extras(self, state) -> dict:
        """Market mood at review time, stored beside the verdict."""
        return {
            "sentiment_score": round(float(getattr(state, "sentiment_score", 0.0) or 0.0), 4),
            "fear_greed": int(self.fear_greed.value) if self.fear_greed else 0,
        }

    def _event_bias_for(self, event, states) -> dict:
        """
        The bull/bear bias for this event: Groq's answer once it arrives,
        the engine's news sentiment until then. Asks Groq once per event,
        in the background, so the paper tick never waits on a model.
        """
        from analysis.event_bias import from_sentiment
        key = f"{event.kind}:{event.name}:{event.at}"
        got = self._event_biases.get(key)
        if got is not None:
            return got
        if key not in self._event_bias_pending:
            self._event_bias_pending.add(key)
            asyncio.create_task(self._fetch_event_bias(key, event))
        scores = [st.sentiment_score for st in states.values()
                  if getattr(st, "sentiment_score", None) is not None]
        return from_sentiment(sum(scores) / len(scores) if scores else 0.0)

    async def _fetch_event_bias(self, key: str, event) -> None:
        from analysis.event_bias import SYSTEM, parse, prompt
        from collectors.llm_client import ask_json, chain_for
        try:
            if not chain_for("briefing"):
                return
            when = (event.at + timedelta(hours=5, minutes=30)).strftime("%d %b %H:%M")
            reply = await ask_json("briefing", SYSTEM, prompt(event.name, event.kind, when),
                                   max_tokens=900, temperature=0.2, timeout=60.0)
            got = parse(reply.data) if reply else None
            if got is not None:
                got["model"] = reply.served_by
                self._event_biases[key] = got
                log.info("event_bias", event_name=event.name, bias=got["bias"],
                         confidence=got["confidence"], reason=got["reason"][:160])
        except Exception:
            log.exception("event_bias_failed")
        finally:
            self._event_bias_pending.discard(key)

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

            log.info("crypto_news_sentiment_updated", global_score=global_sentiment,
                     items_analyzed=len(news_items))
        except Exception:
            log.exception("crypto_news_job_failed")

    async def _crypto_snapshot_job(self) -> None:
        """
        Save periodic crypto & commodity snapshots for ML training and backtesting.

        One commit for the whole watchlist, not one per coin. Each commit is a
        round trip to the database; on a remote database with real latency,
        ten sequential commits for ten rows was most of this job's 8-14s.
        """
        try:
            async with AsyncSessionFactory() as session:
                repo = Repository(session)
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
                            commit=False,
                        )

                for cstate in await self.commodity_store.get_all():
                    if cstate.current_price > 0:
                        await repo.save_commodity_snapshot(
                            symbol=cstate.symbol,
                            price=cstate.current_price,
                            rsi_14=cstate.rsi_14,
                            atr_14=cstate.atr_14,
                            commit=False,
                        )
                await session.commit()
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
        # Mirror review's live re-review loop — same cadence as the
        # detectors themselves, since it exists to react to the same market
        # ticks. A no-op (returns immediately) while the feature is off or
        # nothing is being tracked.
        self.scheduler.add_job(
            self._mirror_review_job,
            "interval",
            seconds=60,
            id="mirror_review",
            max_instances=1,
            next_run_time=datetime.now(UTC) + timedelta(seconds=30),
        )
        self.scheduler.add_job(
            self._move_attribution_job,
            "interval",
            minutes=settings.move_attribution_minutes,
            id="move_attribution",
            max_instances=1,
            next_run_time=datetime.now(UTC) + timedelta(minutes=5),
        )
        # Genuinely rare — see the job's own docstring. Not latency-
        # sensitive at all, so a few hours between runs is more than
        # enough; the no-op case (nothing in the lookahead window, or
        # everything already researched) is the common one.
        if settings.event_precedent_enabled:
            self.scheduler.add_job(
                self._event_precedent_job,
                "interval",
                hours=4,
                id="event_precedent",
                max_instances=1,
                next_run_time=datetime.now(UTC) + timedelta(minutes=10),
            )
        if settings.event_monitor_enabled:
            # Self-scheduling: each run books the next one (adaptive timing).
            self._schedule_monitor(1)
        else:
            self.scheduler.add_job(
                self._market_briefing_job,
                "interval",
                minutes=settings.market_briefing_minutes,
                id="market_briefing",
                max_instances=1,
                next_run_time=datetime.now(UTC),
            )
        self.scheduler.add_job(
            self._daily_trend_job,
            "interval",
            minutes=60,
            id="daily_trend",
            max_instances=1,
            next_run_time=datetime.now(UTC) + timedelta(seconds=30),
        )
        self.scheduler.add_job(
            self._v2_backtest_job,
            "cron",
            hour=settings.v2_backtest_hour_utc,
            minute=17,
            id="v2_backtest",
            max_instances=1,
        )
        reports = Path(settings.v2_reports_dir)
        if (not (reports / "backtest_v2_latest.json").exists()
                and not (reports / "backtest_v2_attempt.json").exists()):
            # First deploy: do not wait until tomorrow for the first answer.
            # Only once: if that attempt fails, the daily slot retries, never
            # a restart (that is how a crash turned into a restart loop).
            self.scheduler.add_job(
                self._v2_backtest_job, "date",
                run_date=datetime.now(UTC) + timedelta(minutes=3), id="v2_backtest_first")
        self.scheduler.add_job(
            self._v2_shadow_job,
            "interval",
            minutes=settings.v2_shadow_minutes,
            id="v2_shadow",
            max_instances=1,
            next_run_time=datetime.now(UTC) + timedelta(minutes=2),
        )
        self.scheduler.add_job(
            self._event_evaluation_job,
            "interval",
            minutes=30,
            id="event_evaluation",
            max_instances=1,
            next_run_time=datetime.now(UTC) + timedelta(minutes=3),
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
        # Settings saved on /settings, before anything is scheduled or started.
        await self.load_settings_overrides()

        # Measure before anything is scheduled, so every job is timed and a
        # stalled event loop can be traced to the job that stalled it.
        from scheduler import perf
        perf.instrument_scheduler(self.scheduler)
        self._ws_tasks.append(asyncio.create_task(perf.loop_monitor(), name="loop_monitor"))
        self._ws_tasks.append(asyncio.create_task(perf.db_ping_monitor(), name="db_ping"))
        self.setup_jobs()
        self.scheduler.start()

        # Launch continuous WebSocket tasks concurrently (non-blocking)
        self._sync_binance_ws()

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
        data_dir = Path(settings.v2_reports_dir).parent
        starts = _record_start(data_dir / "engine_starts.json")
        sha, subject = _code_version()
        # Real-time alerts: errors while running, and a crash report now if
        # the previous run died instead of stopping.
        from scheduler import alerts
        alerts.configure(data_dir)
        crashed = alerts.mark_running(sha)
        self._ws_tasks.append(asyncio.create_task(alerts.sender(
            lambda text: self.notifier.send_text(text, parse_mode=ParseMode.HTML)),
            name="alert_sender"))
        if crashed is not None:
            await self.notifier.send_text(alerts.crash_report(crashed),
                                          parse_mode=ParseMode.HTML)
        if _version_changed(data_dir / "engine_version.txt", sha):
            # A deploy: always say so, however soon after the last restart.
            # (Silencing restarts within the hour, to stop a crash loop's
            # spam, also silenced back-to-back deploys.)
            await self.notifier.send_text(
                f"🚀 <b>Deployed</b> <code>{sha}</code>: {subject}\n"
                f"{sources}\n"
                f"AI Sentinel: {'Active' if settings.groq_api_key else 'Disabled (no key)'}",
                parse_mode=ParseMode.HTML,
            )
        elif len(starts) <= 1:
            await self.notifier.send_text(
                "🪙 Crypto Signal Engine started.\n"
                f"{sources}\n"
                "Paper Trading: Active in background\n"
                f"AI Sentinel: {'Active' if settings.groq_api_key else 'Disabled (no key)'}",
                parse_mode=ParseMode.HTML,
            )
        elif len(starts) in (3, 10, 30) and crashed is not None:
            # A restart loop used to send this message every ten minutes for
            # hours. Now repeats inside an hour are silent, and a loop is
            # reported as the fault it is only if it actually crashed.
            await self.notifier.send_text(
                f"⚠️ Engine restarted {len(starts)} times in the last hour after crashing. "
                "Check <code>sudo journalctl -u crypto-engine -n 200</code> "
                "and <code>sudo dmesg | grep -i oom</code>.",
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
                "binance_ws_connected": (self.binance_ws._running
                                        and self.binance_ws._consecutive_failures == 0),
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
