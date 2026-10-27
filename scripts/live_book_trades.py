"""Build the live-book simulator's trade set (analysis/simulator/live_book.py; owner 27 Oct 2026).

    python -m scripts.live_book_trades --full            # every trade the lake allows (run where the 5-year lake is)
    python -m scripts.live_book_trades --days 45         # rebuild the last 45 days and merge (nightly on the server)

The trades are the lab's: the 8 live specs on the 12 live coins with the live exit (rb_std_c60_b25), Delta fees and
funding, 1m execution. A rebuild loads 200 days more than it keeps (the lab needs 500 signal bars: 8h bars take ~170
days) so indicators warm up as in a full build. A coin / timeframe without enough data for the rebuild keeps its old
trades (a short data gap must never delete history). A trade the data cut short (a "time" exit before 7 days) is still
open and is left out.
"""
from __future__ import annotations

import argparse
import json
import tempfile
import time
from pathlib import Path

import numpy as np
import pandas as pd

from analysis.simulator.live_book import META_PATH, TRADES_PATH

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT", "ADAUSDT", "DOGEUSDT", "AVAXUSDT", "LINKUSDT",
           "SUIUSDT", "TIAUSDT", "PAXGUSDT"]
RUNS = (("4h", 2.5), ("8h", 2.0))            # the live specs: Keltner k differs by timeframe
HOLD_MS = 7 * 86_400_000
WARMUP_DAYS = 200
MIN_SIGNAL_BARS = 500            # analysis/lab/runner.work_symbol skips a symbol with fewer
KEY = ["symbol", "tf", "strategy", "sig_t", "side"]


def covered(start: str | None, root: str = "data/lake") -> set[tuple[str, str]]:
    """(symbol, tf) pairs the lab can actually run from `start` (enough signal bars, and 1m bars to execute on)."""
    from analysis.lab.data import load_bars
    s0 = int(pd.Timestamp(start, tz="UTC").timestamp() * 1000) if start else None
    out = set()
    for sym in SYMBOLS:
        try:
            if not len(load_bars(sym, "1m", root, "um", s0, None)):
                continue
        except Exception:  # noqa: BLE001 - a missing symbol is simply not covered
            continue
        for tf, _ in RUNS:
            try:
                if len(load_bars(sym, tf, root, "um", s0, None)) >= MIN_SIGNAL_BARS:
                    out.add((sym, tf))
            except Exception:  # noqa: BLE001
                pass
    return out


def lab_trades(start: str | None, root: str = "data/lake", workers: int = 2) -> pd.DataFrame:
    from analysis.lab.runner import RunSpec, run_lab
    from scripts.run_lab import EXITS
    parts = []
    for tf, k in RUNS:
        spec = RunSpec(strategies=["vol_breakout", "keltner_break", "donchian", "ichimoku"], symbols=SYMBOLS,
                       exits=[EXITS["rb_std_c60_b25"]], cost="india_gst", sig_tf=tf, exec_tf="1m", start=start,
                       overrides={"vol_breakout": {"z": 3.0}, "keltner_break": {"k": k}, "donchian": {"n": 100}},
                       root=root)
        with tempfile.TemporaryDirectory() as tmp:
            res = run_lab(spec, workers=workers, out_dir=tmp, log=lambda *a, **k: None)
        t = res["trades"].copy()
        t["tf"] = tf
        parts.append(t)
    return pd.concat(parts, ignore_index=True)


def prepare(t: pd.DataFrame) -> pd.DataFrame:
    """The columns the live simulator reads: spec index, coin (gold = XAUT), BTC volatility filter, priority."""
    from analysis import swing_book as sb
    from scripts.edge_report import tag_regimes
    from scripts.live_sim import GOLD, SPECS
    t = t.copy()
    t["spec"] = [SPECS.index((tf, s)) if (tf, s) in SPECS else -1 for tf, s in zip(t.tf, t.strategy)]
    t = t[t.spec >= 0].copy()
    t["coin"] = np.where(t.symbol == GOLD, "XAUT", t.symbol.str.replace("USDT", "", regex=False))
    t = tag_regimes(t)
    t["filter_ok"] = t.btc_vol_rank <= 2 / 3
    t["prio"] = [sb.priority(f"swing_{s}", tf, c + "USDT") for s, tf, c in zip(t.strategy, t.tf, t.coin)]
    return t


def drop_open(t: pd.DataFrame) -> pd.DataFrame:
    """A 'time' exit shorter than the 7-day limit means the data ended first: the trade is still open."""
    cut = (t.reason == "time") & ((t.exit_t - t.entry_t) < HOLD_MS - 3_600_000)
    return t[~cut]


def merge(old: pd.DataFrame, new: pd.DataFrame, since_ms: int, rebuilt: set | None = None) -> pd.DataFrame:
    """Old trades before the rebuilt window, new ones inside it; one row per signal. `rebuilt`: the (symbol, tf) pairs
    the rebuild covered - the others keep their old trades inside the window too."""
    if len(old):
        in_window = old.entry_t >= since_ms
        if rebuilt is not None:
            was_rebuilt = pd.Series([(s, tf) in rebuilt for s, tf in zip(old.symbol, old.tf)], index=old.index)
            keep = old[~in_window | ~was_rebuilt]
        else:
            keep = old[~in_window]
    else:
        keep = old
    out = pd.concat([keep, new[new.entry_t >= since_ms]], ignore_index=True)
    return out.drop_duplicates(KEY, keep="last").sort_values("entry_t").reset_index(drop=True)


def write(t: pd.DataFrame, path: Path = TRADES_PATH, meta_path: Path = META_PATH, mode: str = "") -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp.parquet")
    t.to_parquet(tmp, index=False)
    tmp.replace(path)
    meta = {"refreshed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "mode": mode, "trades": int(len(t)),
            "first_entry_ms": int(t.entry_t.min()) if len(t) else None,
            "last_entry_ms": int(t.entry_t.max()) if len(t) else None}
    meta_path.write_text(json.dumps(meta))
    return meta


def main() -> None:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--full", action="store_true")
    g.add_argument("--days", type=int)
    ap.add_argument("--workers", type=int, default=2)
    a = ap.parse_args()
    if a.full:
        t = drop_open(prepare(lab_trades(None, workers=a.workers)))
        print(write(t, mode="full"))
        return
    since = pd.Timestamp.now(tz="UTC").normalize() - pd.Timedelta(days=a.days)
    load_from = (since - pd.Timedelta(days=WARMUP_DAYS)).strftime("%Y-%m-%d")
    rebuilt = covered(load_from)
    new = drop_open(prepare(lab_trades(load_from, workers=a.workers)))
    new = new[[(s, tf) in rebuilt for s, tf in zip(new.symbol, new.tf)]]
    old = pd.read_parquet(TRADES_PATH) if TRADES_PATH.exists() else pd.DataFrame(columns=new.columns)
    meta = write(merge(old, new, int(since.timestamp() * 1000), rebuilt), mode=f"last {a.days} days")
    missing = sorted({f"{s} {tf}" for s in SYMBOLS for tf, _ in RUNS} - {f"{s} {tf}" for s, tf in rebuilt})
    print({**meta, "not_rebuilt": missing})


if __name__ == "__main__":
    main()
