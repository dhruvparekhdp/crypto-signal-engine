"""
Swing book: the strategies that survived the 5-year test, traded live on closed 4h bars.

Why this exists. The intraday book traded 15m signals with stops of 0.3-0.7% of price while a round
trip costs about 0.17%, so fees took a third to a half of the risk on every trade before price moved.
Five years of 1m-resolution backtests (analysis.lab) found no 15m strategy that survives costs, while
four 4h breakout strategies did: positive after costs, beat random entries (null test), held up on
unseen data (walk-forward) and with 2.5x worse slippage. This module runs exactly those, with exactly
the tested exits:

    entry   the strategy fires on a CLOSED 4h bar; the trade opens at the next price
    stop    3 x ATR(14) of the 4h bars, never closer than 0.3% (lab ExitModel.min_stop_pct)
    target  3 x the stop distance
    limit   7 days (the backtest showed removing it lowers profit and deepens drawdowns)

No profit lock, trailing stop, stagnation exit, breakeven ratchet or AI early close touches these
positions: none of them were in the test, and the AI early close lost on every live trade it made.

Signals come from analysis.lab.strategies, the same code the backtest ran, so live and backtest
cannot drift apart (tests/test_swing_book.py checks they agree bar for bar).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import numpy as np

from analysis.crypto_signal import CryptoSignal
from analysis.lab import features as F
from analysis.lab.data import INTERVAL_MS, Bars
from analysis.lab.strategies import REGISTRY

FAPI_KLINES = "https://fapi.binance.com/fapi/v1/klines"
TF = "4h"
TF_MS = INTERVAL_MS[TF]
STOP_ATR = 3.0
REWARD_RISK = 3.0
MIN_STOP_PCT = 0.003
MAX_STOP_PCT = 0.08
DEFAULT_SPECS = ("4h@vol_breakout:z=3.0,4h@keltner_break:k=2.5,4h@donchian:n=100,4h@ichimoku,"
                 "8h@keltner_break:k=2.0,8h@vol_breakout:z=3.0,8h@donchian:n=100,8h@ichimoku")
# 8h joined after its own null test (all four beat random entries, p < 0.04) and 2.5x slippage stress.
# 12h is not live: Ichimoku failed its null test there (p = 0.17).


def parse_specs(text: str) -> list[tuple[str, str, dict]]:
    """'4h@vol_breakout:z=3.0,8h@ichimoku' -> [('4h', 'vol_breakout', {'z': 3.0}), ('8h', 'ichimoku', {})],
    in priority order. A spec without '<tf>@' is 4h."""
    out = []
    for part in [p.strip() for p in (text or "").split(",") if p.strip()]:
        tf, at, rest_ = part.partition("@")
        if not at:
            tf, rest_ = TF, part
        sid, _, rest = rest_.partition(":")
        params = {}
        for kv in [x for x in rest.split(";") if x]:
            k, _, v = kv.partition("=")
            try:
                params[k] = int(v) if v.isdigit() else float(v)
            except ValueError:
                params[k] = v
        if sid in REGISTRY and tf in INTERVAL_MS:
            out.append((tf, sid, params))
    return out


def timeframes(specs) -> list[str]:
    return sorted({tf for tf, _, _ in specs}, key=lambda t: INTERVAL_MS[t])


def bars_from_klines(symbol: str, rows: list, now_ms: int, tf: str = TF) -> Bars | None:
    """Binance kline rows -> Bars of CLOSED bars only (the forming bar is dropped)."""
    rows = [r for r in rows if int(r[0]) + INTERVAL_MS[tf] <= now_ms]
    if len(rows) < 150:
        return None
    a = np.array([[float(r[i]) for i in (0, 1, 2, 3, 4, 5, 9)] for r in rows])
    return Bars(symbol.upper(), tf, a[:, 0].astype(np.int64), a[:, 1], a[:, 2], a[:, 3], a[:, 4], a[:, 5], a[:, 6])


async def fetch_bars(client, symbol: str, now_ms: int, tf: str = TF, limit: int = 500) -> Bars | None:
    r = await client.get(FAPI_KLINES, params={"symbol": symbol.upper(), "interval": tf, "limit": limit}, timeout=15)
    r.raise_for_status()
    return bars_from_klines(symbol, r.json(), now_ms, tf)


@dataclass
class SwingSetup:
    symbol: str
    strategy: str
    side: int                 # +1 long, -1 short
    bar_open_ms: int          # the closed bar the signal fired on
    atr: float
    close: float
    tf: str = TF


def evaluate(b: Bars, specs) -> SwingSetup | None:
    """The first strategy (in priority order) for this bar size that fires on the LAST closed bar, or None.
    `specs` items are (tf, strategy, params); only those matching b.interval are used."""
    atr = float(F.atr(b.h, b.l, b.c)[-1])
    if not atr > 0:
        return None
    for tf, sid, params in specs:
        if tf != b.interval:
            continue
        s = int(REGISTRY[sid].signals(b, params)[-1])
        if s != 0:
            return SwingSetup(b.symbol, sid, s, int(b.t[-1]), atr, float(b.c[-1]), b.interval)
    return None


def levels(entry: float, side: int, atr: float) -> tuple[float, float] | None:
    """Stop and target for an entry, exactly as the backtest placed them. None if the stop would be too wide."""
    dist = max(STOP_ATR * atr, MIN_STOP_PCT * entry)
    if dist > MAX_STOP_PCT * entry:
        return None
    return entry - side * dist, entry + side * REWARD_RISK * dist


def to_signal(setup: SwingSetup, price: float, now: datetime) -> CryptoSignal | None:
    lv = levels(price, setup.side, setup.atr)
    if lv is None:
        return None
    stop, target = lv
    stop_pct = abs(price - stop) / price * 100
    return CryptoSignal(
        symbol=setup.symbol.lower(), signal_type=f"swing_{setup.strategy}",
        direction="long" if setup.side > 0 else "short",
        trigger_description=(f"{REGISTRY[setup.strategy].name} fired on the {setup.tf} bar closed at "
                             f"{datetime.fromtimestamp((setup.bar_open_ms + INTERVAL_MS[setup.tf]) / 1000, UTC):%Y-%m-%d %H:%M} UTC; "
                             f"stop 3xATR ({stop_pct:.2f}%), target 3R, 7-day limit"),
        confidence=0.8, current_price=price, target_price=target, stop_loss=stop,
        edge_pct=0.0, stake_pct=0.0, timeframe=setup.tf, sentiment_score=0.0,
        indicators_summary=f"atr{setup.tf}={setup.atr:.6g} bar_close={setup.close:.6g}",
        timestamp=now, trade_mode="swing", leverage_suggested=1.0)


def adaptive_risk(base: float, dd: float, losing_streak: int,
                  floor: float = 0.25, streak_brake: int = 3) -> float:
    """Risk per trade as a share of the wallet: base risk, cut to 75% in a 10% drawdown and 50% in a 20%
    drawdown, halved again after `streak_brake` losses in a row. Mirrors analysis.lab.wallet's adaptive
    mode, which was backtested. `dd` is the swing book's own drawdown, not the old intraday book's."""
    f = 0.5 if dd >= 0.20 else 0.75 if dd >= 0.10 else 1.0
    if losing_streak >= streak_brake:
        f *= 0.5
    return base * max(f, floor)


def size(wallet: float, free: float, risk_pct: float, entry: float, stop: float, costs: float,
         max_leverage: float) -> tuple[float, float] | None:
    """(margin, leverage) so that a stop-out costs risk_pct of the wallet, at the lowest leverage the free
    margin allows (capped at max_leverage). None if it cannot be funded."""
    move = abs(entry - stop) / entry + max(costs, 0.0)
    if move <= 0 or wallet <= 0:
        return None
    notional = wallet * risk_pct / move
    usable = free * 0.9
    if usable <= 0:
        return None
    leverage = min(max(1.0, notional / usable), max_leverage)
    margin = notional / leverage
    if margin > usable:
        margin = usable
    return margin, leverage


def book_state(closed: list) -> tuple[float, int]:
    """(drawdown fraction, current losing streak) from the swing book's closed trades, oldest first.
    Each item needs `net_pnl` and `wallet_after`."""
    cum = peak = 0.0
    streak = 0
    dd = 0.0
    for t in closed:
        cum += t.net_pnl
        peak = max(peak, cum)
        streak = streak + 1 if t.net_pnl <= 0 else 0
        equity_at_peak = t.wallet_after + (peak - cum)
        dd = (peak - cum) / equity_at_peak if equity_at_peak > 0 else 0.0
    return dd, streak


# Backtest R per trade after costs (5 years, scripts/mirror_analysis.py), used only to choose between signals
# that arrive together: the open slots go to the strongest strategy and coin first.
STRATEGY_R = {("vol_breakout", "4h"): 0.254, ("donchian", "4h"): 0.153, ("keltner_break", "4h"): 0.145,
              ("ichimoku", "4h"): 0.131, ("keltner_break", "8h"): 0.294, ("vol_breakout", "8h"): 0.293,
              ("donchian", "8h"): 0.283, ("ichimoku", "8h"): 0.191}
COIN_R = {"SOL": 0.440, "LINK": 0.392, "DOGE": 0.328, "SUI": 0.298, "ADA": 0.272, "ETH": 0.215, "BNB": 0.206,
          "AVAX": 0.190, "BTC": 0.112, "XRP": 0.099, "LTC": 0.003, "BCH": -0.016}


def priority(signal_type: str, timeframe: str, symbol: str) -> float:
    """Higher = take first. Unknown strategies or coins rank at the book's average."""
    strat = signal_type.replace("swing_", "")
    coin = symbol.upper().replace("USDT", "")
    return STRATEGY_R.get((strat, timeframe), 0.2) + COIN_R.get(coin, 0.2)
