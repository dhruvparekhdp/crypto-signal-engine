# AUD-2 — Baseline audit of crypto-signal-engine

Read-only audit of `main` at `2429e2c` (6 Nov 2026, Asia/Calcutta). No production host, database, or `.env` was opened. Conclusions are from the source tree. A deployed `app_settings` row can override many of the defaults below without a code change.

This repository is the crypto engine itself, not a monorepo of dhruv-ai / NSE-TRADE / ocr. `india_equity_engine/` and `ios-port/` sit beside the engine and are not started by `main.py`.

## How this was read

Files: `README.md`, `HANDOFF.md`, `SYSTEM_CONTEXT.md`, `docs/INTEGRATION_PLAN.md`, `main.py`, `config/settings.py`, `config/overrides.py`, `scheduler/runner.py`, `scheduler/health.py`, `scheduler/security.py`, `execution/`, `analysis/swing_book.py`, `analysis/signal_audit.py`, `storage/models.py`, `storage/database.py`, `collectors/llm_client.py`. Order placement was traced by searching every `POST`/`DELETE` to a broker and every import of `execution`.

`docs/INTEGRATION_PLAN.md` (7 Oct 2026, against `2ccdd77`) is a historical findings list. HANDOFF says its phase-1 items were deployed. This audit does not re-score those IDs. Where the current file still matters, the line below is from `2429e2c`.

## Assumptions

- Code defaults are the baseline. Production may have flipped `delta_india_mode` / `delta_india_live_orders` in Postgres. That was not checked.
- "The main engine" means `python main.py` → `AppRunner.start()` → APScheduler jobs. Operator scripts under `scripts/` are a separate process.
- Env names come from the `Settings` model (pydantic-settings uppercases each field), plus `os.environ` / `os.getenv` call sites. No values were read or copied.
- `docs/audit/` did not exist. `audit/FINDINGS.md` is the older lab ledger, not this baseline.

## 1. Architecture

One asyncio process.

1. `main.py` binds the aiohttp server first (`scheduler/health.py` `start_health_server`, `0.0.0.0:$PORT`), then `init_db()`, then `AppRunner.start()`.
2. Collectors fill an in-memory `CryptoState` per symbol (`analysis/crypto_state_store.py`).
3. Two signal paths run together:
   - **Swing book** (`analysis/swing_book.py`, job `swing_scan`): the only family that opens paper trades by default (`paper_open_families="swing"`).
   - **15-minute detectors** (`analysis/crypto_engine.py`, `analysis/crypto_signals.py`, job `crypto_analysis`): five analyzers, logged, shadow unless `paper_open_families` also names `intraday`.
4. Paper fills live in Postgres (`analysis/paper_trading.py`, `analysis/paper_cycle.py`). They do not call a broker.
5. After a paper swing open, `AppRunner._mirror_delta_india` may copy the trade onto Delta Exchange India (`execution/delta_book.py`). That is the only broker-order path the scheduler calls.
6. The Binance USD-M client (`execution/binance_futures.py`, `execution/live_book.py`) is built and unit-tested against a fake exchange. Nothing in `main.py` or `scheduler/runner.py` constructs it.

`README.md` still describes a confluence gate in front of five detectors and says `execution/` is "built, not wired". The second sentence is true for Binance. It is not true for Delta: the scheduler wires `DeltaBook`. `SYSTEM_CONTEXT.md` section 0 (6 Oct 2026) is closer, and it is already stale on a few defaults (CoinDCX is `False` in code now; see `config/settings.py` line 630). Section 0 wins over the rest of that file when they disagree; this audit wins over section 0 where the code has moved.

## 2. Data sources

Reads only. None of these place orders.

| Source | Role | Where |
|---|---|---|
| Binance spot klines | Live 1m candles for the intraday state | `collectors/binance_klines.py` (`data-api.binance.vision`, `api.binance.com`, `api1`–`api3`) |
| Binance USD-M public REST | Swing bars, funding, exchange info, open interest | `analysis/swing_book.py` `FAPI_KLINES`, `collectors/v2_feed.py`, `collectors/binance_futures_oi.py`, `collectors/binance_symbols.py` — host `fapi.binance.com`, GET only |
| Binance public data lake | Historical zip klines | `collectors/binance_lake.py`, `collectors/binance_history.py`, `collectors/historical_klines.py` (`data.binance.vision`) |
| Binance WebSocket | Optional trades / liquidations. `binance_ws_enabled` defaults off | `collectors/binance_ws.py` |
| Delta Exchange India public + signed reads | Candles, marks, products, wallet, order book | `collectors/delta_market.py`, `execution/delta_india.py`. Signed reads use the same client that can also write orders |
| CoinDCX, CoinGecko | Secondary prices. CoinDCX collector default is now off | `collectors/coindcx.py`, `collectors/coingecko.py` |
| Twelve Data | Gold, silver, crude | settings `twelvedata_symbols` |
| CryptoPanic, Fear & Greed | News / sentiment | `collectors/cryptopanic.py`, `collectors/sentiment_feeds.py` |
| RSS and macro | Fed, ECB, CoinDesk, BBC, GDELT, Polymarket, Kalshi, DefiLlama, FRED, FMP | `collectors/hermes.py`, `collectors/news/sources.py` |

`binance_only_mode` (default `False`) turns the non-Binance price feeds off. `delta_only_mode` / `delta_india_data_enabled` (default on) pull Delta candles.

## 3. Scheduler

APScheduler, in-process, registered in `scheduler/runner.py` `setup_jobs` (line 4540). Jobs that can lead to an order are marked.

| Job id | Cadence | What it does |
|---|---|---|
| `db_cleanup` | 6 h | Archive snapshots, cold-store old rows |
| `snapshot_labels` | 15 min | Forward labels on snapshots |
| `research_pass` | 7 d | Weekly research write-up |
| `heartbeat` | 10 min | Liveness |
| `paper_trading_tick` | `paper_tick_interval_seconds` (30 s) | Mark and exit paper positions. Swing opens call the Delta mirror |
| `resolve_signal_outcomes` | 15 min | Score fired signals |
| `sentiment_refresh` | if feeds on and not binance-only | Fear & Greed |
| `coindcx_poll` | `coindcx_poll_interval_seconds` | CoinDCX prices (collector default off) |
| `candle_refresh` | 1 h | Candle maintenance |
| `swing_outcomes` | 30 min | Replay swing signals to stop / target / 7 days |
| `swing_scan` | `swing_scan_seconds` | Closed 4h/8h strategies → queue paper entries |
| `binance_klines` | `binance_klines_seconds` | Spot candles |
| `coingecko_poll` | `coingecko_poll_interval_seconds` | CoinGecko |
| `crypto_analysis` | 60 s | Five intraday detectors |
| `mirror_review` | 60 s | Opposite-direction AI review; no-op while the feature is off |
| `move_attribution` | `move_attribution_minutes` | "Why it moved" |
| `event_precedent` | 4 h, only if enabled | Historical event briefs |
| `market_briefing` or event monitor | briefing minutes, or self-scheduled | World-event AI |
| `daily_trend` | 60 min | Daily SMA filter |
| `delta_reconcile` | 5 min | **Can amend or close Delta positions** if that book is live or testnet |
| `parity_check` | 00:40 UTC | Swing signal vs replay |
| `bracket_paper` | 2 min | Virtual bracket book, no orders |
| `impulse` | 1 min | Move detector, paper + alerts |
| `news_engine` | 5 min | News collectors |
| `candidate_book` | 2 min | Non-live strategies, paper only |
| `vol_paper` | 5 min | Options straddles, paper only |
| `weekly_forward_report` | Mon 03:30 UTC | Forward report |
| `v2_backtest` | cron `v2_backtest_hour_utc:17` | Off by default (`v2_backtest_enabled=False`) |
| `v2_shadow` | `v2_shadow_minutes` | v2 signals, shadow |
| `event_evaluation` | 30 min | Event shadow trades |
| `news_sentiment` | 5 min | Headline scores |
| `hermes_rss` | 5 min | RSS ingest |
| `crypto_news` | CryptoPanic interval, unless binance-only | News |
| `crypto_snapshot` | `crypto_snapshot_interval_seconds` | Feature rows |
| `binance_oi_poll` | 120 s, if OI on and not delta-only | Open interest |

## 4. Strategies

**Live paper book** (`config/settings.py` `swing_strategies`, same string as `analysis/swing_book.py` `DEFAULT_SPECS`):

- 4h: `vol_breakout` z=3, `keltner_break` k=2.5, `donchian` n=100, `ichimoku`
- 8h: `keltner_break` k=2, `vol_breakout` z=3, `donchian` n=100, `ichimoku`

Exits in `swing_book.py`: stop 3×ATR(14), floor 0.3% and cap 8% of price, target 3R, hold 7 days. Signals are computed with `analysis/lab/strategies.py` `REGISTRY`, the same functions the lab backtest uses. `swing_scan` checks `spec_problems` first and, with `swing_config_fail_closed` (default on), refuses to trade a bad spec (`scheduler/runner.py` 1361–1374).

BCH and LTC are excluded (`swing_exclude_symbols`). A BTC 30-day volatility filter is on (`swing_regime_filter`, `analysis/regime_gate.py`). `swing_regime_fail_closed` defaults on.

**Intraday, shadow by default.** `ConfluenceAnalyzer`, `RSIDivergenceAnalyzer` (flag default off), `VolumeSpikeAnalyzer`, `BollingerSqueezeAnalyzer`, `SentimentShiftAnalyzer` in `analysis/crypto_signals.py`. `paper_open_families` default is `"swing"` only (`scheduler/runner.py` `_family_may_trade`, line 252).

**Research-only books**, no broker calls: bracket (`analysis/bracket_book.py`), candidates (`analysis/candidate_book.py`), vol straddles (`analysis/vol_book.py`), v2 shadow (`analysis/v2_shadow.py`).

The lab registry has about 40 more strategies (EMA, MACD, mean-reversion, flow, a random control). They are not the live book unless named in `swing_strategies` or `swing_candidate_strategies`.

## 5. Backtests

| Path | What it replays | Started by the engine? |
|---|---|---|
| `analysis/lab/` | 1-minute Binance lake, costs, null test, walk-forward. Source of the swing book | No. `scripts/run_lab.py` and related scripts |
| `analysis/backtest.py` | Older path through `CryptoState` and the paper fill | Script / tests |
| `analysis/v2_backtest.py` | v2 candidates, stop-first fills, deflated Sharpe | Daily job, **disabled** (`v2_backtest_enabled=False`, line 467) |
| `analysis/simulator/` | Multi-year replay, including a stepped mode. HTTP can launch it | Only if something POSTs `/api/simulator/start` |
| `scripts/live_book_trades.py` + `GET /api/sim/live-book` | What the live book *would* have done. Reads parquet. Does not trade | HTTP GET |

Look-ahead on the swing path: `bars_from_klines` drops the forming bar and `evaluate` reads the last closed bar. `tests/test_swing_book.py` checks live vs lab parity. `docs/INTEGRATION_PLAN.md` section 0 still says the live swing parameters were chosen on the same history they are scored on. That research caveat is not re-litigated here.

## 6. Signal auditing

`analysis/signal_audit.py` `audit()` scores resolved `crypto_signal_log` rows (win rate, expectancy, slices by symbol, setup, horizon, direction, confidence). `method_catalogue()` introspects the analyzer source so the `/audit` page does not depend on a hand-written list.

HTTP: `GET /audit`, `GET /api/audit`, `GET /api/audit/methods`, `GET /api/audit/reviewer`, `GET /api/signals/accuracy`, `GET /api/signals/history`. Outcome rows are filled by `resolve_signal_outcomes` and, for swing, `swing_outcomes`.

`audit/trial_ledger.jsonl` and `audit/FINDINGS.md` are lab notes, not the signal table.

## 7. Notifications

`notifications/telegram_notifier.py`. The bot is built from the current token (env or `/keys`), not at import time.

- `send_text` → `telegram_chat_id`. Paper swing opens, reconcile alerts, config errors. Off, with an hourly log line, when token or chat id is missing.
- `send_channel` → `telegram_channel_id`. Live Delta opens and closes via `notifications/channel_feed.py`. Empty channel id means off. The channel text is not supposed to include price or size.
- `shadow_signal_alerts` defaults off, so shadow-family signals do not page Telegram.
- GitHub Actions (`.github/workflows/deploy.yml`) sends its own deploy messages using Actions secrets. That workflow is not the engine process. Pushing this audit branch does not deploy; deploy runs on push to `main`.

No email, no ads, no public social post in the engine.

## 8. AI providers

One client: `collectors/llm_client.py` `ask_json`. Callers pass a role. The role's chain is `llm_chain_<role>` on `Settings`.

Providers in `PROVIDERS` (around line 183):

| Name | Endpoint shape | Key field |
|---|---|---|
| `groq` | OpenAI `/chat/completions` | `groq_api_key` |
| `openrouter` | OpenAI | `openrouter_api_key` |
| `gemini` | Google OpenAI-compatible | `gemini_api_key` |
| `anthropic` | Messages API | `anthropic_api_key` |
| `ollama` | Local, skipped when `ollama_base_url` is empty | none |
| `hf` | Hugging Face router or `hf_base_url` | `hf_api_token` / `hf_space_api_key` |

`collectors/llm_budget.py` enforces a per-model free-tier budget (`GET /api/llm/budget`). Successful calls can be logged under `data/llm_calls/` when `llm_call_log` is on.

Pre-trade review is `collectors/macro_sentinel.py`, gated by `groq_signal_review_enabled` and by `_family_may_trade` (shadow signals do not spend the quota). Headlines can be scored locally (`analysis/headline_triage.py`, FinBERT rules) before a model call.

`huggingface_space/app.py` is a separate Gradio process (FinBERT, embeddings, optional llama.cpp). It is not imported by `main.py`. The engine calls it over HTTP when `hf_base_url` is set.

AI never places an order. A REJECT verdict zeroes confidence; it does not call `DeltaBook`.

## 9. Database

`storage/database.py`. SQLAlchemy 2 async. Default URL `sqlite+aiosqlite:///./crypto_engine.db`. Production is PostgreSQL (`postgresql://` rewritten to `postgresql+asyncpg://`). `init_db` runs `create_all` plus column adds. There is no Alembic revision directory in use for day-to-day schema changes (Alembic is a dependency; the running path is `create_all` + `_migrate_columns`).

Precedence for UI settings: database `app_settings` > environment > code default (`config/overrides.py` lines 1–8). `AppRunner.start` calls `load_settings_overrides()` before jobs are scheduled.

Tables in `storage/models.py`: `market_candles`, `crypto_snapshots`, `crypto_snapshots_archive`, `commodity_snapshots`, `commodity_snapshots_archive`, `crypto_signal_log`, `crypto_watchlist`, `paper_cycles`, `paper_trading_config`, `admin_auth`, `strategy_config`, `signal_reviews`, `market_briefings`, `market_events`, `event_shadow_trades`, `move_attributions`, `event_precedents`, `history_events`, `v2_shadow_signals`, `trade_journal`, `app_settings`, `trade_events`, `paper_positions`, `paper_trades`, `news_sentiment`, `strategy_registry`, `paper_withdrawals`.

News engine (`storage/news_models.py`): `news_collector_state`, `news_documents`, `news_events`, `news_event_stories`, `news_calendar_rows`, `news_odds_snapshots`, `news_liquidation_minutes`, `news_cross_asset_ticks`, `news_defi_snapshots`, `news_sentiment_indices`, `news_event_reactions`, `news_impulse_links`, `news_institutions`, `news_gdelt_batches`.

Retention: snapshots move to archive after `snapshot_retention_days` (120). Older history is gzipped JSONL under `cold_storage_dir` after `db_retention_days` (365) by `storage/cold_storage.py`. Open positions, settings, and auth are not archived that way.

Secrets in `app_settings` are sealed with `config/secret_box.py` when `SECRETS_MASTER_KEY` is set. Without it they are stored in plaintext. That key is read from the environment only; it is not a `Settings` field.

`admin_auth` holds a PBKDF2 password hash and a session token. `ADMIN_PASSWORD` (environment, not a `Settings` field) is re-applied on every startup (`storage/database.py` 89–104).

TLS: `sslmode=require` (the usual URL) builds an SSL context with `verify_mode=CERT_NONE` and `check_hostname=False` (`storage/database.py` 38–48). `verify-full` plus `DATABASE_SSL_CA` verifies the server. The code prints a warning for the unverified mode.

## 10. Where an order can be placed

There is no withdrawal or transfer call. `execution/capital_rules.py` only *counts* deposits and withdrawals it reads back from Delta. Binance USD-M methods do not call the wallet-withdraw API.

### 10.1 Binance USD-M — client exists, engine cannot reach it

`execution/binance_futures.py` `BinanceFutures` (default host `https://fapi.binance.com`, testnet host `https://testnet.binancefuture.com`):

| Method | Line | HTTP |
|---|---|---|
| `prepare` | 143–149 | `POST /fapi/v1/marginType`, `POST /fapi/v1/leverage` |
| `market` | 151–156 | `POST /fapi/v1/order` (MARKET) |
| `protect` | 158–172 | `POST /fapi/v1/order`, fallback `POST /fapi/v1/algoOrder` |
| `cancel_all` | 174–179 | `DELETE /fapi/v1/allOpenOrders`, `DELETE /fapi/v1/algoOpenOrders` |

`execution/live_book.py` `LiveBook.open_from_paper` (line 176) calls `prepare` then `market`, then `protect`. `close` (line 202) calls `market` with `reduce_only` and `cancel_all`.

Callers of `LiveBook` or `BinanceFutures`: `execution/live_book.py` itself and `tests/test_live_book.py`. The test uses an in-memory fake exchange for orders. The signature test constructs a client and does not send HTTP. `scheduler/runner.py` imports `BinanceFuturesOICollector` (public open-interest polls only). It never imports `execution.binance_futures` or `execution.live_book`.

`live_trading_mode` defaults to `"off"` (`config/settings.py` 311). The only runtime read is `scheduler/portal.py` 195, which puts the string in `GET /api/portal/meta`. Setting the variable to `live` changes that JSON field and nothing else. The flag is not in `config/overrides.py` `FIELDS`, so `/settings` cannot turn Binance trading on either.

`crypto_auto_execute` (`config/settings.py` 793) defaults `False` and has no other reference. It does not place orders.

### 10.2 Delta Exchange India — the engine can reach it

`execution/delta_india.py`:

| Method | Line | HTTP |
|---|---|---|
| `place_market` | 279 | `POST /v2/orders` |
| `place_stop_market` | 291 | `POST /v2/orders` |
| `place_take_profit_market` | 304 | `POST /v2/orders` |
| `cancel_all` | 317 | `DELETE /v2/orders/all` |
| `edit_stop` | 332 | `PUT /v2/orders` |
| `cancel_order` | 338 | `DELETE /v2/orders` |
| `set_leverage` | 357 | `POST /v2/products/{id}/orders/leverage` |

`execution/delta_book.py` calls those from `on_paper_open` (market at line 453, stop at 460, take-profit at 476), and from the ladder / reconcile / close paths (lines 581, 625, 633, 675, 685, 689, 741, 760, 790, 825).

`DeltaBook.live` (line 152) is `mode == "live" and live_orders`. If that is false, `on_paper_open` logs a shadow row and returns without `place_market` (lines 394–402).

The scheduler builds that object in `_delta_call_one` (`scheduler/runner.py` 1731–1780), called from `_mirror_delta_india` (2482), which paper swing open calls at 1696–1701. `delta_reconcile` (1788) calls `reconcile` on the same object, which can cancel and replace stops when the book is live.

Gates that must all be true before a **real-money** order:

1. `delta_india_mode == "live"` (`_delta_targets`, line 1715). Default `"off"` (settings line 326).
2. `delta_india_live_orders is True` (passed at runner line 1758). Default `False` (settings line 330).
3. API key and secret are set (runner 1744). Default unset.
4. Symbol is in `delta_live_coins` (runner 1735–1739).
5. `delta_india_kill_switch` is false (default false means the switch is **not** blocking; settings line 359). The name is "on = block new orders".
6. `DeltaBook.on_paper_open` then requires trading status `operational`, the entry-guard book check, a successful `set_leverage`, loss-guard / capital rules, and a stop that the exchange accepts. A rejected stop flattens the position (delta_book.py 457–469).

Shadow mode (`delta_india_mode="shadow"`) sizes and appends `data/delta_shadow.jsonl`. It does not call `place_market`.

### 10.3 Delta testnet — also reachable from the engine, fake money

If `delta_india_mode == "testnet"`, or `delta_testnet_mirror` is true while mode is not `"off"`, `_delta_targets` adds `"testnet"` (runner 1717–1718). `_delta_call_one` then forces `mode="live"` and `live_orders=True` against `delta_india_testnet_url` (lines 1753–1758). The kill switch is not applied to the demo. Defaults: mode `off`, `delta_testnet_mirror=False` (settings line 347), so this does not run until a setting changes. The URL default contains `testnet`.

### 10.4 Scripts — not started by `main.py`

These call the same Delta write methods when a person runs them:

| Script | What it writes | Guard in the file |
|---|---|---|
| `scripts/delta_live_rehearsal.py` | Market close, cancel, leverage on the **testnet** URL | Refuses a base URL that does not contain `testnet` (line 29) |
| `scripts/delta_stop_fill_test.py` | Same class of testnet writes | Testnet URL |
| `scripts/delta_testnet_flow.py` | `DeltaBook` with `live_orders=True` on the testnet URL | Testnet URL |
| `scripts/delta_testnet_adopt.py` | Same, adopts an existing testnet position | Asserts `testnet` in the URL |

`scripts/delta_angle_test.py` prints mode flags and uses the client for checks. It is not in the `place_market` call graph.

No HTTP route calls `BinanceFutures.market` or `DeltaIndia.place_market` directly. The routes that affect orders are below.

### 10.5 HTTP routes that can change whether an order goes out

- `POST /api/app-settings` (admin session or bearer). Can set `delta_india_mode` and `delta_india_live_orders` because both are in `FIELDS`. Fail-closed when logged out (`scheduler/settings_page.py` 22–32).
- `POST /api/delta/shadow-now` (`scheduler/delta_api.py` 39–83). **No auth.** It sets the process-global `settings.delta_india_mode` and `delta_india_live_orders` to shadow/false, mirrors open paper swings, then restores the saved values in `finally`. The comment says this cannot place a live order. That holds for a single request. It does not hold if a second request (or the paper job) interleaves on the same event loop: request B can save the temporary shadow state, request A can restore `live`/`True` before B's `await` finishes, and B's mirror then runs with live orders on. B's `finally` can then leave the process in shadow until restart. See finding AUD2-2.
- `POST /api/settings/toggle` can persist collector switches. It does not place an order. Auth is fail-open when `API_AUTH_TOKEN` is empty (finding AUD2-4).

`GET /api/delta/live` and `GET /api/parity` are admin-only reads.

## 11. Is live trading disabled, and how?

**Binance USD-M live trading is disabled because it is not connected.** The default `live_trading_mode=off` is not what stops it. There is no call from the engine into `LiveBook`. Turning the flag on does not create a client.

**Delta real-money trading is disabled by two default flags, not by missing code.** `delta_india_mode` is `"off"` and `delta_india_live_orders` is `False`. Either one is enough to keep `DeltaBook.live` false on the real-money target. Both are database-overridable. This audit did not read production `app_settings`, so a deployed box can be live while this tree's defaults are off. `GET /api/portal/meta` exposes `delta_mode`, `delta_live`, and `delta_kill_switch` for that check.

**Delta testnet mirroring is off by default** (`delta_testnet_mirror=False`, mode not `testnet`).

**Paper trading is the path `main.py` actually runs.** `paper_trading_enabled` defaults `True`. That opens simulated positions only.

The kill switch defaults to off, which means it is **not** an extra lock. Safety at rest is the mode and the second gate.

## 12. HTTP endpoints

One server: aiohttp, `scheduler/health.py` `make_app` (line 7869), bound to `0.0.0.0` (`start_health_server`, line 8319). Middleware is timing, rate limit, security headers, gzip. There is no global auth middleware. Each handler opts in.

`scheduler/api_docs.py` `ENDPOINTS` is a phone catalogue. It matches the router, with duplicate chart rows. The lists below are from `app.router.add_*` calls.

Auth words used below:

- **open** — no session and no bearer.
- **admin** — `admin_session` cookie or bearer `API_AUTH_TOKEN`. Empty token: settings/keys/v2 return 401; watchlist and collector toggle do not (AUD2-4).
- **ingest** — header `X-Ingest-Token` must match `SENTIMENT_INGEST_TOKEN`, else 503 when unset.

### Pages (GET, open unless noted)

`/`, `/command`, `/book`, `/evidence`, `/system`, `/{hub}/{page}`, `/data` (redirect), `/classic`, `/settings`, `/settings/classic`, `/keys`, `/predict`, `/moves`, `/audit`, `/pipeline`, `/chart`, `/world`, `/v2`, `/journal`, `/api-docs`, `/portal/static/{name}`.

### JSON GET

Open: `/health`, `/api/status`, `/api/debug`, `/api/tables`, `/api/settings`, `/api/settings/auth/status`, `/api/paper/config`, `/api/strategy/config`, `/api/crypto/coins`, `/api/crypto/signals`, `/api/simulator/status`, `/api/simulator/config`, `/api/simulator/cycles`, `/api/simulator/cycle-ledger`, `/api/simulator/ai-events`, `/api/crypto/forecasts`, `/api/paper`, `/api/swing`, `/api/llm/budget`, `/api/paper/events`, `/api/debug/coindcx`, `/api/research`, `/api/sentiment/recent`, `/api/signals/history`, `/api/debug/signals`, `/api/predict`, `/api/signals/accuracy`, `/api/audit`, `/api/audit/methods`, `/api/audit/reviewer`, `/api/reviews`, `/api/debug/volume`, `/api/crypto/watchlist`, `/api/binance/symbols`, `/api/symbols/search`, `/api/events`, `/api/moves`, `/api/commodities`, `/api/debug/binance`, `/api/debug/null-test`, `/api/pipeline/progress`, `/api/chart/klines`, `/api/chart/overlays`, `/api/chart/strategies`, `/api/chart/strategy-scan`, `/api/chart/history-signals`, `/api/chart/indicator-catalog`, `/api/chart/parity`, `/api/app-settings`, `/api/pipeline`, `/api/v2/backtest`, `/api/v2/shadow`, `/api/journal`, `/api/vol`, `/api/delta`, `/api/delta/demo`, `/api/golive`, `/api/sim/live-book`, `/api/sim/live-book/meta`, `/api/parity` is **admin**, `/api/delta/live` is **admin**, `/api/bracket`, `/api/candidates`, `/api/ai/quality`, `/api/portal/candles`, `/api/portal/signals`, `/api/portal/meta`, `/api/portal/news`, `/api/portal/world`, `/api/keys` (status only; values are not in this payload).

`/api/debug/perf` is admin. `/api/tables` redacts `admin_auth` secrets and `app_settings` rows whose key name looks like a secret (`scheduler/health.py` 5467–5482). The rest of the database, including paper positions, is returned to anyone who can reach the port.

### JSON POST

| Path | Auth | Effect |
|---|---|---|
| `/api/auth/verify` | bearer, fail-closed | Confirms the API token |
| `/api/settings/auth/login` | password | Sets `admin_session` and returns the token in JSON |
| `/api/settings/auth/logout` | session | Invalidates the token |
| `/api/paper/config` | admin session only | Paper wallet settings |
| `/api/strategy/config` | admin session only | Legacy strategy flags |
| `/api/settings/toggle` | admin, **fail-open if token empty** | Collector on/off, persisted |
| `/api/crypto/signals/live-check` | open (read of a price check) | Does not order |
| `/api/simulator/config` | **open** | Writes `data/simulator/config.json` and echoes it |
| `/api/simulator/start` | **open** | Spawns `scripts.run_market_simulator` |
| `/api/simulator/pause` | **open** | Terminates that process |
| `/api/sentiment/ingest` | ingest token, fail-closed | Stores scored headlines |
| `/api/moves/run` | admin | Starts move-attribution |
| `/api/crypto/watchlist/add` | admin, **fail-open if token empty** | Watchlist |
| `/api/crypto/watchlist/remove` | admin, **fail-open if token empty** | Watchlist |
| `/api/app-settings` | admin, fail-closed | Any `FIELDS` setting, including Delta live gates |
| `/api/v2/backtest/run` | admin, fail-closed | Starts v2 backtest if enabled |
| `/api/journal` | admin on the write path in `v2_pages.py` | Journal row |
| `/api/delta/shadow-now` | **open** | See §10.5 |
| `/api/keys/save`, `/clear`, `/test`, `/import-env`, `/reveal`, `/hf-space-deploy` | admin | Key management. `reveal` returns one secret to the admin |

No route is mounted at `/integration/v1`.

Other HTTP listeners, not `main.py`: `scripts/dash.py` (lab monitor, port 8765), `scripts/work_server.py` (lab jobs), `huggingface_space/app.py` (Gradio, port 7860). They are out of the engine process.

## 13. Environment variable names

Names only. `Settings` (`config/settings.py`) loads `.env` with no prefix, so each field is an environment variable. 326 fields. Database `app_settings` overrides these for keys listed in `config/overrides.py` `FIELDS`.

`DATABASE_URL`, `DATABASE_SSL_CA`, `SNAPSHOT_RETENTION_DAYS`, `PORT`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `TELEGRAM_CHANNEL_ID`, `GROQ_API_KEY`, `GROQ_MODEL`, `GROQ_SIGNAL_REVIEW_ENABLED`, `GROQ_REJECT_PENALTY`, `GROQ_CAUTION_MIN_PENALTY`, `PRE_TRADE_REVIEW_CACHE_TTL_SECONDS`, `GROQ_POSTMORTEM_MODEL`, `GROQ_POSTMORTEM_ENABLED`, `GROQ_REASONING_EFFORT`, `OPENROUTER_API_KEY`, `GEMINI_API_KEY`, `ANTHROPIC_API_KEY`, `LLM_CHAIN_PRE_TRADE`, `LLM_CHAIN_POST_TRADE`, `LLM_CHAIN_BRIEFING`, `MARKET_BRIEFING_ENABLED`, `MARKET_BRIEFING_MINUTES`, `MARKET_BRIEFING_QUIET_MINUTES`, `TRADE_NOTES_ENABLED`, `LLM_CHAIN_TRADE_NOTE`, `LLM_CHAIN_ATTRIBUTION`, `MOVE_ATTRIBUTION_ENABLED`, `EVENT_MONITOR_ENABLED`, `EVENT_MONITOR_DAILY_CAP`, `EVENT_MONITOR_RELEASE_CAP`, `EVENT_MONITOR_MAX_QUIET_MINUTES`, `LLM_CHAIN_BRIEFING_CALM`, `LLM_CHAIN_HISTORY`, `LLM_CHAIN_HISTORY_SEARCH`, `SWING_ENABLED`, `SWING_STRATEGIES`, `SWING_CANDIDATE_STRATEGIES`, `SWING_SCAN_SECONDS`, `SWING_MAX_PER_COIN`, `SWING_MULTI_PER_COIN_UNTIL`, `SWING_RISK_PCT`, `SWING_ADAPTIVE_RISK`, `SWING_RISK_MIN`, `SWING_WEAK_SPECS`, `SWING_WEAK_SPEC_FACTOR`, `SWING_MAX_OPEN`, `SWING_MAX_LEVERAGE`, `SWING_MAX_MARGIN_FRAC`, `SWING_BINANCE_RULES`, `SWING_REGISTRY`, `SWING_DD_ALARM_FACTOR`, `SWING_PROMOTE_MIN_TRADES`, `SWING_PROMOTE_T`, `SWING_LOCK_MODE`, `SWING_LOCK_LADDER`, `SWING_LOCK_BUFFER_R`, `SWING_LOCK_CONFIRM_MIN`, `SWING_TRAIL_AFTER_R`, `SWING_TRAIL_R`, `SWING_TARGET_CHASE_AFTER_R`, `SWING_TARGET_CHASE_GAP_R`, `SWING_LOCK_AFTER_R`, `SWING_LOCK_TRAIL_ATR`, `SWING_HOLD_MINUTES`, `SWING_SIGNAL_MAX_AGE_MINUTES`, `SWING_PERP_PRICES`, `PAPER_GAP_FILLS`, `SWING_CONFIG_FAIL_CLOSED`, `SWING_ACCOUNT_GUARDS`, `SWING_DAILY_LOSS_PCT`, `SWING_STREAK_PAUSE_AFTER`, `SWING_STREAK_PAUSE_MINUTES`, `SWING_NEWS_GUARD`, `SWING_MAX_SIDE_RISK_PCT`, `SWING_REGIME_FAIL_CLOSED`, `SWING_REGIME_FILTER`, `SWING_REGIME_VOL_RANK_MAX`, `SWING_REGIME_ADX_MAX`, `SWING_MAX_SAME_SIDE`, `SWING_EXCLUDE_SYMBOLS`, `SWING_EXTRA_SYMBOLS`, `PAPER_MIRRORS_LIVE`, `SHADOW_SIGNAL_ALERTS`, `HEADLINE_DEDUPE_ENABLED`, `HEADLINE_DEDUPE_THRESHOLD`, `HEADLINE_DEDUPE_WINDOW_HOURS`, `HEADLINE_TRIAGE_ENABLED`, `LIVE_TRADING_MODE`, `BINANCE_API_KEY`, `BINANCE_API_SECRET`, `BINANCE_TESTNET_API_KEY`, `BINANCE_TESTNET_API_SECRET`, `LIVE_MAX_BALANCE_USDT`, `LIVE_RISK_PCT`, `LIVE_MAX_RISK_PCT`, `LIVE_MAX_OPEN`, `LIVE_MAX_LEVERAGE`, `LIVE_DAILY_LOSS_PCT`, `LIVE_RESPECT_REGIME_FILTER`, `DELTA_INDIA_MODE`, `DELTA_INDIA_API_KEY`, `DELTA_INDIA_API_SECRET`, `DELTA_INDIA_BASE_URL`, `DELTA_INDIA_LIVE_ORDERS`, `DELTA_INDIA_RISK_PCT`, `DELTA_INDIA_MAX_OPEN`, `DELTA_INDIA_USD_INR`, `DELTA_INDIA_TESTNET_API_KEY`, `DELTA_INDIA_TESTNET_API_SECRET`, `DELTA_INDIA_TESTNET_URL`, `DELTA_TESTNET_RISK_PCT`, `DELTA_TESTNET_MAX_OPEN`, `DELTA_TESTNET_MIRROR`, `DELTA_WS_ENABLED`, `DELTA_LIVE_COINS`, `DELTA_INDIA_KILL_SWITCH`, `DELTA_INDIA_BACKSTOP_R`, `DELTA_INDIA_DAILY_LOSS_PCT`, `DELTA_INDIA_MIN_WALLET_INR`, `DELTA_INDIA_FLOOR_FRAC`, `DELTA_INDIA_CAPITAL_BASE_INR`, `DELTA_INDIA_CAPITAL_SINCE`, `DELTA_INDIA_RULES_FROM_INR`, `DELTA_INDIA_KILL_AFTER_TRADES`, `DELTA_INDIA_WITHDRAW_SHARE`, `DELTA_INDIA_KILL_RULE_ACK`, `DELTA_INDIA_MIN_LEVERAGE`, `DELTA_INDIA_ROUND_UP_RISK_PCT`, `BRACKET_PAPER_ENABLED`, `CANDIDATE_BOOK_ENABLED`, `IMPULSE_ENABLED`, `IMPULSE_ALERTS`, `IMPULSE_AI_DAILY_CAP`, `BRACKET_LEVELS_ENABLED`, `BRACKET_WALLET_INR`, `BRACKET_MARGIN_INR`, `BRACKET_LEVERAGE`, `VOL_PAPER_ENABLED`, `VOL_PAPER_UNDERLYINGS`, `VOL_NOTIONAL_USD`, `VOL_HEDGE_BAND`, `DELTA_INDIA_MAX_SPREAD_PCT`, `DELTA_INDIA_MAX_SLIPPAGE_R`, `PAPER_OPEN_FAMILIES`, `INTRADAY_MIN_CONFIDENCE`, `INTRADAY_MAX_OPEN`, `INTRADAY_MARGIN_CAP_FRAC`, `INTRADAY_MIN_TIMEFRAME_MINUTES`, `SMART_60M_ENABLED`, `CANDLE_REFRESH_ENABLED`, `PROFIT_LOCK_ENABLED`, `PROFIT_LOCK_AT_PCT`, `PROFIT_LOCK_TO_PCT`, `PROFIT_LOCK_TRAIL_PCT`, `PROFIT_LOCK_DEFERS_TO_TRAIL_ENABLED`, `PREMIUM_ROE_PCT`, `PREMIUM_FILLS_BOOK`, `PROTECTIONS_ENABLED`, `SESSION_FILTER_ENABLED`, `SESSION_START_UTC`, `SESSION_END_UTC`, `SESSION_WEEKDAYS_ONLY`, `DAILY_LOSS_LIMIT_PCT`, `MAX_SAME_DIRECTION_POSITIONS`, `V2_SHADOW_ENABLED`, `V2_SHADOW_MINUTES`, `V2_BACKTEST_ENABLED`, `V2_BACKTEST_YEARS`, `V2_BACKTEST_HOUR_UTC`, `V2_LAKE_DIR`, `V2_REPORTS_DIR`, `V2_BACKTEST_TIMEOUT_MINUTES`, `V2_BACKTEST_DOWNLOAD`, `DB_RETENTION_DAYS`, `COLD_STORAGE_DIR`, `MOVE_ATTRIBUTION_MINUTES`, `MOVE_ATTRIBUTION_WINDOW_HOURS`, `MOVE_ATTRIBUTION_MIN_MOVE_PCT`, `MOVE_ATTRIBUTION_TOP_N`, `MOVE_ATTRIBUTION_DAILY_CAP`, `POSITION_REVIEW_ENABLED`, `POSITION_REVIEW_INTERVAL_SECONDS`, `POSITION_REVIEW_CLOSE_ON_OUTAGE`, `POSITION_REVIEW_CONFIRM_CLOSE`, `POSITION_REVIEW_CONFIRM_GAP_MINUTES`, `POSITION_REVIEW_MIN_HOLD_MINUTES`, `POSITION_REVIEW_INTERVAL_SECONDS_WINNING`, `OLLAMA_BASE_URL`, `HF_BASE_URL`, `HF_SPACE_REPO`, `KAGGLE_USERNAME`, `KAGGLE_KEY`, `LLM_CALL_LOG`, `HF_SPACE_API_KEY`, `HF_SPACE_TIMEOUT_SECONDS`, `LLM_LIMITS`, `HF_API_TOKEN`, `LLM_CHAIN_NEWS_SCORING`, `LLM_CHAIN_POSITION_REVIEW`, `LLM_CHAIN_RESEARCH`, `EVENT_PRECEDENT_ENABLED`, `EVENT_PRECEDENT_LOOKAHEAD_DAYS`, `EVENT_PRECEDENT_MIN_SAMPLE_SIZE`, `EVENT_PRECEDENT_EXTENDED_HOLD_ENABLED`, `EVENT_PRECEDENT_EXTENDED_HOLD_MINUTES`, `LLM_CHAIN_EVENT_PRECEDENT`, `TWELVEDATA_API_KEY`, `TWELVEDATA_SYMBOLS`, `COINDCX_POLL_INTERVAL_SECONDS`, `COINGECKO_API_KEY`, `COINGECKO_POLL_INTERVAL_SECONDS`, `CRYPTOPANIC_AUTH_TOKEN`, `CRYPTOPANIC_POLL_INTERVAL_SECONDS`, `CRYPTO_WATCHLIST_SEED`, `CRYPTO_KLINE_INTERVAL`, `CRYPTO_TIMEFRAMES`, `BINANCE_WS_ENABLED`, `COINDCX_ENABLED`, `COINGECKO_ENABLED`, `TWELVEDATA_ENABLED`, `BINANCE_KLINES_ENABLED`, `BINANCE_KLINES_SECONDS`, `DELTA_INDIA_DATA_ENABLED`, `DELTA_KLINES_SECONDS`, `DELTA_ONLY_MODE`, `BINANCE_ONLY_MODE`, `MAX_PRICE_JUMP_PCT`, `MAX_TARGET_PCT`, `MIN_CONFIDENCE`, `MAX_STAKE_PCT`, `BANK_SIZE`, `SIGNAL_COOLDOWN_MINUTES`, `CRYPTO_MIN_CONFIDENCE`, `COUNTER_TREND_SHORT_MIN_CONFIDENCE`, `CRYPTO_SIGNAL_COOLDOWN_MINUTES`, `CRYPTO_SNAPSHOT_INTERVAL_SECONDS`, `CRYPTO_ALERT_TELEGRAM`, `CRYPTO_VOLUME_SPIKE_ENABLED`, `CRYPTO_HTF_FILTER_ENABLED`, `BINANCE_OI_ENABLED`, `ORDERFLOW_ENABLED`, `HIGH_CONVICTION_ONLY`, `BINANCE_LIQUIDATION_STREAM_ENABLED`, `BINANCE_LARGE_TRADE_STREAM_ENABLED`, `MIRROR_REVIEW_ENABLED`, `MIRROR_REVIEW_CAN_TRADE`, `MIRROR_REVIEW_CONFIDENCE_DELTA_THRESHOLD`, `MIRROR_REVIEW_MIN_ELAPSED_PCT`, `MIRROR_REVIEW_MAX_ROUNDS`, `MIRROR_TARGET_JITTER_PCT`, `LARGE_TRADE_NOTIONAL_USD`, `PAPER_TRADING_ENABLED`, `PAPER_STARTING_WALLET`, `PAPER_TARGET_WALLET`, `PAPER_REAL_FUNDING`, `PAPER_SWEEP_AT`, `PAPER_SWEEP_AMOUNT`, `PAPER_LEVERAGE`, `PAPER_STOP_PCT_OF_MARGIN`, `PAPER_REWARD_RISK`, `PAPER_MIN_CONFIDENCE`, `PAPER_MAX_CONCURRENT`, `PAPER_MAX_HOLD_MINUTES`, `PAPER_SCALED_SIZING`, `PAPER_TRAILING_ENABLED`, `PAPER_SCALED_LEVERAGE`, `PAPER_LADDER_ENABLED`, `PAPER_LADDER_TIGHT`, `PAPER_MAX_LEVERAGE`, `PAPER_TICK_INTERVAL_SECONDS`, `PAPER_USDT_INR`, `PAPER_ALERT_TELEGRAM`, `INTRADAY_LEVERAGE`, `DELIVERY_WALLET_ALLOCATION_PCT`, `DELIVERY_LEVERAGE`, `MAX_CONCURRENT_INTRADAY`, `CONSECUTIVE_LOSS_COOLDOWN_MINUTES`, `ANTI_FLIP_COOLDOWN_MINUTES`, `API_AUTH_TOKEN`, `API_RATE_LIMIT_REQUESTS`, `API_RATE_LIMIT_WINDOW_SECONDS`, `API_AUTH_RATE_LIMIT_REQUESTS`, `API_AUTH_RATE_LIMIT_WINDOW_SECONDS`, `SENTIMENT_INGEST_TOKEN`, `TRUSTED_PROXY_IPS`, `SENTIMENT_FEEDS_ENABLED`, `NEWS_SENTIMENT_ENABLED`, `RSI_DIVERGENCE_ENABLED`, `DAILY_TREND_FILTER_ENABLED`, `EVENT_BLACKOUT_ENABLED`, `EVENT_BIAS_MODE`, `CALENDAR_CAUTION_ENABLED`, `FEAR_GREED_REFRESH_MINUTES`, `CRYPTO_MAX_STAKE_PCT`, `USE_FINBERT`, `CRYPTO_AUTO_EXECUTE`, `NEWS_ENGINE_ENABLED`, `NEWS_COLLECTOR_ECONOMIC_CALENDAR_ENABLED`, `NEWS_COLLECTOR_CENTRAL_BANK_RSS_ENABLED`, `NEWS_COLLECTOR_US_TREASURY_FRED_ENABLED`, `NEWS_COLLECTOR_POLYMARKET_ODDS_ENABLED`, `NEWS_COLLECTOR_KALSHI_ODDS_ENABLED`, `NEWS_COLLECTOR_BINANCE_LIQUIDATIONS_ENABLED`, `NEWS_COLLECTOR_DEFILLAMA_ENABLED`, `NEWS_COLLECTOR_CROSS_ASSET_PRICES_ENABLED`, `NEWS_COLLECTOR_REGIONAL_RSS_ENABLED`, `NEWS_COLLECTOR_GDELT_15M_ENABLED`, `NEWS_EVENT_RESOLVER_ENABLED`, `NEWS_CLASSIFIER_ENABLED`, `NEWS_REACTION_TRACKER_ENABLED`, `NEWS_SENTIMENT_INDICES_ENABLED`, `NEWS_IMPULSE_LINKER_ENABLED`, `NEWS_PORTAL_WIDGETS_ENABLED`, `NEWS_ENGINE_FEEDS_MONITOR`, `NEWS_STUDY_MAC_ENABLED`, `NEWS_PAPER_STOP_REVERSE_ENABLED`, `NEWS_PAPER_FED_REACTION_ENABLED`, `NEWS_PAPER_EVENT_TEMPLATE_ENABLED`, `NEWS_PAPER_RISK_RADAR_ENABLED`, `FMP_API_KEY`, `FRED_API_KEY`, `ACLED_API_KEY`, `ACLED_EMAIL`, `NEWS_GDELT_WORKER_ENABLED`.

Read from the environment and **not** fields of `Settings`:

- Engine: `ADMIN_PASSWORD`, `SECRETS_MASTER_KEY`, `PYTHONUNBUFFERED`, `LLM_BUDGET_PATH`, `NEWS_USE_FIXTURES`, `BINANCE_PROXY_URL`, `PROXY_URL`.
- Scripts / lab only: `ENGINE_URL`, `NEWS_LEDGER`, `LAB_ROOT`, `SIM_GEMINI_KEY`, `SIM_HF_TOKENS`, `SIM_OPENROUTER_KEY`, `NEON_DATABASE_URL`, `AIVEN_DATABASE_URL`, `HUGGINGFACE_API_KEY`, `HF_TOKEN`, `BASE_MODEL`.
- Hugging Face Space process (`huggingface_space/app.py`), not the engine: `MODEL_REPO`, `MODEL_FILE`, `SPACE_API_KEY`, `CTX`, `LOAD_LLM`, `CLASSIFIER_ID`, `EMBEDDER_ID`.
- GitHub Actions secret names: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `EC2_HOST`, `EC2_USER`, `EC2_SSH_KEY`, `EC2_PORT`.

`main.py` also reads `PORT` directly, which is the same name as the `Settings` field.

## 14. Tests

Command, from the repo root, Python 3.12, dependencies from `requirements-dev.txt` in a temporary virtualenv (not committed):

`/tmp/cse-venv/bin/python -m pytest -q --tb=line`

Result: **2082 passed, 3 skipped, 24 warnings, 1208 subtests passed, exit 0**, in 77.4 seconds. The skips are the two tests in `tests/test_backup_redaction.py` `TestTheCommittedBackup` (`backups/neon_backup_latest.json.gz` is not in this checkout) and `tests/test_regime_gate.py` `test_live_check_matches_the_backtest_series` (no local Binance lake).

Warnings are pre-existing (`datetime.utcnow` deprecation, an un-awaited mock in `tests/test_manual_close.py`, pandas `dayofweek`). None failed.

Ruff (`ruff check .`) exits 1 with **6065** findings, almost all `E501` line length. Mypy on `config`, `scheduler`, `execution`, `analysis`, `storage`, `collectors`, `notifications`, and `main.py` exits 1 with **253** errors. This ticket does not change Python, so those were not edited. Detail is in the handoff.

## 15. Findings, highest risk first

| ID | Risk | Where | Finding |
|---|---|---|---|
| AUD2-1 | High (latent) | `scheduler/runner.py` 1696–1701 and 1731–1780; `execution/delta_book.py` 152–154, 453 | The running engine can place, amend, and cancel Delta India orders. Two defaults stop it: `delta_india_mode="off"` and `delta_india_live_orders=False`. Both are editable in the database. Production was not inspected. |
| AUD2-2 | High if Delta is live; low while it is off | `scheduler/delta_api.py` 62–83 | `POST /api/delta/shadow-now` is unauthenticated and assigns the process-global live gates. Overlapping requests can run a mirror while those gates are restored to live, then leave the process stuck in shadow. The handler comment says it never places a live order. |
| AUD2-3 | High on an open port | `scheduler/health.py` 271–340, 432–462, 466–482 | `POST /api/simulator/start`, `/pause`, and `/config` have no auth. Start writes the JSON body to `data/simulator/config.json` and puts caller-supplied model keys into the child environment. `GET /api/simulator/config` merges that file into the response, so a saved key is returned to the next caller. |
| AUD2-4 | Medium | `scheduler/health.py` 1656–1660, 1844–1848, 5738–5742 | Watchlist add/remove and collector toggle skip bearer auth when `API_AUTH_TOKEN` is empty. `scheduler/security.py` says protected routes fail closed. These three do not. They do not place orders. They can change what the engine watches. |
| AUD2-5 | Medium | `scheduler/health.py` 5418–5496, 5574–5581; `main.py` bind via `TCPSite` | The server listens on all interfaces. Most reads are open, including a database dump (`/api/tables`, secrets redacted) and the full settings list (`/api/app-settings`, secret values omitted). Login sets `Secure` on the session cookie only when the request is already HTTPS, and it also returns the session token in the JSON body. HANDOFF already lists "close port 8080" as open work. |
| AUD2-6 | Medium | `scheduler/runner.py` 1717–1718 and 1757–1758 | Testnet Delta orders are on the engine path when mode is `testnet` or `delta_testnet_mirror` is on. The call forces `live_orders=True`. Default is off. Money is the exchange's demo ledger, not the INR wallet. |
| AUD2-7 | Medium | `storage/database.py` 38–48 | `sslmode=require` encrypts without verifying the server certificate. A host that can answer for the database address can see the connection, including the password in the URL. `verify-full` plus `DATABASE_SSL_CA` is the checked mode. |
| AUD2-8 | Low | `execution/binance_futures.py` 151–179; `scheduler/portal.py` 195–196 | Binance USD-M order methods exist and are not reachable from `main.py`. `live_trading_mode` is a display field. Do not treat "mode=off" as the control that disables Binance. |
| AUD2-9 | Low | `config/settings.py` 793 | `crypto_auto_execute` defaults false and is never read. |
| AUD2-10 | Low | `README.md` lines 20–46; `SYSTEM_CONTEXT.md` section 0 | README still describes the confluence-gate architecture and a fully unwired `execution/` package. Section 0 says CoinDCX defaults on; `coindcx_enabled` is `False` at settings line 630. |

Confirmed non-findings:

- No code sends a Binance or Delta withdrawal or internal transfer.
- Paper trading (`analysis/paper_trading.py`) does not import `execution`.
- `POST /api/app-settings` and `/api/keys/*` require an admin session and fail closed.
- `POST /api/sentiment/ingest` fails closed when its token is unset.
- Public kline and open-interest calls are GET requests.

## 16. Where a read-only `/integration/v1` API should live

Add `scheduler/integration_v1.py` with a `register(app, runner)` function, and call it from `make_app` in `scheduler/health.py` next to the other `register()` calls (after line 7922, before the legacy routes). Do not add the routes inside `health.py`. That file is already past 8,000 lines, and a Jarvis client should not share handlers with the open dashboard.

Mount only GET routes under `/integration/v1/`:

- `/integration/v1/health` — process up, database reachable, job heartbeats.
- `/integration/v1/gates` — `live_trading_mode`, `delta_india_mode`, `delta_india_live_orders`, kill switch, `paper_open_families`. Booleans and enums. Never keys.
- `/integration/v1/book` — paper wallet, open positions, last swing signals. Reuse `storage/repository.py` reads that `/api/paper` and `/api/swing` already use.
- `/integration/v1/audit` — a thin wrapper over `analysis/signal_audit.py` `audit()`.
- `/integration/v1/budget` — the counts from `collectors/llm_budget.py`, not the provider keys.

Auth: a new `Settings` field `integration_read_token`, checked with `scheduler/security.py` `check_bearer_auth`. Empty token means 503, the same fail-closed path as `SENTIMENT_INGEST_TOKEN`. Do not copy the watchlist condition that skips the check when the token is unset (AUD2-4).

The module must not import `execution.delta_book`, `execution.delta_india`, or `execution.binance_futures`. It must not call `_mirror_delta_india` or `_delta_call_one`. Read tables and in-memory status only.

Why this shape, and not the existing `/api/*` surface: those routes are mostly unauthenticated, several POST handlers change settings or spawn processes, and `/api/delta/shadow-now` already touches the live gates. Pointing dhruv-ai at `/api/paper` would work on a trusted network and would inherit AUD2-3 and AUD2-5. A separate prefix makes a later allow-list obvious.

`docs/INTEGRATION_PLAN.md` item Q14 (a stdio MCP server in `scripts/mcp_server.py`) is a different adapter. If MCP is added later, it should call the same read functions as `/integration/v1`, not grow a second query layer. The HTTP prefix is the one the orchestrator can poll from outside the box.

Do not put this API in `execution/`. That package is the order path.
