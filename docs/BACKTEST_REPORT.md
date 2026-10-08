# Backtest report: the swing book after the roadmap changes (7 Oct 2026)

Data: Binance USD-M, Oct 2021 – Sep 2026, 1-minute execution, Indian costs (0.05% taker + 18% GST + slippage),
10 coins (BCH/LTC excluded), 4h and 8h bars, stop 3×ATR, target 3R, 7-day limit, BTC volatility filter on.
Scripts: `scripts/edge_report.py`, `scripts/wallet_plan_backtest.py`, `scripts/sizing_backtest.py`,
`scripts/run_lab.py` (exits, stress, daily). Raw data: `data/lab/edge_report.json`, `data/lab/wallet_plan_backtest.json`.

**Read everything as an upper bound.** The strategies, coins and filter were chosen on this same history. The trial
ledger now counts **2,727 configurations tried** in total; the strict family-wise line is about t ≥ 4.1.

## 1. Is the edge real? (cluster-aware, roadmap Q-1)

Trades opened in the same 4h window are one market bet, so significance is computed on clusters (block bootstrap).

| | Trades | Mean R | Win | Naive t | **Cluster t** | 95% range of mean R |
|---|---|---|---|---|---|---|
| **Whole live book** | 3,430 | **+0.357** | 48% | 13.9 | **6.5** | +0.25 to +0.47 |
| 4h vol_breakout | 457 | +0.456 | 50% | 6.2 | 4.3 | +0.25 to +0.66 |
| 4h keltner_break | 658 | +0.371 | 47% | 6.1 | 4.6 | +0.21 to +0.54 |
| 4h ichimoku | 792 | +0.296 | 49% | 5.8 | 4.8 | +0.18 to +0.42 |
| 4h donchian | 654 | +0.306 | 45% | 5.1 | 4.1 | +0.17 to +0.45 |
| 8h keltner_break | 304 | +0.421 | 53% | 5.2 | 4.2 | +0.23 to +0.63 |
| 8h ichimoku | 274 | +0.329 | 49% | 4.0 | 3.5 | +0.15 to +0.52 |
| 8h donchian | 177 | +0.375 | 50% | 3.5 | 2.9 | +0.13 to +0.63 |
| 8h vol_breakout | 114 | +0.473 | 50% | 3.2 | 2.4 | +0.09 to +0.85 |

- Clustering cuts the naive t by about half. The **book as a whole clears even the strictest line** (6.5 vs ~4.1).
- Individually, five specs clear ~4.1; the three 8h specs with fewer trades (donchian, ichimoku, vol_breakout)
  are positive with ranges above zero but do not clear the strict line on their own. They stay in paper and earn
  promotion on forward results (F-3).
- **Ichimoku with the filter on is solid** (4h cluster t 4.8). The earlier "inconclusive" came from the unfiltered
  random-entry test. It keeps half risk until it is promoted on forward results.

## 2. When does it work? (regimes, roadmap Q-8)

| Slice | Mean R | Cluster t |
|---|---|---|
| Every year 2022-2026 | +0.29 to +0.51 | 3.0-4.0 each |
| 2021 (Oct-Dec only, 93 trades) | −0.16 | −1.1 (not significant) |
| Longs / shorts | +0.39 / +0.31 | 5.1 / 4.0 |
| BTC above / below its 200-day average | +0.39 / +0.32 | 5.6 / 3.7 |
| **Unfiltered, BTC volatility calm / normal / wild** | +0.36 / +0.40 / **−0.32** | 4.8 / 4.6 / **−5.1** |
| Best coins | SOL +0.59, LINK +0.56, SUI/ADA/DOGE +0.47 | |
| Weakest coins | XRP +0.23, BTC +0.24 | still positive |

The edge does not depend on direction or on a bull market. **It disappears completely in wild markets**, which is
exactly what the live volatility filter skips: the skipped trades average −0.19 R (cluster t −3.3).

## 3. Filters proposed by the blueprint (roadmap Q-5, Q-6)

| Filter (on unfiltered trades) | Keeps | Kept mean R | Dropped mean R | Verdict |
|---|---|---|---|---|
| Live BTC volatility filter | 75% | **+0.375** | −0.189 | **keep (already live)** |
| KAMA(10,2,30) agrees with direction | 98% | +0.236 | +0.051 | no effect: breakouts already agree with the trend |
| McGinley(14) agrees | 99% | +0.235 | +0.101 | no effect |
| Skip "dead" markets (Garman-Klass bottom third) | 39% | +0.135 | **+0.297** | **harmful**: quiet markets are where breakouts work best |
| Live filter + KAMA | 74% | +0.380 | −0.177 | same as the live filter alone |

None of the blueprint filters adds anything; the dead-market gate is backwards for this book. Not adopted.

## 4. Exit variants (roadmap Q-4, blueprint B5)

| Exit | 4h mean R (cluster t) | 8h mean R (cluster t) |
|---|---|---|
| **Current: stop 3×ATR, target 3R** | **+0.209 (4.3)** | **+0.314 (4.0)** |
| Stop 1.75×ATR, target 3R | +0.207 (4.6) | +0.231 (4.0) |
| Trail: breakeven at 1R, 3×ATR trail | +0.169 (4.0) | +0.282 (3.6) |
| Half off at 1.5R, rest trailed | +0.154 (3.9) | +0.242 (3.7) |
| NNFX: half at 1×ATR, breakeven, trail | +0.074 (3.5) | +0.075 (2.7) |

(Unfiltered trades, so lower than section 1.) **The current exit is the best or tied best.** Taking profit early
(NNFX, scale-out) raises the win rate to ~65% and destroys the edge: the money is in the few trades that run to 3R.
No change.

## 5. Costs (roadmap Q-12)

Stress costs (5 bps slippage, 8 bps on stops) cut each 4h spec by only 0.01-0.014 R (e.g. vol_breakout +0.314 →
+0.300). With stops 3×ATR wide, costs are small next to the move. **The edge does not depend on cheap fills.**

## 6. Daily timeframe on BTC/ETH (roadmap Q-7)

2-6 trades per strategy in 5 years with the current parameters: far too few to judge (the report now refuses a
verdict under 30 trades; before this fix one 2-trade config was labelled "beats random"). Needs daily-scaled
parameters before it is worth testing again.

## 7. The wallet you chose (₹5,000, withdraw ₹2,500 at ₹10,000, risk 1-3%)

12-month windows started every month (overlapping), corrected drawdown metric:

| Setup | Typical 12-month total (withdrawn + balance) | Worst window | Typical / worst fall | 24 months | 5 years withdrawn |
|---|---|---|---|---|---|
| Flat 3% | ×2.19 | ×1.07 | 29% / 45% | ×3.26 | ₹37,500 |
| Flat 2% | ×2.11 | ×1.06 | 24% / 32% | ×3.44 | ₹32,500 |
| Flat 1% | ×1.49 | ×1.06 | 14% / 17% | ×2.19 | ₹15,000 |
| **Live: 1-3% + Ichimoku half + guards** | **×2.08** | ×0.90 | **23% / 31%** | **×3.41** | **₹25,000** |

Year by year (live setup, one run from Oct 2021): withdrawn ₹2,500 (2023), ₹7,500 (2024), ₹7,500 (2025),
₹7,500 (2026 to Sep); balance ₹8,497 at the end. No window went bust.

## 8. What changed in the engine because of these results

- Kept: volatility filter, current exits, all 8 specs (Ichimoku at half risk until promoted).
- Rejected: KAMA/McGinley filters, dead-market gate, early profit-taking exits.
- Added: cluster-aware significance, promotion on forward results, risk reasons on trade cards, weekly report,
  entry shortfall tracking, ledger back-fill, minimum-trade rule for verdicts, research-tool fixes (R3, R4, R5,
  R8, R12).

## 9. Honest limits

1. In-sample: chosen and tested on the same 5 years. Forward paper since 7 Oct is the only clean test.
2. Survivorship: only coins listed today (R7, open).
3. Overlapping windows inflate the apparent consistency of the wallet results.
4. Live entries arrive minutes after the bar close; the shortfall is now measured per trade (Q-9) and should be
   checked after the first 20 forward trades.
