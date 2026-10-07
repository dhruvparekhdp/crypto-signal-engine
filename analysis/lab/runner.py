"""Run many strategies x parameters x exit models over real data, in parallel, and save the lot.

    python -m scripts.run_lab --strategies donchian,rsi2 --years 2

Work is split by symbol so each worker holds one symbol's bars. Everything is seeded and
deterministic: the same arguments produce byte-identical trades.
"""
from __future__ import annotations

import json
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from analysis.lab import ledger
from analysis.lab.costs import PRESETS, CostModel
from analysis.lab.data import LAKE, Bars, available_symbols, load_bars
from analysis.lab.metrics import by, trade_stats, walk_forward
from analysis.lab.simulate import COLS, ExitModel, simulate_symbol
from analysis.lab.strategies import REGISTRY
from analysis.lab.wallet import WalletConfig, cycle_odds, run_wallet
from analysis.significance import judge, perm_p

CRYPTO = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT", "ADAUSDT", "DOGEUSDT", "AVAXUSDT",
          "LINKUSDT", "LTCUSDT", "BCHUSDT", "SUIUSDT"]


@dataclass
class RunSpec:
    strategies: list[str]
    symbols: list[str] = field(default_factory=lambda: list(CRYPTO))
    exits: list[ExitModel] = field(default_factory=lambda: [ExitModel()])
    cost: str = "india_gst"
    grid: bool = False
    grid_limit: int = 6
    sig_tf: str | None = None
    exec_tf: str = "1m"
    start: str | None = None
    end: str | None = None
    null_trials: int = 0
    overrides: dict = field(default_factory=dict)   # {strategy_id: {param: value}}
    root: str = str(LAKE)
    seed: int = 1
    # trial ledger (analysis/lab/ledger.py): a family name records every configuration and sets N for Bonferroni
    ledger_family: str | None = None
    ledger_kind: str = "search"
    hypothesis: str = ""
    proposed_by: str = "owner"

    def window_ms(self):
        ms = lambda s: int(pd.Timestamp(s, tz="UTC").timestamp() * 1000) if s else None
        return ms(self.start), ms(self.end)


def cfg_key(strategy: str, params: dict, exit: ExitModel) -> str:
    p = ",".join(f"{k}={v}" for k, v in sorted(params.items()))
    return f"{strategy}|{p}|{exit.key()}"


def _random_signals(n: int, count: int, seed: int, warm: int = 60) -> np.ndarray:
    rng = np.random.default_rng(seed)
    sig = np.zeros(n, np.int8)
    if count and n > warm + 10:
        idx = rng.choice(np.arange(warm, n - 2), size=min(count, n - warm - 2), replace=False)
        sig[idx] = np.where(rng.random(len(idx)) < 0.5, 1, -1)
    return sig


def work_symbol(args) -> dict:
    symbol, spec = args
    s0, s1 = spec.window_ms()
    cost: CostModel = PRESETS[spec.cost]
    try:
        ex = load_bars(symbol, spec.exec_tf, spec.root, "um", s0, s1)
    except Exception as e:  # missing symbol
        return {"symbol": symbol, "error": str(e), "trades": {}, "null": {}}
    trades: dict[str, list] = {}
    null: dict[str, list] = {}
    tf_cache: dict[str, Bars] = {}
    for sid in spec.strategies:
        st = REGISTRY[sid]
        tf = spec.sig_tf or st.tf
        if tf not in tf_cache:
            tf_cache[tf] = load_bars(symbol, tf, spec.root, "um", s0, s1)
        sb = tf_cache[tf]
        if len(sb) < 500:
            continue
        if sid in spec.overrides:
            sets = [{**st.defaults, **spec.overrides[sid]}]
        else:
            sets = st.param_sets(spec.grid_limit) if spec.grid else [dict(st.defaults)]
        for params in sets:
            try:
                sig = st.signals(sb, params)
            except Exception as e:
                print(f"[{symbol}] {sid} failed: {e}", file=sys.stderr)
                continue
            count = int(np.count_nonzero(sig))
            for exm in spec.exits:
                key = cfg_key(sid, params, exm)
                rows = simulate_symbol(sb, sig, ex, exm, cost, sid)
                trades[key] = rows
                if spec.null_trials and count:
                    sums = []
                    for t in range(spec.null_trials):
                        rs = simulate_symbol(sb, _random_signals(len(sb), count, spec.seed * 1000 + t + sum(map(ord, symbol))),
                                             ex, exm, cost)
                        sums.append((sum(r[11] for r in rs), len(rs)))
                    null[key] = sums
    return {"symbol": symbol, "trades": trades, "null": null}


def _record_trials(spec: RunSpec, summary: pd.DataFrame, syms: list[str], log=print) -> int:
    """N for Bonferroni: this run's configurations, or the ledger family's count after recording them."""
    if not spec.ledger_family:
        return len(summary)
    data_fp = ledger.data_fingerprint(syms, spec.root)
    codes, rows = {}, []
    for _, r in summary.iterrows():
        sid = r["strategy"]
        codes.setdefault(sid, ledger.code_digest(sid))
        tf = spec.sig_tf or REGISTRY[sid].tf
        params = dict(kv.split("=", 1) for kv in r["params"].split(",") if kv)
        rows.append({"digest": ledger.trial_digest(sid, params, tf, r["exit"], (spec.start, spec.end), spec.cost,
                                                   data_fp, codes[sid]),
                     "cfg": r["cfg"], "timeframe": tf, "symbols": len(syms), "trades": int(r.get("trades", 0) or 0),
                     "expectancy_r": r.get("expectancy_r"), "null_p": r.get("null_p")})
    res = ledger.record(rows, spec.ledger_family, spec.ledger_kind, spec.hypothesis, spec.proposed_by)
    log(f"ledger: {res['new']} new trials, family '{spec.ledger_family}' N={res['n_family']}")
    return max(res["n_family"], len(summary))


def run_lab(spec: RunSpec, workers: int = 6, out_dir: str | None = None, wallets: list[WalletConfig] | None = None,
            log=print) -> dict:
    t0 = time.time()
    syms = [s for s in spec.symbols if s in set(available_symbols(spec.root))]
    log(f"lab: {len(spec.strategies)} strategies x {len(syms)} symbols, exec={spec.exec_tf}, cost={spec.cost}")
    results = []
    if workers <= 1:
        for s in syms:
            results.append(work_symbol((s, spec)))
            log(f"  {s} done ({time.time() - t0:.0f}s)")
    else:
        with ProcessPoolExecutor(workers) as pool:
            for r in pool.map(work_symbol, [(s, spec) for s in syms]):
                results.append(r)
                log(f"  {r['symbol']} done ({time.time() - t0:.0f}s)")
    frames, null_acc = [], {}
    for r in results:
        for key, rows in r["trades"].items():
            if rows:
                d = pd.DataFrame(rows, columns=COLS)
                d["cfg"] = key
                frames.append(d)
        for key, sums in r["null"].items():
            acc = null_acc.setdefault(key, np.zeros((len(sums), 2)))
            acc += np.array(sums, float)
    trades = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=COLS + ["cfg"])
    trades["strategy"] = trades["cfg"].str.split("|").str[0]
    trades = trades.sort_values(["entry_t", "symbol"], kind="stable").reset_index(drop=True)

    rows = []
    for key, g in trades.groupby("cfg"):
        s = trade_stats(g)
        s.update(cfg=key, strategy=key.split("|")[0], params=key.split("|")[1], exit=key.split("|")[2])
        if key in null_acc:
            acc = null_acc[key]
            means = acc[:, 0] / np.maximum(acc[:, 1], 1)
            s["null_mean_r"] = float(means.mean())
            s["null_sd_r"] = float(means.std())
            s["null_trials"] = len(means)
            s["null_p"] = perm_p(int((means >= s["expectancy_r"]).sum()), len(means))   # never 0
        rows.append(s)
    summary = pd.DataFrame(rows)
    n_tried = _record_trials(spec, summary, syms, log) if len(summary) else 0
    if "null_p" in summary:
        summary["n_tried"] = n_tried
        summary["null_verdict"] = [judge(p, n_tried, t) if p == p else None
                                   for p, t in zip(summary["null_p"], summary["null_trials"])]
    out = {"spec": {**asdict(spec), "exits": [asdict(e) for e in spec.exits]}, "summary": summary, "trades": trades,
           "elapsed_s": time.time() - t0}

    if wallets:
        wr = []
        for key, g in trades.groupby("cfg"):
            for w in wallets:
                res = run_wallet(g, w).summary()
                res.update(cfg=key, strategy=key.split("|")[0])
                s = trade_stats(g, boot=1)
                mc = cycle_odds(g["net_ret"].to_numpy(), g["stop_frac"].to_numpy(), w, n_cycles=4000)
                res.update(mc_p_target=mc["p_target"], mc_p_bust=mc["p_bust"])
                wr.append(res)
        out["wallet"] = pd.DataFrame(wr)
    if out_dir:
        p = Path(out_dir)
        p.mkdir(parents=True, exist_ok=True)
        trades.to_parquet(p / "trades.parquet")
        summary.to_csv(p / "summary.csv", index=False)
        if "wallet" in out:
            out["wallet"].drop(columns=["skipped"], errors="ignore").to_csv(p / "wallet.csv", index=False)
        (p / "spec.json").write_text(json.dumps(out["spec"], indent=1, default=str))
        log(f"saved to {p}")
    return out
