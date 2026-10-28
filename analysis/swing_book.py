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


class SpecError(ValueError):
    """The swing strategy setting has a mistake. Raised (or reported) instead of silently dropping a strategy."""


def _split(part: str) -> tuple[str, str, dict]:
    tf, at, rest_ = part.partition("@")
    if not at:
        tf, rest_ = TF, part
    sid, _, rest = rest_.partition(":")
    params = {}
    for kv in [x for x in rest.split(";") if x]:
        k, _, v = kv.partition("=")
        try:
            params[k.strip()] = int(v) if v.strip().isdigit() else float(v)
        except ValueError:
            params[k.strip()] = v.strip()
    return tf.strip().lower(), sid.strip(), params


def spec_problems(text: str) -> list[str]:
    """Every mistake in a swing strategy setting, in plain words. Empty list = valid."""
    parts = [p.strip() for p in (text or "").split(",") if p.strip()]
    if not parts:
        return ["no swing strategies configured"]
    out = []
    for part in parts:
        tf, sid, params = _split(part)
        if sid not in REGISTRY:
            out.append(f"'{part}': unknown strategy '{sid}'")
            continue
        if tf not in INTERVAL_MS:
            out.append(f"'{part}': unknown timeframe '{tf}'")
        st = REGISTRY[sid]
        allowed = set(st.defaults) | set(st.grid) | {"warmup"}
        for k, v in params.items():
            if k not in allowed:
                out.append(f"'{part}': '{sid}' has no setting '{k}' (it has: {', '.join(sorted(allowed))})")
            elif isinstance(v, str):
                out.append(f"'{part}': '{k}={v}' is not a number")
    return out


def parse_specs(text: str, strict: bool = False) -> list[tuple[str, str, dict]]:
    """'4h@vol_breakout:z=3.0,8h@ichimoku' -> [('4h', 'vol_breakout', {'z': 3.0}), ('8h', 'ichimoku', {})],
    in priority order. A spec without '<tf>@' is 4h; timeframes are case-insensitive.
    strict=True raises SpecError listing every mistake; otherwise invalid entries are left out (callers that trade
    must use strict, or check spec_problems first: a typo must never silently change a live strategy)."""
    problems = spec_problems(text)
    if strict and problems:
        raise SpecError("; ".join(problems))
    out = []
    for part in [p.strip() for p in (text or "").split(",") if p.strip()]:
        tf, sid, params = _split(part)
        if sid in REGISTRY and tf in INTERVAL_MS and not any(f"'{part}'" in p for p in problems):
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


def bars_from_delta(symbol: str, rows: list, now_ms: int, tf: str = TF) -> Bars | None:
    """Delta /v2/history/candles rows -> Bars of CLOSED bars only."""
    iv = INTERVAL_MS[tf]
    closed = []
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict):
            continue
        try:
            t = int(r["time"])
            if t > 1e12:
                t //= 1000
            open_ms = t * 1000
            if open_ms + iv > now_ms:
                continue
            closed.append([
                open_ms, float(r["open"]), float(r["high"]), float(r["low"]),
                float(r["close"]), float(r.get("volume") or 0.0), 0.0,  # no taker-buy
            ])
        except (KeyError, TypeError, ValueError):
            continue
    closed.sort(key=lambda x: x[0])
    if len(closed) < 150:
        return None
    a = np.array(closed)
    return Bars(symbol.upper(), tf, a[:, 0].astype(np.int64), a[:, 1], a[:, 2], a[:, 3],
                a[:, 4], a[:, 5], a[:, 6])


def _aggregate_delta_rows(rows: list, src_secs: int, dst_secs: int) -> list:
    """Merge lower-TF Delta candles into dst_secs bars (UTC-aligned). Delta has no 8h."""
    if dst_secs <= src_secs or dst_secs % src_secs:
        return list(rows) if isinstance(rows, list) else []
    buckets: dict[int, dict] = {}
    # Delta answers NEWEST FIRST. Without this sort each 8h bar took its open from the later 4h half and its close from
    # the earlier one (18 Oct 2026 review) - every 8h close-based signal read the wrong close.
    ordered = sorted((r for r in (rows if isinstance(rows, list) else []) if isinstance(r, dict) and "time" in r),
                     key=lambda r: float(r["time"]))
    for r in ordered:
        try:
            t = int(r["time"])
            if t > 1e12:
                t //= 1000
            open_t = (t // dst_secs) * dst_secs
            o, h, l, c = float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"])
            v = float(r.get("volume") or 0.0)
        except (KeyError, TypeError, ValueError):
            continue
        b = buckets.get(open_t)
        if b is None:
            buckets[open_t] = {"time": open_t, "open": o, "high": h, "low": l, "close": c, "volume": v,
                               "_n": 1}
        else:
            b["high"] = max(b["high"], h)
            b["low"] = min(b["low"], l)
            b["close"] = c
            b["volume"] += v
            b["_n"] += 1
    need = dst_secs // src_secs
    out = []
    for t in sorted(buckets):
        b = buckets[t]
        if b["_n"] < need:
            continue  # incomplete bucket
        out.append({k: b[k] for k in ("time", "open", "high", "low", "close", "volume")})
    return out


async def fetch_bars(client, symbol: str, now_ms: int, tf: str = TF, limit: int = 500) -> Bars | None:
    """Fetch closed bars. Delta India first when delta_only_mode is on (live money is on Delta); if Delta errors or
    returns nothing, Binance USD-M (owner, 17 Oct 2026: Delta's candle history answered 502 for every coin)."""
    from config.settings import settings
    if getattr(settings, "delta_only_mode", False):
        try:
            bars = await _fetch_bars_delta(client, symbol, now_ms, tf, limit)
            if bars is not None and len(bars.c) > 0:
                return await with_binance_volume(client, bars, symbol, tf, limit)
            why = "empty"
        except Exception as e:  # noqa: BLE001 - fall back below
            why = f"{type(e).__name__}: {str(e)[:80]}"
        import structlog
        structlog.get_logger().warning("swing_bars_binance_fallback", symbol=symbol, tf=tf, delta_error=why)
    r = await client.get(FAPI_KLINES, params={"symbol": symbol.upper(), "interval": tf, "limit": limit},
                         timeout=15)
    r.raise_for_status()
    return bars_from_klines(symbol, r.json(), now_ms, tf)


async def with_binance_volume(client, bars: Bars, symbol: str, tf: str, limit: int) -> Bars:
    """Delta prices with Binance USD-M volume. 18 Oct 2026 venue check over 900 recent bars x 10 coins: Delta and Binance
    closes differ by a median 0.003-0.13% and donchian/ichimoku/keltner fire on the same bars 80-99% of the time, but
    vol_breakout (a volume z-score, backtested on Binance volume) agreed only 36-41% on Delta's thin volume - a different
    strategy. So participation is measured on the deep market; prices, levels and orders stay Delta's. If Binance does
    not cover every bar, Delta's own volume is kept whole (two scales are never mixed in one z-score)."""
    try:
        from collectors.binance_klines import venue_symbol
        r = await client.get(FAPI_KLINES, params={"symbol": venue_symbol(symbol), "interval": tf, "limit": min(limit + 10, 1500)},
                             timeout=15)
        r.raise_for_status()
        vol = {int(k[0]): float(k[5]) for k in r.json()}
    except Exception as e:  # noqa: BLE001 - Delta volume is the fallback
        import structlog
        structlog.get_logger().info("swing_binance_volume_unavailable", symbol=symbol, tf=tf, error=str(e)[:80])
        return bars
    if not all(int(t) in vol for t in bars.t):
        return bars
    return Bars(bars.symbol, bars.interval, bars.t, bars.o, bars.h, bars.l, bars.c,
                np.array([vol[int(t)] for t in bars.t], dtype=float), bars.tb)


async def _fetch_bars_delta(client, symbol: str, now_ms: int, tf: str, limit: int) -> Bars | None:
    from config.settings import settings
    from collectors.delta_market import TF_SECONDS, TF_TO_RES
    from execution.delta_india import INDIA_URL, USER_AGENT, binance_to_delta_symbol
    # Delta India has no 8h — pull 4h and aggregate (UTC 00/08/16).
    native_tf = "4h" if tf == "8h" else tf
    res = TF_TO_RES.get(native_tf, native_tf)
    secs = TF_SECONDS.get(res, INTERVAL_MS[native_tf] // 1000)
    fetch_limit = min(limit * (2 if tf == "8h" else 1), 2000)
    end = now_ms // 1000
    start = end - secs * fetch_limit
    base = (getattr(settings, "delta_india_base_url", None) or INDIA_URL).rstrip("/")
    r = await client.get(
        f"{base}/v2/history/candles",
        params={"symbol": binance_to_delta_symbol(symbol), "resolution": res,
                "start": start, "end": end},
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        timeout=15,
    )
    r.raise_for_status()
    body = r.json()
    rows = body.get("result") if isinstance(body, dict) else body
    if tf == "8h":
        rows = _aggregate_delta_rows(rows or [], src_secs=4 * 3600, dst_secs=8 * 3600)
    return bars_from_delta(symbol, rows or [], now_ms, tf)


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
    sig = CryptoSignal(
        symbol=setup.symbol.lower(), signal_type=f"swing_{setup.strategy}",
        direction="long" if setup.side > 0 else "short",
        trigger_description=(f"{REGISTRY[setup.strategy].name} fired on the {setup.tf} bar closed at "
                             f"{datetime.fromtimestamp((setup.bar_open_ms + INTERVAL_MS[setup.tf]) / 1000, UTC):%Y-%m-%d %H:%M} UTC; "
                             f"stop 3xATR ({stop_pct:.2f}%), target 3R, 7-day limit"),
        confidence=0.8, current_price=price, target_price=target, stop_loss=stop,
        edge_pct=0.0, stake_pct=0.0, timeframe=setup.tf, sentiment_score=0.0,
        indicators_summary=f"atr{setup.tf}={setup.atr:.6g} bar_close={setup.close:.6g}",
        timestamp=now, trade_mode="swing", leverage_suggested=1.0)
    # roadmap Q-9: the backtest fills at the first price after this bar's close; keep it to measure the shortfall
    sig.bar_close_price = setup.close
    sig.bar_close_ms = setup.bar_open_ms + INTERVAL_MS[setup.tf]
    return sig


def shortfall(side: int, entry: float, bar_close: float, bar_close_ms: int, now_ms: int) -> tuple[float, float]:
    """(bps worse than the backtest's fill at the bar close, minutes after the close). Positive bps = paid more."""
    bps = side * (entry - bar_close) / bar_close * 1e4 if bar_close > 0 else 0.0
    return bps, (now_ms - bar_close_ms) / 60_000


def adaptive_risk(base: float, dd: float, losing_streak: int,
                  floor: float = 0.25, streak_brake: int = 3) -> float:
    """Risk per trade as a share of the wallet: base risk, cut to 75% in a 10% drawdown and 50% in a 20%
    drawdown, halved again after `streak_brake` losses in a row. Mirrors analysis.lab.wallet's adaptive
    mode, which was backtested. `dd` is the swing book's own drawdown, not the old intraday book's."""
    f = 0.5 if dd >= 0.20 else 0.75 if dd >= 0.10 else 1.0
    if losing_streak >= streak_brake:
        f *= 0.5
    return base * max(f, floor)


def risk_for(signal_type: str, timeframe: str, dd: float, streak: int, trusted: bool = False) -> float:
    """Risk per trade between swing_risk_min and swing_risk_pct (owner, 7 Oct 2026): cut in book drawdowns and
    after losing streaks (adaptive_risk), halved for specs whose random-entry test was inconclusive, never below
    the minimum. Backtested as variant F in scripts/wallet_plan_backtest."""
    from config.settings import settings
    top = settings.swing_risk_pct
    risk = adaptive_risk(top, dd, streak) if settings.swing_adaptive_risk else top
    key = f"{timeframe}@{signal_type.removeprefix('swing_')}"
    weak = {k.strip() for k in settings.swing_weak_specs.split(",") if k.strip()}
    if key in weak and not trusted:          # a weak spec that earned "trusted" on forward results gets full risk
        risk *= settings.swing_weak_spec_factor
    return max(risk, min(settings.swing_risk_min, top))


def risk_reason(signal_type: str, timeframe: str, dd: float, streak: int, trusted: bool = False) -> str:
    """Plain-English why for the trade card (roadmap F-5)."""
    from config.settings import settings
    parts = []
    if settings.swing_adaptive_risk and dd >= 0.10:
        parts.append(f"book {dd * 100:.0f}% below its peak")
    if settings.swing_adaptive_risk and streak >= 3:
        parts.append(f"{streak} losses in a row")
    key = f"{timeframe}@{signal_type.removeprefix('swing_')}"
    if key in {k.strip() for k in settings.swing_weak_specs.split(",")} and not trusted:
        parts.append("strategy not yet proven vs random (half risk)")
    r = risk_for(signal_type, timeframe, dd, streak, trusted)
    if r <= settings.swing_risk_min + 1e-12 and parts:
        parts.append("floor reached")
    return "full risk" if not parts else ", ".join(parts)


def size(wallet: float, free: float, risk_pct: float, entry: float, stop: float, costs: float,
         max_leverage: float, max_margin_frac: float = 0.0, min_leverage: float = 0.0) -> tuple[float, float] | None:
    """(margin, leverage) so that a stop-out costs risk_pct of the wallet, at the lowest leverage the free
    margin allows (capped at max_leverage). None if it cannot be funded.

    max_margin_frac > 0 caps one trade's margin at that share of the wallet, so one trade can no longer lock the
    whole wallet (7 Oct 2026: a 1x SOL trade took Rs1,238 of Rs1,735 and every later signal was skipped).
    Leverage rises to fit the cap; the stop-out risk is unchanged, only less margin is tied up.

    min_leverage > 0 uses the live Delta rule instead of the lowest leverage (owner, 18 Oct 2026: "1.6x makes no
    sense"): the highest leverage whose liquidation is >= 2x the stop distance away, at least min_leverage. The
    stop-out loss in rupees is the same at any leverage; only the margin locked (and the liquidation distance) changes."""
    move = abs(entry - stop) / entry + max(costs, 0.0)
    if move <= 0 or wallet <= 0:
        return None
    notional = wallet * risk_pct / move
    usable = free * 0.9
    if max_margin_frac > 0:
        usable = min(usable, wallet * max_margin_frac)
    if usable <= 0:
        return None
    leverage = min(max(1.0, notional / usable), max_leverage)
    if min_leverage > 0:
        from execution.delta_book import leverage_for
        leverage = min(max(leverage, leverage_for(abs(entry - stop) / entry, int(min_leverage))), max_leverage)
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


def spec_of(signal_type: str, timeframe: str) -> str:
    """One trade's strategy identity on a coin: 'swing_donchian@4h'."""
    return f"{signal_type or ''}@{timeframe or ''}"


def per_coin_limit(now: datetime | None = None) -> int:
    """Swing trades allowed on one coin now: settings.swing_max_per_coin until swing_multi_per_coin_until, then 1
    (owner 28 Oct 2026: on for 3 days, then review)."""
    from config.settings import settings
    n = max(1, int(settings.swing_max_per_coin or 1))
    until = (settings.swing_multi_per_coin_until or "").strip()
    if n > 1 and until:
        try:
            end = datetime.fromisoformat(until.replace("Z", "+00:00"))
        except ValueError:
            return 1                                        # a bad date must never leave it on
        end = end if end.tzinfo else end.replace(tzinfo=UTC)
        if (now or datetime.now(UTC)).astimezone(UTC) >= end:
            return 1
    return n


def coin_refusal(open_on_coin: list[tuple[int, str]], side: int, spec: str, limit: int) -> str:
    """Why another swing trade on this coin is refused ('' = allowed). open_on_coin: (side, spec) of the coin's open
    swing trades. Same direction only (Delta nets a coin's trades into one position) and one trade per strategy."""
    if not open_on_coin:
        return ""
    if len(open_on_coin) >= limit:
        return "already_open_in_symbol"
    if any(s != side for s, _ in open_on_coin):
        return "opposite_side_open_in_symbol"
    if any(sp == spec for _, sp in open_on_coin):
        return "same_strategy_open_in_symbol"
    return ""


def priority(signal_type: str, timeframe: str, symbol: str) -> float:
    """Higher = take first. Unknown strategies or coins rank at the book's average."""
    strat = signal_type.replace("swing_", "")
    coin = symbol.upper().replace("USDT", "")
    return STRATEGY_R.get((strat, timeframe), 0.2) + COIN_R.get(coin, 0.2)


BE_COST_FRAC = 0.00158          # round-trip fees + slippage (lab CostModel.round_trip, india_gst): break-even covers costs
DEFAULT_LADDER = "1:0,1.5:0.5,2:1,2.5:1.5"
LADDER_STEP_R = 0.5


def parse_ladder(text: str) -> list[tuple[float, float]]:
    """'1:0,1.5:0.5' -> [(1.0, 0.0), (1.5, 0.5)]."""
    out = []
    for part in (text or "").split(","):
        if ":" in part:
            a, b = part.split(":", 1)
            out.append((float(a), float(b)))
    return out


def swing_lock(side: int, entry: float, initial_stop: float, stop: float, peak: float, price: float,
               mode: str, after_r: float, trail_atr: float, ladder: str = DEFAULT_LADDER,
               buffer_r: float = 0.0) -> tuple[float, float]:
    """Profit lock for an open swing trade (owner, 12 Oct 2026). Returns (new stop, new best price).

    mode "be": once the best price is `after_r` R in profit, the stop moves to the entry.
    mode "ladder": stepped locks from `ladder` ("trigger:lock,..." in R), the backtest's S1 winner.
    mode "trail": once `after_r` R in profit, the stop trails `trail_atr` ATRs behind the best price
    (1 R = STOP_ATR ATRs, the initial stop distance). The stop never moves backwards. "off" changes nothing.
    Mirrors the lab's ExitModel be_trigger_r / trail_atr + trail_after_r, so live and backtest agree."""
    risk = abs(entry - initial_stop)
    best = price if peak <= 0 else (max(peak, price) if side > 0 else min(peak, price))
    if mode == "off" or risk <= 0:
        return stop, best
    gained_r = side * (best - entry) / risk
    if mode == "ladder":
        # S1 (backtest 13 Oct): at 1R -> break-even (entry + round-trip costs), 1.5R -> lock 0.5R, 2R -> 1R, 2.5R -> 1.5R
        new = stop
        steps = parse_ladder(ladder)
        for trig, lock_r in steps:
            if gained_r >= trig:
                lk = lock_r - buffer_r          # robustness grid 13 Oct: 0.25R of room below each lock
                cand = (entry * (1 + side * BE_COST_FRAC) if lock_r <= 0 and not buffer_r
                        else entry + side * lk * risk)
                new = max(new, cand) if side > 0 else min(new, cand)
        if steps and gained_r >= steps[-1][0] + LADDER_STEP_R:
            # open-ended (owner, 13 Oct): beyond the last step, every further 0.5R locks another 0.5R,
            # so the lock keeps working however far the trade runs
            last_trig, last_lock = steps[-1]
            extra = int((gained_r - last_trig) / LADDER_STEP_R + 1e-9) * LADDER_STEP_R
            cand = entry + side * (last_lock + extra - buffer_r) * risk
            new = max(new, cand) if side > 0 else min(new, cand)
        return new, best
    if gained_r < after_r:
        return stop, best
    if mode == "be":
        cand = entry
    else:
        atr = risk / STOP_ATR
        cand = best - side * trail_atr * atr
    new = max(stop, cand) if side > 0 else min(stop, cand)
    return new, best


def ladder_status(side: int, entry: float, init_stop: float, stop: float, peak: float, price: float, qty: float,
                  usdt_inr: float, ladder: str = DEFAULT_LADDER, buffer_r: float = 0.0, confirm_min: int = 0,
                  mode: str = "ladder") -> dict:
    """Everything the portal shows about an open swing trade's profit lock (owner, 14 Oct 2026)."""
    risk = abs(entry - init_stop)
    if risk <= 0 or entry <= 0:
        return {}
    best = price if not peak else (max(peak, price) if side > 0 else min(peak, price))
    r_now = side * (price - entry) / risk
    best_r = side * (best - entry) / risk
    locked_r = side * (stop - entry) / risk               # -1 = original stop; 0 = break-even; >0 = profit locked
    if abs(stop - init_stop) <= risk * 1e-6:
        state = "original stop"
    elif abs(locked_r) < 0.05:
        state = "break-even"
    elif locked_r > 0:
        state = f"profit locked +{locked_r:.2f}R"
    else:
        state = f"risk cut to {-locked_r:.2f}R"
    nxt = None
    if mode == "ladder":
        steps = parse_ladder(ladder)
        for trig, lock_r in steps:
            if best_r < trig:
                nxt = (trig, lock_r - buffer_r)
                break
        if nxt is None and steps:
            last_trig, last_lock = steps[-1]
            k = int((best_r - last_trig) / LADDER_STEP_R + 1e-9) + 1
            nxt = (last_trig + k * LADDER_STEP_R, last_lock + k * LADDER_STEP_R - buffer_r)
    worst = qty * (entry - stop) * side * usdt_inr       # INR lost if the stop fills now (negative = profit kept)
    return {
        "mode": mode, "state": state, "initial_stop": init_stop, "stop": stop, "best_price": best,
        "r_now": round(r_now, 3), "best_r": round(best_r, 3), "locked_r": round(locked_r, 3),
        "next_trigger_r": nxt[0] if nxt else None,
        "next_trigger_price": (entry + side * nxt[0] * risk) if nxt else None,
        "next_lock_r": nxt[1] if nxt else None,
        "next_lock_price": (entry + side * nxt[1] * risk) if nxt else None,
        "confirm": (f"locked stop needs a {confirm_min}-minute candle close beyond it; the original stop "
                    f"{init_stop:.6g} fires instantly") if confirm_min else "locked stop fires on touch",
        "worst_case_inr": round(worst, 2),
    }
