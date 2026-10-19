"""Replay every logged signal on real 1-minute bars and measure what was known at the moment it fired (owner, 19 Oct
2026: "reanalyse all the signals with mirror and run on live market simulator ... try to find some correlation so we
can improve in accuracy"). Research only: nothing here can reach the paper or live books.

    python -m scripts.signal_replay replay SIGNALS.json.gz OUT_DIR    # one row per signal -> OUT_DIR/replayed.parquet

SIGNALS is an export of crypto_signal_log (every column). For each signal:
  entry      the next 1m bar's open after the signal (a market order), not the logged price
  replay     the logged target and stop on 1m bars; a bar that touches both counts as the STOP (the live resolver
             credited the target when the bar closed in the trade's favour); expiry at the signal's own horizon
             (its timeframe, e.g. '81m'); fees 0.15% round trip (Delta taker 0.05% + 18% GST each side, + slippage)
  path       signed returns at 5m..8h, best and worst point (MFE / MAE) in R, and whether +1R came before -1R
  features   only bars that CLOSED before the signal: momentum 5m..24h, volatility, volume surge, taker-buy share,
             position in the 3h / 24h range, distance from the 24h VWAP, stop size in ATRs, BTC's trend, and Binance
             open interest and long/short ratios. Every directional feature is signed by the trade's side, so a
             positive value always means "with the trade".
Bars come from the local lake (data.binance.vision) topped up from Binance's public REST for the days the daily
files have not been published yet.
"""
from __future__ import annotations

import gzip
import json
import math
import re
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

LAKE = Path("data/lake/um")
COST_PCT = 0.15
FWD_MIN = (5, 15, 30, 60, 120, 240, 480)
SWING_HORIZON_MIN = 7 * 24 * 60
SYMBOL_MAP = {"XAUTUSDT": "PAXGUSDT"}             # Binance has no XAUT perpetual; PAXG is the same ounce of gold


def _lake_1m(sym: str, since: str) -> pd.DataFrame:
    files = sorted((LAKE / "klines" / sym / "1m").glob("*.parquet"))
    files = [f for f in files if f.stem >= since[:7]]
    if not files:
        return pd.DataFrame()
    df = pd.concat([pd.read_parquet(f, columns=["open_time", "open", "high", "low", "close", "volume",
                                                "taker_buy_volume"]) for f in files], ignore_index=True)
    return df.rename(columns={"open_time": "t", "taker_buy_volume": "tb"})


def _rest_1m(sym: str, start_ms: int) -> pd.DataFrame:
    import httpx
    rows, t = [], start_ms
    now = int(time.time() * 1000)
    with httpx.Client(timeout=15) as c:
        while t < now - 60_000:
            r = c.get("https://fapi.binance.com/fapi/v1/klines",
                      params={"symbol": sym, "interval": "1m", "startTime": t, "limit": 1500})
            r.raise_for_status()
            k = r.json()
            if not k:
                break
            rows += [(int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5]), float(x[9]))
                     for x in k if int(x[6]) < now]          # closed bars only
            t = int(k[-1][0]) + 60_000
            time.sleep(0.15)
    return pd.DataFrame(rows, columns=["t", "open", "high", "low", "close", "volume", "tb"])


def load_1m(sym: str, since: str, cache: Path) -> pd.DataFrame:
    df = _lake_1m(sym, since)
    last = int(df.t.max()) if len(df) else int(pd.Timestamp(since, tz="UTC").timestamp() * 1000)
    cache.mkdir(parents=True, exist_ok=True)
    cf = cache / f"topup_{sym}.parquet"
    top = pd.read_parquet(cf) if cf.exists() else pd.DataFrame()
    if top.empty or int(top.t.max()) < int(time.time() * 1000) - 30 * 60_000:
        top = _rest_1m(sym, last + 60_000)
        top.to_parquet(cf)
    df = pd.concat([df, top], ignore_index=True).drop_duplicates("t").sort_values("t").reset_index(drop=True)
    return df[df.t >= pd.Timestamp(since, tz="UTC").timestamp() * 1000 - 8 * 86_400_000].reset_index(drop=True)


def load_metrics(sym: str) -> pd.DataFrame:
    files = sorted((LAKE / "metrics" / sym / "-").glob("*.parquet"))
    if not files:
        return pd.DataFrame()
    m = pd.concat([pd.read_parquet(f) for f in files[-3:]], ignore_index=True)
    m["t"] = pd.to_datetime(m.ts, utc=True).astype("int64") // 1_000_000
    return m.sort_values("t").reset_index(drop=True)


class Series1m:
    """Arrays for one symbol plus the rolling pieces the features need, computed once."""

    def __init__(self, df: pd.DataFrame):
        self.t = df.t.to_numpy(np.int64)
        self.o, self.h, self.l, self.c = (df[k].to_numpy(float) for k in ("open", "high", "low", "close"))
        self.v, self.tb = df.volume.to_numpy(float), df.tb.to_numpy(float)
        lr = np.r_[0.0, np.diff(np.log(self.c))]
        self.vol60 = pd.Series(lr).rolling(60, min_periods=30).std().to_numpy() * 100
        self.vol60_med7d = pd.Series(self.vol60).rolling(7 * 1440, min_periods=1440).median().to_numpy()
        self.cv, self.ctb, self.cpv = np.cumsum(self.v), np.cumsum(self.tb), np.cumsum(self.v * self.c)
        self.hi3h = pd.Series(self.h).rolling(180, min_periods=60).max().to_numpy()
        self.lo3h = pd.Series(self.l).rolling(180, min_periods=60).min().to_numpy()
        self.hi24 = pd.Series(self.h).rolling(1440, min_periods=600).max().to_numpy()
        self.lo24 = pd.Series(self.l).rolling(1440, min_periods=600).min().to_numpy()
        rng5 = pd.Series(self.h).rolling(5).max() - pd.Series(self.l).rolling(5).min()
        self.atr5_pct = (rng5.rolling(60, min_periods=30).mean() / pd.Series(self.c) * 100).to_numpy()

    def idx(self, t_ms: int) -> int:
        """Index of the first bar opening at or after t_ms (the entry bar)."""
        return int(np.searchsorted(self.t, t_ms, side="left"))

    def ret(self, i_last: int, n: int) -> float:
        j = i_last - n
        return math.log(self.c[i_last] / self.c[j]) * 100 if j >= 0 else np.nan

    def window(self, a: int, b: int, arr: np.ndarray) -> float:
        a = max(a, 0)
        return arr[b] - (arr[a - 1] if a > 0 else 0.0)


def _horizon(sig) -> int:
    if sig.get("trade_mode") == "swing":
        return SWING_HORIZON_MIN
    m = re.fullmatch(r"(\d+)\s*(m|h)", str(sig.get("timeframe") or "").strip())
    if not m:
        return 240
    return int(m.group(1)) * (60 if m.group(2) == "h" else 1)


def _summary(sig) -> dict:
    s = str(sig.get("indicators_summary") or "")
    out = {}
    if m := re.search(r"(\d)/(\d) agree", s):
        out["agree"] = int(m.group(1)) / int(m.group(2))
    for key, pat in (("rsi", r"RSI ([\d.]+)"), ("atr_logged", r"ATR ([\d.]+)%"), ("vol_x", r"Vol ([\d.]+)x"),
                     ("chg24_logged", r"24h ([+-][\d.]+)%")):
        if m := re.search(pat, s):
            out[key] = float(m.group(1))
    trig = str(sig.get("trigger_description") or "").lower()
    for key, words in (("chk_trend", "trending"), ("chk_vwap", "vwap"), ("chk_structure", "higher highs|lower highs"),
                       ("chk_rejection", "rejected|defended"), ("chk_flow", "own \\d+% of the volume"),
                       ("chk_divergence", "divergence"), ("chk_squeeze", "squeeze"), ("chk_spike", "spike|surge")):
        out[key] = int(bool(re.search(words, trig)))
    return out


def _run(s: Series1m, i: int, side: int, entry: float, tgt: float, stop: float, stop_pct: float, tgt_pct: float,
         hz: int) -> tuple:
    """(outcome, exit price, minutes) for one bracket held up to hz minutes from bar i."""
    if stop_pct <= 0:
        return "stop_passed", entry, 0
    if tgt_pct <= 0:
        return "target_passed", entry, 0
    last = min(len(s.t), i + hz)
    for j in range(i, last):
        if (s.l[j] <= stop) if side > 0 else (s.h[j] >= stop):
            return "lost", stop, j - i + 1
        if (s.h[j] >= tgt) if side > 0 else (s.l[j] <= tgt):
            return "won", tgt, j - i + 1
    if last - i < hz:
        return "running", None, None
    return "expired", s.c[last - 1], hz


def replay_one(sig: dict, s: Series1m, btc: Series1m | None, met: pd.DataFrame) -> dict | None:
    side = 1 if sig["direction"] == "long" else -1
    t0 = int(pd.Timestamp(sig["timestamp"], tz="UTC").timestamp() * 1000)
    i = s.idx((t0 // 60_000 + 1) * 60_000)                 # first bar that opens after the signal
    if i <= 1440 or i >= len(s.t) or s.t[i] - t0 > 5 * 60_000:
        return None
    k = i - 1                                              # last closed bar at the signal
    entry = s.o[i]
    tgt, stop = float(sig["target_price"]), float(sig["stop_loss"])
    stop_pct = (entry - stop) / entry * 100 * side           # > 0: stop below a long / above a short
    tgt_pct = (tgt - entry) / entry * 100 * side
    hz = _horizon(sig)
    end = min(len(s.t), i + max(hz, max(FWD_MIN), 1440) + 1)
    row = {"id": sig["id"], "entry": entry, "slip_pct": (entry / float(sig["current_price"]) - 1) * 100 * side,
           "stop_pct_e": stop_pct, "tgt_pct_e": tgt_pct, "horizon_min": hz, "bars_after": end - i}
    # ---- replay with the logged target and stop (stop first when one bar touches both), over the signal's own
    # horizon (how a trade would be held) and over 24 h (how the live resolver labels won / lost)
    for suffix, window in (("", hz), ("_24h", 1440)):
        out, exit_px, mins = _run(s, i, side, entry, tgt, stop, stop_pct, tgt_pct, window)
        row.update({f"replay{suffix}": out, f"minutes{suffix}": mins})
        if exit_px is not None:
            g = (exit_px / entry - 1) * 100 * side
            row.update({f"gross_pct{suffix}": g, f"net_pct{suffix}": g - COST_PCT,
                        f"r_net{suffix}": (g - COST_PCT) / stop_pct if stop_pct > 0 else np.nan})
    # ---- path: signed forward returns, MFE / MAE in R, +1R before -1R
    for m in FWD_MIN:
        j = i + m - 1
        row[f"fwd_{m}"] = (s.c[j] / entry - 1) * 100 * side if j < len(s.t) else np.nan
    seg_h, seg_l = s.h[i:min(len(s.t), i + hz)], s.l[i:min(len(s.t), i + hz)]
    if len(seg_h) and stop_pct > 0:
        fav = (seg_h.max() / entry - 1) * 100 if side > 0 else (1 - seg_l.min() / entry) * 100
        adv = (1 - seg_l.min() / entry) * 100 if side > 0 else (seg_h.max() / entry - 1) * 100
        row.update(mfe_r=fav / stop_pct, mae_r=adv / stop_pct)
        up = entry * (1 + side * stop_pct / 100)
        dn = entry * (1 - side * stop_pct / 100)
        first = "none"
        for j in range(i, min(len(s.t), i + 480)):
            a = s.h[j] >= up if side > 0 else s.l[j] <= up
            b = s.l[j] <= dn if side > 0 else s.h[j] >= dn
            if b:
                first = "-1R"
                break
            if a:
                first = "+1R"
                break
        row["first_1r"] = first
    # ---- features, from bars closed before the signal (index <= k)
    c = s.c[k]
    f = {f"f_ret{n}": s.ret(k, n) * side for n in (5, 15, 60, 240, 1440)}
    f["f_vol60"] = s.vol60[k]
    f["f_volrank"] = s.vol60[k] / s.vol60_med7d[k] if s.vol60_med7d[k] else np.nan
    v15 = s.window(k - 14, k, s.cv)
    v24 = s.window(k - 1439, k, s.cv)
    f["f_vsurge15"] = v15 / (v24 / 96) if v24 else np.nan
    for n in (5, 15, 60):
        vv = s.window(k - n + 1, k, s.cv)
        f[f"f_flow{n}"] = (s.window(k - n + 1, k, s.ctb) / vv - 0.5) * side if vv else np.nan
    for lab, hi, lo in (("3h", s.hi3h, s.lo3h), ("24h", s.hi24, s.lo24)):
        pos = (c - lo[k]) / (hi[k] - lo[k]) if hi[k] > lo[k] else np.nan
        f[f"f_pos{lab}"] = pos if side > 0 else 1 - pos       # 1 = buying the top / selling the bottom
    vw = s.window(k - 1439, k, s.cpv) / v24 if v24 else np.nan
    f["f_vwap24"] = (c / vw - 1) * 100 * side if vw else np.nan
    f["f_stop_atr"] = stop_pct / s.atr5_pct[k] if s.atr5_pct[k] else np.nan
    f["f_tgt_atr"] = tgt_pct / s.atr5_pct[k] if s.atr5_pct[k] else np.nan
    f["f_atr5"] = s.atr5_pct[k]
    if btc is not None:
        b = btc.idx((t0 // 60_000 + 1) * 60_000) - 1
        if 1440 < b < len(btc.t):
            f.update(f_btc60=btc.ret(b, 60) * side, f_btc240=btc.ret(b, 240) * side, f_btc1440=btc.ret(b, 1440) * side,
                     f_btcvol=btc.vol60[b])
    if len(met):
        j = int(np.searchsorted(met.t.to_numpy(), t0, side="right")) - 1
        if j >= 48:
            oi = met.sum_open_interest.to_numpy(float)
            f["f_oi60"] = (oi[j] / oi[j - 12] - 1) * 100
            f["f_oi240"] = (oi[j] / oi[j - 48] - 1) * 100
            f["f_top_ls"] = math.log(met.sum_toptrader_long_short_ratio.iloc[j]) * side
            f["f_crowd_ls"] = math.log(met.count_long_short_ratio.iloc[j]) * side
            tk = met.sum_taker_long_short_vol_ratio.iloc[j - 5:j + 1].to_numpy(float)
            f["f_taker_ls30"] = float(np.log(tk[tk > 0]).mean()) * side if (tk > 0).any() else np.nan
    ts = pd.Timestamp(t0, unit="ms", tz="UTC")
    f.update(hour=ts.hour, dow=ts.dayofweek)
    row.update(f)
    row.update(_summary(sig))
    return row


def replay(signals_path: str, out_dir: str) -> pd.DataFrame:
    sigs = json.load(gzip.open(signals_path) if signals_path.endswith(".gz") else open(signals_path))
    sigs = [s for s in sigs if not s.get("suppressed_by")]
    out = Path(out_dir)
    since = min(s["timestamp"] for s in sigs)[:10]
    syms = sorted({s["symbol"].upper() for s in sigs})
    series, metrics = {}, {}
    for sym in syms + ["BTCUSDT"]:
        src = SYMBOL_MAP.get(sym, sym)
        if src in series or not (LAKE / "klines" / src).exists():
            continue
        series[src] = Series1m(load_1m(src, since, out / "bars"))
        metrics[src] = load_metrics(src)
        print(f"{src}: {len(series[src].t)} bars to {pd.Timestamp(series[src].t[-1], unit='ms')}", flush=True)
    rows, skipped = [], {}
    for s in sigs:
        src = SYMBOL_MAP.get(s["symbol"].upper(), s["symbol"].upper())
        if src not in series:
            skipped[src] = skipped.get(src, 0) + 1
            continue
        r = replay_one(s, series[src], series.get("BTCUSDT"), metrics[src])
        if r is None:
            skipped["no bars"] = skipped.get("no bars", 0) + 1
            continue
        rows.append(r)
    meta = pd.DataFrame(sigs)
    keep = ["id", "symbol", "signal_type", "direction", "timeframe", "confidence", "edge_pct", "current_price",
            "target_price", "stop_loss", "outcome", "pnl_pct", "timestamp", "trade_mode", "candidate_role",
            "mirror_of_log_id", "parent_signal_id", "skip_reason", "veto_reason", "rejection_reason", "stake_pct",
            "sentiment_score", "review_round"]
    df = meta[[k for k in keep if k in meta]].merge(pd.DataFrame(rows), on="id", how="inner")
    out.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out / "replayed.parquet")
    print(f"replayed {len(df)} of {len(sigs)} signals; skipped {skipped}")
    return df


if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "replay":
        replay(sys.argv[2], sys.argv[3])
    else:
        print(__doc__)
