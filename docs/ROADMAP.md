# Roadmap: from quant research to a live trade (7 Oct 2026)

One list of everything pending, merged from docs/INTEGRATION_PLAN.md (phases 3-5, Q/B/O items), HANDOFF.md open
work, the forecast and AI threads, infrastructure, and docs/BINANCE_PLAN.md. Paper trading only until the
**REAL MONEY** gate (section 7). Owner rules: tests per fix, CI before deploy, production DB read-only except
owner-approved writes (backed up), no secrets in the repo.

## 0. Where we are (done)

| Area | Done |
|---|---|
| Production safety | Phase 1 (L1-L11): fail-closed config, perp prices, account guards, news rule, 9% side cap, gap fills, dedup, regime fail-closed, CI gate |
| Honest measurement | Phase 2: trial ledger, honest p + Bonferroni at N, swing-exit null tests (6/8 specs beat random), strategy registry + forward clock + drawdown alarm, cost reconciliation, input guards, findings ledger, v2 N from ledger |
| Sizing | margin cap 15%, Binance order rules, risk 1-3% (drawdown/streak brake, Ichimoku half), lab drawdown metric fixed |
| Paper wallet | reset 7 Oct to Rs5,000 (cycle 2); Rs2,500 withdrawn each time equity reaches Rs10,000 |
| Venue | Binance verified from India (read-only key, futures access, limits, WebSocket split) |
| AI | budget manager, keys page, fine-tuned 3B model on the HF Space (better than base on headlines) |

## 1. Quant research (the edge) — Phase 3/4

Every experiment is recorded in the trial ledger (`run_lab --ledger-family ...`) and judged at Bonferroni N.

| # | Item | Why | Source | Size |
|---|---|---|---|---|
| Q-1 | **Cluster-aware significance** (block bootstrap by 4h window across coins) | Trades cluster (92-97% same side per day); today's z-scores overstate | O3, R11 | M |
| Q-2 | **Back-fill the ledger** with past lab runs (579 configs) as real rows | N is a floor today | Q2 | S |
| Q-3 | **Family-wise permutation null** on the home laptops (best-of-N random vs real best) | Stronger than Bonferroni for correlated configs | O1, O8 | M |
| Q-4 | **Exit variants** on the 8 specs: 1.75xATR stop; 50% at 1.5R + trail; NNFX half at 1xATR then breakeven | Exits drive R more than entries | B5 | M |
| Q-5 | **Baseline filter** (KAMA, McGinley): longs only above a rising baseline | Cheap orthogonal filter | B3 | S |
| Q-6 | **Volatility gate** (Garman-Klass / ATR expansion): skip dead markets | Complements the BTC wild-market filter | B4 | S |
| Q-7 | **Daily-timeframe trend** on BTC/ETH only | Literature: time-series momentum is strongest daily | research 7 Oct | S |
| Q-8 | **Regime-sliced report**: every spec's R by BTC volatility and trend regime | Is the edge one regime's luck? | O7 | S |
| Q-9 | **Implementation shortfall**: live entry/exit vs the backtest's theoretical fill, per spec | Makes L5 (late entries) measurable | O4, L5 | S |
| Q-10 | Fix research-tool bugs: stepped simulator NameError, target-before-stop, synthetic data, outcome replay window, simulator fees, walk-forward purge, v2 run-now | Results we might trust are wrong | R3-R10, R12 | M |
| Q-11 | Survivorship: add delisted perps where data exists; note it in every report | Universe is today's winners | R7 | M |
| Q-12 | Cost stress: rerun the live book at `--cost stress` and at Binance VIP0 without GST | Know the margin of safety | — | S |
| Q-13 | `known_at` convention + leak battery over every PnL path | One rule for "when was this knowable" | Q6, Q13 | M |
| Q-14 | Ichimoku: re-test with more data / variants; promote to full risk only if it clears the line | Inconclusive today (kept at half risk) | 7 Oct null test | S |

## 2. Forward evidence and risk (paper, running now)

| # | Item | When |
|---|---|---|
| F-1 | Let cycle 2 run: target ~3 months or ~60 swing trades before any verdict | from 7 Oct |
| F-2 | **Weekly forward report** (Telegram + Paper tab): forward R per spec vs its backtest band, skips by reason, withdrawals | week 1 |
| F-3 | **Promotion rule** incubating -> trusted: sequential test (SPRT / Bayesian on mean R) instead of a calendar | O2, B1 — week 2-3 |
| F-4 | Review the drawdown alarm factor (1.0x backtest worst) after the first 30 forward trades per spec | month 2 |
| F-5 | Dynamic risk review: log the risk each trade used and why (drawdown, streak, weak spec) on the trade card | week 1 |
| F-6 | Paper = live parity: paper wallet sized like the planned live wallet (done, Rs5,000), Binance min orders (done), fees with/without GST decision | done / CA |

## 3. Execution and live trading (REAL MONEY gated)

| # | Item | Gate |
|---|---|---|
| X-1 | **Binance Futures testnet** wiring of `execution/` (no real money): place / cancel / reconcile, kill switch, Telegram on fills | none (testnet) |
| X-2 | Position reconciliation: engine state vs Binance `positionRisk` every minute; mismatch pauses trading | X-1 |
| X-3 | WebSocket on `/market/stream` (mark price 1/s) for live stops instead of 10 s REST polling | X-1 |
| X-4 | Order-rule refresh job (`scripts/binance_filters`) weekly; alert on changes | X-1 |
| X-5 | CA's written view on tax (VDA 30% vs speculative business income) | owner |
| X-6 | New Binance key with futures (never withdrawals), IP-whitelisted to the trading server | owner + REAL MONEY alert |
| X-7 | First live trades at minimum size alongside paper; compare fills daily | owner yes |

## 4. Data and infrastructure

| # | Item | Status |
|---|---|---|
| I-1 | Home server (Lenovo C340) vs EC2 Sydney: decide when home Wi-Fi is ready (2-3 days) | owner, deferred |
| I-2 | Move Postgres off Aiven free (1 GB, ~1.4 s per query) onto the server | with I-1 |
| I-3 | Close port 8080 to the internet; Tailscale only | owner |
| I-4 | Nightly DB backup to Google Drive + offline copy | with I-1 |
| I-5 | GitHub secret `EC2_SSH_KEY` update, then remove the old key | owner, deferred |
| I-6 | Key rotation (OpenRouter, HF, Gemini) | owner, deferred |
| I-7 | Lab jobs on the home laptops via the work queue (Tailscale) | when Tailscale is up |

## 5. AI (support role; no AI decides a trade without proof)

| # | Item | Status |
|---|---|---|
| A-1 | Read the laptop's with-AI vs without-AI entry test (was 32/150); AI may veto entries only if it beats rules with a confidence interval | Tailscale down |
| A-2 | HF Space: stays on ZeroGPU; optional new CPU-basic Space (always on) if sleeping hurts headline scoring | optional |
| A-3 | Fine-tune round 2: fix the 600-token cut on move explanations, add the call logs collected since 6 Oct | month 2 |
| A-4 | Forecast phase 2 (volatility regime in band width, /predict page) — direction stays off (coin flip) | backlog |
| A-5 | Retire AI features that never earn: monthly review of AI calls vs outcomes | backlog |

## 6. App and code health

| # | Item |
|---|---|
| C-1 | Paper tab: per-trade risk and reason, withdrawals history list, forward report card |
| C-2 | Module review: analysis/ (18k lines) and scheduler/ (14k lines); remove dead 15m code paths after shadow data is archived |
| C-3 | Read-only MCP server for research queries (optional) | Q14 |
| C-4 | Keep audit/FINDINGS.md current: every fix gets an ID and a named test |

## 7. Go-live checklist (all must be true)

1. ~3 months / ~60 forward swing trades on cycle 2, forward mean R inside the backtest's expected band, no spec
   in alarm.
2. Two weeks without operational errors (paper job, scans, data feeds).
3. Testnet execution (X-1, X-2) working end to end, including the kill switch.
4. CA's written tax view (X-5).
5. Owner's explicit yes to the **REAL MONEY** alert, then X-6 and X-7 at minimum size.

## 8. Suggested order

| When | Work |
|---|---|
| Week 1 (now) | F-2 weekly report, F-5 risk on trade cards, Q-2 ledger back-fill, Q-12 cost stress, C-1 |
| Weeks 2-3 | Q-1 cluster significance, F-3 promotion rule, Q-8 regime slices, Q-9 shortfall, X-1 testnet |
| Weeks 3-6 | Q-4/Q-5/Q-6/Q-7 experiments (ledger + family-wise null on laptops), Q-10 research-tool fixes, X-2/X-3 |
| Month 2-3 | Q-11 survivorship, Q-13 leak battery, A-1/A-3, I-1/I-2 home server, F-4 alarm review |
| ~Month 3 | Go-live checklist (section 7) |
