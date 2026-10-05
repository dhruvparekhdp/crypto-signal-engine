# 7-day price forecast: plan

Status: PLAN, nothing built yet. Written 2026-10-05.

## What you get on the dashboard (Market → Price Outlook)

For every watchlist coin, refreshed daily after the 00:00 UTC close:

- **Fan chart**: the next 7 days, day by day. A middle line (median) plus 50% and 80% bands. The real price is drawn over old forecasts so you can see how well they did.
- **Odds**:
  - P(higher in 7 days);
  - P(touches +5% / −5% / +10% / −10% at any point);
  - the expected high and low of the week.
- **Key values table**: every input, with:
  - its current value;
  - where that sits in 5 years of history (percentile);
  - what it did to the forecast (pushed the median ↑ or ↓, widened or narrowed the bands).
- **History check**: for example, "In 143 similar past setups (12 coins, 5 years), 7 days later the median was +1.2% and 58% were higher."
- **AI section**: news and catalysts with source links and dates, the AI's adjustment and its reasoning, and what would change its view.
- **Run log**: every step with its timing; the data snapshot used; the model, prompt version, raw request and response, retries and errors.
- **Track record**:
  - how often the price landed inside the 50% and 80% bands;
  - direction hit rate against P(up);
  - quant-only, quant+AI and "no skill" baselines side by side.

## Honest framing (agreed earlier: no stack can promise direction)

A single 7-day price target is false precision. What we can defend is a **calibrated range with a lean**. The page states how much skill the forecast has actually shown, measured against a no-skill baseline. That baseline is a random walk with the coin's own volatility. If the lean has no skill, the page says so and the median stays at spot.

## Layers

### L0 · Point-in-time snapshot (no AI)

One frozen JSON per coin per run, hashed and stored. Everything later reads only this.

- Price:
  - 1d/4h/8h bars from Binance fapi plus the local lake;
  - returns over 1, 3, 7 and 30 days;
  - distance from EMA 20/50/200 in ATRs;
  - position in the 30-day range.
- Volatility:
  - realised vol over 7, 30 and 90 days, and its 5-year percentile;
  - ATR %;
  - volatility regime.
- Derivatives:
  - funding now and its 7-day average (lake + live);
  - open-interest change over 1 and 7 days (collectors/binance_futures_oi);
  - basis.
- Market:
  - BTC trend and 7-day return;
  - the coin's 30-day correlation with BTC;
  - Fear & Greed.
- Live strategies: any 4h/8h swing-book signal active now and its side, plus open paper positions.
- News: headline sentiment from the last 24h and 7d (news_sentiment, cryptopanic, sentiment_feeds), with the top headlines.
- Calendar:
  - events in the next 7 days (event_calendar: FOMC, CPI, token unlocks);
  - macro (macro_sentinel: DXY, SPX).

### L1 · Quant base forecast (no AI)

- **Width (volatility)**:
  - a HAR/EWMA forecast of 7-day volatility on daily bars;
  - widened on event days;
  - the shape comes from empirical 7-day return quantiles in the same volatility regime (5 years × 12 coins), not a normal curve, because crypto tails are fat.
- **Lean (drift)**:
  - quantile regression or logistic regression on the L0 features, walk-forward trained;
  - **shrunk to zero** unless walk-forward skill beats the baseline with a confidence interval above zero.
- **Paths**: bootstrap residual paths give the daily quantiles for days 1–7 and the touch probabilities.

### L2 · History check (analogs, no AI)

- Find the k nearest past setups in 5 years × 12 coins, using normalised L0 features.
- Show their 7-day outcome distribution.
- Blend it with L1 only if a backtest shows the blend scores better.

### L3 · AI (APIs). Three narrow jobs; never asked to produce numbers from nothing.

1. **News and catalyst analyst** (Groq gpt-oss-120b +search; one batched call for all coins). Strict JSON, one entry per catalyst:
   - coin, event, date, direction, size bucket (S/M/L), confidence and source URL.
   - Anything without a source is dropped.
2. **Forecast reviewer**, one call per coin.
   - Inputs: L0 key values, the L1 quantiles, L2 analog stats and the L3a catalysts.
   - What it may do:
     - shift the median by at most ±0.25 × the 7-day σ;
     - widen bands up to 1.5×;
     - **never** narrow the bands below the quant base.
   - Every adjustment must cite the key values behind it.
   - Model chain: gpt-oss-120b → gpt-oss-20b → OpenRouter `:free` → Gemini. If all fail, the page shows the quant forecast with "AI unavailable".
3. **Explainer** (same call or a cheap model): a plain-English summary and a ranked "why" list.

Guardrails:

- JSON schema validation; quantiles must stay in order; bounds are clamped and every clamp is logged.
- Retries plus the fallback chain.
- Prompts are versioned (`prompt_id` = hash), with a changelog.
- **Auto-off**: the AI adjustment's weight drops to zero if over the last 60 scored forecasts it doesn't beat quant-only. The score is pinball loss with a bootstrap confidence interval. It is shown, but labelled "shadow".

### L4 · Storage and logs

New table `forecast_runs`, one row per coin per run, created after a backup and never dropped. It holds:

- the snapshot JSON and its hash;
- base, analog and final quantiles;
- for each AI step: prompt id, model, latency, tokens, raw response, parsed output, clamps and errors;
- the realised path, filled in later.

The page's run log reads from this table.

### L5 · Scoring (daily job)

When a forecast's 7 days are up, it is scored on:

- 50% and 80% coverage;
- pinball loss / CRPS;
- Brier score on P(up);
- touch-probability calibration.

Three versions are scored side by side: quant-only, quant+AI and the random-walk baseline. A calibration chart goes on the page.

## Testing on old data before anything goes live

- **Quant layers (L1/L2)**:
  - walk-forward over 5 years of daily bars, a forecast every week for each coin: about 250 dates × 12 coins ≈ 3,000 forecasts;
  - the parameters are fitted only on data before each forecast date;
  - it must beat the baseline on pinball loss and be calibrated (80% band holds 75–85%).
- **AI layers: leakage is the main risk.** Models were trained on the history we would test them on; they know BTC crashed in 2022. Mitigations:
  1. Score only forecast dates **after the model's training cutoff** (the 2025–2026 window).
  2. **Anonymise**: "Coin A", no dates, prices rebased to 100. This tests reasoning on the numbers rather than memory.
  3. **No web search in backtests**, because search would see the future. News replay uses only headlines we archived ourselves at the time.
- **Prompt refinement loop**:
  - a dev window (e.g. 2025 H1) is used for iterating prompts;
  - a frozen test window (2025 H2 – 2026) is touched once per prompt version;
  - tracked per version: parse rate, clamp rate, skill against quant-only;
  - prompts are iterated on the ThinkPad's local models when it's connected; APIs are used only for the final validation run.

## API budget (free tiers)

- Live: about 1–2 news calls plus 12 reviewer calls a day ≈ 15 calls a day.
- Event refresh:
  - triggered when price leaves the 80% band, or on major news;
  - capped at 3 a day per coin.
- Backtest: about 60 post-cutoff dates × 12 coins ≈ 720 calls per prompt version. These are spread over days to stay inside Groq and OpenRouter free limits, or run locally.

## Phases

| # | What | Gate to move on |
|---|---|---|
| 1 | L0 snapshot, L1 quant forecast, backtest harness, scoring metrics | beats baseline, calibrated on 5 years |
| 2 | L2 analogs; `forecast_runs` table; fan chart, key values, run log and track record on /predict | page E2E-tested on phone and desktop |
| 3 | AI prompts v1; offline evaluation (post-cutoff, anonymised); refinement loop | parse rate ≥ 98%; no loss against quant-only on the test window |
| 4 | Live in **shadow**: AI shown and scored, quant forecast leads | 60 scored forecasts; AI on only if it beats quant-only with a CI |

It is informational only: the swing book does not use it. Using it as a trade filter would be a separate, tested decision later.

## Decisions needed

1. Coins: all 12 watchlist coins, or a subset first?
2. API spend: free tiers only, or a small paid budget for the reviewer?
3. Is the leakage approach (post-cutoff window plus anonymisation) OK?
4. Start phase 1 now (no AI needed), with AI prompt work when the ThinkPad is connected?

## Decisions (2026-10-05)

- **Coins: BTC, ETH, SOL, XRP, DOGE.** These are the top 5 by futures volume over the last 12 months, and all are volatile (45–73% a year). SOL and DOGE also have the best results per trade in the live strategies. SUI is next in line if we want more volatility.
- **APIs:** free tiers until the proof of concept works; paid when we start trading. No extra keys from family accounts: Groq, OpenRouter and Gemini forbid using several accounts to get round rate limits, and they ban all linked keys together. Instead:
  - spread the calls across providers;
  - cache results and make fewer calls;
  - use the ThinkPad's local models.
- **Leakage approach: approved.** Examples:
  - *Memory:* given "BTC, 2022-11-07, price 20,900", a model may "predict" a crash because it remembers the FTX collapse. That would be recall, not skill.
    - Fix 1: anonymise the input to "Coin A, day 0, price rebased to 100", with returns, volatility, funding and so on as plain numbers.
    - Fix 2: only score forecast dates after the model's training cutoff.
  - *Search sees the future:* a backtest at 2025-03-01 with web search on could read a 2025-03-05 article.
    - Fix: no search in backtests. News comes only from headlines we archived at the time, filtered so that `published_at` is before the forecast time.
  - *Hindsight in our own data:* an indicator that uses the current, unfinished daily bar, or volatility estimated on the whole sample.
    - Fix: every input is computed from bars that had closed before the forecast time and frozen in a snapshot. A test rebuilds a snapshot from a truncated lake and checks it matches.
  - *Prompt overfitting:* tuning a prompt until it scores well on the same period it is tested on.
    - Fix: tune on a dev window (2025 H1); the frozen test window (2025 H2 – 2026) is scored once per prompt version.
- **Order of work:** phase 1 (maths forecast and its 5-year backtest) now. The AI prompt rework also starts now, because the current AI calls benefit from it too.
