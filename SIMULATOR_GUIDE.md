# Live Market Simulator & Quantitative Strategy Testing Guide

Welcome to the definitive user and technical guide for the **Live Market Simulator** (`crypto-signal-engine`). This tool allows you to rigorously backtest institutional setups, compare quantitative trading strategies, and simulate compounding cycle challenges (e.g., growing $25 into $100) before risking live capital on Binance.

---

## 1. Demystifying 'R', ROI, and ROE (The $100 Math Model)

### What is 'R'?
In quantitative trading, **R** stands for **Unit of Risk**. Rather than using arbitrary dollar targets or fixed price percentages, professional hedge funds calibrate position risk to the asset's current volatility (ATR — Average True Range).

In this simulator:
$$\text{1R Benchmark Distance} = \max(1.8 \times \text{ATR}, \text{Price} \times 0.012) \approx 1.2\% \text{ price move}$$

### Why Does Leverage Scale 'R'?
When trading futures with leverage, your **Return on Equity (ROE)** is:
$$\text{ROE (\%)} = \text{Underlying Price Movement (\%)} \times \text{Leverage}$$

### Concrete Example on $100 Base Margin:

Suppose you allocate **$100.00 USDT** as margin for a trade:

| Parameter | At 10x Leverage | At 25x Leverage | Notes |
|---|---|---|---|
| **Effective Position Size** | $1,000.00 USDT | $2,500.00 USDT | Margin × Leverage |
| **1R Volatility Baseline** | 1.20% price move | 1.20% price move | Minimum structural noise buffer |
| **1R Cash Equivalent** | **$12.00 USDT** | **$30.00 USDT** | 1R = 12% ROE (10x) vs 30% ROE (25x) |
| **Target Multiple (2.2R)** | **+26.4% ROE** (+$26.40 profit) | **+66.0% ROE** (+$66.00 profit) | Price target: +2.64% |
| **Target Multiple (5.5R)** | **+66.0% ROE** (+$66.00 profit) | **+165.0% ROE** (+$165.00 profit) | Price target: +6.60% |
| **Stop-Loss Multiple (1.5R)** | **-18.0% ROE** (-$18.00 loss) | **-45.0% ROE** (-$45.00 loss) | Stop price: -1.80% |
| **Stop-Loss Multiple (3.0R)** | **-36.0% ROE** (-$36.00 loss) | **-90.0% ROE** (-$90.00 loss) | Stop price: -3.60% |

### Live Interactive UI Helper
Inside the dashboard's **⚙️ Strategy & Settings** tab, you will find:
1. **Live Input Badges**: Right above the input boxes, the badges automatically display the exact ROE % and dollar profit/loss on $100 as you type:
   - `🎯 Target: +55.0% ROE · +$55 on $100`
   - `🛑 Stop-Loss: -30.0% ROE · -$30 on $100`
2. **Interactive Math Breakdown Card**: Updates in real-time with price distances, risk-to-reward ratio ($R:R = \frac{T_R}{S_R}$), and simulated account balance after trade.

---

## 2. Quantitative Strategy Catalogue

You can test individual strategies or an ensemble using the **🎯 Strategy Selection** dropdown:

### 1. `all` — Multi-Strategy Ensemble
Evaluates all strategy detectors in an institutional priority waterfall:
- High-probability Confluence setups receive primary allocation.
- Breakout & squeeze setups capture trending momentum.
- High-confidence reversal divergences take counter-trend entries at key levels.

### 2. `confluence` — 5-Family Confluence Gate
The most conservative setup. Requires multi-family alignment across:
- **Trend**: Alignment with Higher Timeframe EMAs (4h / 1h).
- **Momentum**: RSI & MACD directional agreement.
- **Volume**: Volume expansion confirmed via volume baseline.
- **Volatility**: Bollinger & Keltner band expansion.
- **Order Flow**: Cumulative Volume Delta (CVD) support.

### 3. `bollinger_squeeze` — Volatility Squeeze Breakout
Detects periods when standard Bollinger Bands contract inside the Keltner Channel (volatility compression), and enters on the explosive directional expansion candle.

### 4. `volume_spike` — Volume Anomaly Surge
Fires when buy or sell volume surges to 2.5x - 4x the 20-period moving average, detecting institutional whale accumulation or distribution.

### 5. `rsi_divergence` — Momentum Divergence
Identifies trend exhaustion:
- **Bullish Divergence**: Lower low in price with higher low in RSI.
- **Bearish Divergence**: Higher high in price with lower high in RSI.
Exempt from macro-trend filters to capture early turning points.

### 6. `trend_pullback` — Dynamic EMA Retracement
Waits for a strong directional trend on the 1h/15m chart, then triggers on shallow pullbacks into the 20-period or 50-period EMA support with a candlestick confirmation bounce.

### 7. `range_breakout` — Momentum Range Breakout
Identifies tight horizontal consolidation channels (consolidation over 15+ bars) and enters when price decisively breaks outside the channel with ATR buffer confirmation.

### 8. `sweep_reclaim` — Liquidity Sweep & Reclaim
Hunts false breakouts. If price sweeps below key swing lows to trigger retail stop orders and immediately reclaims the level within 1-2 candles, it fires an aggressive reversal entry.

### 9. `breakout_retest` — Support/Resistance Flip
Waits for a key swing high to break, then confirms an entry only after price pulls back and tests the prior resistance level as newly established support.

---

## 3. Compounding Cycle Challenge ($25 ➔ $100)

The compounding challenge simulates realistic capital scaling:
- **Starting Account Balance**: $25.00 USDT.
- **Goal**: Grow account balance to $100.00 USDT (400% gain).
- **Dynamic Position Sizing**: By default, each trade risks 25% of current equity with 10x or 25x leverage. As your balance compounds ($25 ➔ $40 ➔ $70 ➔ $100), position size scales up automatically.
- **Real-World Execution Friction**:
  - Exchange Taker Fee: 0.05% applied on full leveraged position value.
  - Realistic Dynamic Slippage applied on both entry and exit.
- **Cycle Outcomes**:
  - `TARGET_REACHED`: Balance reaches $100.00. Recorded as a win, balance resets to $25.00 to start the next cycle.
  - `BUSTED`: If balance drops to $0.00 (liquidation / max drawdown), cycle is marked as busted and resets to $25.00.

---

## 4. Zero-AI / 100% Offline Mode

When running the simulator:
- Set **AI Provider** to **`⛔ Disabled / Offline (0 AI API Calls)`** (Default).
- **Why?**
  1. **Zero Token Cost**: Consumes 0 API calls on Gemini, Groq, OpenRouter, or Hugging Face.
  2. **Zero Rate Limits**: Never triggers 429 quota exhaustion or daily token limits.
  3. **Instant Speed**: Evaluates 1,000s of candles per second using local quantitative algorithms.
  4. **Consistent Evaluation**: Local quantitative heuristics generate complete reasoning for why trades succeeded or hit stop losses.

---

## 5. Mobile Zero-Lag Architecture

Previous simulator versions lagged mobile devices by sending 100,000+ lines of raw JSON (5,000+ completed trades) directly to the browser DOM.

### The Modular View Selector
The new interface divides simulator data into 5 modular, on-demand tabs:
1. **📊 Executive Metrics**: Summary cards, win rate, profit factor, and compounding challenge scorecard.
2. **🏆 Compounding Cycles**: Choose individual cycles from a dropdown picker. Only loads 15 transactions per page for the selected cycle via `/api/simulator/cycle-ledger`.
3. **📜 Trade Stream**: Smooth 10-trade client pagination with `[◀ Prev]` and `[Next ▶]` buttons.
4. **📡 AI Events Log**: View external AI API telemetry only when enabled.
5. **⚙️ Strategy & Settings**: Strategy selector, live duration estimator, and the live R math breakdown card.

---

## 6. CLI Usage & Headless Execution

To run simulations directly from the command line:

```bash
# 1-Day fast test with Confluence strategy and 0 AI calls
python -m scripts.run_market_simulator \
  --symbols BTCUSDT,ETHUSDT,SOLUSDT \
  --years 0.00274 \
  --strategy confluence \
  --ai-provider none \
  --cycle-start 25 \
  --cycle-target 100 \
  --cycle-leverage 10

# 1-Week test with Bollinger Squeeze strategy
python -m scripts.run_market_simulator \
  --symbols BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT \
  --years 0.0192 \
  --strategy bollinger_squeeze \
  --ai-provider none \
  --tp-r 2.5 \
  --sl-r 1.2
```
