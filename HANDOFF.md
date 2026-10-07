# Handoff: where the project stands (6 Oct 2026)

Read this first in a new session (Claude Code on the web or a laptop). SYSTEM_CONTEXT.md section 0 has the system
map; this file has the decisions, results, open work and the owner's rules.

## Owner's rules (non-negotiable)
- **Everything is dev / proof of concept: paper trading only.** Before any real-money API work (live Binance keys,
  wiring `execution/`, Delta Exchange keys, any order placement) give a strict **REAL MONEY** alert and wait for an
  explicit yes. Testnet/paper needs no alert.
- `main` auto-deploys to production (GitHub Actions -> EC2). Full test suite green before every push; batch deploys
  (each push restarts the engine). Every fix gets a test that fails before and passes after.
- Production database: read-only unless a fix needs a write; back up before schema changes; never DROP/DELETE/
  TRUNCATE (the /keys "Clear" saves an empty value instead of deleting).
- Public repo: never commit secrets. Keys live encrypted in the database (/keys); never print them.
- Owner prefers: plain-English, chart-rich, phone-friendly pages with drill-down; honest numbers; mobile workflow
  (keys are edited on the phone at /keys).

## Production (EC2 52.62.37.4:8080, systemd `crypto-engine`)
- **Paper book = swing book**: 4h + 8h breakout strategies (vol-breakout, Keltner, Donchian, Ichimoku), stop 3xATR,
  target 3R, 7-day limit, 3% risk per trade, 10x leverage ceiling (each trade uses the least it needs), no limit on
  open trades, BCH/LTC excluded, strongest signal first. **Volatility filter ON** (skip when BTC 30-day volatility is
  in the top third of its past year); daily-ADX trend leg OFF (blocked 10/12 coins).
- Every swing signal, traded or skipped, is replayed to stop/target/7 days (`_swing_outcomes_job`); Paper tab card
  compares taken vs skipped.
- Old 15m detectors run shadow-only. Paper wallet still carries old 15m losses (1,735 of 3,000): owner to decide reset.
- AI: every call through `collectors/llm_client.ask_json` with a per-model free-tier budget
  (`collectors/llm_budget.py`, `/api/llm/budget`), exact cooldowns from 429s, models that stop being free benched.
  Search roles: Groq gpt-oss +search -> OpenRouter `google/gemma-4-31b-it:free` (+DuckDuckGo) -> our HF Space.
  Headlines are scored in one batch on the Space (FinBERT + rules, `analysis/headline_rules.py`), Groq as fallback.
  Every successful AI call is logged to `data/llm_calls/` on the server (fine-tuning data).
- **Keys**: `/keys` page (admin login = ADMIN_PASSWORD; database follows .env on every start). Stored encrypted
  (`config/secret_box.py`, SECRETS_MASTER_KEY in .env). Server .env holds only PORT, PYTHONUNBUFFERED, DATABASE_URL,
  ADMIN_PASSWORD, SECRETS_MASTER_KEY. Keys saved: Groq, OpenRouter, Telegram token + chat id, Twelve Data, HF token,
  HF Space URL + name. Not saved: Gemini, Anthropic, Kaggle (owner adding), Binance (live trading not wired).
- **HF Space** `dhruvdp/dhruv-llm` (private, Gradio): llama.cpp Qwen2.5-3B on CPU + FinBERT `/v1/classify` +
  bge-small `/v1/embed`. Upload with `python -m scripts.hf_space_deploy` on the server. Hardware must be CPU basic
  (it was created on ZeroGPU; owner says it switches automatically).
- Health check every 30 min from the Mac (`scripts/prod_health.py`) shown on the lab monitor.
- Not wired: `execution/` Binance live book (built and unit-tested on a fake exchange; wiring needs explicit
  permission + the real-money alert). `v2_backtest` and CoinDCX off.

## Research results (lab, 5 years of 1-minute Binance data, costs included)
- 15m strategies lose after costs; 4h/8h breakouts win (+0.20 R/trade). Mirror test: every live strategy beats its
  mirror; flipping never helped on unseen data.
- Volatility filter: +0.20 -> +0.32 R/trade, every year better; confirmed on 1h/2h/12h data and on 56 other
  strategies (trend strategies fail in wild markets, fades do the opposite).
- Wallet 25 -> 100 USDT: live settings (filter, 3%, 10x, unlimited) hit 100 in 29/48 twelve-month starts and 36/36
  twenty-four-month starts, 0 busts, median drawdown ~78%. 2-3 cycles a month is not supported by any test.
- 2h book: valid alone (+0.24 R filtered) but adding it to the 3% wallet made results worse; not added.
- Wild-market fade book: fails the null test; dropped.
- 7-day forecast phase 1: calibrated range works; direction no better than a coin flip; lean off.
- With-AI vs without-AI entry test (anonymised: no coin, no date) running on the laptop's Ollama: results in
  `status/ai_gate_live.json` there (~9 h from 6 Oct afternoon).

## Machines
- Mac: lab monitor `python -m scripts.dash` (port 8765), lab runs via `scripts/job.py`.
- Laptop `dhruv-ai` (Tailscale 100.71.216.94, user dhruv, 12 cores, Ollama): repo at ~/projects/crypto-signal-engine
  with venv and a copy of data/lake.
- SSH to EC2: key `~/.ssh/crypto-ec2` (new). GitHub secret EC2_SSH_KEY still has the old key: owner to update, then
  remove the old key from the server's authorized_keys.

## Live status (7 Oct)
- **Phase 1 of docs/INTEGRATION_PLAN.md is done and deployed** (findings L1-L11): fail-loud swing config, perp
  prices, swing account guards + news rule, 9% per-direction risk cap, gap fills, persistent dedup, regime
  fail-closed, simulator keys via env, CI test gate before deploy. Every change has a setting (config/settings.py,
  `swing_*`, `paper_gap_fills`). Defaults chosen: fail-closed config, 9% side cap, regime fail-closed.
- **Fine-tune done and better than base** on 114 held-out headlines: valid JSON 98 -> 114, score error 0.21 -> 0.13,
  event accuracy 44% -> 68%; move explanations 0/6 for both (answers cut at 600 tokens: next round). Model:
  private HF repo dhruvdp/crypto-analyst-3b-gguf. The Space is set to it (variables MODEL_REPO/MODEL_FILE, secret
  HF_TOKEN). **Space RUNNING with it (7 Oct)**: /health ready, /v1/classify 0.1 s, chat 13 s, valid JSON.
  Startup fixes: launch via Gradio (no own uvicorn), no-op @spaces.GPU for ZeroGPU, musl for the llama.cpp wheel.
- Space stays on ZeroGPU (HF blocks the move to CPU basic without PRO); works via CPU-only code + GPU stub.
- Deferred by owner (7 Oct): key rotation, GitHub EC2_SSH_KEY update (old key still on server, deploys work),
  home server move (Wi-Fi in 2-3 days). Production stays on EC2.
- Phase 2 started: honest permutation p (never 0), Bonferroni on N, trial ledger `analysis/lab/ledger.py`
  (`audit/trial_ledger.jsonl`). Release notes + evening plan: docs/RELEASE_NOTES.md.
- Application review document (map, wireframes, flowcharts): https://claude.ai/artifact/5erobCpcHL5segVYCyBeHc
- Local dev server: `scripts/dev_server.sh` (own SQLite DB, no AI keys, port 8090); all 20 pages checked, no JS errors.
- Laptop AI entry test: last seen 32/150; Tailscale was down on 7 Oct.

## Open work (in order)
1. See docs/RELEASE_NOTES.md "Plan: pending work" (sections A-E) — the current to-do list.
2. Phase 2 rest: swing null test run, ledger back-fill, v2 N from ledger, registry + live_from + drawdown alarm,
   leak/cost/known-answer/bad-data tests, findings ledger.
3. Owner decisions listed in the review document (wallet reset + live_from, sizing leverage target, server home vs
   Sydney after the Binance test).
3. Owner: update GitHub secret EC2_SSH_KEY; decide paper wallet reset; close port 8080 to the internet (Tailscale).
4. Read the AI entry-test result; decide whether AI may veto swing entries (only if it beats rules with a CI).
5. Forecast phase 2 (volatility regime in band width, /predict page); AI prompt rework with logged calls.
6. Live Binance wiring: blocked by owner permission; REAL MONEY alert required.
7. Module-by-module review of analysis/ (18k lines) and scheduler/ (14k lines).
