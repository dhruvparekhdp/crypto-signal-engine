"""Point-in-time regime filters for the live trades, and filtered trade files for the wallet simulator.

Every threshold is computed only from data before the trade: BTC's 30-day volatility is ranked against its own
previous 365 days, the coin's daily ADX uses closed daily bars. Nothing is fitted on the whole sample.

    python -m scripts.regime_filter
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import numpy as np
import pandas as pd

from analysis.lab import features as F
from analysis.lab.data import load_bars

DAY = 86_400_000
SRC = {"4h": "data/lab/runs/cand_4h", "8h": "data/lab/runs/null_8h"}


def btc_vol_rank() -> tuple[np.ndarray, np.ndarray]:
    """(time known, percentile of BTC 30d vol within the previous 365 days), from closed daily bars."""
    b = load_bars("BTCUSDT", "1d")
    vol = pd.Series(np.diff(np.log(b.c), prepend=np.nan)).rolling(30).std()
    rank = vol.rolling(365, min_periods=180).apply(lambda w: (w[:-1] < w[-1]).mean(), raw=True)
    return b.t + DAY, rank.to_numpy()


def coin_adx(symbol: str) -> tuple[np.ndarray, np.ndarray]:
    b = load_bars(symbol, "1d")
    return b.t + DAY, F.adx(b.h, b.l, b.c)[0]


def tag(tr: pd.DataFrame) -> pd.DataFrame:
    bt, br = btc_vol_rank()
    i = np.searchsorted(bt, tr.sig_t.to_numpy(), side="right") - 1
    tr = tr.assign(btc_vol_rank=np.where(i >= 0, br[np.clip(i, 0, None)], np.nan))
    adx = np.full(len(tr), np.nan)
    for s in tr.symbol.unique():
        t, a = coin_adx(s)
        m = (tr.symbol == s).to_numpy()
        k = np.searchsorted(t, tr.sig_t.to_numpy()[m], side="right") - 1
        adx[m] = np.where(k >= 30, a[np.clip(k, 0, None)], np.nan)
    return tr.assign(coin_adx=adx)


RULES = {
    "all (live today)": lambda d: np.ones(len(d), bool),
    "skip wild BTC vol (rank > 0.67)": lambda d: ~(d.btc_vol_rank > 0.67),
    "only calm BTC vol (rank < 0.5)": lambda d: d.btc_vol_rank < 0.5,
    "skip coin ADX > 30": lambda d: ~(d.coin_adx > 30),
    "skip wild vol AND ADX > 30": lambda d: ~(d.btc_vol_rank > 0.67) & ~(d.coin_adx > 30),
}


def main():
    trs = []
    for tf, d in SRC.items():
        t = pd.read_parquet(f"{d}/trades.parquet")
        t = t[t.cfg.str.contains("sl3.0_rr3.0") & t.cfg.str.contains("h10080")]
        trs.append(t.assign(tf=tf))
    tr = tag(pd.concat(trs, ignore_index=True))
    tr["year"] = pd.to_datetime(tr.sig_t, unit="ms").dt.year
    out = {}
    for name, rule in RULES.items():
        k = tr[rule(tr).astype(bool)]
        yrs = {int(y): (len(x), round(float(x.r_net.mean()), 3)) for y, x in k.groupby("year")}
        se = k.r_net.std() / np.sqrt(len(k))
        out[name] = {"n": len(k), "kept": len(k) / len(tr), "r": float(k.r_net.mean()), "t": float(k.r_net.mean() / se),
                     "total_r": float(k.r_net.sum()), "by_year": yrs}
        print(f"{name:34} kept {len(k):5} ({len(k) / len(tr):.0%})  R/trade {k.r_net.mean():+.3f} (t {k.r_net.mean() / se:.1f})  "
              f"total {k.r_net.sum():6.0f} R  by year {yrs}")
    Path("data/lab/regime_filter.json").write_text(json.dumps(out, default=float))
    # filtered trade files so scripts.wallet_experiments can replay them
    for name, rule in [("filt_vol", RULES["skip wild BTC vol (rank > 0.67)"]), ("filt_both", RULES["skip wild vol AND ADX > 30"])]:
        for tf, d in SRC.items():
            dst = Path(f"data/lab/runs/{name}_{tf}")
            dst.mkdir(parents=True, exist_ok=True)
            part = tr[(tr.tf == tf) & rule(tr).astype(bool)].drop(columns=["tf", "year", "btc_vol_rank", "coin_adx"])
            part.to_parquet(dst / "trades.parquet")
            shutil.copy(f"{d}/spec.json", dst / "spec.json")
    print("saved data/lab/regime_filter.json and data/lab/runs/filt_*")


if __name__ == "__main__":
    main()
