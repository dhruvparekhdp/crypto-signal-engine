# System Context & Engine Memory

This document is the technical memory of the **Crypto Signal Engine** (`dhruvparekhdp/crypto-signal-engine`), written so a
fresh session can pick up full context. **Section 0 is current (6 Oct 2026). Sections 1-11 below it describe the older
15-minute intraday system; where they disagree with section 0, section 0 wins.**

---

## 0. Current state (verified 6 Oct 2026)

### What trades (paper)
- **Swing book** (`analysis/swing_book.py`, `scheduler/runner.py::_swing_scan_job/_open_swing_signals`): the only family
  that opens paper trades (`paper_open_families="swing"`). Strategies validated on 5 years of 1-minute Binance data with
  costs (fees + 18% GST + slippage + funding), null test, walk-forward and 2.5x slippage stress (`analysis/lab`):
  4h vol-breakout z=3, Keltner k=2.5, Donchian n=100, Ichimoku; 8h Keltner k=2, vol-breakout z=3, Donchian n=100, Ichimoku.
  Live signals are computed by the same code as the backtest (parity test in `tests/test_swing_book.py`).
- Exits exactly as tested: stop 3xATR (refused if wider than 8%), target 3R, 7-day limit. No profit lock, trail, ladder,
  stagnation exit or AI close on swing positions.
- Sizing: `swing_risk_pct` 3% of the wallet per trade, leverage ceiling 10x (each trade uses the lowest it needs),
  no limit on open trades (`swing_max_open=0`, `swing_max_same_side=0`), BCH/LTC excluded (no edge in 5 years),
  strongest strategy/coin first when signals arrive together (`swing_book.priority`).
- **Market filter** (`analysis/regime_gate.py`, `swing_regime_filter="on"`): skip a swing signal when Bitcoin's 30-day
  volatility is in the top third of its own past year (point in time). Backtest: +0.20 -> +0.32 R/trade, every year
  better, and it held on 1h/2h/12h data and on 56 other strategies. The daily-ADX "strong trend" leg exists but is off
  (`swing_regime_adx_max=0`): it blocked 10 of 12 coins in a trending week.
- Every swing signal, traded or skipped, is replayed on 1h bars to stop/target/7 days (`_swing_outcomes_job`), so the
  Paper tab can compare "taken" vs "skipped by the filter". The intraday outcome job ignores swing signals.
- The old 15m detectors still run and log signals, **shadow only** (no paper trades, no AI review).

### AI providers
- Every call goes through `collectors/llm_client.py::ask_json` and a per-model free-tier budget
  (`collectors/llm_budget.py`): requests/min, requests/day, tokens/min, tokens/day at 85% of the limit, Groq's
  `x-ratelimit-*` headers honoured, a 429 cools the model down for exactly the time the provider gives. Counts persist in
  `data/llm_budget.json`. View: `GET /api/llm/budget`.
- Search roles (briefing, attribution, event monitor): Groq gpt-oss +search, then OpenRouter free with DuckDuckGo
  results injected, then our own Hugging Face Space (`huggingface_space/`, llama.cpp on the free CPU, slow) once
  `HF_BASE_URL` and `HF_SPACE_API_KEY` are set. Briefing every 60 min, attribution every 3 h, event monitor 30/day.
- Why: on 5-6 Oct Groq's free daily token limit was hit ~240 times; demand was 4-5x what the free tier allows.

### Not wired / off
- `execution/` (Binance USD-M live book): built and unit-tested on a fake exchange, **not called by the engine**;
  `live_trading_mode="off"`. Wiring it needs the owner's explicit permission.
- `v2_backtest` (timed out at 90 min daily), `coindcx` (0 symbols matched): off.

### Research tooling (Mac / laptop, not production)
- `analysis/lab` backtester; `scripts/run_lab.py`, `portfolio_wallet.py`, `wallet_experiments.py`, `mirror_analysis.py`,
  `regime_filter.py`, `wild_research.py`, `forecast7d_backtest.py`; tracked with `scripts/job.py`.
- Lab monitor `scripts/dash.py` (port 8765): backtest progress, findings, wallet passbooks, production health
  (`scripts/prod_health.py`, every 30 min).
- Second machine `dhruv-ai` (Tailscale 100.71.216.94, user `dhruv`, 12 cores, Ollama): repo at
  `~/projects/crypto-signal-engine` with its own venv and a copy of `data/lake`.

### Operations
- Web portal: `/` opens the four-hub portal (`/command`, `/book`, `/evidence`, `/system` and their pages, e.g.
  `/book/paper`); every other page carries the same header (`scheduler/portal/nav.js` is the one nav map). Code in
  `scheduler/portal.py` and `scheduler/portal/` (core + per-hub widget modules). Old addresses (`/#paper`, `/data`, …)
  redirect. Design, page map, redirects, components: `docs/PORTAL_REVAMP.md`.
- Production: EC2 `52.62.37.4:8080`, systemd `crypto-engine`, deploy = push to `main` (GitHub Actions restarts the
  service). Each deploy restarts the engine, so changes go out in batches.
- Tests: 1,473 passing (`python -m pytest -q`). `tests/conftest.py` gives every test a fresh AI budget file.

---

## 1. System Overview & Architecture

The **Crypto Signal Engine** is a real-time market-intelligence and paper-trading simulator running 24/7 on a single EC2 box. It ingests 1-minute OHLC candles and order-book/trade-stream data across a watchlist of crypto perpetuals (plus a few commodities), evaluates setups across 5 independent detector families with a higher-timeframe trend filter, runs the surviving candidates through an AI review chain, and simulates leveraged paper executions with profit-lock and trailing-stop exits.

```
┌────────────────────────────┐    ┌────────────────────────────────┐    ┌────────────────────────────────┐
│     Live Market Data       │    │       Engine Pipeline           │    │     Execution & Storage        │
├────────────────────────────┤    ├──────────────────────────────────┤    ├────────────────────────────────┤
│ • Binance Klines (1m OHLC) │───►│ • 5 detector families           │───►│ • Paper Simulator (Leverage)  │
│ • Binance liq/large-trade  │    │   (confluence, RSI divergence,  │    │ • Profit lock + trailing stop │
│   streams (observational)  │    │   volume spike, BB squeeze,     │    │ • AI post-mortem on close     │
│ • CoinDCX / CoinGecko      │    │   sentiment shift)              │    │ • PostgreSQL (Aiven, prod) /  │
│ • TwelveData (Gold/Oil)    │    │ • Orderflow / CVD + absorption  │    │   SQLite (tests/dev)          │
│ • CryptoPanic sentiment    │    │ • Daily + 1h/15m trend filter   │    │ • Telegram alerts              │
└────────────────────────────┘    │ • Calendar caution / precedent  │    │ • Web dashboard (port 8080)   │
                                   │ • Confidence floor              │    └────────────────────────────────┘
                                   │ • AI pre-trade review chain     │
                                   │   (mirror review, opposite      │
                                   │   direction, optional)          │
                                   └──────────────────────────────────┘
```

Notes on what changed since the last refresh of this doc:
- There is **no confluence "gate" that blocks the other four families** — `ConfluenceAnalyzer` is one of 5 independent detectors evaluated in parallel in `CryptoEngine.process()` (`analysis/crypto_engine.py`). It is listed first in code because it is the one that requires multi-indicator agreement, not because it gates the rest.
- There are 5 detector families today, not 6: Confluence, RSI Divergence, Volume Spike, Bollinger Squeeze, Sentiment Shift.
- "Groq AI Sentinel" is no longer the only AI reviewer — see §6 (Recent major features) for the multi-provider fallback-chain system that replaced the single-provider Groq call and the settings-page model dropdown.
- Orderbook wall veto is not implemented as described in the previous version of this doc; what exists is orderflow/CVD alignment and absorption detection (`analysis/orderflow.py`), applied as a confidence adjustment or veto per direction, not a standalone "wall" check.

### Signal pipeline, in order (`CryptoEngine.process`, `analysis/crypto_engine.py`)
1. Orderflow/CVD trend computed from 1m candles (`orderflow_enabled`).
2. Each of the 5 detectors independently proposes a candidate signal (or `None`).
3. Absorption veto/boost and CVD-divergence confidence penalty (orderflow).
4. Institutional-flow calendar caution — confidence penalty only, never a veto (`calendar_caution_enabled`).
5. Event-precedent brief — confidence penalty and optional extended-hold flag, off by default (`event_precedent_enabled`).
6. Confidence floor check (`crypto_min_confidence`) — signals below this are dropped before they ever fire or are shown anywhere.
7. Higher-timeframe trend veto (`crypto_htf_filter_enabled`, `daily_trend_filter_enabled`) — RSI divergence is exempt since it hunts reversals by design.
8. Cooldown, "already live" duplicate suppression, and opposing-live-signal contradiction check.
9. Signal fires, is logged, and goes to the AI pre-trade review chain and, if it survives, the paper trading simulator.

### Scheduler jobs (`scheduler/runner.py::setup_jobs`)
Always-on: `db_cleanup` (6h), `snapshot_labels` (15m), `research_pass` (weekly), `heartbeat` (10m), `paper_trading_tick` (configurable, default 30s), `resolve_signal_outcomes` (15m), `coindcx_poll`, `binance_klines`, `coingecko_poll`, `crypto_analysis` (60s), `mirror_review` (60s, no-op unless mirror review is on and something is tracked), `move_attribution` (hourly by default), `daily_trend` (60m), `v2_backtest` (daily cron at `v2_backtest_hour_utc`, currently 02:00 UTC / ~07:30 IST, plus a one-off "first deploy" run 3 minutes after startup if no report exists yet), `v2_shadow` (5m), `event_evaluation` (30m), `news_sentiment` (5m), `crypto_snapshot` (configurable, default 120s), `binance_oi_poll` (120s, if enabled). Conditional: `sentiment_refresh` (if sentiment feeds on and not binance-only), `crypto_news` (if not binance-only), `event_precedent` (4h, only if `event_precedent_enabled`), `market_briefing` (only if the adaptive `event_monitor` is off; otherwise the monitor self-schedules).

---

## 2. Infrastructure & Deployment Environment

| Component | Specification | Details |
|---|---|---|
| **Compute Host** | AWS EC2 (Ubuntu, `t3.small` per repo history — unverified live instance type; not re-confirmed this pass) | Hosted at the IP in the GitHub Actions `EC2_HOST` secret |
| **Service Manager** | Linux `systemd`, unit `crypto-engine.service` (`deploy/crypto-engine.service`) | `Restart=always`, runs as the `ubuntu` user, `WorkingDirectory=/home/ubuntu/crypto-signal-engine` |
| **Database** | PostgreSQL in prod (Aiven.io in practice); SQLite (`sqlite+aiosqlite:///./crypto_engine.db`) is the default/test connection string | `database_url` in `.env`; `database_ssl_ca` optional for `verify-full` |
| **Port Routing** | App listens on `PORT` (8080) | `deploy/setup_ec2.sh` / historical notes mention an `iptables` 80→8080 redirect; not re-verified this pass |
| **CI/CD Pipeline** | GitHub Actions (`.github/workflows/deploy.yml`), triggered on push to `main` (and manual `workflow_dispatch`) | **Not a webhook** — the job SSHes into the EC2 box (`appleboy/ssh-action`), runs `git fetch origin main && git reset --hard origin/main`, reinstalls `requirements.txt`, restarts the `crypto-engine` service, then **polls `http://127.0.0.1:8080/health` up to 10 times (3s apart) for HTTP 200** before declaring success. If `/health` never returns 200 within ~30s the workflow fails loudly (this replaced an earlier version that only checked `systemctl status`, which reported "success" for a process seconds from crashing). Telegram notifications are sent at start, success, and failure if `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` secrets are set. |
| **First-time EC2 setup** | `deploy/setup_ec2.sh` | Installs the systemd unit, grants the `ubuntu` user passwordless `sudo systemctl {start,stop,restart,status}` for the service (needed for the Actions deploy step), caps journald at 500 MB, and sends a one-time Telegram ping. |
| **Test Coverage** | `pytest` | **1289 passed, 853 subtests passed** as of 2026-09-28 (run: `rm -f crypto_engine.db && python -m pytest -q`). Do not quote "606" anywhere — that figure is long stale. |

### Data retention & archival — corrected

The previous version of this doc said "snapshot retention is limited to 3 days." That is wrong and has been wrong for a while. The real design, per `config/settings.py` and `storage/cold_storage.py`:

- **`snapshot_retention_days` = 120** (was a hardcoded `days=3` originally; raised because `crypto_snapshots` is the only table with both features and forward-looking labels, and a model cannot be trained on 3 days of history). Snapshots older than this move from the live `crypto_snapshots`/`commodity_snapshots` tables into `crypto_snapshots_archive`/`commodity_snapshots_archive` — **never deleted**, only relocated (`storage/repository.py::archive_old_crypto_data`, run inside `db_cleanup` every 6h). The copy and delete happen in one transaction keyed off an id ceiling captured up front, so a re-run never double-archives and a row written mid-run is never touched.
- **`db_retention_days` = 365**. Rows older than a year (across the archive tables, `crypto_signal_log`, `signal_reviews`, `news_sentiment`, `market_briefings`, `move_attributions`, `market_events`, etc. — see `TIME_COLUMN` in `storage/cold_storage.py`) are written out to gzipped JSON Lines files, one per table per month, under `cold_storage_dir` (default `data/archive/{table}/{YYYY-MM}.jsonl.gz`), and only then removed from the database. `storage/cold_storage.py::read_rows` streams them back for analysis (`zcat | jq`-readable, no special tooling required). The owner's stated rule: **never delete history**, keep the live DB footprint bounded instead.
- The signal log (`crypto_signal_log`) is exempt from the 120-day snapshot archival step — it is the evaluation record the null test, accuracy page, and audit read — but it does eventually cold-storage out past `db_retention_days` like everything else.
- Tables holding configuration or open state (watchlist, open positions, settings, auth) are never touched by either process.

---

## 3. Key Architectural Decisions & Pivots

### A. Decoupled Sports Betting Subsystem
- The original build contained sports collectors and Markov-chain models for tennis/football.
- All sports collectors and associated poll routines were decoupled from runtime startup; CPU and RAM on the EC2 instance are dedicated to crypto order flow, candle processing, and simulator execution.

### B. Database Migration from Neon to Aiven.io
- **Reason**: Neon's free tier introduced quota locks and connection drops that caused downtime.
- **SSL Resolution**: Aiven uses a self-signed root CA for cluster certificates. In `storage/database.py`, `_make_url` configures `asyncpg` with an `ssl.SSLContext` (`ssl.CERT_NONE`, `check_hostname=False`), enabling TLS in transit while avoiding `CERTIFICATE_VERIFY_FAILED` errors. `database_ssl_ca` exists as a path to a real CA file for anyone who wants to move to `verify-full` instead.

### C. Zero-Downtime Admin Settings & Paper Trading Control
- **Database Persistence**: Operational toggles, paper trading levers, and strategy parameters are stored in singleton tables in the database (`paper_trading_config`, `strategy_config`), with precedence **database value > `.env` > code default** (`config/overrides.py`).
- **This replaced the old Groq-model-dropdown UI.** The `/settings` page used to expose a single dropdown for picking one Groq model. That is gone. In its place is the multi-provider fallback-chain system described in §6 below — chains of `provider:model` pairs, configured via `LLM_CHAIN_*` env vars, with per-role chains (pre-trade, post-trade, position review, research, briefing, attribution, news scoring, event precedent, history labelling).
- A field's `live=True`/`False` flag in `config/overrides.py::Field` controls whether a change takes effect immediately or needs a restart; the settings page is honest about which is which rather than pretending everything is live.

### D. Instant Boot Web Server
- `main.py` starts the health/web server immediately on process startup, well before background database migrations or historical data checks run, so `/health` (and the Actions deploy poll) does not time out waiting on slow startup work.

---

## 4. Environment Configuration (`.env`)

The real, current `.env.example` at the repo root is the source of truth — copy it, don't hand-type this. As of this pass it contains:

```ini
# ── Core Server ───────────────────────────────────────────────────────────────
PORT=8080
PYTHONUNBUFFERED=1

# ── Database & Admin Authentication ───────────────────────────────────────────
# PostgreSQL Database URL (Aiven, Neon, Supabase, or AWS RDS):
DATABASE_URL=postgresql://user:password@host:port/dbname?sslmode=require
ADMIN_PASSWORD=your_admin_password_here

# ── Telegram Notifications ───────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here
TELEGRAM_CHAT_ID=your_telegram_chat_id_here

# ── Groq AI Sentinel ──────────────────────────────────────────────────────────
GROQ_API_KEY=your_groq_api_key_here

# ── Commodity Feeds (Optional) ────────────────────────────────────────────────
TWELVEDATA_API_KEY=

# ── Model providers ───────────────────────────────────────────────────────
# Four accounts, several jobs. Chains are "provider:model, provider:model" in
# preference order; an entry whose key is unset is skipped.
OPENROUTER_API_KEY=
GEMINI_API_KEY=
ANTHROPIC_API_KEY=
# LLM_CHAIN_PRE_TRADE=groq:openai/gpt-oss-20b, openrouter:qwen/qwen3-32b
# LLM_CHAIN_POST_TRADE=anthropic:claude-sonnet-5, gemini:gemini-2.5-flash, groq:openai/gpt-oss-120b
# LLM_CHAIN_RESEARCH=anthropic:claude-opus-5, gemini:gemini-2.5-pro
# LLM_CHAIN_POSITION_REVIEW=gemini:gemini-2.5-flash, groq:openai/gpt-oss-120b, anthropic:claude-sonnet-5
# LLM_CHAIN_EVENT_PRECEDENT=groq:openai/gpt-oss-120b+search, openrouter:qwen/qwen3-32b:online

# A model on your own hardware. Empty = not set up; every chain naming
# ollama simply skips it.
# OLLAMA_BASE_URL=http://dhruv-ai:11434

# Hermes (the local sentiment bridge) runs on the machine with the model, not
# on the engine. Set these THERE, not here:
#   ENGINE_URL=http://<ec2-host>:8080
#   SENTIMENT_INGEST_TOKEN=<the same value the engine has>
#   OLLAMA_BASE_URL=http://localhost:11434
```

Every role also has an in-code default chain in `config/settings.py` (`llm_chain_pre_trade`, `llm_chain_post_trade`, `llm_chain_briefing`, `llm_chain_briefing_calm`, `llm_chain_attribution`, `llm_chain_history`, `llm_chain_history_search`, `llm_chain_news_scoring`, `llm_chain_position_review`, `llm_chain_research`, `llm_chain_event_precedent`) — the `.env` entries above only need to be set to override those defaults, most deployments can leave them commented out. Most non-secret settings (retention days, thresholds, feature flags) are **not** environment variables at all — they are `Settings` defaults overridable from the `/settings` UI, per §5.

---

## 5. Settings & Feature-Flag Map

Everything below comes from `config/settings.py` (the `Settings` pydantic model — code defaults) and `config/overrides.py` (the `FIELDS` tuple — what's actually exposed and editable on `/settings`, organized into the `GROUPS` the settings page itself uses: **data / signals / exits / protect / ai / v2 / storage / keys**). Database value overrides `.env` overrides code default. A future session should treat this table as the fast path to "is X on or off right now, and does flipping it change real trading behaviour" — read it before re-reading either 400+-line file from scratch.

**Legend**: 🔴 = `False`/off by default (shadow feature, dormant, or intentionally not yet trusted) · 🟢 = `True`/on by default (live behaviour).

### Market data (`data`)
| Flag | Default | What it does |
|---|---|---|
| `binance_only_mode` | 🔴 False | Master override: turns CoinDCX, CoinGecko, TwelveData and news feeds off, Binance becomes the sole source for price/candles/depth (recommended but not the shipped default). |
| `binance_klines_enabled` | 🟢 True | Real 1m candles/depth/price from Binance — nearly everything downstream depends on this. |
| `binance_ws_enabled` | 🔴 False | Optional tick-by-tick WebSocket on top of the candle poll. |
| `binance_oi_enabled` | 🟢 True | Binance futures open interest, polled every 2 minutes. |
| `binance_liquidation_stream_enabled` | 🟢 True | Free liquidation stream, subscribed alongside klines. **Purely observational** — nothing in the signal engine reads it yet; visible on `/api/pipeline`. |
| `binance_large_trade_stream_enabled` | 🟢 True | Same as above for large trades ≥ `large_trade_notional_usd` (default $50,000). Also observational only. |
| `coindcx_enabled` / `coingecko_enabled` / `twelvedata_enabled` | 🟢 True each | Secondary/fallback price sources; ignored entirely when `binance_only_mode` is on. |
| `news_sentiment_enabled` | 🟢 True | Scored headlines feed `CryptoState.sentiment_score` and news pauses. |

### Signals (`signals`)
| Flag | Default | What it does |
|---|---|---|
| `crypto_min_confidence` | 0.60 (float, not bool) | **The entry-generation floor.** Signals below this never fire, never get shown anywhere. See §6 for why this is easy to confuse with `paper_min_confidence`. |
| `high_conviction_only` | 🔴 False | Stricter scalp/conviction gates. Restart-only (`live=False`). |
| `crypto_htf_filter_enabled` | 🟢 True | Drop signals fighting the hourly (or 15m fallback) trend. |
| `daily_trend_filter_enabled` | 🟢 True | Longs only above daily SMA20, shorts only below it. |
| `crypto_volume_spike_enabled` | 🟢 True | Enables the volume-spike detector family. |
| `orderflow_enabled` | 🟢 True | Taker buy/sell CVD alignment + absorption checks. |
| `mirror_review_enabled` | 🔴 False | Turns on the whole mirror-review machinery (build/track/re-review the opposite-direction candidate). Restart-only. See §6. |
| `mirror_review_can_trade` | 🔴 False | Separate switch: even with mirror review on, a winning mirror candidate does not open a real paper trade unless this is also on. Restart-only. |
| `mirror_review_confidence_delta_threshold` | 0.05 | Re-review a tracked mirror candidate only once its local confidence has moved this much. |
| `mirror_review_min_elapsed_pct` | 0.33 | Re-review only once this fraction of the signal's own timeframe has elapsed. |
| `mirror_review_max_rounds` | 2 | Hard cap on AI re-reviews per mirror candidate after round 0. |
| `mirror_target_jitter_pct` | 0.20 | +/- jitter applied to the mirror candidate's target/stop vs. an exact mechanical reflection. |

### Exits / profit lock (`exits`)
| Flag | Default | What it does |
|---|---|---|
| `profit_lock_enabled` | 🟢 True | Once price moves `profit_lock_at_pct` (0.5%) in favor, the stop jumps to `profit_lock_to_pct` (0.35%) beyond entry, then trails `profit_lock_trail_pct` (0.15%) behind the best price. |
| `profit_lock_defers_to_trail_enabled` | 🔴 False | **Intentionally off pending the owner's decision to test it.** See §6 and §9 — fixes a real bug (profit-lock usually arms before the runner-trail's own activation distance, so the trail's "ride to 2R" branch almost never fires) but changes live trading behaviour, so it ships off by default. |
| `premium_fills_book` | 🟢 True | When an open trade's target pays ≥ `premium_roe_pct` (50%) on margin, the book stays full until it closes (same-tick signals are still taken best-first). |

### Protections (`protect`)
| Flag | Default | What it does |
|---|---|---|
| `protections_enabled` | 🟢 True | Master switch for the whole group. |
| `session_filter_enabled` | 🟢 True | Only trade `session_start_utc`–`session_end_utc` (07–17 UTC / ~12:30–22:30 IST by default). |
| `session_weekdays_only` | 🟢 True | No new trades on weekends. |
| `daily_loss_limit_pct` | 3.0% | Stop opening new trades for the day past this loss. |
| `max_same_direction_positions` | 2 | Correlated-exposure cap (most coins move with BTC). |
| `event_blackout_enabled` | 🟢 True | No new trades around FOMC/CPI/NFP/PCE/PPI, or for an hour after high-impact scored news. |
| `event_bias_mode` | 🟢 True | During an event window, trade *with* the AI's bull/bear read instead of pausing everything outright. |
| `calendar_caution_enabled` | 🟢 True | Softens confidence (never vetoes) around month/quarter-end rebalancing, options/futures expiry, thin weekend liquidity. Distinct from `event_blackout_enabled` (FOMC/CPI/NFP specifically). |
| `event_precedent_enabled` | 🔴 False | See §6 — historical-precedent event context, off by default, shadow-first like every AI-touching feature. |
| `event_precedent_lookahead_days` | 7 | How far ahead an event must be before it's researched. |
| `event_precedent_min_sample_size` | 2 | Minimum real historical occurrences with measured market data before a brief can touch anything live. |
| `event_precedent_extended_hold_enabled` | 🔴 False | Allows a longer paper-trade hold ceiling for a signal covered by a usable precedent brief. Has no effect unless `event_precedent_enabled` is also on. Never touches stop-loss or sizing. |
| `event_precedent_extended_hold_minutes` | 4320 (3 days) | The extended hold ceiling itself. |

### AI (`ai`)
| Flag | Default | What it does |
|---|---|---|
| `groq_signal_review_enabled` | 🟢 True | AI pre-trade review can veto a signal, never add confidence to it. |
| `position_review_enabled` | 🟢 True | AI review of open positions; a model-driven close needs two agreeing votes ~10 min apart (`position_review_confirm_close`, `position_review_confirm_gap_minutes`). |
| `groq_postmortem_enabled` | 🟢 True | Post-mortem review after a trade closes — builds the labelled dataset, no deadline. |
| `pre_trade_review_cache_ttl_seconds` | 180 | Reuse the last real review for a near-identical repeat signal (same symbol/direction/setup, price within ~0.15%). 0 disables the cache. |
| `event_monitor_enabled` | 🟢 True | Adaptive, jittered web-search event monitor with a daily request cap (`event_monitor_daily_cap`, 120). Restart-only. |
| `move_attribution_enabled` | 🟢 True | Hourly "why did this coin move" analysis, shown on `/moves`. |

Not on the settings page but real code defaults worth knowing: `groq_reject_penalty` (0.15), `groq_caution_min_penalty` (0.01), `groq_reasoning_effort` ("high", gpt-oss-only), `position_review_interval_seconds` (300 for losing positions), `position_review_interval_seconds_winning` (900 — winners are reviewed less often), `position_review_close_on_outage` (True — if no model answers, the tie-break is to close, not hold), `position_review_min_hold_minutes` (30 — no discretionary close in a position's first 30 minutes).

### v2 strategy (`v2`)
| Flag | Default | What it does |
|---|---|---|
| `v2_shadow_enabled` | 🟢 True | Records v2 setups live, resolved with the backtest's own fill rules, **never trades**. Results on `/v2`. |
| `v2_backtest_enabled` | 🟢 True | Daily full backtest at `v2_backtest_hour_utc` (02:00 UTC / ~07:30–07:47 IST), in a separate process; downloads missing Binance data itself (`v2_backtest_download`). |
| `v2_backtest_years` | 2.0 | Backtest window. `v2_backtest_timeout_minutes` = 90 is the budget for 7 coins × 2 years × 12 variants — see §9 for whether it's actually being met. |

### Storage (`storage`)
| Flag | Default | What it does |
|---|---|---|
| `db_retention_days` | 365 | See §2 — how long a row lives in the database before cold-storaging to JSON. Editable 90–3650 days on `/settings`. |
| `snapshot_retention_days` | 120 (not on the settings page, code-only) | How long a snapshot lives in the *live* table before moving to the archive table. |

### API keys (`keys`)
All `kind="secret"` on `/settings` — editable from the phone, never sent back to the page once saved (only "is one set" is shown): `groq_api_key`, `openrouter_api_key`, `gemini_api_key`, `anthropic_api_key`, `telegram_bot_token`, `twelvedata_api_key`, `coingecko_api_key`, `cryptopanic_auth_token`.

### Notable defaults not grouped above
- `rsi_divergence_enabled` = 🔴 **False** — "it never fired before (window too short); now that it can, it trades against trends. Off until it passes the null test." (direct code comment, `config/settings.py`).
- `crypto_auto_execute` = 🔴 False — no live-money execution exists/is enabled; this is a paper-trading engine.
- `use_finbert` = 🔴 False.
- `paper_scaled_leverage`, `paper_ladder_enabled`, `paper_ladder_tight` = 🔴 False each — paper-trading position-sizing variants not in use.
- `paper_trading_enabled`, `paper_scaled_sizing`, `paper_trailing_enabled`, `paper_alert_telegram` = 🟢 True.
- `paper_min_confidence` = 0.70 — see §6, this is the *second*, usually stricter, floor a fired signal must also clear before it becomes a paper trade.

---

## 6. Recent Major Features (changelog-style, most recent first)

**28 Sep 2026 — AI-call reduction pass.** Three changes, all aimed at cutting Groq/OpenRouter call volume without changing what gets reviewed: (1) a shared throttle helper used across the AI-calling code paths; (2) a daily call counter for visibility; (3) the round-0 mirror-review call (original signal + its opposite-direction mirror) is now combined into a single AI call instead of two; (4) a short, market-aware cache (`pre_trade_review_cache_ttl_seconds`, default 180s) reuses the last real pre-trade review answer for a near-duplicate repeat signal (same symbol/direction/setup, price within ~0.15% of what was actually reviewed) instead of calling the model again. Live by default except the cache TTL is small on purpose — this is a safety-relevant review, not a read-heavy endpoint.

**28 Sep 2026 — Profit-lock exit-reason fix + `profit_lock_defers_to_trail_enabled` (off).** Root cause: a flat `profit_lock_at_pct` usually arms *before* the runner-trail's own `activate_at_r` distance, so profit-lock tightens the stop first on almost every winner and the trail's "ride to 2R" branch essentially never gets to fire — most winners get walked down to a small locked gain instead of being allowed to run. The fix (`Position.apply_profit_lock`) is built and gated behind `profit_lock_defers_to_trail_enabled`: when on, profit lock only arms once price has passed `max(profit_lock_at_pct, 1.3x the trail's own activation distance)`, giving the trail first look. **Default is off** — behaviour is byte-for-byte identical to before the setting existed — pending the owner's decision to test it live. Do not flip it on without that go-ahead.

**28 Sep 2026 — The two-confidence-floor UX fix.** This caused real, recurring confusion across this project's development and a future session should not repeat it. **There are two separate, independently-configured confidence thresholds, not one:**
  - `crypto_min_confidence` (default 0.60) — the **signal-generation floor**, in `Settings`/`config/overrides.py` group `signals`. A candidate below this never fires, never appears anywhere, full stop.
  - `paper_min_confidence` (default 0.70) — a **separate, usually higher, floor specific to the paper trading config**, that a signal which *did* fire must also clear before it becomes a paper trade.
  Lowering `crypto_min_confidence` alone does **not** make more signals turn into trades if `paper_min_confidence` is still above it — a fired-but-not-traded signal is completely normal and expected, not a bug. The `/settings` page label for `crypto_min_confidence` now says explicitly: "A fired signal can still fail the separate, usually higher, paper-trading floor... that is a second, stricter check, not a duplicate of this one." Read that label rather than assuming there's one knob.

**~27-28 Sep 2026 — AI chart-review timeframe randomization.** The AI pre-trade chart review used to always send the same fixed higher timeframe (15m) alongside the signal's own timeframe. It now varies the higher-timeframe pool it draws from (15m and 30m so far), so the reviewer isn't anchored to one fixed context window every single time. Purely a review-quality change; does not affect which signals fire.

**27 Sep 2026 — Event-precedent (`event_precedent_enabled`, off by default).** *Not* a price-direction forecaster — the owner was told plainly that nothing in this detector stack can promise that, and agreed. For an upcoming calendar event (`event_precedent_lookahead_days` ahead), a web-search-capable model chain (`llm_chain_event_precedent`) looks up real historical precedent (same category, same US administration), the engine's own Binance Parquet lake measures what actually happened around those precedent dates, and a second AI call characterizes the pattern grounded in those real numbers. Advisory only: clamped through the same penalty-bucket confidence scale as `calendar_caution_enabled`, never raises confidence, never lowers the entry bar. A brief only counts once it has `event_precedent_min_sample_size` (2) real measured occurrences behind it. Can also, separately, extend a paper trade's hold-time ceiling (`event_precedent_extended_hold_enabled`, off by default, no effect on stop-loss or sizing). Genuinely rare — a handful of AI calls a year, cached per event occurrence.

**27 Sep 2026 — Mirror review (`mirror_review_enabled` / `mirror_review_can_trade`, both off by default).** For every fired signal, the engine also synthesizes the opposite-direction candidate and gives the AI reviewer a second look at it in parallel. A mirror candidate that isn't already strong enough to open on its round-0 review is tracked and re-reviewed as the market moves — up to `mirror_review_max_rounds` (2) more times, gated on both a confidence-delta threshold (0.05) and a minimum elapsed-fraction-of-timeframe (0.33) — until it wins, is rejected, or times out against its own timeframe. **Two independent switches**: `mirror_review_enabled` turns on the generation/tracking/review machinery itself (mirrors get built, reviewed, and show up in the Signals-page review trail); `mirror_review_can_trade` separately decides whether a candidate that wins its round is actually allowed to queue a real paper trade — with it off, a winning mirror candidate is logged and its trail shows "Not traded — mirror trading switch is off," with zero effect on real trades. This lets the feature run and be watched with the trading side fully isolated until the owner is ready.

**Earlier this session — Perf fixes (v2_backtest / snapshot_labels / db_cleanup).** `v2_backtest`'s hot loop was vectorized rather than looping row-by-row (aimed at getting the daily 7-coin × 2-year × 12-variant run under its 90-minute budget — see §9 for whether this is confirmed). `snapshot_labels` was changed to stop blocking on labelling work it didn't need to block on. `db_cleanup`/`archive_old_crypto_data` was changed to check for a `None` ceiling and skip the INSERT/DELETE round trip entirely on days when nothing is actually old enough to move — each round trip pays this database's ~1.4-1.5s network floor, and the job was measured at ~29s for what turned out to be mostly empty work on most days.

---

## 7. Testing & Development Discipline

This has been followed consistently across the project's whole history and should not be skipped:

1. **Every fix or feature gets a test verified to fail against pre-fix code.** Write the test, `git stash` to go back to the pre-fix state, confirm the test fails for the right reason, `git stash pop`, confirm it passes.
2. **The full test suite must pass before any push.** `rm -f crypto_engine.db && python -m pytest -q` — delete the SQLite file first if the schema changed, since a stale local DB file can mask or fake failures.
3. **`ruff check` is diffed against a baseline, never run cold.** Never introduce a new lint error; never "fix" an unrelated pre-existing one as a drive-by — that's noise in a diff meant to be reviewed for one change.
4. **`storage.models` / `storage.database` imports must happen inside test bodies, not at module top level.** Importing them at import time binds `DATABASE_URL` before a test can override it, which produces confusing binding-order bugs across the test session (different tests silently talking to different/wrong databases). This is a recurring footgun — respect it in any new test file.
5. **Commit messages explain WHY, not just what** — this repo's git log is written for a future reader (human or LLM) trying to understand a decision months later, not as a change summary. Match that style.

---

## 8. Git / Branch Discipline

- Development happens on a designated feature branch (currently `claude/debug-previous-session-4wNw3` at the time of this pass). Work is **never pushed straight to `main`** without an explicit review step from the owner — `main` auto-deploys to production EC2 on every push (§2), so an unreviewed push to `main` is a production deploy.
- **Never open a pull request unless explicitly asked to.**
- **This repository is public** (`dhruvparekhdp/tennis-bet` / crypto-signal-engine on GitHub). Never print, commit, or otherwise expose secrets — API keys, passwords, tokens — anywhere, including in commit messages, code comments, or this document. The `.env.example` correctly uses placeholder values only; keep it that way.

---

## 9. Known Open Items (honest, unresolved as of this pass)

- **v2_backtest under 90 minutes**: the hot-loop vectorization fix (§6) is meant to get the daily 7-coin × 2-year × 12-variant backtest under its `v2_backtest_timeout_minutes` (90) budget, but this is **only confirmable by watching a live scheduled run** (cron at `v2_backtest_hour_utc`, currently ~07:47 IST). Not yet independently confirmed as of this doc pass.
- **Null test result is stale.** `scripts/null_test.py` and the `/api/debug/null-test` endpoint check whether signals actually beat random entries. The last known result recorded in this codebase's own prior run was that they did **not** beat random entries. It has not been re-run against current data as part of this documentation pass — a future session should re-run it before trusting either a "yes" or "no" answer here.
- **`profit_lock_defers_to_trail_enabled` is built but intentionally off** (§6) pending the owner's explicit decision to test it in live paper trading. Do not flip it on unilaterally.
- **`rsi_divergence_enabled` is off pending its own null test** — per the code comment in `config/settings.py`, it now fires (previously it structurally couldn't, window too short) but "trades against trends... off until it passes the null test." Unconfirmed either way.
- **EC2 instance type / IP** in §2 are carried over from the previous version of this doc and were **not** independently re-verified this pass (no server access in this task) — treat as plausible, not confirmed.

---

## 10. Web Dashboard & API Endpoints

Verified against the actual route registrations in `scheduler/health.py`, `scheduler/settings_page.py`, `scheduler/v2_pages.py`, and `scheduler/api_docs.py` (`app.router.add_get/add_post`) as of this pass.

### Pages
- **`/`** — real-time multi-coin dashboard, conviction scores, open paper positions, live charts.
- **`/data`** — historical trade explorer and market data review.
- **`/settings`** — admin control panel (password-protected via `/api/settings/auth/*`). Feature-flag groups per §5, API keys, collector toggles. `/settings/classic` is an older/alternate version of the same page, still routed.
- **`/predict`** — multi-horizon forecast engine and predictive analytics.
- **`/audit`** — performance audit, method catalogue, win-rate attribution.
- **`/moves`** — the hourly move-attribution feed ("why did this coin move").
- **`/v2`** — v2 backtest and shadow results.
- **`/journal`** — v2 trade journal.
- **`/api-docs`** — human-readable listing of the API surface.

### API — core data
`/api/tables`, `/api/status`, `/api/debug` (general collector/engine diagnostics — note: the sidebar HTML links to `/api/debug/collectors`, but that path is **not actually registered as a route**; it 404s. The real diagnostics endpoint is `/api/debug`. This looks like a stale/typo'd link in `scheduler/health.py` and should be fixed or the link updated in a future pass — flagging it here rather than silently "fixing" it, since this task is docs-only), `/api/crypto/coins`, `/api/crypto/signals`, `/api/crypto/forecasts`, `/api/crypto/watchlist` (+ `/add`, `/remove`), `/api/binance/symbols`, `/api/commodities`, `/api/paper`, `/api/paper/events`, `/api/paper/config` (get/post), `/api/strategy/config` (get/post), `/api/app-settings` (get/post), `/api/settings` (collector states), `/api/settings/toggle`, `/api/settings/auth/login`, `/api/settings/auth/status`, `/api/settings/auth/logout`, `/api/auth/verify`.

### API — signals, research, events
`/api/research`, `/api/signals/history`, `/api/signals/accuracy`, `/api/reviews`, `/api/predict`, `/api/audit`, `/api/audit/methods`, `/api/audit/reviewer`, `/api/events`, `/api/moves`, `/api/moves/run` (POST), `/api/sentiment/ingest` (POST), `/api/sentiment/recent`, `/api/pipeline`.

### API — v2 / journal
`/api/v2/backtest`, `/api/v2/backtest/run` (POST), `/api/v2/shadow`, `/api/journal` (GET/POST).

### API — debug family (newer)
`/api/debug/coindcx`, `/api/debug/signals`, `/api/debug/volume`, `/api/debug/binance`, `/api/debug/perf`, `/api/debug/null-test`. (`/api/debug/collectors` is referenced in the UI but not registered — see above.)

### Health
`/health` — polled by the GitHub Actions deploy step; returns 200 once the app is actually serving, per §2.

---

## 11. Live Market Simulator & Strategy Testing Architecture

Added 2026-10-01 to enable high-fidelity historical replay and compounding cycle verification before deploying live Binance capital.

### A. Core Engine Architecture
- **Stepped & Fast Replay (`analysis/simulator/replay_engine.py`, `analysis/simulator/stepped_replay.py`)**:
  - Replays historical multi-coin 1-minute OHLCV klines with tick-by-tick simulation.
  - Supports configurable time horizons (1 Day, 3 Days, 1 Week, 2 Weeks, 1 Month, 3 Months, 1 Year, 3 Years).
  - Pacing budget clock: allows step-by-step market hour pacing (e.g. 1 market hour analyzed every 10-60s) or ultra-fast non-stepped execution.
- **Offline / Zero AI API Calls Mode (`--ai-provider none`)**:
  - The simulator defaults to `none` (`0` external API calls dispatched to Gemini, Groq, OpenRouter, or Hugging Face).
  - Eliminates external API quota consumption, rate limits, and latency during backtests.
  - Generates instant quantitative analytical justifications for trades locally via `offline_quant_engine`.
  - Supports optional hybrid AI fallback (`auto`, `gemini`, `groq`, `openrouter`, `hf`) when qualitative reasoning is explicitly requested.

### B. Modular Strategy Dispatcher (`analysis/simulator/strategy_dispatcher.py`)
Provides an interactive dropdown on the UI to test individual quantitative strategies or the full ensemble:
1. `all`: Multi-strategy ensemble (waterfall evaluation across confluence, squeeze, volume, patterns).
2. `confluence`: 5-Family Confluence Gate (requires multi-family agreement across trend, mom, vol & structure).
3. `bollinger_squeeze`: Volatility contraction followed by directional expansion breakout.
4. `volume_spike`: Volume anomaly spike relative to recent moving baseline.
5. `rsi_divergence`: Price vs momentum discrepancies at cycle extremes (regular & hidden).
6. `trend_pullback`: 15m/5m dynamic EMA pullback in macro trend.
7. `range_breakout`: Momentum range breakout beyond ATR buffer.
8. `sweep_reclaim`: Key liquidity sweep of swing highs/lows with close reclaim.
9. `breakout_retest`: Prior resistance/support breakout with confirmatory retest hold.

### C. Compounding Cycle Challenge ($25 ➔ $100)
- **Rules**:
  - Starting Capital: $25.00 USDT.
  - Target Goal: $100.00 USDT (4x capital growth).
  - Margin per Trade: Configurable (default 25% of current equity, or 100% full-margin compounding).
  - Leverage: 10x - 25x isolated futures.
  - Realistic Friction: Standard maker/taker exchange fee deduction (0.05% taker) + dynamic slippage applied on every entry and exit.
  - Ruin / Bust Threshold: If equity drops to $0.00, cycle ends as `BUSTED` and resets.
  - Success Threshold: If equity reaches or exceeds $100.00 USDT, cycle ends as `TARGET_REACHED` and resets.

### D. R-Multiple & ROE Mathematics (Base $100 Margin Model)
- **What is 1R?** 
  `1R` represents 1 base unit of structural market risk, defined as `max(1.8 * ATR, price * 0.012)` (minimum 1.2% price move).
- **Leverage & ROE Scaling**:
  $$\text{ROE (\%)} = \text{Price Move (\%)} \times \text{Leverage}$$
  $$\text{1R ROE at 10x} = 1.2\% \times 10 = 12.0\% \quad (\text{\$12.00 on \$100 margin})$$
  $$\text{1R ROE at 25x} = 1.2\% \times 25 = 30.0\% \quad (\text{\$30.00 on \$100 margin})$$
- **Target Multiple ($T_R$):**
  At $5.5R$ with $25x$ leverage:
  $$\text{Price Target} = +5.5 \times 1.2\% = +6.6\% \quad \Longrightarrow \quad \text{ROE} = +6.6\% \times 25 = +165.0\% \quad (\text{+\$165.00 on \$100 margin})$$
- **Stop-Loss Multiple ($S_R$):**
  At $3.0R$ with $25x$ leverage:
  $$\text{Price Stop} = -3.0 \times 1.2\% = -3.6\% \quad \Longrightarrow \quad \text{ROE} = -3.6\% \times 25 = -90.0\% \quad (\text{-\$90.00 on \$100 margin})$$
- **Risk-to-Reward Ratio (R:R):**
  $$R:R = \frac{T_R}{S_R} = \frac{5.5}{3.0} = 1.83 : 1$$

### E. Mobile-Optimized Zero-Lag Architecture
- **Problem**: Previous simulator iterations pushed 100,000+ lines of raw JSON (3MB - 10MB) containing thousands of trade transactions in `status.json`. Mobile browsers crashed or froze while parsing and rendering these massive objects every 2.5s.
- **Solution**:
  1. Full transaction statements are written to `data/simulator/reports/cycle_statements.json`.
  2. `status.json` only retains lightweight aggregate metrics (< 25KB payload).
  3. Client-side **View Tab Selector** separates metrics into distinct screens:
     - 📊 Executive Metrics (High-level compounding scorecard)
     - 🏆 Compounding Cycles (Interactive dropdown picker + on-demand paginated statement ledger via `/api/simulator/cycle-ledger`)
     - 📜 Trade Stream (Client-side paginated 10 trades per page)
     - 📡 AI Events Log (Filtered API call stream)
     - ⚙️ Strategy & Settings (Interactive strategy selection, R math live breakdown card, and parameter inputs)

