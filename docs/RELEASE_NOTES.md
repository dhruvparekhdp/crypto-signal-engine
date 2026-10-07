# Release notes

Everything below is dev / proof of concept: **paper trading only**. No real-money API is wired.

---

## 7 Oct 2026: Phase 1 protection, our own AI model online, honest statistics (Phase 2 start)

### 1. Production safety (Phase 1 of docs/INTEGRATION_PLAN.md) — live since commit 7364dde
Each item has a setting in `config/settings.py` so it can be switched back.

| What changed | Why it matters | Setting (default) |
|---|---|---|
| Swing strategy list is checked at every scan; a typo (unknown strategy, timeframe, parameter) stops swing trading and sends one Telegram alert | A typo used to be ignored silently and the book traded something else | `swing_config_fail_closed` (on) |
| Swing trades are priced on the Binance futures (perp) price, one batch call every 10 s | The book trades perps but was priced on spot | `swing_perp_prices` (on) |
| Daily loss limit 9% and a 2-hour pause after 3 losses in a row now apply to swing trades | Swing trades bypassed the account guards | `swing_account_guards`, `swing_daily_loss_pct` (9), `swing_streak_pause_after` (3), `swing_streak_pause_minutes` (120) |
| News blackout / news-bias rule applies to swing entries | Swing could open into a news blackout | `swing_news_guard` (on) |
| Total open risk per direction capped at 9% (skip reason `side_risk_cap_9pct`) | 3% risk x many same-direction trades was one big BTC bet | `swing_max_side_risk_pct` (0.09) |
| A stop that price gapped through fills at the gap price, not the stop | Paper results were too kind on gaps | `paper_gap_fills` (on) |
| Already-seen swing signals survive a restart (`data/swing_seen.json`); signal queue only fills while paper is on (max 50) | Restarts re-opened old signals | — |
| Regime filter fails closed: no market data, no trade (skip reason `regime_unknown`) | It used to trade when the filter could not run | `swing_regime_fail_closed` (on) |
| Market simulator gets API keys through the environment, not the command line | Keys were visible in the process list | — |
| GitHub Actions runs the full test suite before every deploy; a failure blocks the deploy | A broken import once crashed production | `.github/workflows/deploy.yml` |

The CI gate already earned its keep: it blocked three bad deploys on 7 Oct (two missing packages, one Linux-only
race in the lab work queue). All fixed.

### 2. Our own AI model on the Hugging Face Space — working
- **Fine-tuned model** (Qwen2.5-3B, LoRA on Kaggle's free GPU) beats the base model on 114 held-out headlines:
  valid JSON 98 → 114, score error 0.21 → 0.13, event-type accuracy 44% → 68%. Stored privately at
  `dhruvdp/crypto-analyst-3b-gguf`.
- **Space `dhruvdp/dhruv-llm` is RUNNING** with the fine-tuned model. Tested today (3 calls total):
  - `/health`: model ready, no errors
  - `/v1/classify` (FinBERT): 2 headlines in 0.12 s
  - `/v1/chat/completions`: "Solana ETF approved by SEC" → `{"score":0.3,"event_type":"regulatory","symbol":"sol"}`
    in 13 s
- Three startup bugs fixed along the way:
  1. The app started its own web server on the port Gradio already uses → now launches through Gradio and attaches
     the API routes to it (SSR off so the routes live on port 7860).
  2. The Space is still on ZeroGPU hardware, which refuses apps without a GPU function → a no-op `@spaces.GPU`
     function (ignored on CPU Basic).
  3. The prebuilt llama.cpp wheel needs the musl C library → `packages.txt` installs it and the app links it.
- Headline scoring now goes to the Space first (FinBERT + rules), Groq only as fallback; the Space is also the last
  fallback for market briefings and move explanations.
- Note: FinBERT called "Exchange hacked, 200M stolen" neutral. The rules layer (`analysis/headline_rules.py`)
  catches hacks as an event; watch this in the first days.

### 3. Honest statistics (Phase 2 tasks 2.1 + 2.2) — new
- **p-values are never 0 any more.** p = (random trials that matched or beat + 1) / (trials + 1). With 200 random
  trials the best possible p is 0.005, not 0.000.
- **Bonferroni on how many things were tried.** "Beats random" now needs p ≤ 0.05 / N. Lab runs report `n_tried`
  and `null_verdict` per configuration, and say "too few trials" when there were not enough random draws
  (needs 20 × N / 0.05) to tell.
- **Trial ledger** (`analysis/lab/ledger.py`, file `audit/trial_ledger.jsonl`). Every lab configuration is
  recorded with a fingerprint of its code, parameters, timeframe, exits, window, costs and data. Re-running the
  same test adds nothing; changing anything (even the data) counts as a new trial. Kept in git, not in the
  production database, which stays read-only. Switch on with `RunSpec(ledger_family="swing", ...)`.
- Old tests updated; 8 new tests (`tests/test_phase2_significance.py`, `tests/test_hf_space_app.py`).

### 4. Earlier this cycle (6–7 Oct, already live)
- `/keys` page: all API keys encrypted in the database, reveal / copy / test / clear, phone friendly; .env holds
  only the server basics.
- AI budget manager: every model kept under 85% of its free limits, exact cool-downs after rate limits, models that
  stop being free are benched automatically.
- App review document (map, wireframes, flowcharts): https://claude.ai/artifact/5erobCpcHL5segVYCyBeHc
- Local dev server (`scripts/dev_server.sh`, port 8090, own database, no AI keys): all 20 pages checked.

### Known issues
- The Space runs on ZeroGPU hardware (moving to CPU Basic needs PRO). It works, but may sleep when idle.
- Move explanations from the small model get cut at 600 tokens (0/6 in the eval for both models): next fine-tune.
- The paper wallet still carries the old 15-minute losses (1,735 of 3,000).

---

## Plan: pending work (this evening)

### A. Owner actions (decided 7 Oct)
- Space hardware: **stays on ZeroGPU**. Hugging Face blocks the move to CPU Basic without PRO. It runs fine on
  ZeroGPU because our code only uses the CPU (no-op `@spaces.GPU` stub). If it sleeps when idle, the engine falls
  back to Groq until it wakes.
- Key rotation: deferred by the owner.
- GitHub secret `EC2_SSH_KEY`: not needed now. Deploys work because the old key is still on the server; update it
  only before that old key is removed.
- Home server / Binance-from-home test: later (home Wi-Fi 2-3 days away). Production stays on EC2 Sydney.
- Still open: close port 8080 to the internet (Tailscale); decide on the paper wallet reset (recommended, pairs
  with task C2).

### B. Finish Phase 2 statistics (me, ~2 h)
1. Run the swing book's real null test on the Mac lake: 4h/8h specs, 3×ATR stop / 3R / 7 days, ledger family
   "swing", N from the ledger; report p and verdict per spec. (CPU heavy: Mac or laptop when Tailscale is back.)
2. Back-fill the ledger with the past lab runs we can reconstruct, so N is realistic, not 8.
3. v2 report reads N from the ledger instead of the fixed `TRIALS`.

### C. Forward-only evidence (me, ~2–3 h)
1. Strategy registry table: spec id, digest, status (research / incubating / trusted / paused / retired),
   `live_from`, validation record. Swing trades only incubating/trusted specs.
2. Per-spec `live_from` stamp (resets when the spec's code or params change) and a three-part card on the Paper
   tab: backtest in-sample / out-of-sample / forward since `live_from`.
3. Drawdown alarm per spec: pauses the spec and sends a Telegram alert.
   (New tables only; I back up first, nothing existing is altered.)

### D. Test batteries (me, ~2 h, all run in CI)
1. Leak battery: a "cheating" strategy must be caught, a blind one must earn about zero.
2. One canonical trade priced through lab, v2, paper and simulator cost models; they must agree.
3. Known-answer tests (stop and target on the same bar = stop; constant drift = known R).
4. Bad data: delisting gap, zero price, frozen price, spike tick → no NaN, no inverted verdict.
5. `audit/FINDINGS.md`: findings ledger seeded from the plan, each fixed item with its named test.

### E. When Tailscale is back
- Read the laptop's with-AI vs without-AI entry test (was 32/150).
- Use the laptops for the heavy null tests (B1).

### Blocked on you
- Live Binance trading: **REAL MONEY** — not started, needs your explicit yes.
