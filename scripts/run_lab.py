"""Strategy Lab command line. No AI, no network: reads data/lake and writes data/lab/runs/.

    python -m scripts.run_lab --list
    python -m scripts.run_lab --strategies donchian,rsi2,bb_reversion --since 2023-01-01
    python -m scripts.run_lab --family mean_reversion,trend --grid --null-trials 30
    python -m scripts.run_lab --all --exits std,trail --risk 0.01,0.02,0.05 --leverage 3,5,10
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

import pandas as pd

from analysis.lab.data import coverage
from analysis.lab.runner import CRYPTO, RunSpec, run_lab
from analysis.lab.simulate import ExitModel
from analysis.lab.strategies import REGISTRY, catalog
from analysis.lab.wallet import WalletConfig

EXITS = {
    "std": ExitModel(stop_atr=1.5, rr=2.0),
    "tight": ExitModel(stop_atr=1.0, rr=1.5, max_hold_min=480),
    "wide": ExitModel(stop_atr=2.5, rr=2.0, max_hold_min=2880),
    "scalp": ExitModel(stop_atr=1.0, rr=1.0, max_hold_min=240),
    "be": ExitModel(stop_atr=1.5, rr=3.0, be_trigger_r=1.0),
    "trail": ExitModel(stop_atr=2.0, rr=8.0, be_trigger_r=1.0, trail_atr=2.0, max_hold_min=4320),
    "partial": ExitModel(stop_atr=1.5, rr=3.0, partial_r=1.0, partial_frac=0.5, be_trigger_r=1.0),
    "swing": ExitModel(stop_atr=3.0, rr=3.0, max_hold_min=10080),
    # Roadmap Q-4 / blueprint B5 exit variants on the swing book (7-day limit kept so only the exit rule changes)
    "b5_tight": ExitModel(stop_atr=1.75, rr=3.0, max_hold_min=10080),
    "b5_partial": ExitModel(stop_atr=3.0, rr=6.0, partial_r=1.5, partial_frac=0.5, be_trigger_r=1.5,
                            trail_atr=3.0, trail_after_r=1.5, max_hold_min=10080),
    "b5_nnfx": ExitModel(stop_atr=1.5, rr=6.0, partial_r=0.667, partial_frac=0.5, be_trigger_r=0.667,
                         trail_atr=1.5, trail_after_r=0.667, max_hold_min=10080),
    # 12 Oct 2026, owner: lock profit on swing trades that ran far then came back. Target stays 3R.
    "lock_be15": ExitModel(stop_atr=3.0, rr=3.0, be_trigger_r=1.5, max_hold_min=10080),
    "lock_be2": ExitModel(stop_atr=3.0, rr=3.0, be_trigger_r=2.0, max_hold_min=10080),
    "lock_tr2_1r": ExitModel(stop_atr=3.0, rr=3.0, trail_atr=3.0, trail_after_r=2.0, max_hold_min=10080),
    "lock_tr2_half": ExitModel(stop_atr=3.0, rr=3.0, trail_atr=1.5, trail_after_r=2.0, max_hold_min=10080),
    "lock_tr15": ExitModel(stop_atr=3.0, rr=3.0, trail_atr=2.0, trail_after_r=1.5, max_hold_min=10080),
    # 13 Oct 2026 smart-switch options (owner): S1 ladder, S2 trail from 1R/1.5R, S3 giveback, S4 stale, S5 bank half
    "s1_ladder": ExitModel(stop_atr=3.0, rr=3.0, max_hold_min=10080, ladder=((1.0, 0.0), (1.5, 0.5), (2.0, 1.0), (2.5, 1.5))),
    "s1_ladder_tight": ExitModel(stop_atr=3.0, rr=3.0, max_hold_min=10080,
                                 ladder=((1.0, 0.25), (1.5, 0.75), (2.0, 1.25), (2.5, 1.75))),
    "s2_trail1r_075": ExitModel(stop_atr=3.0, rr=3.0, trail_atr=2.25, trail_after_r=1.0, max_hold_min=10080),
    "s2_trail1r_1": ExitModel(stop_atr=3.0, rr=3.0, trail_atr=3.0, trail_after_r=1.0, max_hold_min=10080),
    "s2_trail15r_075": ExitModel(stop_atr=3.0, rr=3.0, trail_atr=2.25, trail_after_r=1.5, max_hold_min=10080),
    "s3_giveback15": ExitModel(stop_atr=3.0, rr=3.0, giveback_after_r=1.5, giveback_frac=0.5, max_hold_min=10080),
    "s3_giveback1": ExitModel(stop_atr=3.0, rr=3.0, giveback_after_r=1.0, giveback_frac=0.5, max_hold_min=10080),
    "s4_stale3d": ExitModel(stop_atr=3.0, rr=3.0, stale_after_min=4320, stale_min_r=1.0, max_hold_min=10080),
    "s4_stale4d": ExitModel(stop_atr=3.0, rr=3.0, stale_after_min=5760, stale_min_r=1.0, max_hold_min=10080),
    "s5_bank_half": ExitModel(stop_atr=3.0, rr=3.0, partial_r=1.5, partial_frac=0.5, be_trigger_r=1.5, max_hold_min=10080),
    "s6_ladder_stale3d": ExitModel(stop_atr=3.0, rr=3.0, max_hold_min=10080, stale_after_min=4320, stale_min_r=1.0,
                                   ladder=((1.0, 0.0), (1.5, 0.5), (2.0, 1.0), (2.5, 1.5))),
    # 13 Oct robustness grid (owner: dips that rebound within 1-2 funding cycles must not shake trades out)
    **{f"rb_{ln}_c{cf or 0}_b{int(bf * 100)}": ExitModel(stop_atr=3.0, rr=3.0, max_hold_min=10080, ladder=lad,
                                                          lock_confirm_min=cf, lock_buffer_r=bf)
       for ln, lad in (("std", ((1.0, 0.0), (1.5, 0.5), (2.0, 1.0), (2.5, 1.5))),
                       ("slow", ((1.5, 0.0), (2.0, 0.5), (2.5, 1.0))),
                       ("fast", ((0.75, 0.0), (1.25, 0.5), (1.75, 1.0), (2.25, 1.5))))
       for cf in (None, 60, 240) for bf in (0.0, 0.25)},
    # 21 Oct 2026 edge research: 31% of live-exit trades close at the 7-day limit, averaging +0.56R with 71% in profit -
    # does a longer hold (or a farther target) keep more of those? Same ladder, confirmation and buffer as the live exit
    **{name: ExitModel(stop_atr=3.0, rr=rr, max_hold_min=hold, lock_confirm_min=60, lock_buffer_r=0.25,
                       ladder=((1.0, 0.0), (1.5, 0.5), (2.0, 1.0), (2.5, 1.5)))
       for name, hold, rr in (("live_h10d", 14400, 3.0), ("live_h14d", 20160, 3.0), ("live_h14d_rr5", 20160, 5.0))},
    # 18 Oct 2026 owner: "at 1.28R the lock should be 0.75-1R" - a tight trail from 1R, lock = best - gap, stepped
    # every 0.25R, with the live 1h-close confirmation (compare with rb_std_c60_b25, the live ladder)
    **{f"own_gap{int(g * 100):02d}_c60": ExitModel(stop_atr=3.0, rr=3.0, max_hold_min=10080, lock_confirm_min=60,
                                                    ladder=tuple((1.0 + 0.25 * i, round(1.0 + 0.25 * i - g, 2))
                                                                 for i in range(8)))
       for g in (0.5, 0.3)},
    # 23 Oct 2026 owner: "trade reached 2.8R then profit order trail to 3.25 and 2.3 to 2.5R for sl profit" - on top of
    # the live exit (rb_std_c60_b25). Ladder locks are written +0.25 because the live 0.25R buffer is subtracted, so
    # the stop really sits at 2.3R / 2.5R. "ratchet": the same step every 0.25R up to 6R (target always 0.45R above the
    # trigger, stop 0.5R / 0.3R under it); lock_only / ext_only split the idea into its two halves
    **{name: ExitModel(stop_atr=3.0, rr=3.0, max_hold_min=10080, lock_confirm_min=60, lock_buffer_r=0.25,
                       ladder=((1.0, 0.0), (1.5, 0.5), (2.0, 1.0), (2.5, 1.5)) + lad, target_steps=ts)
       for name, lad, ts in (
           ("x_own_l23", ((2.8, 2.55),), ((2.8, 3.25),)),
           ("x_own_l25", ((2.8, 2.75),), ((2.8, 3.25),)),
           ("x_lock_only_l25", ((2.8, 2.75),), ()),
           ("x_ext_only", (), ((2.8, 3.25),)),
           ("x_ratchet_g05", tuple((round(2.8 + 0.25 * i, 2), round(2.55 + 0.25 * i, 2)) for i in range(14)),
            tuple((round(2.8 + 0.25 * i, 2), round(3.25 + 0.25 * i, 2)) for i in range(14))),
           ("x_ratchet_g03", tuple((round(2.8 + 0.25 * i, 2), round(2.75 + 0.25 * i, 2)) for i in range(14)),
            tuple((round(2.8 + 0.25 * i, 2), round(3.25 + 0.25 * i, 2)) for i in range(14))))},
    # 18 Oct 2026 smart switch on top of the live ladder (1/1.5/2/2.5R, 0.25R buffer, 1h-close confirmation)
    **{name: ExitModel(stop_atr=3.0, rr=3.0, max_hold_min=10080, lock_confirm_min=60, lock_buffer_r=0.25,
                       ladder=((1.0, 0.0), (1.5, 0.5), (2.0, 1.0), (2.5, 1.5)), **extra)
       for name, extra in (("smart_flip20_g05", {"flip_ema": 20, "flip_gap_r": 0.5}),
                           ("smart_flip20_g075", {"flip_ema": 20, "flip_gap_r": 0.75}),
                           ("smart_decay3d", {"decay_after_min": 4320, "decay_frac": 0.5}),
                           ("smart_flip_decay", {"flip_ema": 20, "flip_gap_r": 0.75, "decay_after_min": 4320,
                                                 "decay_frac": 0.5}))},
    # 18 Oct 2026 scalping research (owner): 5m signals, stop 2 x ATR(5m) (0.3%..2%), 1R or 1.5R target, 4 h limit;
    # a market entry at the next open, or a limit at the signal close resting 15 minutes (maker fill)
    **{f"scalp_{tag}_{ent}": ExitModel(stop_atr=2.0, rr=rr, max_hold_min=240, min_stop_pct=0.003, max_stop_pct=0.02,
                                        entry_limit_bps=(0.0 if ent == "lim" else None), entry_window_min=15)
       for tag, rr in (("1r", 1.0), ("15r", 1.5)) for ent in ("mkt", "lim")},
    # 18 Oct 2026 day-scalp iteration: 5m scalps were fee-bound (stops ~0.43% -> fees 0.15R). On 15m/1h signals: stop
    # 2 x ATR (0.5%..3%), 1R or 1.5R, 12 h limit, market or 30-minute limit entry - the 1.5-3% intraday swings
    **{f"day_{tag}_{ent}": ExitModel(stop_atr=2.0, rr=rr, max_hold_min=720, min_stop_pct=0.005, max_stop_pct=0.03,
                                      entry_limit_bps=(0.0 if ent == "lim" else None), entry_window_min=30)
       for tag, rr in (("1r", 1.0), ("15r", 1.5)) for ent in ("mkt", "lim")},
    # 15 Oct 2026 intraday research (owner: 1h minimum): two exit styles for 1h/2h signals
    "intra_a": ExitModel(stop_atr=2.0, rr=2.0, max_hold_min=1440),
    "intra_b": ExitModel(stop_atr=1.5, rr=3.0, max_hold_min=2880),
    "daily_trend": ExitModel(stop_atr=3.0, rr=3.0, max_hold_min=43200),     # Q-7: daily bars need a 30-day limit
    "swing_trail": ExitModel(stop_atr=3.0, rr=10.0, be_trigger_r=1.0, trail_atr=3.0, trail_after_r=1.0,
                             max_hold_min=10080),
}


def _overrides(items):
    out = {}
    for it in items:
        left, val = it.split("=", 1)
        sid, name = left.split(".", 1)
        try:
            v = int(val)
        except ValueError:
            try:
                v = float(val)
            except ValueError:
                v = val
        out.setdefault(sid, {})[name] = v
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true", help="show every strategy and exit")
    ap.add_argument("--strategies", default="", help="comma list of strategy ids")
    ap.add_argument("--family", default="", help="comma list of families")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--exclude", default="")
    ap.add_argument("--symbols", default=",".join(CRYPTO))
    ap.add_argument("--since", default=None)
    ap.add_argument("--until", default=None)
    ap.add_argument("--exits", default="std", help=",".join(EXITS))
    from analysis.lab.costs import PRESETS as _COSTS
    ap.add_argument("--cost", default="india_gst", choices=sorted(_COSTS))
    ap.add_argument("--exec", default="1m", dest="exec_tf")
    ap.add_argument("--tf", default=None, help="override every strategy's signal timeframe")
    ap.add_argument("--grid", action="store_true", help="sweep each strategy's parameter grid")
    ap.add_argument("--grid-limit", type=int, default=6)
    ap.add_argument("--null-trials", type=int, default=0)
    ap.add_argument("--set", action="append", default=[], help="strategy.param=value, repeatable")
    ap.add_argument("--risk", default="0.02", help="wallet risk per trade, comma list")
    ap.add_argument("--leverage", default="5", help="wallet leverage, comma list")
    ap.add_argument("--concurrent", default="1")
    ap.add_argument("--start-balance", type=float, default=25.0)
    ap.add_argument("--target", type=float, default=100.0)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--out", default=None)
    ap.add_argument("--ledger-family", default=None, help="record every configuration in the trial ledger")
    ap.add_argument("--ledger-kind", default="search", help="search | stress | ablation | live_rescore")
    ap.add_argument("--hypothesis", default="")
    ap.add_argument("--n-tried-floor", type=int, default=0,
                    help="Bonferroni N is at least this (e.g. configs tried before the ledger existed)")
    a = ap.parse_args()

    if a.list:
        for s in catalog():
            print(f"{s['id']:18} {s['family']:15} {s['source']:10} {s['tf']:4} {s['name']}")
        return
    ids = [i for i in a.strategies.split(",") if i]
    ids += [s.id for s in REGISTRY.values() if s.family in a.family.split(",") and s.id not in ids]
    if a.all:
        ids = list(REGISTRY)
    ids = [i for i in ids if i not in a.exclude.split(",")]
    bad = [i for i in ids if i not in REGISTRY]
    if bad or not ids:
        raise SystemExit(f"unknown or no strategies: {bad or 'none selected'} (use --list)")
    spec = RunSpec(strategies=ids, symbols=a.symbols.split(","), exits=[EXITS[e] for e in a.exits.split(",")],
                   cost=a.cost, grid=a.grid, grid_limit=a.grid_limit, sig_tf=a.tf, exec_tf=a.exec_tf,
                   start=a.since, end=a.until, null_trials=a.null_trials, overrides=_overrides(a.set),
                   ledger_family=a.ledger_family, ledger_kind=a.ledger_kind, hypothesis=a.hypothesis,
                   n_tried_floor=a.n_tried_floor)
    wallets = [WalletConfig(start=a.start_balance, target=a.target, risk_pct=float(r), leverage=float(l),
                            max_concurrent=int(c))
               for r in a.risk.split(",") for l in a.leverage.split(",") for c in a.concurrent.split(",")]
    out = a.out or f"data/lab/runs/{time.strftime('%Y%m%d-%H%M%S')}"
    res = run_lab(spec, workers=a.workers, out_dir=out, wallets=wallets)
    s = res["summary"].sort_values("expectancy_r", ascending=False)
    cols = ["strategy", "params", "n", "win_rate", "expectancy_r", "ci_lo", "ci_hi", "profit_factor", "max_dd_r"]
    if "null_p" in s:
        cols += ["null_mean_r", "null_p", "null_z", "null_p_normal", "n_tried", "null_verdict"]
    pd.set_option("display.width", 220, "display.max_colwidth", 38, "display.float_format", "{:.3f}".format)
    print(s[cols].head(25).to_string(index=False))
    print(f"\n{len(res['trades']):,} trades in {res['elapsed_s']:.0f}s -> {out}")


if __name__ == "__main__":
    main()
