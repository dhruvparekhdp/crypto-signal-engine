"""
The protections every serious bot runs, and this engine did not.

From the research (Freqtrade's StoplossGuard / CooldownPeriod / MaxDrawdown,
and standard risk practice), applied before a new position opens:

  session        trade 07:00-17:00 UTC on weekdays only, when moves are big
                 enough to clear fees (BTC volume peaks 13-15 UTC; weekends
                 and the Asian night are quiet)
  daily loss     stop opening for the rest of the UTC day once today's
                 closed trades lost `daily_loss_pct` of the day's start wallet
  losing streak  3 losses in a row -> 2 hours off; 5 in a row -> rest of day
  pair cooldown  no re-entry in a symbol for 15 minutes after closing it
  correlation    at most 2 crypto positions in the same direction: every
                 alt is BTC beta, so two longs are already one big BTC long
  stop floor     the stop must be at least 1.5x the round-trip cost away,
                 or fees alone are most of the risk

`max_leverage_for_liquidation` is the other half: leverage is capped so the
liquidation price sits at least 3x the stop distance from entry, because
gaps and cascades go through stops.

Pure functions; the runner hands in the recent trades and open positions.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True)
class ProtectionConfig:
    enabled: bool = True
    session_filter: bool = True
    session_start_utc: int = 7
    session_end_utc: int = 17
    weekdays_only: bool = True
    daily_loss_pct: float = 3.0
    streak_pause_after: int = 3
    streak_pause_minutes: int = 120
    streak_stop_day_after: int = 5
    pair_cooldown_minutes: int = 15
    consecutive_loss_cooldown_minutes: int = 60
    anti_flip_cooldown_minutes: int = 90
    max_same_direction: int = 2
    min_stop_cost_multiple: float = 1.5
    liquidation_stop_multiple: float = 3.0


@dataclass(frozen=True)
class RecentTrade:
    symbol: str
    closed_at: datetime
    net_pnl: float
    direction: str = ""


def in_session(now: datetime, cfg: ProtectionConfig) -> bool:
    if cfg.weekdays_only and now.weekday() >= 5:
        return False
    return cfg.session_start_utc <= now.hour < cfg.session_end_utc


def losing_streak(trades: list[RecentTrade]) -> tuple[int, datetime | None]:
    """Consecutive losses ending with the latest trade, and when the last one closed."""
    ordered = sorted(trades, key=lambda t: t.closed_at, reverse=True)
    n = 0
    for t in ordered:
        if t.net_pnl < 0:
            n += 1
        else:
            break
    return n, (ordered[0].closed_at if ordered else None)


def check_entry(now: datetime, symbol: str, direction: str, trades: list[RecentTrade],
                day_start_wallet: float, open_positions: list, cfg: ProtectionConfig,
                is_crypto: bool = True, sentiment_score: float = 0.0) -> tuple[bool, str]:
    """(ok, reason). `now` and the trade times are naive UTC."""
    if not cfg.enabled:
        return True, "ok"
    if cfg.session_filter and not in_session(now, cfg):
        return False, "outside_session"

    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today = [t for t in trades if t.closed_at >= day_start]
    lost_today = -sum(t.net_pnl for t in today)
    if day_start_wallet > 0 and lost_today >= day_start_wallet * cfg.daily_loss_pct / 100.0:
        return False, "daily_loss_limit"

    streak, last = losing_streak(today)
    if streak >= cfg.streak_stop_day_after:
        return False, "losing_streak_day_over"
    if (streak >= cfg.streak_pause_after and last is not None
            and now - last < timedelta(minutes=cfg.streak_pause_minutes)):
        return False, "losing_streak_pause"

    # Adaptive Pair Cooldown: 2 consecutive stop-outs pause symbol for 60m-90m (up to 120m-180m if sentiment hostile)
    sym_trades = sorted([t for t in trades if t.symbol.lower() == symbol.lower()],
                        key=lambda t: t.closed_at, reverse=True)
    sym_streak = 0
    sym_last = None
    for t in sym_trades:
        if t.net_pnl < 0:
            sym_streak += 1
            if sym_last is None:
                sym_last = t.closed_at
        else:
            break

    if sym_streak >= 2 and sym_last is not None:
        cooldown_mins = getattr(cfg, "consecutive_loss_cooldown_minutes", 60)
        hostile = (direction.lower() == "long" and sentiment_score < -0.2) or \
                  (direction.lower() == "short" and sentiment_score > 0.2)
        if hostile:
            cooldown_mins = int(cooldown_mins * 1.5)
        if now - sym_last < timedelta(minutes=cooldown_mins):
            remaining_mins = max(1, int((timedelta(minutes=cooldown_mins) - (now - sym_last)).total_seconds() / 60))
            return False, f"adaptive_loss_cooldown_{remaining_mins}m_rem"

    for t in trades:
        if (t.symbol.lower() == symbol.lower()
                and now - t.closed_at < timedelta(minutes=cfg.pair_cooldown_minutes)):
            return False, "pair_cooldown"

    # Smart Anti-Flip Directional Guard
    last_dir = None
    last_time = None
    
    for t in trades:
        if t.symbol.lower() == symbol.lower() and getattr(t, "direction", ""):
            if last_time is None or t.closed_at > last_time:
                last_time = t.closed_at
                last_dir = t.direction.lower()

    for p in open_positions:
        p_sym = str(getattr(p, "symbol", ""))
        if p_sym.lower() == symbol.lower():
            opened_at = getattr(p, "opened_at", getattr(p, "timestamp", getattr(p, "created_at", None)))
            p_side = str(getattr(p.side, "value", p.side)).lower()
            if opened_at is not None and (last_time is None or opened_at > last_time):
                last_time = opened_at
                last_dir = p_side

    if last_dir and last_dir != direction.lower() and last_time is not None:
        mins_since = (now - last_time).total_seconds() / 60
        cooldown_mins = getattr(cfg, "anti_flip_cooldown_minutes", 90)
        if mins_since < cooldown_mins:
            flip_allowed = False
            if abs(sentiment_score) > 0.3:
                flip_allowed = True
            else:
                try:
                    from analysis.event_calendar import EVENTS_2026
                    for ev in EVENTS_2026:
                        gap_mins = (now - ev.at).total_seconds() / 60
                        if 0 <= gap_mins <= 30:
                            lvl = {"fomc": 5 if "projections" in ev.name.lower() else 4, "cpi": 4, "nfp": 4}.get(ev.kind, 3)
                            if lvl >= 4:
                                flip_allowed = True
                                break
                except ImportError:
                    pass
            
            if not flip_allowed:
                return False, "anti_flip_directional_cooldown"

    if is_crypto and cfg.max_same_direction > 0:
        from analysis.instruments import spec_for
        same = sum(1 for p in open_positions
                   if str(getattr(p.side, "value", p.side)).lower() == direction.lower()
                   and spec_for(p.symbol).kind == "crypto")
        if same >= cfg.max_same_direction:
            return False, "correlated_exposure"
    return True, "ok"


def stop_too_tight(entry: float, stop: float, round_trip: float,
                   cfg: ProtectionConfig) -> bool:
    """A stop closer than 1.5x the round-trip cost is mostly paying fees."""
    if entry <= 0 or stop <= 0:
        return False
    return abs(entry - stop) / entry < cfg.min_stop_cost_multiple * round_trip


def max_leverage_for_liquidation(entry: float, stop: float, mm_pct: float,
                                 cfg: ProtectionConfig) -> float | None:
    """
    Highest leverage that keeps liquidation >= `liquidation_stop_multiple`
    times the stop distance from entry. Liquidation distance is about
    1/L - mm, so L <= 1 / (k * stop_move + mm). None when there is no stop.
    """
    if entry <= 0 or stop <= 0 or stop == entry:
        return None
    stop_move = abs(entry - stop) / entry
    return 1.0 / (cfg.liquidation_stop_multiple * stop_move + mm_pct)
