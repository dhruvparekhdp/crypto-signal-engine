# crypto-signal-engine

A real-time crypto market analysis system. It watches a configurable set of
USDT perpetual pairs, runs five independent signal detectors over live
candles, and only acts where independent evidence agrees. Signals are
tracked through a paper-trading simulator (no real capital), scored against
their actual outcomes, and exposed through a web dashboard and a small
authenticated API.

The project began as a tennis-match betting monitor. That lineage is
visible in the git history and nowhere else: the sports collectors,
analyzers, models and routes were removed once the crypto path became the
whole system. Keeping a second, unrunnable application alive behind a
feature flag cost more in confusion than it saved in optionality.

---

## Architecture

```
Binance WebSocket ─┐
CoinDCX REST        ├─► CryptoStateStore ─► five detectors ─► confluence gate ─► level policy
CoinGecko REST      │      (in-memory,        (independent      (≥3 of 5         (cost floor,
CryptoPanic (news) ─┘       per-symbol,        per family,       families,        trailing stop,
                             rolling candle     one vote each)    no direction     reward:risk 2)
                             window)                              on veto)
                                                                        │
                                                                        ▼
                                              cooldown + contradiction rejection
                                                        (CryptoEngine)
                                                                        │
                                    ┌───────────────────────────────────┼──────────────────┐
                                    ▼                                   ▼                  ▼
                          paper-trading simulator              outcome logging      Telegram alert
                          (trailing stop, laddered              (crypto_signal_log)
                           exits, simulated fills)                       │
                                    │                                    ▼
                                    ▼                          signal_audit (win rate,
                          paper_trades / paper_cycles           expectancy, per-family
                                                                 slicing, introspected
                                                                 method catalogue)
```

Two entry points share this pipeline. The live path is driven by APScheduler
jobs polling real exchanges. The backtest path
(`analysis/backtest.py`) walks historical candles through the *same*
`CryptoState`, the *same* detectors, and the *same* paper-trading fill logic
— a backtest result and a live paper cycle differ only in where the candles
came from. Look-ahead is prevented structurally: a signal generated on
candle N can only fill at candle N's close and is only ever resolved against
candle N+1 onward.

### Why confluence, not five independent alerts

The instinct on seeing one detector dominate the signal feed is to add more
detectors. That makes the false-positive rate worse, not better — more
signals at the same accuracy is strictly worse once every trade pays a real
round-trip cost.

Indicators are grouped into six families — trend, momentum, volatility,
volume, structure, pattern — so that RSI, stochastic and MACD, three views
of the same momentum, can't be counted as three independent confirmations.
Each family gets at most one vote, weighted (trend and structure carry more
weight than a single candlestick pattern), and a trade requires at least
three of five voting families to agree. Volatility is not a voting family:
it can veto a setup outright (the market is too quiet to be worth trading)
but it never elects a direction on its own.

### Why levels are derived from cost, not from volatility alone

Every tradeable instrument has a real round-trip cost — exchange fee, spread,
and a slippage buffer — measured per market rather than assumed globally.
Gold's brokerage on CoinDCX is roughly a fifth of a crypto pair's; holding
both to one global floor would refuse profitable gold setups and accept
unprofitable crypto ones. The target for a signal is required to clear a
multiple of that cost floor before it's published at all, and the stop is
sized so the round-trip cost can never dominate the risk being taken. Reward-
to-risk defaults to 2:1 with a trailing stop once the position is ahead,
rather than a symmetric fixed target — a symmetric target at typical costs
requires close to a 60% hit rate just to break even, which the measured hit
rate on live signals didn't clear.

### Order book, as a distinct source

Every other input is a transformation of past prices. The order book is the
one signal that describes what other market participants have committed to
do next, and it's used two ways: to reprice the assumed spread/slippage with
a measurement from the live book instead of a flat estimate, and to veto a
target that has a resting order wall between entry and target large enough
to plausibly stop the move before it arrives. It never elects a direction.

### Cooldown and contradiction rejection

Two separate problems, addressed separately. A cooldown per (symbol, signal
type) stops one detector from re-firing on the same continuous move. A
second, independent check stops the system from holding two *contradictory*
live signals on the same symbol — a confluence long and a volume-spike short
on the same coin, seven minutes apart, both technically real detector
outputs — by refusing a new signal in the opposite direction of one that's
still open, until the open one resolves against its own stop or target.

---

## What's real vs. what the paper trader does

Nothing here places a live exchange order. The paper-trading simulator
(`analysis/paper_trading.py`, `analysis/paper_cycle.py`) models a fixed
starting wallet, leveraged positions, trailing stops, and an optional
laddered exit (locking in partial profit at return-on-equity milestones
before the position resolves). Every open position and every closed trade —
fees, funding, slippage, net P&L — is written to Postgres so the numbers can
be audited later, not just watched live.

## Outcome tracking

`analysis/signal_audit.py` turns the logged signal history into a scoreboard:
win rate, but also net expectancy (win rate alone is a bad measure once
costs are netted in — a coin-flip win rate at 1:1 reward:risk is a loser
after fees), sliced by symbol, setup, time horizon, direction, and
confidence band, so a systematic failure is attributable rather than
guessed at. It also introspects the actual analyzer/indicator source at
request time and serves a live method catalogue alongside the audit table —
the documentation of "what fired this signal" is read from the code, not
maintained by hand, so it can't drift out of sync with it.

There is no machine-learning model retraining on this crypto outcome
history today. The outcome archive is the input such a model would need,
which is why it is recorded whether or not anything consumes it yet.

## API and security

The web dashboard and a JSON API are served from one aiohttp process
(`scheduler/health.py`). Read endpoints — price, forecasts, signal history —
are open; they carry no secret and gating them would only break the
dashboard for no benefit. The three endpoints that change server state
(collector toggles, watchlist edits) require a bearer token
(`scheduler/security.py`), checked with a constant-time comparison and
failing closed if unconfigured — an unset token means those endpoints
refuse everything, not that they're silently open. A separate, much
tighter rate-limit bucket applies to the token-verification endpoint
specifically, since its entire job is accepting attempts at a secret.
General API traffic is rate-limited per client IP (read from
`X-Forwarded-For`, since Render terminates TLS in front of the app and the
raw peer address is the load balancer's, not the caller's).

## Companion iOS app

An in-progress native iOS client lives under [`ios-port/`](ios-port/) — not
a separate repo, a source tree meant to be merged into a sibling Xcode
project. It talks to this backend's API rather than reimplementing the
detection logic in Swift, so there is one implementation of the trading
logic, not two that can drift. See `ios-port/SESSION_CONTEXT.md` for the
current state; it is explicitly marked unverified against a real Swift
toolchain as of the last commit that touched it.

---

## Tech stack

| | |
|---|---|
| Language | Python 3.11+ |
| Web / API | aiohttp |
| Scheduling | APScheduler (in-process, 24/7 interval + cron jobs) |
| Database | SQLAlchemy (async) — SQLite locally, Aiven.io PostgreSQL in production |
| Live data | `websockets` (Binance), `httpx` (CoinDCX, CoinGecko, TwelveData, CryptoPanic REST) |
| AI Sentinel | Groq API (Qwen 3.8 27B / Llama 3.3 70B, selectable via Admin UI) |
| Logging | structlog |
| Notifications | python-telegram-bot |
| Tests | pytest (606 tests, 100% passing) |
| Deployment | AWS EC2 (`systemd` service: `crypto-engine`), automated via GitHub Actions CI/CD |

## Running locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env   # fill in what you need — see Configuration below
python main.py
```

Health check: `curl http://localhost:8080/health`
Dashboard: `http://localhost:8080/`

Run the tests:

```bash
.venv/bin/pytest tests/
```

## Deployment

Deployed on **AWS EC2** as a systemd service (`crypto-engine`). Auto-deployment is powered by GitHub Actions (`.github/workflows/deploy.yml`) on every push to `main`, featuring automated Telegram alerts for deployment start, success, and failure.

See [`SYSTEM_CONTEXT.md`](SYSTEM_CONTEXT.md) for full operational runbook, service commands, and disaster recovery.

## Configuration

Core secret credentials are kept in `.env`. Operational levers (Paper Trading on/off, leverage, risk, trailing stops, Groq AI models, and coin watchlist) are managed dynamically in PostgreSQL via the `/settings` Admin UI.

| Variable | Default | Purpose |
|---|---|---|
| `PORT` | `8080` | Web server port |
| `DATABASE_URL` | `sqlite+aiosqlite:///./crypto_engine.db` | PostgreSQL connection URL (e.g. Aiven) |
| `ADMIN_PASSWORD` | — | Secret password to unlock `/settings` and manual controls |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | — | Signal and trade alerts via Telegram |
| `GROQ_API_KEY` | — | Groq API key for trade pre-signal reviews |
| `API_AUTH_TOKEN` | — | Bearer token for the mutating API endpoints and the iOS client. **Fails closed** — unset means those endpoints refuse every request with a 503 |
| `SENTIMENT_INGEST_TOKEN` | — | Optional: shared secret for pushed news-sentiment scores |
| `TWELVEDATA_API_KEY` | — | Optional: TwelveData API for Gold, Silver, and Crude Oil |

These are credentials, not tunables — which is why they live in `.env`
rather than in the DB-backed settings above.

## Project structure

```
crypto-signal-engine/
├── main.py                       # entry point
├── config/settings.py            # all configuration
├── collectors/
│   ├── binance_klines.py         # live candles, primary crypto data source
│   ├── coindcx.py, coingecko.py  # price/ticker sources
│   ├── binance_futures_oi.py     # futures open interest
│   ├── macro_sentinel.py         # Groq pre-signal review
│   └── sentiment_feeds.py        # CryptoPanic news sentiment, Fear & Greed
├── analysis/
│   ├── crypto_state.py, crypto_state_store.py   # canonical live market state
│   ├── indicators.py             # RSI, MACD, Bollinger, ATR, volume metrics, etc.
│   ├── confluence.py             # family voting + volatility veto
│   ├── scalp_levels.py           # cost-floor-derived target/stop/reward:risk
│   ├── crypto_signals.py         # the five detectors + the confluence detector
│   ├── crypto_engine.py          # cooldown, contradiction rejection, HTF filter
│   ├── orderbook.py              # book-derived cost and wall-veto
│   ├── orderflow.py              # CVD, taker delta, absorption detection
│   ├── paper_trading.py, paper_cycle.py         # simulated execution
│   ├── backtest.py               # historical replay through the same pipeline
│   └── signal_audit.py           # outcome scoring + live method catalogue
├── scheduler/
│   ├── runner.py                 # APScheduler job registration and orchestration
│   ├── health.py                 # aiohttp app: dashboard + JSON API
│   └── security.py               # bearer auth, rate limiting, security headers
├── storage/                      # SQLAlchemy models + repository
├── notifications/                # Telegram formatting and delivery
├── ios-port/                     # in-progress native iOS client (see above)
└── tests/                        # ~580 tests
```
