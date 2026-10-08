# Findings ledger

Flat IDs, never reused. A finding is closed only when a named test fails on the old code and passes on the new,
and the probe that found it has been re-run. Seeded from docs/INTEGRATION_PLAN.md section 1 (7 Oct 2026).
Status: **fixed** (test named), **open**, **accepted** (known, deliberately not changed).

## Live paper trading

| ID | Finding (short) | Status | Test / note |
|---|---|---|---|
| L1 | Swing positions priced on spot, signals on perps | fixed (Phase 1) | `test_phase1.py::test_l1_swing_positions_and_entries_use_perp_prices` |
| L2 | Swing skipped account guards and news rule | fixed (Phase 1) | `test_phase1.py` (account guard, blackout tests) |
| L3 | Unbounded same-side risk | fixed (Phase 1) | `test_phase1.py::test_l3_total_same_side_risk_is_capped` |
| L4 | Stops filled at the level after a gap | fixed (Phase 1) | `test_phase1.py` (gap fill tests) |
| L5 | Live entry timing differs from backtest (up to 60 min late, no chase limit) | open | Phase 3: implementation-shortfall tracker (O4) |
| L6 | Restart re-fired swing signals | fixed (Phase 1) | `test_phase1.py` (persistent dedup) |
| L7 | Swing queue grew while paper off | fixed (Phase 1) | `test_phase1.py` (queue cap) |
| L8 | Regime gate failed open | fixed (Phase 1) | `test_phase1.py` (`regime_unknown`) |
| L9 | Bad swing spec silently dropped | fixed (Phase 1) | `test_phase1.py` (spec_problems / fail-closed) |
| L10 | Costs differ across paths | fixed for lab vs paper (both 0.188% stop-out) | `test_phase2_batteries.py::test_lab_and_paper_charge_the_same_for_a_stop_out`; regime scorer's flat 0.17% still open |
| L11 | Simulator keys on the command line | fixed (Phase 1) | `test_phase1.py` (keys via env) |
| L12 | One 1x trade could lock the whole wallet; book froze (7 Oct: ₹1,238 of ₹1,735) | fixed (branch) | `test_swing_sizing_binance.py::test_margin_cap_raises_leverage_instead_of_locking_the_wallet`, `::test_second_and_third_signals_still_get_real_positions` |
| L13 | Paper orders ignored Binance step / min qty / min order value (CoinDCX lot table, most coins missing) | fixed (branch) | `test_swing_sizing_binance.py::test_binance_rules_round_down_and_enforce_min_order_value`, `::test_btc_is_skipped_when_the_wallet_cannot_meet_binance_minimum` |
| L14 | No per-strategy forward clock or drawdown alarm | fixed (branch) | `test_strategy_registry.py` |

## Research tools

| ID | Finding (short) | Status | Test / note |
|---|---|---|---|
| R1 | Null test: wrong exits, p could be 0 | fixed (branch) | `test_phase2_significance.py::test_p_value_is_never_zero`; swing exits via `run_lab --exits swing --null-trials` |
| R2 | No multiple-testing correction | fixed (branch) | Bonferroni at ledger N (floor 579): `test_phase2_significance.py` (ledger + verdict tests) |
| R3 | Stepped simulator NameError | open | Phase 3 |
| R4 | Simulator checks target before stop on the same bar | open | Phase 3 (lab/v2 already stop-first: `test_lab_engine.py::test_same_bar_stop_and_target_resolves_to_the_stop`) |
| R5 | Synthetic candles / injected 2023 events | open | Phase 3 (lab loader refuses: `test_lab_engine.py::test_data_loader_never_fabricates_candles`) |
| R6 | Overlapping windows counted as independent | accepted, labelled | `scripts/sizing_backtest.py` and wallet reports say "windows overlap" |
| R7 | Survivorship (only today's perps) | open | Phase 3 |
| R8 | Outcome replay starts in the bar containing the signal | open | Phase 3 |
| R9 | Simulator R without fees, 1-bar "ATR" | open | Phase 3 |
| R10 | Walk-forward label leak (no purge) | open | Phase 3 |
| R11 | Significance treats clustered trades as independent | open | Phase 3 (O3); note: swing null z-scores (7 Oct) are overstated for the same reason |
| R12 | v2 "run now" silently no-op when disabled | open | Phase 3 |
| R13 | Lake clock and cost inputs unchecked (seconds vs ms, negative fees) | fixed (branch) | `test_phase2_batteries.py::test_lake_clock_must_be_epoch_ms_on_the_grid`, `::test_lab_cost_model_refuses_nonsense`, `::test_paper_fee_model_refuses_nonsense` |
| R15 | Lab wallet drawdown was (highest - lowest) / highest even when the low came before the high: overstated every reported drawdown (e.g. "median 78%", "~65%") | fixed (branch) | `test_swing_sizing_binance.py::test_lab_wallet_drawdown_is_measured_from_the_running_peak` |
| R14 | Binance futures WebSocket split: legacy `/stream` silently returns nothing for markPrice/aggTrade/kline | open (engine has WebSocket off) | docs/BINANCE_PLAN.md section 3; use `/market/stream` when WebSocket is enabled |

## Not automated yet (SCENARIOS)

- Leak battery over every PnL path (synthetic "alternating winner" data + one cheating strategy per data route).
  Partly covered: `test_lab_engine.py::test_every_strategy_is_causal` (every registered strategy, truncation test).
- Bad data in the live paper path (zero / frozen / spike prints): swing evaluator covered
  (`test_phase2_batteries.py::test_swing_signal_on_bad_prints_is_none_not_nan`); live tick gate is
  `price_is_plausible` (existing tests).
