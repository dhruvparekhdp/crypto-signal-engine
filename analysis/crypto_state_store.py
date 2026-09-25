from __future__ import annotations

import asyncio
import math
from datetime import UTC, datetime

import structlog

from analysis.crypto_state import CommodityState, CryptoState, OHLCVCandle
from config.settings import settings

log = structlog.get_logger()


def price_is_plausible(symbol: str, current: float, incoming: float) -> bool:
    """
    Reject a tick that cannot be a price move.

    A feed once resolved XAUUSDT to an unrelated token and reported gold at
    $0.00004049 against a running $4,341. The value was written straight into
    state, poisoned the forming candle, drove the 1-minute ATR from 1.2 to
    ~311, and the level policy turned that into a published 43% target with a
    36-minute horizon — every stage working correctly on one bad number.

    The check is deliberately loose. It is not trying to catch a sharp move;
    it is trying to catch a different asset, a scaling error or a decimal
    slip, all of which miss by orders of magnitude rather than by percent.
    """
    if incoming <= 0:
        return False
    if current <= 0:
        return True          # nothing to compare against yet
    move = abs(incoming - current) / current
    if move <= settings.max_price_jump_pct:
        return True
    log.error("price_tick_rejected", symbol=symbol, current=current,
              incoming=incoming, move_pct=round(move * 100, 2),
              hint="tick is too far from the running price to be a move; "
                   "check the feed's symbol mapping for this pair")
    return False


def _compute_rsi(closes: list[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0

    gains = []
    losses = []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        if diff >= 0:
            gains.append(diff)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(abs(diff))

    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period

    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0

    rs = avg_gain / avg_loss
    return round(100.0 - (100.0 / (1.0 + rs)), 2)


def _ema_series(values: list[float], period: int) -> list[float]:
    """EMA at every point, seeded with the SMA of the first `period` values."""
    if len(values) < period:
        return []
    k = 2.0 / (period + 1.0)
    ema = sum(values[:period]) / period
    out = [ema]
    for v in values[period:]:
        ema = (v - ema) * k + ema
        out.append(ema)
    return out


def _macd(closes: list[float]) -> tuple[float, float, float]:
    """
    (line, signal, histogram). The signal is the 9-period EMA of the MACD line.

    Only the line used to be computed; signal and histogram stayed 0.0, so the
    position reviewer's "MACD still points our way" read the line against zero
    and every stored snapshot has a meaningless macd_signal.
    """
    fast, slow = _ema_series(closes, 12), _ema_series(closes, 26)
    if not slow:
        return 0.0, 0.0, 0.0
    fast = fast[len(fast) - len(slow):]
    line = [f - s for f, s in zip(fast, slow)]
    sig = _ema_series(line, 9)
    if not sig:
        return line[-1], 0.0, 0.0
    return line[-1], sig[-1], line[-1] - sig[-1]


def _compute_ema(values: list[float], period: int) -> float:
    if not values:
        return 0.0
    if len(values) < period:
        return sum(values) / len(values)

    multiplier = 2.0 / (period + 1.0)
    ema = sum(values[:period]) / period
    for val in values[period:]:
        ema = (val - ema) * multiplier + ema
    return ema


def _compute_bollinger(closes: list[float], period: int = 20, std_dev: float = 2.0) -> tuple[float, float, float, float]:
    if len(closes) < period:
        mid = closes[-1] if closes else 0.0
        return mid, mid, mid, 0.0

    subset = closes[-period:]
    mid = sum(subset) / period
    variance = sum((x - mid) ** 2 for x in subset) / period
    std = math.sqrt(variance)
    upper = mid + (std * std_dev)
    lower = mid - (std * std_dev)
    bandwidth = (upper - lower) / mid if mid > 0 else 0.0
    return round(upper, 4), round(mid, 4), round(lower, 4), round(bandwidth, 4)


def _compute_atr(candles: list[OHLCVCandle], period: int = 14) -> float:
    if len(candles) < 2:
        return 0.0

    tr_list = []
    for i in range(1, len(candles)):
        c = candles[i]
        prev_c = candles[i - 1]
        tr = max(
            c.high - c.low,
            abs(c.high - prev_c.close),
            abs(c.low - prev_c.close),
        )
        tr_list.append(tr)

    if len(tr_list) < period:
        return sum(tr_list) / len(tr_list)
    return sum(tr_list[-period:]) / period


CANDLE_WINDOW = 360
"""Rolling 1-minute window kept per symbol (6 hours). Shared so the backtest cannot
silently diverge from the live store by holding a different amount of history."""

# The longest indicator lookback is EMA-200, but RSI and ATR only ever consume
# their last `period + 1` inputs, so handing them the whole window is wasted
# work for a bit-identical answer.
_RSI_PERIOD = 14
_ATR_PERIOD = 14


def append_candle(state: CryptoState, candle: OHLCVCandle) -> None:
    """Append and trim to the rolling window."""
    state.candles_1m.append(candle)
    if len(state.candles_1m) > CANDLE_WINDOW:
        del state.candles_1m[:-CANDLE_WINDOW]


def recalculate_indicators(state: CryptoState) -> None:
    """
    Recompute every indicator the analyzers read.

    Module-level and self-free on purpose: the backtest calls this exact
    function, so "the backtest sees the same indicators as live" is structural
    rather than a comment that can rot.
    """
    closes = [c.close for c in state.candles_1m]
    if len(closes) < 14:
        return

    state.rsi_14_prev = state.rsi_14
    state.rsi_14 = _compute_rsi(closes[-(_RSI_PERIOD + 1):], _RSI_PERIOD)

    line, signal, hist = _macd(closes)
    if line or signal:
        state.macd_line, state.macd_signal, state.macd_histogram = line, signal, hist
    else:
        state.macd_line = _compute_ema(closes, 12) - _compute_ema(closes, 26)

    state.ema_9 = _compute_ema(closes, 9)
    state.ema_20 = _compute_ema(closes, 20)
    state.ema_50 = _compute_ema(closes, 50)
    state.ema_200 = _compute_ema(closes, 200)

    upper, mid, lower, bw = _compute_bollinger(closes, 20, 2.0)
    state.bollinger_upper = upper
    state.bollinger_mid = mid
    state.bollinger_lower = lower
    state.bollinger_bandwidth = bw

    state.atr_14 = _compute_atr(state.candles_1m[-(_ATR_PERIOD + 1):], _ATR_PERIOD)


class CryptoStateStore:
    """
    Thread-safe in-memory cache for live crypto states.

    The watchlist itself is DB-backed (crypto_watchlist table), not env-var
    based — call seed() once at startup with symbols loaded from the database.
    symbols_version bumps on every add/remove so BinanceWSCollector can detect
    a watchlist change and resubscribe without a restart.
    """

    def __init__(self) -> None:
        self._states: dict[str, CryptoState] = {}
        self._lock = asyncio.Lock()
        self.symbols_version = 0

    async def seed(self, symbols: list[str]) -> None:
        """Populate the store from a symbol list (called once at startup)."""
        async with self._lock:
            for sym in symbols:
                sym = sym.strip().lower()
                if sym and sym not in self._states:
                    base = sym.replace("usdt", "").replace("busd", "").upper()
                    self._states[sym] = CryptoState(symbol=sym, base_asset=base)
            self.symbols_version += 1

    async def update_kline(
        self,
        symbol: str,
        open_: float,
        high: float,
        low: float,
        close: float,
        volume: float,
        timestamp: datetime,
        is_closed: bool,
    ) -> None:
        """Process incoming Binance WebSocket Kline tick."""
        sym = symbol.lower()
        async with self._lock:
            state = self._states.get(sym)
            if not state:
                base = sym.replace("usdt", "").replace("busd", "").upper()
                state = CryptoState(symbol=sym, base_asset=base)
                self._states[sym] = state

            state.current_price = close
            state.timestamp = timestamp

            candle = OHLCVCandle(
                open=open_,
                high=high,
                low=low,
                close=close,
                volume=volume,
                timestamp=timestamp,
                is_closed=is_closed,
            )

            # Update real-time candle
            if state.candles_1m and not state.candles_1m[-1].is_closed:
                state.candles_1m[-1] = candle
            else:
                state.candles_1m.append(candle)

            if len(state.candles_1m) > CANDLE_WINDOW:
                del state.candles_1m[:-CANDLE_WINDOW]

            if is_closed:
                recalculate_indicators(state)

    async def replace_candles(self, symbol: str, bars: list[dict]) -> None:
        """
        Install real OHLCV bars over the poll-aggregated ones.

        Poll aggregation is a fallback, not a measurement: two polls a minute
        cannot see the true high and low, and the poller does not know
        per-bar volume at all. When the venue will hand over real candles they
        replace the estimate outright rather than being merged into it —
        merging would leave a history that is real in some places and invented
        in others, with no way to tell which bar is which.

        A short or empty payload is ignored, so a bad response cannot wipe a
        history that was working.
        """
        if len(bars) < 20:
            return
        sym = symbol.lower()
        async with self._lock:
            state = self._states.get(sym)
            if not state:
                base = sym.replace("usdt", "").replace("busd", "").upper()
                state = CryptoState(symbol=sym, base_asset=base)
                self._states[sym] = state

            state.candles_1m = [
                OHLCVCandle(open=b["open"], high=b["high"], low=b["low"],
                            close=b["close"], volume=b["volume"],
                            timestamp=b["timestamp"], is_closed=True,
                            taker_buy_volume=b.get("taker_buy_volume", 0.0))
                for b in bars[-CANDLE_WINDOW:]
            ]
            # The last bar is still forming; marking it closed would let the
            # next poll append beside it instead of updating it.
            state.candles_1m[-1].is_closed = False
            # Normally a ticker collector owns the price and this only seeds a
            # cold start. With one source there is no ticker, so the newest
            # close is the price — and price and candles then cannot disagree,
            # which is the disagreement that produced the 43% gold target.
            if settings.binance_only_mode or state.current_price <= 0:
                state.current_price = state.candles_1m[-1].close
            recalculate_indicators(state)

    async def update_24h_stats(
        self,
        symbol: str,
        price_24h_ago: float,
        volume_24h: float,
        high_24h: float,
        low_24h: float,
    ) -> None:
        sym = symbol.lower()
        async with self._lock:
            state = self._states.get(sym)
            if state:
                state.price_24h_ago = price_24h_ago
                state.volume_24h = volume_24h
                state.high_24h = high_24h
                state.low_24h = low_24h

    async def update_from_rest(
        self,
        symbol: str,
        price: float,
        high_24h: float,
        low_24h: float,
        volume_24h: float,
        change_24h_pct: float,
        timestamp: datetime,
    ) -> None:
        """
        Ingest a REST poll snapshot (CoinGecko) — appends one synthetic candle
        per poll so RSI/MACD/Bollinger accumulate over time, same math as the
        WebSocket path but at poll-interval resolution instead of per-tick.
        """
        sym = symbol.lower()
        async with self._lock:
            state = self._states.get(sym)
            if not state:
                base = sym.replace("usdt", "").replace("busd", "").upper()
                state = CryptoState(symbol=sym, base_asset=base)
                self._states[sym] = state

            if not price_is_plausible(sym, state.current_price, price):
                return

            state.current_price = price
            state.high_24h = high_24h
            state.low_24h = low_24h
            state.volume_24h = volume_24h
            state.price_24h_ago = (
                price / (1.0 + change_24h_pct / 100.0) if change_24h_pct > -100.0 else price
            )
            state.timestamp = timestamp

            # Aggregate polls into a real minute bar instead of writing one
            # flat candle per poll. A candle with high == low has zero true
            # range, so ATR decayed toward zero and every ATR-derived level
            # came out microscopic — the 0.05% targets seen on the dashboard.
            #
            # Two 30-second polls per minute still understate the true high and
            # low, so this ATR is a floor on real volatility, never an
            # overstatement. That is the safe direction to be wrong in: it
            # refuses marginal trades rather than inventing them.
            bucket = timestamp.replace(second=0, microsecond=0)
            last = state.candles_1m[-1] if state.candles_1m else None

            # Per-bar volume is left at zero, NOT set to the rolling 24-hour
            # figure. Stamping a 24h total onto a one-minute bar made every
            # bar carry a near-identical number, so relative volume was always
            # ~1.0, VWAP degenerated to a plain average and money-flow was
            # driven purely by price with a constant weight. The volume family
            # was voting on a constant and calling it evidence. The 24h total
            # is already on the state, where it means something; zero here is
            # honest about the poller not knowing per-minute volume, and the
            # volume checks abstain rather than invent a reading.
            if last is not None and last.timestamp == bucket:
                last.high = max(last.high, price)
                last.low = min(last.low, price)
                last.close = price
            else:
                if last is not None:
                    last.is_closed = True
                append_candle(state, OHLCVCandle(
                    open=price, high=price, low=price, close=price,
                    volume=0.0, timestamp=bucket, is_closed=False,
                ))
            recalculate_indicators(state)

    def _recalculate_indicators(self, state: CryptoState) -> None:
        recalculate_indicators(state)

    async def set_funding_rate(self, symbol: str, rate: float) -> None:
        """Record the venue's funding rate. Feeds confidence, never a trade."""
        async with self._lock:
            state = self._states.get(symbol.lower())
            if state is not None:
                state.funding_rate_per_8h = rate

    async def update_sentiment(self, base_asset: str, score: float, news_count: int) -> None:
        base = base_asset.upper()
        async with self._lock:
            for state in self._states.values():
                if state.base_asset == base or base in ("ALL", "CRYPTO"):
                    state.sentiment_score = score
                    state.sentiment_news_count = news_count
                    state.last_sentiment_update = datetime.now(UTC)

    async def set_sentiment(self, symbol: str, score: float, news_count: int) -> None:
        """Set one symbol's sentiment, already combined from its own and macro news."""
        async with self._lock:
            state = self._states.get(symbol.lower())
            if state is not None:
                state.sentiment_score = score
                state.sentiment_news_count = news_count
                state.last_sentiment_update = datetime.now(UTC)

    async def get(self, symbol: str) -> CryptoState | None:
        async with self._lock:
            return self._states.get(symbol.lower())

    async def get_all(self) -> list[CryptoState]:
        async with self._lock:
            return list(self._states.values())

    async def get_symbols(self) -> list[str]:
        async with self._lock:
            return list(self._states.keys())

    async def add_symbol(self, symbol: str) -> None:
        sym = symbol.strip().lower()
        async with self._lock:
            if sym not in self._states:
                base = sym.replace("usdt", "").replace("busd", "").upper()
                self._states[sym] = CryptoState(symbol=sym, base_asset=base)
                self.symbols_version += 1
                log.info("crypto_watchlist_symbol_added", symbol=sym)

    async def remove_symbol(self, symbol: str) -> None:
        sym = symbol.strip().lower()
        async with self._lock:
            if self._states.pop(sym, None) is not None:
                self.symbols_version += 1
                log.info("crypto_watchlist_symbol_removed", symbol=sym)

    async def count(self) -> int:
        async with self._lock:
            return len(self._states)


class CommodityStateStore:
    """Thread-safe in-memory cache for live commodity states (Gold, Silver, Oil)."""

    def __init__(self) -> None:
        self._states: dict[str, CommodityState] = {}
        self._lock = asyncio.Lock()
        for sym in settings.twelvedata_symbols.split(","):
            s = sym.strip()
            if s:
                name = "Gold" if "XAU" in s or "GOLD" in s else ("Silver" if "XAG" in s or "SILVER" in s else "Crude Oil")
                self._states[s] = CommodityState(symbol=s, name=name)

    async def update_price(self, symbol: str, price: float, timestamp: datetime) -> None:
        async with self._lock:
            state = self._states.get(symbol)
            if not state:
                name = "Gold" if "XAU" in symbol else ("Silver" if "XAG" in symbol else "Crude Oil")
                state = CommodityState(symbol=symbol, name=name)
                self._states[symbol] = state

            if not price_is_plausible(symbol, state.current_price, price):
                return

            state.current_price = price
            state.timestamp = timestamp
            state.price_history.append((price, timestamp))
            if len(state.price_history) > 500:
                state.price_history = state.price_history[-500:]

            prices = [p[0] for p in state.price_history]
            if len(prices) >= 15:
                state.rsi_14 = _compute_rsi(prices, 14)

    async def get(self, symbol: str) -> CommodityState | None:
        async with self._lock:
            return self._states.get(symbol)

    async def get_all(self) -> list[CommodityState]:
        async with self._lock:
            return list(self._states.values())
