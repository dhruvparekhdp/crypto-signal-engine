from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime


@dataclass
class OHLCVCandle:
    open: float
    high: float
    low: float
    close: float
    volume: float
    timestamp: datetime
    is_closed: bool = True
    taker_buy_volume: float = 0.0


def resample_candles(candles: list[OHLCVCandle], timeframe_minutes: int) -> list[OHLCVCandle]:
    """
    Resample 1-minute OHLCVCandles into higher timeframe bars (e.g. 15m, 30m, 1h, 4h).
    """
    if not candles or timeframe_minutes <= 1:
        return list(candles)

    bucket_secs = timeframe_minutes * 60
    buckets: dict[int, list[OHLCVCandle]] = {}
    for c in candles:
        ts = int(c.timestamp.timestamp())
        bucket_key = (ts // bucket_secs) * bucket_secs
        buckets.setdefault(bucket_key, []).append(c)

    resampled: list[OHLCVCandle] = []
    for key in sorted(buckets.keys()):
        group = buckets[key]
        if not group:
            continue
        o = group[0].open
        h = max(x.high for x in group)
        low = min(x.low for x in group)
        close = group[-1].close
        vol = sum(x.volume for x in group)
        tb_vol = sum(x.taker_buy_volume for x in group)
        ts = datetime.fromtimestamp(key, tz=UTC)
        is_closed = len(group) >= timeframe_minutes and group[-1].is_closed
        resampled.append(OHLCVCandle(
            open=o, high=h, low=low, close=close, volume=vol,
            timestamp=ts, is_closed=is_closed, taker_buy_volume=tb_vol
        ))
    return resampled


@dataclass
class CryptoState:
    symbol: str                                                    # e.g., "btcusdt"
    base_asset: str                                                # e.g., "BTC"
    quote_asset: str = "USDT"                                      # e.g., "USDT"
    current_price: float = 0.0
    price_24h_ago: float = 0.0
    volume_24h: float = 0.0
    volume_24h_avg: float = 0.0
    high_24h: float = 0.0
    low_24h: float = 0.0

    # Candle histories for multiple timeframes
    candles_1m: list[OHLCVCandle] = field(default_factory=list)    # last 120-480 candles
    candles_5m: list[OHLCVCandle] = field(default_factory=list)    # last 72 candles (6h)
    candles_15m: list[OHLCVCandle] = field(default_factory=list)   # last 96 candles (24h)
    candles_1h: list[OHLCVCandle] = field(default_factory=list)    # last 168 candles (7d)
    candles_4h: list[OHLCVCandle] = field(default_factory=list)    # last 60 candles (10d)
    candles_1d: list[OHLCVCandle] = field(default_factory=list)    # last 30 candles (30d)

    def get_candles(self, timeframe: str = "1m") -> list[OHLCVCandle]:
        """Return candles for requested timeframe ('1m', '15m', '30m', '1h', '4h')."""
        tf = timeframe.strip().lower()
        if tf == "1m":
            return self.candles_1m
        elif tf == "5m":
            return self.candles_5m or resample_candles(self.candles_1m, 5)
        elif tf == "15m":
            return self.candles_15m or resample_candles(self.candles_1m, 15)
        elif tf == "30m":
            return resample_candles(self.candles_1m, 30)
        elif tf in ("1h", "60m"):
            return self.candles_1h or resample_candles(self.candles_1m, 60)
        elif tf in ("4h", "240m"):
            return self.candles_4h or resample_candles(self.candles_1m, 240)
        return self.candles_1m

    # Key Technical Indicators (Computed periodically on candle updates)
    rsi_14: float = 50.0
    rsi_14_prev: float = 50.0
    macd_line: float = 0.0
    macd_signal: float = 0.0
    macd_histogram: float = 0.0
    bollinger_upper: float = 0.0
    bollinger_mid: float = 0.0
    bollinger_lower: float = 0.0
    bollinger_bandwidth: float = 0.0                               # (upper - lower) / mid
    ema_9: float = 0.0
    ema_20: float = 0.0
    ema_50: float = 0.0
    ema_200: float = 0.0
    atr_14: float = 0.0                                            # Average True Range (volatility)

    # Perpetual funding rate per 8h, from the futures venue. None means the
    # instrument has no funding or none was reported — distinct from 0.0,
    # which is a real reading of "nobody is paying anybody".
    funding_rate_per_8h: float | None = None

    # Derivatives Open Interest & Order Flow
    open_interest: float = 0.0
    oi_change_1h_pct: float = 0.0
    cvd_trend: str = "neutral"                                      # "bullish_absorption", "bearish_absorption", "bullish_delta", "bearish_delta", "neutral"

    # Latest L2 snapshot, when a venue publishes one. Typed loosely to keep
    # this module free of the analysis imports; it is an analysis.orderbook
    # OrderBook. None means "not measured", which is distinct from "the book
    # is empty" — the first falls back to the assumed costs, the second is a
    # reason to stand aside.
    order_book: object | None = None

    # Sentiment (FinBERT / VADER / CryptoPanic)
    sentiment_score: float = 0.0                                   # -1.0 (bearish) to +1.0 (bullish)
    # Daily trend, refreshed hourly from Binance daily candles: the 20-day
    # simple average of closes, and when it was computed. None until fetched.
    daily_sma20: float | None = None
    daily_trend_at: datetime | None = None
    sentiment_news_count: int = 0
    last_sentiment_update: datetime | None = None

    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def price_change_24h_pct(self) -> float:
        if self.price_24h_ago <= 0:
            return 0.0
        return ((self.current_price - self.price_24h_ago) / self.price_24h_ago) * 100.0

    @property
    def volume_ratio(self) -> float:
        """
        24-hour volume against its own average. 0.0 means unknown.

        It used to return 1.0 when the baseline was missing, and the baseline
        is never populated by anything — so every coin on the board read
        "Vol x1.0" forever, which looked like a measurement of a perfectly
        ordinary session and was the absence of any measurement at all.
        Per-bar participation comes from `indicators.relative_volume` now;
        this stays for the 24-hour view and says so when it cannot answer.
        """
        if self.volume_24h_avg <= 0 or self.volume_24h <= 0:
            return 0.0
        return self.volume_24h / self.volume_24h_avg

    def rsi_divergence(self, lookback: int = 40) -> str | None:
        """
        Regular RSI divergence, measured at confirmed swing pivots.

        Bullish — price makes a lower low while RSI makes a higher low.
        Bearish — price makes a higher high while RSI makes a lower high.

        The previous version compared the minimum of one half-window against
        the other and then asked whether RSI happened to be ticking upward. It
        never read RSI at the two lows, which is the entire definition. Measured
        on pure noise that condition fired on 51% of bars — a coin flip, and the
        reason nearly every signal on the dashboard was a "momentum reversal".
        """
        from analysis.indicators import divergence, rsi_series

        candles = self.candles_1m[-lookback:] if len(self.candles_1m) >= lookback \
            else self.candles_1m
        if len(candles) < 20:
            return None

        closes = [c.close for c in candles]
        lows = [c.low for c in candles]
        highs = [c.high for c in candles]
        rs = rsi_series(closes)
        if len(rs) < 8:
            return None

        # RSI starts later than price, so both series are trimmed to the same
        # bars before any comparison — otherwise the pivots do not line up.
        offset = len(closes) - len(rs)
        lows_a, highs_a = lows[offset:], highs[offset:]

        if divergence(lows_a, rs, bullish=True):
            return "bullish"
        if divergence(highs_a, rs, bullish=False):
            return "bearish"
        return None


@dataclass
class CommodityState:
    symbol: str                                                    # e.g., "XAU/USD", "WTI/USD"
    name: str                                                      # "Gold", "Silver", "Crude Oil"
    current_price: float = 0.0
    price_1h_ago: float = 0.0
    price_24h_ago: float = 0.0
    price_history: list[tuple[float, datetime]] = field(default_factory=list)  # [(price, ts)]
    atr_14: float = 0.0
    rsi_14: float = 50.0
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def price_change_24h_pct(self) -> float:
        if self.price_24h_ago <= 0:
            return 0.0
        return ((self.current_price - self.price_24h_ago) / self.price_24h_ago) * 100.0


@dataclass
class NewsItem:
    title: str
    source: str
    currencies: list[str]
    native_sentiment: str                                          # "bullish", "bearish", "neutral"
    published_at: datetime
    url: str

    @classmethod
    def from_api(cls, post: dict) -> NewsItem:
        currencies = [c.get("code", "") for c in post.get("currencies", []) if c.get("code")]
        votes = post.get("votes", {})
        bullish_votes = votes.get("bullish", 0)
        bearish_votes = votes.get("bearish", 0)

        sentiment = "neutral"
        if bullish_votes > bearish_votes:
            sentiment = "bullish"
        elif bearish_votes > bullish_votes:
            sentiment = "bearish"

        created_str = post.get("created_at")
        try:
            pub_date = datetime.fromisoformat(created_str.replace("Z", "+00:00")) if created_str else datetime.now(UTC)
        except Exception:
            pub_date = datetime.now(UTC)

        return cls(
            title=post.get("title", ""),
            source=post.get("source", {}).get("title", ""),
            currencies=currencies,
            native_sentiment=sentiment,
            published_at=pub_date,
            url=post.get("url", ""),
        )
