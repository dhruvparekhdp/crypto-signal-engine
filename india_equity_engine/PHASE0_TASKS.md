# Phase 0 — Foundation: detailed task breakdown

> Goal: a tested repo skeleton that can authenticate to Kite, pull clean adjusted daily bars
> for the Nifty-50 into a Parquet data lake, compute the full Indian cost stack correctly, and
> write/read an append-only SQLite ledger — all with green CI. **No trading logic, no orders.**
>
> Work top to bottom. Each task lists files and acceptance criteria. Check boxes as you go.
> Run `ruff`, `mypy`, `pytest` before marking any task done. Keep `main` green (feature branches + PRs).

---

## 0.1 Repo scaffold & tooling
- [ ] `pyproject.toml`: project `india-engine`, Python 3.12, `src/` layout, deps
  (`pydantic`, `pydantic-settings`, `pykiteconnect`, `pandas`, `pyarrow`, `python-dotenv`),
  dev-deps (`pytest`, `ruff`, `mypy`, `pytest-cov`, `types-*`). Configure `ruff` + `mypy`.
- [ ] `uv lock` → commit `uv.lock`.
- [ ] `.gitignore`: `.env`, `*.pem`, `*.key`, `__pycache__`, `.venv`, `data/`, `*.sqlite3`, `.DS_Store`.
- [ ] `.env.example`: `KITE_API_KEY=`, `KITE_API_SECRET=`, `KITE_ACCESS_TOKEN=` (placeholder),
  `DB_PATH=./data/india_engine.sqlite3`, `LAKE_DIR=./data/lake`, `TZ=Asia/Kolkata`.
- [ ] `.github/workflows/ci.yml`: on push/PR → install uv, `ruff check`, `ruff format --check`,
  `mypy src`, `pytest -q`. Cache the uv venv.
- [ ] `README.md`: quickstart (clone → `uv sync` → copy `.env` → run tests).
- **Accept:** `uv sync && uv run pytest -q` runs (even if 0 tests) and CI is green on an empty suite.

## 0.2 Config & constants
- [ ] `src/india_engine/config.py`: `pydantic-settings` `Settings` reading `.env`; validate
  required keys at import; expose `settings`. Fail loudly on missing config.
- [ ] `src/india_engine/constants.py`: `Segment` enum (CNC/MIS), `Side` (BUY/SELL),
  `TICK_SIZE = 0.05`, `MARKET_OPEN=time(9,15)`, `MARKET_CLOSE=time(15,30)`,
  `MIS_SQUAREOFF=time(15,10)` (our target; broker forces ~15:20), `MAX_INTRADAY_LEVERAGE=5`,
  NSE holiday list (seed 2024–2026; note it must be refreshed), `TZ=ZoneInfo("Asia/Kolkata")`.
- [ ] `tests/test_config.py`: missing env → raises; present env → loads.
- **Accept:** importing `settings` with a valid `.env` succeeds; constants importable.

## 0.3 Time / calendar utilities
- [ ] `src/india_engine/timeutil.py`: `now_ist()`, `is_trading_day(date)` (weekday & not holiday),
  `is_market_open(dt)`, `next_trading_day(date)`, `to_utc(dt_ist)`, `to_ist(dt_utc)`.
- [ ] `tests/test_timeutil.py`: weekend → not trading; a known NSE holiday → not trading;
  09:14 closed / 09:15 open / 15:30 closed; UTC↔IST round-trip.
- **Accept:** all calendar tests pass, including a holiday and a weekend.

## 0.4 Cost model (the most important module — get it exactly right)
- [ ] `src/india_engine/costs.py`: pure functions, itemized breakdown + total.
  - `delivery_charges(buy_value, sell_value, scrips_sold=1) -> ChargeBreakdown`
  - `intraday_charges(orders: list[Order]) -> ChargeBreakdown` (brokerage = min(₹20, 0.03%) per order)
  - `ChargeBreakdown` dataclass: brokerage, stt, txn, sebi, stamp, gst, dp, total.
  - Implement every line from CLAUDE.md §5. GST = 18% × (brokerage + txn + sebi). Round to paisa.
- [ ] `tests/test_costs.py`:
  - Delivery worked example (CLAUDE.md §5c): buy ₹1,00,000 / sell ₹1,10,000 → total ≈ ₹249.7 (±₹1).
  - Intraday: brokerage picks the lower of ₹20 vs 0.03%; STT only on sell; stamp 0.003% buy; no DP.
  - Zero-value edge cases; GST base correctness.
- **Accept:** worked example matches to the paisa; later reconcile vs a real Zerodha contract note.

## 0.5 Instrument universe
- [ ] `src/india_engine/data/universe.py`: `nifty50() -> list[Instrument]`. Fetch the current
  Nifty-50 constituents from NSE (indices CSV) and cache to `data/nifty50.json` with a timestamp;
  fall back to a bundled snapshot if offline. Flag that constituents change over time.
- [ ] `tests/test_universe.py`: returns 50 symbols; cache round-trip; offline fallback works.
- **Accept:** `nifty50()` returns 50 liquid symbols; cache written and re-read.

## 0.6 Kite Connect auth (token flow)
- [ ] `src/india_engine/data/kite_auth.py`: build login URL → user pastes `request_token` →
  exchange for `access_token` (SHA-256 of api_key+request_token+api_secret) → persist to `.env`
  / keyring. Access tokens expire daily — provide `ensure_token()` that refreshes/re-prompts.
  **Never log or commit the token.**
- [ ] Manual step (documented in README): create a Kite Connect app at developers.zerodha.com,
  note api_key/secret (paid). This is a human step; code just consumes the creds.
- **Accept:** given valid creds + request_token, obtain and cache an access_token; `ensure_token()`
  detects expiry. (Unit-test the checksum logic with a mock; live auth is manual.)

## 0.7 Historical data lake (Parquet)
- [ ] `src/india_engine/data/fetch.py`: `fetch_daily(symbol, start, end)` via Kite
  `historical_data` (day interval); `fetch_intraday(symbol, date, interval)` for 5m/15m.
  Handle rate limits (Kite ~3 req/s historical) with backoff; paginate long ranges.
- [ ] `src/india_engine/data/lake.py`: write bars to `LAKE_DIR/<symbol>/daily.parquet`
  (append/upsert by date, dedupe); read back as a typed DataFrame. Store **adjusted** prices
  (handle splits/bonuses) and keep a raw copy. Columns: `date, open, high, low, close, volume`.
- [ ] `tests/test_lake.py`: parquet round-trip; upsert dedupes by date; schema stable.
- [ ] A CLI: `python -m india_engine.data.fetch --symbols all --years 8` backfills the lake.
- **Accept:** backfill all Nifty-50 daily bars for ~8 years; spot-check a known OHLC print
  against Zerodha/NSE; re-running the backfill is idempotent (no duplicate dates).

## 0.8 Ledger (SQLite, append-only)
- [ ] `src/india_engine/ledger/schema.sql`: tables from CLAUDE.md §7
  (config, instrument, signal, order_intent, fill, position, ledger_entry, cycle, recon_report).
  `ledger_entry` has no UPDATE/DELETE path.
- [ ] `src/india_engine/ledger/db.py`: connect, `init_schema()`, PRAGMAs (WAL, foreign_keys).
- [ ] `src/india_engine/ledger/ledger.py`: `append_entry(...)`, `balance()`, readers.
  Enforce append-only (no update/delete functions; optionally a trigger).
- [ ] `tests/test_ledger.py`: append + balance round-trip; attempting mutation is impossible/
  raises; concurrent appends keep `balance_after` consistent.
- **Accept:** ledger round-trips; append-only is enforced; schema initializes from scratch.

## 0.9 Smoke test + CI green
- [ ] An end-to-end smoke test: init DB → write a config row → append a ledger entry →
  write+read a tiny parquet → assert all good. Mark it so CI runs it.
- [ ] `pytest --cov` shows meaningful coverage on `costs`, `timeutil`, `ledger`, `lake`.
- **Accept:** `main` is green; a fresh clone + `uv sync && uv run pytest -q` passes.

---

## Phase 0 definition of done (all true)
- [ ] Clean adjusted daily bars for all Nifty-50, ~8y, idempotent backfill.
- [ ] Cost model matches the hand-computed worked example (and later a real contract note).
- [ ] Append-only ledger round-trips; reconciliation invariant holds.
- [ ] CI green (ruff + mypy + pytest) on `main`; fresh clone passes.
- [ ] No secrets committed; `.env` gitignored; TLS verified on any network call.

> **Stop here.** Phases 1+ (lab/backtest, paper engine, execution) are separate and begin only
> after Phase 0 passes and the owner gives the go-ahead. See CLAUDE.md §10 for the go-live gate.
