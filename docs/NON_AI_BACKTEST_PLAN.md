# Non-AI backtest plan (8 Oct 2026) — Mac + ThinkPad only

EC2 ignored. AI gate paused until this queue finishes. Then AI (Ollama on ThinkPad) resumes.

## Why this order

1. **No AI first.** AI reviews add hours per trade and cannot invent edge. First we need honest
   strategy / timeframe / exit / cost / wallet numbers from the same lab code the paper book uses.
2. **ThinkPad = compute** (12 cores, 15 GB, lake on disk). **Mac = monitor + light reports** (8 GB).
3. **Live book first**, then timeframes, then exits/costs, then wallet sizing, then broader search.
   That matches how we trade today (4h/8h breakouts) and answers “does the new deployment still hold?”
4. Every `run_lab` with `--ledger-family` writes the trial ledger so Bonferroni N stays honest.

## What “new deployment” means here

Paper swing settings in code now:

| Knob | Value | Why we retest it |
|---|---|---|
| Strategies | vol_breakout z=3, keltner (k=2.5@4h / 2.0@8h), donchian n=100, ichimoku | Live specs |
| Exits | 3×ATR stop, 3R target, 7-day limit | Best in prior exit bake-off; confirm after code fixes |
| Costs | india_gst (+ stress) | Matches paper fees |
| Coins | 10 perps (no BCH/LTC) | Live exclude list |
| Sizing | risk 1–3% adaptive, margin ≤15%, lev ≤10×, Binance lots, side risk 9% | Wallet plan F |
| Filter | BTC 30d vol rank ≤ 0.67 | Wild markets kill the edge |

Research-tool fixes (R3/R4/R5/R8/R12, min 30 trades for verdict) are in the synced tree so reports
do not lie.

## Queue (no AI)

| # | Job | Machine | ETA (order-of-magnitude) | Reasoning |
|---|---|---|---|---|
| N0 | Sync code + Mac `data/lab/runs` → ThinkPad | both | 5–15 min | Edge/wallet scripts need named runs; ThinkPad was on old main |
| N1 | `swing_null_4h` + `swing_null_8h` (live params, null trials) | ThinkPad | 1–3 h | Are the 8 live specs still better than random under new code? |
| N2 | TF sweep: same 4 strategies @ **1h, 2h, 4h, 8h, 12h** swing exits | ThinkPad | 3–8 h | Is 4h/8h still the sweet spot vs faster/slower bars? |
| N3 | Exit variants `exits_4h` / `exits_8h` (swing, b5_*, trail) | ThinkPad | 2–4 h | Confirm current exit still wins (Q-4) |
| N4 | `stress_swing_4h` (cost=stress) | ThinkPad | 0.5–1 h | Edge must survive worse fees/slip (Q-12) |
| N5 | Daily TF BTC/ETH only (`daily_trend`) | ThinkPad | 0.5–1 h | Q-7; expect few trades — verdict needs ≥30 |
| N6 | `wallet_plan_backtest` + `sizing_backtest` | Mac or ThinkPad | 10–30 min | ₹5k / sweep ₹2.5k / 1–3% risk curves |
| N7 | `edge_report` (cluster t, regimes, filters) | Mac or ThinkPad | 10–40 min | Publishes the scoreboard JSON for the report |
| N8 | Broader breakout+trend grid @ 4h (ledger-counted) | ThinkPad | 4–12 h | Search for anything that beats live book *after* Bonferroni |

## After non-AI completes → AI

| # | Job | Notes |
|---|---|---|
| A1 | Resume `ai_gate` 68→150 on ThinkPad Ollama (`qwen3:8b` think) | Does AI veto improve mean R vs taking all? |
| A2 | Optional second model (`deepseek-r1:8b`) on same sample | Robustness of the gate decision |
| A3 | Write comparison report | Only promote AI veto if CI beats rules |

## How to watch

```bash
# ThinkPad
ssh dhruv@100.71.216.94 'cd ~/projects/crypto-signal-engine && ls status && tail -f logs/non_ai_queue.log'

# Mac lab monitor (if dash is up)
python -m scripts.dash   # port 8765
```

## Honesty limits (read before celebrating)

- Same history the strategies were chosen on → **upper bound**.
- Overlapping wallet windows inflate consistency.
- Survivorship: only today’s perps.
- Live paper still enters later than the lab’s next-1m-open fill (L5).
