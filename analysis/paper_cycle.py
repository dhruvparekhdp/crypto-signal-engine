"""
The live paper-trading cycle.

This is the piece that turns the engine into something that runs. It holds one
cycle's wallet, opens positions from gated signals, and resolves them against
live prices on every tick — using the same functions the backtest uses, so a
backtest result and a live result mean the same thing.

Two deliberate choices:

* State lives in the database, not in memory. A redeploy mid-cycle would
  otherwise abandon open positions silently, and on a free host redeploys are
  frequent. Every tick reads the open positions back and writes them again.

* Resolution uses the tick price as both high and low. A live poller sees
  prices, not bars, so there is no intrabar range to reason about. That makes
  the live path slightly *less* likely to trigger a stop than the backtest,
  which resolves against a real bar's extremes — worth knowing when comparing
  the two, and the honest direction for the difference to run.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog

from analysis.crypto_signal import CryptoSignal
from analysis.instruments import spec_for
from analysis.paper_trading import (
    ClosedTrade,
    CycleConfig,
    ExitReason,
    FeeModel,
    Position,
    Side,
    close_position,
    liquidation_price,
    open_position,
    resolve_candle,
)

log = structlog.get_logger()


@dataclass
class CycleState:
    """Everything a tick needs to know, loaded from the database."""

    cycle_id: int
    wallet: float
    peak_wallet: float
    positions: list[Position]
    position_ids: dict[int, int]      # index in `positions` -> DB row id


def config_for_cycle(row) -> CycleConfig:
    """Rebuild the exact configuration a cycle was started under."""
    from analysis.paper_trading import (
        LeverageConfig,
        ProfitLadder,
        SizingConfig,
        TrailingStop,
    )

    return CycleConfig(
        starting_wallet=row.starting_wallet,
        target_wallet=row.target_wallet,
        leverage=row.leverage,
        stop_pct_of_margin=row.stop_pct_of_margin,
        reward_risk=row.reward_risk,
        min_confidence=row.min_confidence,
        sizing=SizingConfig() if row.scaled_sizing else None,
        leverage_scaling=(LeverageConfig(ceiling_leverage=row.leverage)
                          if getattr(row, "scaled_leverage", False) else None),
        # The runner preset when trailing is on: arm at 0.75R, ride 1R behind
        # the high, release the fixed target. Distances in R rather than in
        # percent of margin, so the trail scales with the setup's own stop
        # instead of with the leverage dial.
        trailing=(TrailingStop.runner() if row.trailing_enabled
                  else TrailingStop(enabled=False)),
        ladder=(ProfitLadder.tight() if getattr(row, "ladder_tight", False)
                else ProfitLadder(enabled=getattr(row, "ladder_enabled", False))),
    )


def reprice_signal(signal, live_price: float, now: datetime,
                   max_age_seconds: int = 90, max_chase: float = 0.5):
    """
    Re-check a queued signal against the live price before it becomes a trade.

    A signal waits for the AI review, the Telegram send and the next paper
    tick before it opens. It used to fill at the price from when it was
    generated, so a move through the target while it waited was booked as a
    win that was never available. Now:

      - older than `max_age_seconds`  -> dropped ("stale")
      - live price already past target or stop -> dropped
      - more than `max_chase` of the way to target already -> dropped ("chased"):
        the move it predicted has mostly happened
      - otherwise it fills at the live price, levels unchanged

    Returns (signal or None, reason).
    """
    from dataclasses import replace as _replace

    ts = getattr(signal, "timestamp", None)
    if ts is not None:
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        ref = now if now.tzinfo else now.replace(tzinfo=UTC)
        if (ref - ts).total_seconds() > max_age_seconds:
            return None, "stale"
    if live_price <= 0:
        return None, "no_price"
    entry, target, stop = signal.current_price, signal.target_price, signal.stop_loss
    long = signal.direction == "long"
    if target and ((long and live_price >= target) or (not long and live_price <= target)):
        return None, "target_passed"
    if stop and ((long and live_price <= stop) or (not long and live_price >= stop)):
        return None, "stop_passed"
    if target and entry and target != entry:
        progress = (live_price - entry) / (target - entry)
        if progress > max_chase:
            return None, "chased"
    try:
        return _replace(signal, current_price=live_price), "ok"
    except TypeError:
        signal.current_price = live_price
        return signal, "ok"


def stop_out_costs(symbol: str, cfg) -> float:
    """
    What a stop-out costs beyond the price distance, as a fraction of price:
    the round-trip fee, the spread crossed on entry and exit, and the stop's
    extra slippage.
    """
    slip = getattr(cfg, "slippage", None)
    spread = 2 * slip.spread_pct if slip is not None else 0.0
    stop_extra = slip.stop_extra_pct if slip is not None else 0.0
    return fees_for(symbol).round_trip_pct() + spread + stop_extra


def fees_for(symbol: str) -> FeeModel:
    """Per-market costs — gold is a fifth of ether, so this cannot be global."""
    spec = spec_for(symbol)
    return FeeModel(
        taker_pct=spec.taker_pct,
        maker_pct=spec.maker_pct,
        maintenance_margin_pct=spec.maintenance_margin_pct,
        funding_rate_per_8h=spec.funding_rate_per_8h,
    )


def committed_margin(positions: list[Position]) -> float:
    return sum(p.margin for p in positions)


def should_open(
    signal: CryptoSignal,
    cfg: CycleConfig,
    state: CycleState,
    now: datetime,
) -> tuple[bool, str]:
    """
    Decide whether this signal becomes a position. Returns (ok, reason).

    The reason is returned even on success so the caller can log why a tick did
    nothing — a cycle that quietly takes no trades for hours is otherwise
    indistinguishable from one that is broken.
    """
    if signal.confidence < cfg.min_confidence:
        return False, "confidence_below_floor"
    if len(state.positions) >= cfg.max_concurrent:
        return False, "max_concurrent"
    if any(p.symbol == signal.symbol for p in state.positions):
        return False, "already_open_in_symbol"

    margin = cfg.margin_for_signal(state.wallet, signal.confidence,
                                   committed_margin(state.positions))
    if margin <= 0:
        return False, "no_free_margin"
    return True, "ok"


def _hold_minutes(signal: CryptoSignal, cfg: CycleConfig) -> float:
    """
    How long to give this setup, from the signal's own expected duration.

    The level policy derives a horizon from how far the target is and how fast
    the market moves; holding every trade for a flat four hours ignores it. A
    16-minute gold setup sat open until the clock ran out and closed at
    -0.067%, having paid fees to learn nothing. Half again the expected time
    leaves room to be slow without waiting on a setup that has clearly failed,
    and the configured maximum is still the ceiling.
    """
    tf = (signal.timeframe or "").strip().lower()
    minutes = None
    try:
        if tf.endswith("m"):
            minutes = float(tf[:-1]) * 1.5
        elif tf.endswith("h"):
            minutes = float(tf[:-1]) * 90.0
    except ValueError:
        minutes = None
    if not minutes or minutes <= 0:
        return cfg.max_hold_minutes
    return min(max(minutes, 15.0), cfg.max_hold_minutes)


def open_from_signal(
    signal: CryptoSignal,
    cfg: CycleConfig,
    state: CycleState,
    now: datetime,
    usdt_inr: float,
    atr_pct: float | None = None,
    protect=None,
) -> Position | None:
    """
    Build a position from a signal, or None if it fails the viability gate.

    `protect` (analysis.protections.ProtectionConfig) adds two gates: a stop
    closer than 1.5x the round-trip cost is refused, and leverage is capped
    so liquidation sits at least 3x the stop distance away.
    """
    margin = cfg.margin_for_signal(state.wallet, signal.confidence,
                                   committed_margin(state.positions))
    if margin <= 0:
        return None

    # Leverage may scale with confidence too, bounded by volatility, so the
    # margin has to be re-capped against total exposure afterwards.
    leverage = cfg.leverage_for_signal(signal.confidence, atr_pct)

    # Then lowered until being stopped out costs what the risk budget allows.
    # The stop is placed by the market — the signal derives it from volatility
    # — so leaving leverage fixed would make the cost of a failed trade a
    # function of how noisy the hour happened to be. Pinning the loss and
    # letting the leverage move puts that the right way round.
    costs = stop_out_costs(signal.symbol, cfg)
    leverage = cfg.leverage_for_stop(leverage, signal.current_price, signal.stop_loss, costs)

    if protect is not None and protect.enabled:
        from analysis.protections import max_leverage_for_liquidation, stop_too_tight
        spec = spec_for(signal.symbol)
        if stop_too_tight(signal.current_price, signal.stop_loss,
                          fees_for(signal.symbol).round_trip_pct(), protect):
            log.info("paper.rejected", symbol=signal.symbol, reason="stop_inside_fees")
            return None
        cap = max_leverage_for_liquidation(signal.current_price, signal.stop_loss,
                                           spec.maintenance_margin_pct, protect)
        if cap is not None:
            if cap < 1.0:
                log.info("paper.rejected", symbol=signal.symbol, reason="liquidation_too_near")
                return None
            leverage = min(leverage, cap)

    # At the leverage floor a wide stop can still cost more than the budget.
    # Then the position shrinks instead, so the rupees at risk stay at the
    # budget share of the margin that was intended.
    if signal.current_price > 0 and signal.stop_loss > 0:
        stop_move = abs(signal.current_price - signal.stop_loss) / signal.current_price
        loss_frac = leverage * (stop_move + costs)
        if loss_frac > cfg.max_loss_pct_of_margin > 0:
            margin *= cfg.max_loss_pct_of_margin / loss_frac

    margin = cfg.cap_margin_to_notional(margin, leverage, state.wallet)
    if margin <= 0:
        return None

    spec = spec_for(signal.symbol)
    # The signal already decided where this setup is wrong and what it is
    # worth, from the cost floor and the market's own volatility. Recomputing
    # that from a margin-risk budget throws all of it away and leaves the
    # paper book testing something the engine never proposed.
    pos = open_position(
        symbol=signal.symbol,
        side=Side.LONG if signal.direction == "long" else Side.SHORT,
        entry_price=signal.current_price,
        margin=margin,
        leverage=leverage,
        fees=fees_for(signal.symbol),
        stop_pct_of_margin=cfg.stop_pct_of_margin,
        reward_risk=cfg.reward_risk,
        stop_price=signal.stop_loss,
        target_price=signal.target_price,
        opened_at=now,
        signal_type=signal.signal_type,
        timeframe=signal.timeframe,
        confidence=signal.confidence,
        expires_at=now + timedelta(minutes=_hold_minutes(signal, cfg)),
        usdt_inr=usdt_inr,
        lot_step=spec.lot_step,
        slippage=cfg.slippage,
    )
    if pos.coin_qty <= 0:
        log.info("paper.rejected", symbol=signal.symbol, reason="below_one_lot")
        return None
    if not cfg.is_target_viable(pos.entry_price, pos.target_price):
        log.info("paper.rejected", symbol=signal.symbol, reason="target_not_viable")
        return None
    return pos


def resolve_at_price(
    pos: Position,
    price: float,
    now: datetime,
    cfg: CycleConfig,
    wallet: float,
) -> ClosedTrade | None:
    """
    Close the position if this tick price hits a level. None means it survives.

    High and low are both the tick price: a poller sees prices, not bars.
    """
    hit = resolve_candle(pos, high=price, low=price, close=price, ts=now,
                         slippage=cfg.slippage)
    if hit is None:
        fees = fees_for(pos.symbol)
        # Ladder first: it can only raise the stop, and a rung crossed on this
        # tick should protect the position from the next one onward.
        pos.apply_ladder(price, cfg.ladder, fees)
        pos.update_trail(price, price, cfg.trailing, fees)
        return None
    reason, fill = hit
    return close_position(pos, fill, reason, now, fees_for(pos.symbol), wallet)


def cycle_outcome(wallet: float, cfg: CycleConfig, open_count: int) -> str | None:
    """
    Has the cycle finished? Returns a status, or None to keep running.

    Busting is checked against free margin rather than the wallet reaching
    exactly zero: once there is not enough left to open the smallest allowed
    position, the cycle cannot recover and should stop rather than idle.
    """
    if wallet >= cfg.target_wallet:
        return "hit_target"
    if open_count == 0 and wallet < cfg.min_margin:
        return "busted"
    return None


def recompute_liquidation(pos: Position) -> float:
    """Re-derive liquidation after a scale-in changed the average entry."""
    return liquidation_price(pos.entry_price, pos.side, pos.effective_leverage,
                             fees_for(pos.symbol).maintenance_margin_pct)


def summarise(trades: list, wallet: float, cfg: CycleConfig) -> dict:
    """Scorecard for one cycle. Costs stay broken out, never netted away."""
    n = len(trades)
    wins = [t for t in trades if t.net_pnl > 0]
    losses = [t for t in trades if t.net_pnl <= 0]
    gross = sum(t.gross_pnl for t in trades)
    fees = sum(t.trading_fees for t in trades)
    funding = sum(t.funding_paid for t in trades)
    net = sum(t.net_pnl for t in trades)

    avg_win = sum(t.net_pnl for t in wins) / len(wins) if wins else 0.0
    avg_loss = sum(t.net_pnl for t in losses) / len(losses) if losses else 0.0

    streak = worst = 0
    for t in trades:
        streak = streak + 1 if t.net_pnl <= 0 else 0
        worst = max(worst, streak)

    by_reason: dict[str, int] = {}
    for t in trades:
        by_reason[t.exit_reason] = by_reason.get(t.exit_reason, 0) + 1

    return {
        "trades": n,
        "wins": len(wins),
        "win_rate_pct": round(len(wins) / n * 100, 1) if n else 0.0,
        "wallet": round(wallet, 2),
        "gross_pnl": round(gross, 2),
        "trading_fees": round(fees, 2),
        "funding_paid": round(funding, 2),
        "net_pnl": round(net, 2),
        # The number that says whether costs are the problem.
        "costs_as_pct_of_gross": round((fees + funding) / gross * 100, 1) if gross > 0 else None,
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "realised_reward_risk": round(abs(avg_win / avg_loss), 2) if avg_loss else None,
        "expectancy_per_trade": round(net / n, 2) if n else 0.0,
        "longest_losing_streak": worst,
        "exits_by_reason": by_reason,
        "break_even_move_pct": round(cfg.break_even_move_pct(), 4),
    }


def now_utc() -> datetime:
    return datetime.now(UTC)


__all__ = [
    "CycleState",
    "ExitReason",
    "config_for_cycle",
    "committed_margin",
    "cycle_outcome",
    "fees_for",
    "now_utc",
    "open_from_signal",
    "recompute_liquidation",
    "resolve_at_price",
    "should_open",
    "summarise",
]


class ClosedTradeView:
    """
    One flat object with everything a post-mortem needs.

    A ClosedTrade knows the exit and the money; the stored row knows the plan
    it was opened against. The reviewer needs both in one place, and neither
    class should grow a dependency on the other to provide it.
    """

    __slots__ = ("symbol", "side", "entry_price", "exit_price", "target_price",
                 "stop_price", "exit_reason", "signal_type", "confidence",
                 "hours_held", "gross_pnl", "trading_fees", "funding_paid",
                 "net_pnl", "return_on_margin")

    def __init__(self, trade, row):
        self.symbol = row.symbol
        self.side = row.side
        self.entry_price = row.entry_price
        self.exit_price = trade.exit_price
        self.target_price = row.target_price
        self.stop_price = row.initial_stop_price or row.stop_price
        self.exit_reason = trade.reason.value
        self.signal_type = row.signal_type or ""
        self.confidence = row.confidence or 0.0
        self.hours_held = trade.hours_held
        self.gross_pnl = trade.gross_pnl
        self.trading_fees = trade.fees_paid - trade.funding_paid
        self.funding_paid = trade.funding_paid
        self.net_pnl = trade.net_pnl
        self.return_on_margin = trade.return_on_margin
