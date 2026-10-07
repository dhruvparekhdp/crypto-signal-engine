# Integration plan: lessons from qanat and the StrategyFactory blueprint

Written 2026-10-07 against `main` at `2ccdd77`. It is meant for a fresh Claude session (local or web) to review
and then work through, phase by phase. Every finding below was produced by a reader agent and then checked by an
independent verifier agent against the code at `2ccdd77`. Corrections from the verifiers are already folded in.
Re-check file:line references before editing, since `main` may have moved since.

The two sources:

- **qanat** (<https://github.com/fidetolabs/qanat>, MIT): an MCP-first quant research platform for daily/weekly
  rebalanced portfolios. YAML-defined pipelines (raw -> normalized -> features -> weights -> pnl), point-in-time
  replay, in-sample / out-of-sample / live periods, a trial counter that tightens the significance bar with every
  attempt, and an audit "battery" of leak and arithmetic tests.
- **The blueprint** (`Crypto_Quantitative_Architecture_Blueprint.pdf`): Gemini's summary of a YouTube video about
  DaviddTech's StrategyFactory. It proposes an NNFX-style 4-layer signal gate, a two-chamber paper -> live
  incubation system with kill rules, and fixed validation thresholds.

---

## 0. Read this first: the one finding that matters most

**The live swing book has no clean out-of-sample evidence.** These were all chosen on the same 5-year history:

- the strategy and parameter set (`config/settings.py` `swing_strategies`, `analysis/swing_book.py` `DEFAULT_SPECS`)
- the excluded coins BCH/LTC (`swing_exclude_symbols`)
- the per-strategy and per-coin priority weights `STRATEGY_R` / `COIN_R` (`analysis/swing_book.py` ~183-196)
- the regime threshold `vol_rank 0.67` (`analysis/regime_gate.py`, `config/settings.py` ~190-195)

The lab then reports that same history as "every year better" (HANDOFF.md research results). That is in-sample
fitting presented as validation. The lab also screens hundreds of configurations (31 strategies x params x exits x
coins x timeframes) with a raw null p-value and no correction for how many were tried
(`analysis/lab/runner.py`). The v2 deflated Sharpe uses a hard-coded `TRIALS = 15` (`analysis/v2_report.py:50`).

**Consequence:** from now on, the only clean evidence about the swing book is forward paper performance after a
recorded start date. Phase 2 below builds exactly that. Until then, treat every backtest number as optimistic.

What is confirmed good: the swing book only ever evaluates **closed** candles. `swing_book.bars_from_klines` drops
the forming kline, `evaluate` reads only index `[-1]`, `tests/test_swing_book.py` covers 4h and 8h, and the regime
gate uses closed daily bars. There is no look-ahead in live swing signals (verified).

---

## 1. Engine findings (verified against the code at `2ccdd77`)

### 1a. Live paper trading (affects every paper result)

| # | Finding | Where | Severity |
|---|---|---|---|
| L1 | Paper positions are valued with a price that is not the swing book's market. Production runs `binance_only_mode=on`, so `current_price` comes from Binance **spot** 1m klines (`collectors/binance_klines.py`, `data-api.binance.vision /api/v3/klines`, every `binance_klines_seconds`=15s in prod). With code defaults (`binance_only_mode=False`) it comes from CoinGecko's aggregate spot price every 60 s. The swing book's signals and stop/target levels come from Binance **USD-M perpetual** klines (`swing_book.FAPI_KLINES`). Stops and targets are therefore checked against a different market. | `scheduler/runner.py` paper job (~469-560), `analysis/crypto_state_store.py` ~353-380, ~425 | High |
| L2 | Swing trades skip the account-level protections: daily-loss limit, losing-streak pause, correlated-exposure cap (`protections.check_entry`), and the news blackout / news-bias check. `_open_swing_signals` runs before the blackout is even computed. | `scheduler/runner.py` `_open_swing_signals` (~956-1044), blackout at ~659-678 | High |
| L3 | Combined with L2: 3% risk per trade (`swing_risk_pct`), `swing_max_open=0` and `swing_max_same_side=0` (both "no limit"). The repo itself notes about 97% of trades cluster in one direction (settings comment ~193-194), so a single market move can hit many 3% stops at once. | `config/settings.py` ~176-195 | High |
| L4 | Stop fills are optimistic on gaps. Live resolution calls `resolve_candle(high=price, low=price)` and fills at the stop **level** (plus slippage) even when the 30 s tick has gapped well past it. The lab correctly fills a gap at the bar open. | `analysis/paper_trading.py` `resolve_candle` (~997-1049), `analysis/paper_cycle.py` `resolve_at_price` (~352) | Medium |
| L5 | Entry timing differs from the backtest. The lab enters at the next 1m open after the 4h/8h close. Live accepts a signal up to `swing_signal_max_age_minutes`=60 after the close and enters at whatever price the 300 s scan and 30 s tick see, with no chase limit. | `scheduler/runner.py` ~933-940, `config/settings.py` ~186 | Medium |
| L6 | A restart re-fires signals. `_swing_seen` and `_pending_swing` are memory-only. A restart within 60 minutes of a bar close re-queues the same signal; if its trade already stopped out, it re-opens (only `already_open_in_symbol` guards it). | `scheduler/runner.py` ~924-928, ~975-977 | Medium |
| L7 | `_pending_swing` grows without limit while paper trading is off: the scan keeps queuing, but `_open_swing_signals` only runs when `pcfg.enabled`. | `scheduler/runner.py` ~483-487, ~956 | Medium |
| L8 | The regime gate fails open. A fetch error or short history returns `None`, and the signal trades as if approved, even with `swing_regime_filter='on'`. | `analysis/regime_gate.py` ~75-90, `scheduler/runner.py` ~893-896, ~992 | Low-Medium |
| L9 | `swing_book.parse_specs` silently drops a bad spec: an unknown strategy id, a timeframe it doesn't know (including `4H` in capitals), or an unknown param key is ignored with no error. A typo in Settings quietly removes or changes a live strategy. Confirmed by running it. An existing test enforces the silent behaviour: `tests/test_swing_book.py:30`. | `analysis/swing_book.py:47-65` | Medium |
| L10 | Costs are not uniform across paths: paper uses FeeModel+SlippageModel (about 0.19% stop round trip), the lab about 0.158%, the regime scorer a flat 0.17%. | `analysis/paper_trading.py` ~75-171, `analysis/lab/costs.py`, `analysis/regime_gate.py:135` | Low |
| L11 | The simulator start endpoint passes Gemini/HF/OpenRouter keys as subprocess command-line arguments, visible in the process list. | `scheduler/health.py` ~320-329 | Low (security) |

### 1b. Research tools (results you may be trusting)

| # | Finding | Where | Severity |
|---|---|---|---|
| R1 | The null test checks the wrong thing. It replays `crypto_signal_log` (old intraday signals) with a fixed 0.92% / 1.84% / 24 h exit on DB snapshot prices, not the swing book's 3xATR / 3R / 7-day exits. Its p-value is `beaten / trials`, which can report p = 0. | `analysis/null_test.py` ~65-67, ~99-151, `scripts/null_test.py` | High |
| R2 | Multiple testing is uncorrected in the lab, the source of the live book (see section 0). | `analysis/lab/runner.py`, `analysis/v2_report.py:50` | High |
| R3 | The stepped simulator references undefined names (`cycle_results`, `sorted_trades`) in its per-closed-trade loop, so stepped mode dies with `NameError` on the first closed trade. The dashboard can launch it. | `analysis/simulator/stepped_replay.py` ~259-284 | High |
| R4 | The simulator and the multi-year pipeline check take-profit **before** stop on the same bar (optimistic). The lab and v2 do the opposite (stop first). | `analysis/simulator/stepped_replay.py` ~390-400, `analysis/simulator/replay_engine.py` ~257-268, `scripts/backtest_multi_year_pipeline.py` ~387-403 | High |
| R5 | Fake data can look like real results: random-walk candles are generated silently when the lake is missing, and the multi-year pipeline injects hard-coded 2023 "events" with fake prices (open=28000) into every symbol. | `analysis/simulator/replay_engine.py` ~121-137, `scripts/backtest_multi_year_pipeline.py` ~64-118, ~224-238, ~297-320 | High |
| R6 | Overlapping windows are counted as independent wins: "29/48 twelve-month starts, 36/36 twenty-four-month starts" come from monthly-staggered, heavily overlapping windows on the same trades. The multi-year pipeline also double-counts nested windows. | `scripts/wallet_experiments.py` ~38-57, `scripts/backtest_multi_year_pipeline.py` ~56-62, ~784-805 | Medium |
| R7 | Survivorship: the lab universe is today's large perps (including SUI, listed 2023), and the lake only holds currently listed symbols. Delisted coins are never tested. | `analysis/lab/runner.py` ~156-157, `analysis/lab/data.py` ~130-131 | Medium |
| R8 | The swing outcome replay ("taken vs skipped" scoreboard) starts from the 1h bar **containing** the signal time, so pre-signal highs/lows can count as stop/target touches. | `analysis/regime_gate.py` ~148-158, `scheduler/runner.py` ~1693-1700 | Low-Medium |
| R9 | Simulator R is booked with no fees; "ATR" there is a single bar's range; the breakeven ratchet uses the same bar's high before the stop check. | `analysis/simulator/stepped_replay.py` ~340-424 | Medium |
| R10 | The lab_ai ridge baseline's walk-forward trains on trades still open at the test entry (labels leak; no purge/embargo). The lab walk-forward has the same boundary issue. | `analysis/lab_ai/baseline.py` ~15-27, `analysis/lab/metrics.py` ~64-69 | Low |
| R11 | Deflated Sharpe and t-stats treat per-trade R as independent, but trades across coins cluster in one direction, so significance is overstated. | `analysis/v2_backtest.py` ~258-282, `analysis/lab/metrics.py` | Low-Medium |
| R12 | The v2 backtest admin "run now" silently does nothing while `v2_backtest_enabled=False`, and `/v2` shows a stale report if one exists. | `scheduler/v2_pages.py` ~87-95 | Low |

### 1c. Intraday (shadow-only today; matters only if it is ever promoted)

- Intraday analyzers read the forming 1m bar for price and direction (`analysis/confluence.py` ~390-416,
  `analysis/crypto_signals.py` ~402-462). With WebSocket off (default), indicators recompute every 60 s on a series
  whose last bar is still forming. That breaks bar-close discipline.
- `htf_1h_trend` / `htf_4h_trend` are effectively always "neutral" (starved of bars), so the 1h macro-trend veto is
  dead. `momentum_15m` is unused, but would be a multi-timeframe phantom if used (it includes the forming bucket).

---

## 2. qanat: what we integrate, what we don't, and why

qanat's value for this engine is its **discipline**, not its replay machinery.

### Integrate

| # | What | Why | Effort |
|---|---|---|---|
| Q1 | **Fail-loud config.** `parse_specs` and any strategy config raise on unknown strategy id, unknown timeframe, or a param key not in the strategy's `defaults`/`grid` (plus `warmup`). Run it at swing-scan startup; on error, log at error level (which already reaches Telegram) and refuse to trade swing until fixed. Lowercase the timeframe before checking. | qanat validates every config with `extra='forbid'`; a typo is never silently accepted. Fixes L9. | S |
| Q2 | **Trial ledger.** A Postgres table with one row per lab / v2 / null-test run, keyed by a digest of: strategy module source, params, timeframe, exit model, window, cost preset, and a data fingerprint (per symbol: file list, row count, min/max open_time). Columns: hypothesis, parent_id, disposition, `kind` (search / stress / ablation / live_rescore), `proposed_by` (owner / Claude / Groq / Gemini / Ollama ...). Only `kind=search` counts toward N. | qanat's `trial_count` counts distinct digests automatically (`src/qanat/store.py` ~1183-1200). Without a count, no significance number means anything (R2). Hash only the strategy's own inputs, not the whole project (qanat's digest is too broad). | M |
| Q3 | **Honest null test.** p = (beaten + 1) / (trials + 1); require p <= alpha / N (Bonferroni, N from the ledger); random trials >= 20 * N / alpha; and run it against the **swing exits** (3xATR stop, 3R target, 7-day hold), not the old fixed intraday exits. | Fixes R1. Bonferroni is what qanat uses (`src/qanat/backtest.py` ~536); it is conservative for correlated configs, which is an acceptable price. | S-M |
| Q4 | **Per-spec `live_from` stamp.** The first time each swing spec trades on paper, stamp the date (and reset it when its code or params change). The dashboard shows three separate segments per spec: backtest in-sample, out-of-sample, forward paper since `live_from`, plus the gap between forward and backtest-implied return. | qanat's live period is the only one "that cannot be arrived at by looking". This is the fix for section 0. qanat keeps one frontier per project; we keep one per spec. | S |
| Q5 | **Spec digest + "validated by" stamp.** Hash `analysis/lab/{strategies,features,simulate,costs}.py` + strategy id + params + timeframe + exit model + cost preset at every validation run. At swing-scan startup, warn if a live spec's current digest has no passing validation record. | From qanat's `plan.py` staleness. Prevents "the code changed after it was validated". Over-flags (any edit to features.py), which is the conservative direction. | S |
| Q6 | **Leak battery.** A synthetic "alternating winner" dataset (each bar one coin +2%, the other -2%, at random) plus one deliberately cheating strategy per data-access route (direct arrays, `htf_series`, funding, DB snapshots). A blind strategy must earn about 0; the cheater must be caught. Run it over every PnL path: lab `simulate_symbol` + wallet, v2 resolve, null test, stepped replay, swing signals through the lab. | qanat's `audit/batteries/test_leak.py`. Calibrate the honest band per path (stops/targets/costs make a blind strategy lose a predictable amount); qanat's own pass band (`|net| < 1.0`) is too wide. | S-M |
| Q7 | **Cost-model reconciliation test.** Price one canonical trade (1,000 USDT, long, 8 h hold, stop exit then target exit) through lab `CostModel`, v2 `ExecConfig`, paper `FeeModel`, and the simulator. Assert they agree, or assert the documented intentional differences. | Fixes L10 drift. From qanat's `test_arithmetic.py`. | S |
| Q8 | **Known-answer simulator tests.** Bars that touch both stop and target every bar (correct answer: all stops, -1R each); constant-drift bars with a known R; two overlapping trades on different symbols (margin must use the balance before the first). | Would have caught R3/R4. | S |
| Q9 | **Bad-data makers.** Symbol whose rows stop (delisting/gap), a 0 or negative print, a frozen price, one spike tick. Assert no inf/NaN in R or net, the trade is skipped or noted, and the verdict isn't inverted. | qanat's `delisting` / `with_bad_tick`. Extends the live `price_is_plausible` gate to the research paths. | S |
| Q10 | **Clock and input guards.** At lake load, assert `open_time` is epoch ms (1e12-1e13) and bar spacing equals `INTERVAL_MS`. `__post_init__` on `CostModel`, `ExecConfig`, `FeeModel` refuses negative fees/slippage and GST outside [0, 1]. | qanat's "read integers by magnitude, refuse nonsense". Cheap insurance once AI agents propose parameter sweeps. | S |
| Q11 | **CI before deploy.** Add a `test` job to `.github/workflows/deploy.yml` (pytest, Python matching production, Postgres service container) and make `deploy` depend on it (`needs: test`). Grep the output for "N passed" so Postgres tests cannot silently skip. | qanat's `test.yml`. Would have stopped the `fees_for` ImportError that crashed production. Free on a public repo. | S |
| Q12 | **Findings ledger.** `audit/FINDINGS.md` with flat IDs never reused; each finding gets a test named for its ID (`test_l4_live_stop_fills_at_gap_price`). `audit/SCENARIOS.md` lists what is not automated yet. Accept a fix only after re-running the probe that found it. Section 1 of this plan is the seed. | qanat's audit discipline. Only pays off if kept up. | S |
| Q13 | **One `known_at` convention.** Every table that can feed a feature carries a "known at" time (bar close time for klines; `created_at` for AI/news rows). One helper `asof_align(known_at_ms, values, at_ms)` (searchsorted side='right' minus 1) used everywhere. Lab feature code asserts no feature row depends on a bar whose close is after the decision time. | qanat's whole guarantee rests on "the time column means when this row became knowable". | S |
| Q14 | **Read-only MCP server (optional).** A stdio MCP server in the repo (`scripts/mcp_server.py`) with nested scopes: `data` (read-only tables, signals, trades, lake summaries), `research` (adds starting a lab run, reading results, recording a trial), `full`. Enforce read-only at the database (`default_transaction_read_only=on`), hard `LIMIT` on every row tool, table denylist for secrets, scope-contract tests. Long jobs return a job id and a status tool. | Lets Claude Code query results without Bash access. In interactive sessions Claude Code already has Bash, so this is convenience, not new capability. Do it last. | M |

### Do not integrate

| What | Why not |
|---|---|
| qanat itself as the backtest engine | Built for daily/weekly cross-sectional **weights**. No stops, targets, leverage, funding, or intrabar logic, so the swing book's 3xATR / 3R / 7-day rules cannot be expressed. Each as-of stop re-runs the whole pipeline: 5 years of 4h bars is about 11,000 full recomputes, too slow. |
| qanat's stage schema (raw/normalized/features/weights/pnl) | Same reason: the engine is event-driven. Borrow the declared-dependency idea (Q13, and optionally a `reads=` declaration on lab strategies), not the schema. |
| qanat's as-of database views | Heavy for this engine; the `known_at` convention plus leak tests (Q6, Q13) give most of the protection cheaply. |
| qanat's unattended "falsify" agent pass, as shipped | Its briefs point at HTTP endpoints that don't exist and contradict its own tool fence (`src/qanat/research.py` ~80-115 vs `headless.py` ~368). If wanted later, build our own with `claude -p` fenced to a research-scope MCP server; note it draws on the Claude subscription limits. |
| qanat's significance bar as the gate | It uses a normal z critical value with no Student-t, no autocorrelation correction, and tests raw net not excess over a benchmark. The engine's own PSR/DSR (`analysis/v2_backtest.py` ~258-282) is richer. Keep DSR; feed it the real N from Q2. |

---

## 3. Blueprint: what we integrate, what we don't, and why

Source check: DaviddTech does report about 2,000 incubated strategies with about 400 still profitable (80% is
Gemini's arithmetic, unaudited). His public rules hold up: avoid multi-timeframe setups, always include fees and
slippage, judge by profit factor and drawdown not win rate, build your own boilerplate, forward-test before live.
He does use an NNFX-style structure (baseline / confirmation / volume-volatility) with McGinley, WAE, TDFI and STC
in his published strategies. Everything else (the "1,600-strategy autopsy" taxonomy and **every** numeric
threshold) is not traceable to him and is likely the summariser's invention.

### Integrate (as lab experiments, each one counted in the trial ledger)

| # | What | Why | Notes |
|---|---|---|---|
| B1 | **Forward paper incubation as the promotion step.** A strategy goes research -> incubating (paper, with `live_from`) -> trusted. | His rule 5, and standard practice. This is the same idea as Q4. | Use a sequential test or a minimum trade count, not "45 calendar days" (see "do not"). |
| B2 | **Drawdown alarm: live drawdown > 1.5x the backtest's max drawdown.** | A sensible, common practitioner heuristic for "this has stopped working". | Alert and pause new entries for that spec; don't auto-delete. The 1.5x is a starting value, record it. |
| B3 | **Adaptive baseline filter (KAMA, McGinley Dynamic).** Test as an extra filter on the existing swing strategies: long only above a rising baseline, short only below a falling one. | Real indicators (Kaufman 1995; McGinley 1990). Cheap to test in the lab. | Neither is "non-lagging": every causal smoother lags. Skip Jurik (proprietary; public versions are reverse-engineered and differ). |
| B4 | **Volatility gate.** Garman-Klass volatility (OHLC estimator, Garman & Klass 1980) or ATR expansion as a "don't enter in dead markets" filter. | Plausible, cheap, and orthogonal to the breakout triggers. | WAE uses **no volume**, so it cannot measure "institutional participation" as the blueprint claims; test it only as a price-volatility filter if at all. |
| B5 | **Exit variants.** 1.75xATR stop; 50% scale-out at 1.5R with the rest trailed; NNFX-style half at 1xATR then breakeven. Compare against the current 3xATR / 3R / no-trail. | Real, testable alternatives. | The blueprint's "TP 1.5-2R + 50% at 1.5R" is ambiguous; define each variant precisely before testing. |
| B6 | **Prefer 1h+ timeframes for anything new.** | Matches the engine's own finding: 15m strategies lose after costs, 4h/8h win. | The exact 1h/4h cutoff is arbitrary; DaviddTech himself runs 15m bots. |
| B7 | **Bar-close only, signals shifted by one bar.** | Standard practice. The swing book already does this. | Binance `close_time` is `open_time + interval - 1 ms`; don't check `timestamp == close_time` by equality. |

### Do not integrate

| What | Why not |
|---|---|
| The fixed gates: in-sample PF >= 1.6, out-of-sample PF >= 1.3, overfit ratio PF_train/PF_test <= 1.35, >= 40 OOS trades, 70/30 split | Arbitrary and statistically weak. A Monte Carlo found a **zero-edge** strategy reaches PF >= 1.3 about 14.5% of the time over 40 trades (about 19% at 20, about 1% at 200). The ratio gate barely binds: with train >= 1.6 and test >= 1.3 it only rejects when train PF > ~1.755. Use the trial ledger + Bonferroni/DSR + trade-count minimums instead. |
| The Claude loop "inspect the JSON, iterate parameters up to 4 runs, export the passing config" | It builds in multiple testing and leaks the test set: the agent sees OOS PF every iteration, so the "unseen 30%" becomes a validation set being optimised against. Four tries per strategy across many strategies is a search, and must be counted (Q2). |
| "Promote after PF >= 1.25 over 45 continuous days" | Low power (about 58% to detect a genuine PF 1.33 edge) and a high false-pass rate; 45 days on 4h/8h is only about 10-40 trades. Promote on a sequential test or a trade-count minimum. |
| Kill-rule numbers (30-day PF < 1.05, 12% DD, 8 consecutive losses, 60-day PF < 1.10, 7 bots max) | All arbitrary, with no source. The 7-bot cap looks fitted to the owner's 7 coins. Keep the idea of hysteresis (separate promote and kill thresholds) and B2; derive any number from the strategy's own backtest distribution. |
| The fee figures | They contradict each other (0.10% round trip vs 0.08% + 0.02% per order = 0.20% round trip). Binance USD-M VIP 0 is 0.02% maker / 0.05% taker. Keep the engine's own reconciled FeeModel (it includes 18% GST). |
| "Crypto ranges 70% of the time" | Trading folklore with no traceable source; depends entirely on how "range" is defined. |
| STC + TDFI as "orthogonal" confirmations | Both are momentum-derived (STC is a double stochastic of MACD), so pairing them breaks the blueprint's own "never combine two momentum metrics" rule. |
| The Postgres schema `candidate_strategies` / `forward_paper_trades` as written | Thin: no params/config hash, code version, fees or funding. Build the registry from Q2 + Q4 instead. |
| DeepSeek + CryptoPanic sentiment sizing | CryptoPanic's free API is gone (about INR 19,000+/month now). DeepSeek payment from India is unconfirmed. LLM sentiment sizing is an untested idea; if tried, it is a lab experiment like any other. |

---

## 4. What we build ourselves (not in either source)

| # | What | Why |
|---|---|---|
| O1 | **Family-wise permutation null.** For each random-entry draw, run the same random entries through all N candidate configs and record the best total; compare the real best against the distribution of random-bests. | Stronger than Bonferroni when configs are correlated (e.g. Keltner k=2.0 vs 2.5). Costs N x CPU per draw: run it on the home worker laptops. |
| O2 | **Sequential forward test per spec.** Instead of "45 days", a sequential probability ratio test (or a Bayesian posterior on mean R) on forward paper trades since `live_from`: promote, keep watching, or demote as evidence arrives. | Fixes the low-power fixed-window problem. Decisions come as soon as the evidence supports them. |
| O3 | **Cluster-aware significance.** Block/cluster bootstrap by time (trades in the same 4h window across coins are one cluster), so t-stats and DSR stop treating correlated trades as independent. | Fixes R11, and the 97% same-direction clustering. |
| O4 | **Implementation-shortfall tracker.** For every live paper trade, record the gap between the backtest's theoretical entry (next bar open after close) and the actual paper entry, and the same for exits. Show it per spec. | Makes L5 visible and measurable instead of a guess. |
| O5 | **Correlation-aware exposure cap.** Cap total open risk by direction (sum of risk % of all same-side positions), not just count of positions. | Addresses L3 directly. 3% risk x many same-side positions is one large BTC bet. |
| O6 | **Strategy registry with a lifecycle.** A table: spec id, digest, status (research / incubating / trusted / paused / retired), `live_from`, validation record, ledger N at validation, drawdown alarm state. The swing book only trades specs in incubating or trusted. | Ties Q2, Q4, Q5, B1, B2, O2 together into one source of truth instead of a settings string. |
| O7 | **Regime-sliced performance.** Report every spec's R split by BTC volatility regime and trend regime. | Shows whether an edge is real or one regime's luck, and whether the regime gate earns its keep. |
| O8 | **Home cluster job runner.** Extend the existing worker queue (`scripts/work_server.py`, `work_worker.py`, `worker_setup.sh`, Tailscale) to run lab and null-test jobs, every job stamped with its digest and written to the ledger. | Free compute for O1 and Q6 on the owner's spare laptops. |
| O9 | **"Same question?" diff for any two runs.** Equal digests mean any difference is an engine change; different digests list which inputs moved. | From qanat's idea; makes "why did this number change" answerable. |

---

## 5. Phased roadmap

Rules for every phase:

- Work on a branch. Every fix gets a test that fails on the old code and passes on the new.
- Run the full suite (`rm -f crypto_engine.db && python -m pytest -q`) and `ruff check` on touched files before
  proposing a merge.
- `main` auto-deploys to production: merge only after review, and batch deploys (each restarts the engine).
- Public repo: never commit secrets.
- Any change to live trading behaviour gets a setting so it can be switched back.

### Phase 0: owner actions (no code)

1. **Rotate keys.** The OpenRouter key, Hugging Face token and Gemini key were readable through the unauthenticated
   `/api/tables` dump before `763e9cf`. Regenerate them at each provider and save the new ones on `/keys`.
2. **Binance from home test**, which decides where the server lives:
   `curl -s "https://fapi.binance.com/fapi/v1/klines?symbol=BTCUSDT&interval=4h&limit=2"` on a home laptop over
   Jio. Data back means the engine can move home. An error or HTTP 451 means stay in Sydney.
3. **Close port 8080 to the internet** and use Tailscale only (already listed in HANDOFF.md).
4. **Update the GitHub secret `EC2_SSH_KEY`** (HANDOFF.md).

### Phase 1: protect production (week 1)

| Task | Fixes | Acceptance |
|---|---|---|
| 1.1 CI gate before deploy (Q11) | `fees_for`-class crashes | A failing test blocks the deploy job; "N passed" check present. |
| 1.2 Fail-loud swing specs (Q1) | L9 | Bad id / timeframe / param key raises with every problem listed; the runner logs an error and skips swing until fixed; `4H` normalises to `4h`. Update `tests/test_swing_book.py:30` to the new intent. |
| 1.3 Swing priced on the perp market | L1 | Each paper tick fetches Binance USD-M prices for open swing symbols (one batch call to `/fapi/v1/ticker/price`) and uses them for swing entry and resolution; falls back to the store price with a logged warning. Test that a swing position resolves on the perp price. |
| 1.4 Account guards and news check for swing | L2 | Swing opens pass the daily-loss limit, losing-streak pause and correlated-exposure cap (but not the intraday-only pair cooldown / anti-flip, which the swing backtest never had), and the news blackout / news-bias rule. Behind a setting (default on). |
| 1.5 Total same-side risk cap (O5) | L3 | New setting for max total open risk per side (e.g. 9%); skip reason recorded. Default chosen with the owner. |
| 1.6 Stop fills at the gap price | L4 | `resolve_candle` gets an optional open/tick price; a live stop that has been gapped through fills at the worse tick price. Backtest callers unchanged (parameter defaults to None). |
| 1.7 Persist swing dedup, bound the queue, fail-closed regime gate option | L6, L7, L8 | `_swing_seen` survives restart (DB or file); `_pending_swing` is not filled while paper is off (or is capped); a setting chooses fail-open vs fail-closed for the regime gate. |
| 1.8 Keys off the command line | L11 | Simulator subprocess gets keys via environment, not argv. |

### Phase 2: honest measurement (weeks 2-3)

| Task | Fixes | Acceptance |
|---|---|---|
| 2.1 Trial ledger table + digest + data fingerprint (Q2, Q5) | R2 | Every lab / v2 / null-test run writes a row; N per family is queryable; DSR reads N from the ledger instead of `TRIALS=15`. |
| 2.2 Null test fixed and pointed at swing exits (Q3) | R1 | p = (b+1)/(n+1); Bonferroni at ledger N; replays the swing book's real exits; never reports p = 0. |
| 2.3 Per-spec `live_from` + three-segment dashboard (Q4) | Section 0 | Each live spec shows in-sample / out-of-sample / forward-since-live_from separately; `live_from` resets when the spec's digest changes. |
| 2.4 Strategy registry with lifecycle (O6) and drawdown alarm (B2) | Section 0 | Swing trades only registry specs in incubating/trusted; alarm pauses a spec and alerts. |
| 2.5 Leak battery, cost reconciliation, known-answer and bad-data tests (Q6-Q10) | R3-R5, L10 | All in `tests/`, all running in CI. |
| 2.6 Findings ledger (Q12) | - | `audit/FINDINGS.md` seeded from section 1; each fixed item has its named test. |

### Phase 3: fix the research tools (month 2)

- Fix R3 (NameError), R4 (stop-first on same bar everywhere), R5 (no silent synthetic data: refuse or label loudly;
  remove the injected 2023 events), R6 (report non-overlapping windows; label overlapping ones as such), R8, R9, R10.
- Add survivorship notes to every lab report (R7) and, where data allows, include delisted perps.
- Cluster-aware significance (O3) and implementation-shortfall tracking (O4).

### Phase 4: experiments from the blueprint (after Phases 1-2, each counted in the ledger)

- B3 baseline filters (KAMA, McGinley), B4 volatility gate, B5 exit variants, on the swing strategies.
- Run them on the home cluster (O8) with the family-wise null (O1). Promote anything only through the registry and
  forward test (O2), never straight to live.

### Phase 5: optional

- Read-only MCP server (Q14), regime-sliced reporting (O7), "same question?" diff (O9).

---

## 6. Infrastructure and budget (summary)

Full page: <https://claude.ai/artifact/CC3FAZXLfbA8Tyh4tWEcQv> (private to the owner).

- Budget INR 5,000/month; INR 2,000 is Claude Pro (Claude Code included). Expected total INR 2,700-4,600.
- **Server:** if the Binance test works from home, run the engine on the Lenovo C340 (i5, 8 GB, 1 TB SSD) with
  Postgres on the same machine, Debian without a desktop, Tailscale, and EC2 switched off. If Binance is blocked,
  stay in Sydney but change t3.small (~INR 1,856) to t4g.small (~INR 1,475) and move Postgres onto that box.
  Aiven's free plan is only 1 GB now and is the source of the ~1.4 s per-query lag either way.
- **Home fleet roles:** 16 GB Iris laptop (`dhruv-ai`, Debian, OpenVINO/Ollama) = model box; i3 12 GB = standby
  server + backtest worker; Ryzen 3 (4 GB + spare 8 GB stick if a slot is free) = backtest worker; Mac = lab
  dashboard; 2.5" Seagate HDD (exFAT, shared with iPhone) = offline backup copy; Z Flip 4 5G = backup internet.
  Nightly DB backups to Google Drive (2 TB via the Jio Google AI Pro plan).
- **Internet:** JioFiber (or AirFiber) as the main link, C340 on ethernet, no open ports, mini DC UPS for the router.
- **AI providers:** Groq stays primary. Gemini's free API is now small (Pro is paid-only; newer Flash models may
  allow only about 20 requests/day; Flash-Lite is the realistic free fallback); the Jio Google AI Pro plan gives no
  API quota. One-time $10 OpenRouter credit (~INR 1,070) permanently raises its free cap from 50 to 1,000
  requests/day. Local OpenVINO/Ollama on the 16 GB laptop is a free fallback reachable over Tailscale. Check
  `collectors/llm_budget.py` limits against each key's real limits (the Gemini `rpd: 250` entry is probably stale).
- **Skip:** CryptoPanic (paid only now), Hugging Face PRO (Indian cards often fail), DeepSeek (payment unconfirmed),
  Oracle Always Free (halved, needs an international credit card), EC2 Mumbai (Binance reported blocked from Indian
  IPs), GCP/Azure free VMs (US IPs blocked by Binance).

Prices were researched in October 2026 mostly through search results (official pricing pages were blocked from the
research environment). Confirm before paying.

---

## 7. Decisions the owner needs to make

1. Fail-closed or skip-and-alert when the swing config is invalid (1.2). Recommendation: fail-closed.
2. The total same-side risk cap value (1.5).
3. Regime gate on fetch failure: fail-open (today) or fail-closed (1.7).
4. Whether to reset the paper wallet when Phase 1 lands, so forward results start clean with `live_from`.
5. Home server vs Sydney, after the Binance test.
6. Whether to pursue the MCP server (Phase 5) at all.

## 8. Other branches

- `claude/backup-old-mirror-work` holds four older commits that never reached `main`: a REJECT-penalty
  recalibration (0.15 -> 0.08), "no auto kill" (AI review cannot block a trade) plus an `/ai-vs-reality` page,
  and mirror-review fixes. Main has since been rebuilt; review them only if those features are still wanted.
