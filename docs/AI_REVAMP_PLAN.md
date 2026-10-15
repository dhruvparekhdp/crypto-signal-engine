# AI revamp plan (15 Oct 2026)

Owner: "AI part is not up to the mark, revamp entire AI part in detail; use all APIs (Gemini, OpenRouter, Hugging
Face, Groq, others), plan their usage as per their work; don't waste tokens."

## 1. What the audit found (15 Oct)

| Finding | Evidence |
|---|---|
| AI never improved a trade decision | AI gate test, 150 real swing trades, qwen3:8b: AUC 0.50, rank corr +0.03; taking only its picks cut total R from +60 to +19 |
| News sentiment has no predictive value | 1,214 stored headlines since Aug: rank corr with BTC next 4 h / 24 h = -0.03 / -0.03 |
| The web briefing invents facts | 15 Oct 08:37 UTC it reported US PPI "0.6% vs 0.4%" for a 12:30 UTC release; future-dated rows counted at full weight (fixed 1ae6f0c) |
| Most AI calls fail or wait | 24 h: 322 provider failures, 145 "nobody answered"; Groq search calls ~20k tokens vs an 8k tokens/minute limit; OpenRouter free Gemma rate-limited 167 times |
| HF Space analyst is unusable live | 430 s per answer (3B model on CPU in a ZeroGPU Space) |
| News volume is thin | 13-96 headlines/day, 33% of them AI briefing rows; a dead Yahoo feed (fixed 4ea90fa) |
| No AI touches the swing book's money | swing trades use news only for the release pause; intraday (where AI reviewed) is now off |

## 2. Principles

1. **AI never decides direction, size or whether to take a swing trade** until a shadow test with the same
   statistics as the strategies (ledger, holdout, cluster-aware CI) says it adds R. The 150-trade test said it does not.
2. **Every AI output is checked by code before anything uses it**: JSON schema, no facts dated after "now", a
   source for every event, numbers cross-checked against a feed (price, calendar) where one exists.
3. **Event-driven, not polling.** AI runs when something happened (a price shock, a release, a trade event, a new
   headline that rules flag), never on a blind hourly timer.
4. **Cheapest model that passes the bake-off for that job**, with a measured fallback chain; frontier models only
   for low-volume, high-value work (owner briefs, weekly audit, research).
5. **Hard budgets per role** (calls/day, tokens/day, Rs/month for paid keys), result cache by input hash, prompt
   caching for the fixed part of every prompt, batch mode for anything that can wait.
6. **Everything is measured**: an AI quality ledger records every output and grades it later against what happened.

## 3. Jobs and who does them

Bake-off results (section 5) decide the exact model per role; this table is the design.

| Job (role) | Trigger | Volume | Primary | Fallback | Trades? |
|---|---|---|---|---|---|
| **Headline triage** (news_scoring) | every RSS pass (5 min) | 30-100/day | Rules + FinBERT on the HF Space (0.3 s, free) | - | no |
| **High-impact check** (event_check, new) | a headline the rules tag high-impact, or BTC moves > 2.5 sigma in 1 h | 0-10/day | best batch scorer from the bake-off (Groq / OpenRouter free) | next best | pause only, after code verification |
| **Web event search** (briefing, rebuilt) | scheduled release +5 min, price shock, or 2x/day | 2-8/day | Gemini Flash with Google Search grounding (free tier, key needed) | Groq gpt-oss-20b+search | pause only, after verification |
| **Owner daily brief** (owner_brief, new) | 08:30 IST daily + on request | 1-2/day | Claude Haiku (key coming) | Groq gpt-oss-120b | no |
| **Trade explainer** (trade_note, new) | swing open, ladder step, close | 1-6/day | Claude Haiku | Groq gpt-oss-20b | no |
| **Weekly audit** (research) | Monday 03:30 UTC, batch | 1/week | Claude Sonnet (Batch API, half price) or Gemini Pro free | Groq gpt-oss-120b | no (writes hypotheses for the lab) |
| **Move attribution** (attribution) | BTC/coin moves > 3% in 4 h only | 0-5/day | Gemini Flash + search | Groq +search | no |
| **Bulk labelling / fine-tuning data** (history) | nightly batch | hundreds | ThinkPad Ollama qwen3:8b (free) | Groq off-peak | no |
| **Embeddings** (dedup, similar past events) | each stored headline | 30-100/day | bge-small on the HF Space | - | no |
| ~~Position review every 30 s~~ | removed with intraday | - | - | - | - |
| ~~HF Space 3B analyst~~ | removed from all chains (430 s) | - | - | - | - |

## 4. Architecture

* **AI gateway** (`collectors/llm_client.py` grows into it): one registry of roles, each with purpose, trigger,
  output schema, verifier, chain, budget, cache TTL and kill switch. `/settings` edits chains and budgets.
* **Verifier layer** (`analysis/ai_verify.py`, new): rejects future-dated facts, events with no source, numbers that
  disagree with our own feeds, and anything not matching the role's schema; records why.
* **Quality ledger** (`ai_outputs` table, new): input hash, role, model, latency, tokens, cost, verifier result, and a
  later grade (headline score vs BTC move, event pause vs realised volatility, trade note vs outcome).
* **Portal: System > AI** tab: per role calls, failures, cost, quality grade; per provider limits left.
* **Prompt caching + result cache**: the fixed instructions of each role are sent as a cached block (Claude) or
  kept identical (others); identical inputs within the TTL reuse the stored answer.

## 5. Bake-off (15 Oct, 120 real headlines, unreleased-PPI trap, live-price check)

Raw numbers: `data/lab/ai_bakeoff.json` (scripts/ai_bakeoff.py + ai_bakeoff_report.py; re-run when the Gemini and
Claude keys are saved). 120 headlines, 4 batches of 30 (Groq qwen 12 batches of 10, its 6k tokens/minute cap).
"Noise caught" = share of known-noise headlines (Fed bank approvals, stock stories) marked irrelevant. Direction =
rank correlation of the score with BTC's next 24 h move; with 120 headlines anything within about +-0.18 is noise.

| Model | Answered | Speed / 30 headlines | Noise caught | Direction (24 h) | Unreleased-PPI trap | Live BTC price |
|---|---|---|---|---|---|---|
| Groq gpt-oss-120b | 120/120 | 4.0 s | **100%** | -0.07 | honest | said unknown; **correct with search** |
| Groq gpt-oss-20b | 120/120 | **1.2 s** | 92% | -0.07 | honest | said unknown; **correct with search** |
| Groq qwen3.8-27b | 120/120 (batches of 10) | 1.2 s / 10 | **100%** | +0.03 | honest | said unknown |
| HF Space FinBERT | 120/120 | 1.6 s | 58% | -0.09 | - | - |
| ThinkPad qwen3:8b | 120/120 | 278 s | 92% | -0.06 | - | - |
| ThinkPad llama3.2:3b | 120/120 | 143 s | 17% | -0.14 | - | - |
| OpenRouter Nemotron Ultra (free) | 0/120 (broken JSON, 70-105 s) | - | - | - | honest (13 s) | said unknown |
| OpenRouter Gemma 4 31B (free) | 0 (429 rate limit) | - | - | - | no answer | no answer |
| OpenRouter Laguna S (free) | 0 (429) | - | - | - | no answer | said unknown |
| OpenRouter Inkling (free) | 0 (403 blocked for this account) | - | - | - | - | - |
| OpenRouter Nemotron Super (free) | 0 (empty replies) | - | - | - | - | - |
| HF Llama-3.1-8B (router) | 0 (4 min timeouts) | - | - | - | - | - |
| Gemini, Claude | not tested - no key saved | | | | | |

What it means:
* **No model can tell direction from headlines** (every correlation is inside the noise band). News is a risk
  radar (what is happening, should we pause), never a buy/sell input. This matches the 1,214-headline study.
* **Asked plainly, every model was honest about the unreleased PPI.** The invented number came from our briefing
  prompt ("cite the real numbers, vague summaries strictly prohibited"), which pushed the model to produce figures.
  The prompt is fixed and code now drops anything dated after "now" (1ae6f0c).
* **Groq is the only free provider that works reliably today**, so it leads every chain; never three Groq entries
  in a row (they share one rate limit), and a non-Groq last resort. OpenRouter's free tier (50 calls/day on this key,
  constant 429s) is a last resort only. Gemini (free tier with Google Search) is the planned second provider.
* **FinBERT is a fast first pass, not a filter**: it lets 42% of noise through, so the headlines it calls strong or
  high-impact now get one batched Groq check before they can pause trading.
* **The HF analyst and HF Llama are removed from every chain.** The ThinkPad is good for overnight batch work only.

## 6. Phases

* **A. Stop the waste (done 15 Oct):** chains rebuilt from the bake-off (HF analyst, HF Llama, failing OpenRouter
  models removed; research Sonnet not Opus); briefing every 3 h instead of hourly; FinBERT-flagged headlines get
  one batched Groq check; future-dated briefing items dropped; intraday off. Next in A: event-triggered briefing
  (release +5 min, price shock) instead of a timer.
* **B. Claude key:** owner brief, trade explainer, weekly audit; Rs cap on /settings; prompt caching; batch.
* **C. Gemini key:** web event search with grounding; re-run the bake-off with Gemini + Claude in it.
* **D. Quality ledger + System > AI tab:** grade every output; promotion rule before any AI output may pause trading.
* **E. Research agent (offline):** proposes lab hypotheses from the ledger; every idea goes through the trial
  ledger and the untouched holdout like any strategy.

## 7. Owner actions

* Add a **Gemini** key (aistudio.google.com, free tier) on /keys.
* Add the **Claude API key** (console.anthropic.com; not the Pro/Max membership) on /keys, with a monthly cap.
* Decide on the HF Space: keep it for FinBERT + embeddings only (recommended), or move the analyst onto the
  ZeroGPU properly (needs code inside an `@spaces.GPU` function and has a daily GPU-minute quota).
