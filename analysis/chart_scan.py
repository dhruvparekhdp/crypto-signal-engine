"""Walk historical OHLCV bars through the simulator strategy dispatcher for chart markers."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import numpy as np
import pandas as pd

from analysis.crypto_state import CryptoState, OHLCVCandle
from analysis.simulator.exits import bracket
from analysis.simulator.strategy_dispatcher import SimulatorStrategyDispatcher


def _bars_to_df(bars: list[dict[str, Any]]) -> pd.DataFrame:
    rows = []
    for b in bars:
        ts = int(b["time"])
        rows.append(
            {
                "datetime": datetime.fromtimestamp(ts, tz=UTC),
                "open": float(b["open"]),
                "high": float(b["high"]),
                "low": float(b["low"]),
                "close": float(b["close"]),
                "volume": float(b.get("volume") or 0.0),
            }
        )
    return pd.DataFrame(rows)


def scan_strategy_markers(
    bars: list[dict[str, Any]],
    *,
    strategy: str = "all",
    step: int = 2,
    min_confidence: float = 0.60,
    max_markers: int = 120,
    tp_r: float = 3.0,
    sl_r: float = 1.0,
    anti_flip_minutes: int = 90,
) -> list[dict[str, Any]]:
    """
    Replay bars through SimulatorStrategyDispatcher (same stack as the market simulator).
    Returns lightweight marker dicts for the chart UI.
    """
    if len(bars) < 80:
        return []

    df = _bars_to_df(bars)
    dispatcher = SimulatorStrategyDispatcher(selected_strategy=strategy)
    highs = df["high"].values.astype(float)
    lows = df["low"].values.astype(float)
    closes = df["close"].values.astype(float)
    volumes = df["volume"].values.astype(float)
    times = df["datetime"].values

    # Wilder-style ATR(14) on the resampled series
    tr = np.maximum(highs[1:] - lows[1:], np.maximum(np.abs(highs[1:] - closes[:-1]), np.abs(lows[1:] - closes[:-1])))
    atr = np.zeros(len(closes))
    atr[14] = float(np.mean(tr[:14])) if len(tr) >= 14 else float(np.mean(tr) if len(tr) else 1.0)
    for j in range(15, len(closes)):
        atr[j] = (atr[j - 1] * 13 + tr[j - 1]) / 14.0
    atr[atr <= 0] = closes[atr <= 0] * 0.003

    markers: list[dict[str, Any]] = []
    last_trade_time: datetime | None = None
    last_trade_dir = 0
    step = max(1, int(step))

    for i in range(60, len(df), step):
        if len(markers) >= max_markers:
            break
        c_close = float(closes[i])
        c_time = pd.Timestamp(times[i]).to_pydatetime()
        if c_time.tzinfo is None:
            c_time = c_time.replace(tzinfo=UTC)
        c_atr = float(atr[i]) if atr[i] > 0 else c_close * 0.003

        start_w = max(0, i - 120)
        rolling_candles = [
            OHLCVCandle(
                open=float(df["open"].iloc[j]),
                high=float(highs[j]),
                low=float(lows[j]),
                close=float(closes[j]),
                volume=float(volumes[j]),
                timestamp=pd.Timestamp(times[j]).to_pydatetime().replace(tzinfo=UTC),
            )
            for j in range(start_w, i + 1)
        ]
        st = CryptoState(
            symbol="chart",
            base_asset="",
            current_price=c_close,
            candles_1m=rolling_candles,
        )
        htf_window = closes[max(0, i - 48): i + 1]
        htf_ema = float(pd.Series(htf_window).ewm(span=24, adjust=False).mean().iloc[-1])
        is_htf_bull = c_close >= htf_ema * 1.002
        is_htf_bear = c_close <= htf_ema * 0.998

        sig_result = dispatcher.detect_signal(
            symbol="CHART",
            state=st,
            c_close=c_close,
            c_atr=c_atr,
            opens=df["open"].values.astype(float),
            highs=highs,
            lows=lows,
            closes=closes,
            i=i,
            rolling_candles=rolling_candles,
            htf_ema=htf_ema,
            is_htf_bull=is_htf_bull,
            is_htf_bear=is_htf_bear,
        )
        if not sig_result:
            continue
        target_dir, sig_type, sig_conf = sig_result
        if float(sig_conf) < min_confidence:
            continue
        if target_dir == -1 and is_htf_bull:
            continue
        if target_dir == 1 and is_htf_bear:
            continue
        if last_trade_time is not None:
            mins_since = (c_time - last_trade_time).total_seconds() / 60.0
            if mins_since < anti_flip_minutes and target_dir != last_trade_dir:
                continue

        is_long = target_dir == 1
        sl, tp, _ = bracket(c_close, c_atr, 1 if is_long else -1, tp_r, sl_r)
        markers.append(
            {
                "time": int(c_time.timestamp()),
                "direction": "LONG" if is_long else "SHORT",
                "strategy": sig_type,
                "confidence": round(float(sig_conf), 3),
                "entry": round(c_close, 4),
                "tp": round(float(tp), 4),
                "sl": round(float(sl), 4),
                "source": "scan",
            }
        )
        last_trade_time = c_time
        last_trade_dir = target_dir

    return markers
