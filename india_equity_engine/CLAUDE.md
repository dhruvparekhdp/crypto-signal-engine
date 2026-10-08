# India Equity Engine — Project Context (for Claude Code)

> Read this file fully before writing any code. It is the founding spec.
> It encodes hard-won lessons from a prior crypto trading project. Follow the
> conventions and non-negotiables exactly. When in doubt, prefer the more
> conservative, more-tested option.

---

## 1. What we are building

An **Indian equity swing + intraday trading engine** on **Zerodha (Kite Connect)**.

- **Instruments:** Equity **delivery (CNC)** and **intraday (MIS)** only.
- **NO futures.** **Options rarely** — only around major scheduled events, and only
  with explicit per-trade approval and strict IV rules (see §11).
- **Universe:** Nifty-50 / large-cap blue chips (liquid, tight spreads, hard to manipulate).
- **Style:** Patient swing trading on **daily bars**; intraday is secondary, on 5m/15m bars
  within market hours. This is a multi-week/month compounding project, **not** a daily-income machine.
- **Mode:** **Paper / POC only** until a formal go-live gate (§10) is passed with an explicit
  human "yes". The engine must be physically incapable of placing a real order before that.

This is a **new, separate repository**. It shares **no code and no database** with the prior
crypto project. We port *ideas and discipline*, not files.

---

## 2. Non-negotiables (hard rules — do not violate)

1. **Paper-first.** No real order routing until the go-live gate. All "fills" are simulated
   until then. Real-money wiring requires an explicit human approval recorded in writing.
2. **Ledger-first.** Every order intent, fill, fee, and cash movement is an **append-only**
   ledger entry. The ledger is the single source of truth. Nothing mutates history.
3. **Reconciliation from day 1.** A loop continuously compares internal positions vs the
   broker's positions and alerts on any drift. (The crypto project shipped an orphan-position
   bug — ₹1,408 of stale margin — because this was missing. Do not repeat it.)
4. **Cost model first.** Every backtest and every simulated fill nets out the **full Indian
   cost stack** (§5). An edge that doesn't survive costs is not an edge.
5. **Tests + CI gate.** `main` is protected. The full test suite must be green before any
   merge/deploy. Never push red. (Crypto auto-deployed on green; keep that discipline.)
6. **No secrets in git.** API keys, tokens, and passwords live in `.env` (gitignored) or the
   OS keyring. Never at repo root, never committed. Verify `.gitignore` covers them.
7. **TLS verified.** Any network/DB connection verifies certificates. No `CERT_NONE`.
8. **No in-sample constants in the live path.** Any threshold learned from a backtest is
   stored as data/config with its provenance, never hardcoded into decision logic.
9. **In-sample ≠ edge.** Nothing is "an edge" until it survives out-of-sample / walk-forward
   and a null test with a confidence interval that excludes zero.
10. **AI is optional and must prove lift.** Any AI/LLM gate is off by default and only
    enabled after it demonstrates statistically significant lift on held-out data. (In crypto
    the AI gate was a coin flip — AUC 0.499 — and threw away 68% of profit. Do not assume AI helps.)

---

## 3. Tech stack (decided — don't relitigate without a reason)

- **Language:** Python 3.12.
- **Package/env manager:** `uv` (fast, lockfile). `pyproject.toml` + `uv.lock`.
- **Layout:** `src/` layout. Package name `india_engine`.
- **Historical bars (data lake):** **Parquet** files on disk (columnar, fast, free, no server).
- **State + ledger (paper phase):** **SQLite** (local, zero-admin, transactional). Postgres is
  a later, optional upgrade only if we ever run multi-process/live — do NOT start with a hosted
  free DB (crypto's free Postgres had ~1.4s/query latency; avoid that pain).
- **Broker API:** `pykiteconnect` (official Kite Connect client).
- **Tests:** `pytest`. **Lint/format:** `ruff`. **Types:** `mypy` (lenient to start).
- **CI:** GitHub Actions — run `ruff`, `mypy`, `pytest` on every PR/push to `main`.
- **Config:** `pydantic-settings`, env-driven, validated at startup.
- **Time:** everything timezone-aware. Market timezone is **Asia/Kolkata (IST, UTC+5:30)**.
  Store timestamps in UTC in the ledger; render in IST.

---

## 4. Proposed repository layout

```
india_equity_engine/
├── CLAUDE.md                  # this file
├── README.md                  # quickstart
├── PHASE0_TASKS.md            # detailed Phase-0 task breakdown
├── pyproject.toml
├── uv.lock
├── .gitignore
├── .env.example
├── .github/workflows/ci.yml
├── src/india_engine/
│   ├── __init__.py
│   ├── config.py              # pydantic-settings, env validation
│   ├── constants.py           # market hours, segment enums, tick size, Nifty-50 list
│   ├── costs.py               # FULL Indian cost model (§5) — pure functions
│   ├── timeutil.py            # IST calendar, market-hours checks, NSE holidays
│   ├── data/
│   │   ├── kite_auth.py       # request->access token flow, daily token refresh
│   │   ├── fetch.py           # historical daily/intraday bars via Kite
│   │   ├── lake.py            # Parquet read/write, partitioning by symbol
│   │   └── universe.py        # Nifty-50 constituents (fetch live, cache)
│   ├── ledger/
│   │   ├── db.py              # SQLite connection, schema init, migrations
│   │   ├── schema.sql         # tables (§7)
│   │   └── ledger.py          # append-only writer + readers
│   ├── lab/                   # Phase 1: backtesting (NOT built in Phase 0)
│   │   ├── simulate.py
│   │   ├── metrics.py
│   │   └── stats.py           # null tests, walk-forward, cluster t-stats, CIs
│   ├── engine/                # Phase 2: market-hours paper engine (NOT Phase 0)
│   ├── execution/             # Phase 3: order plumbing, reconciliation, kill switch
│   └── dashboard/             # Phase 2: read-only monitor
└── tests/
    ├── test_costs.py          # cost model vs hand-computed contract note
    ├── test_timeutil.py       # market hours, holidays
    ├── test_ledger.py         # append-only, round-trip, no mutation
    └── test_lake.py           # parquet round-trip
```

---

## 5. Indian cost model (exact — implement in `costs.py`, validate against a real contract note)

Charges differ by **segment**. Implement as pure functions returning an itemized breakdown
(dataclass / dict), then a total. All percentages are on turnover unless noted.

### 5a. Equity Delivery (CNC) — buy and hold, take delivery
| Charge | Rate | Side |
|---|---|---|
| Brokerage (Zerodha) | ₹0 (free) | — |
| STT | 0.1% | **buy and sell** |
| Exchange txn (NSE) | 0.00345% | both |
| SEBI charge | ₹10 / crore (= 0.0001%) | both |
| Stamp duty | 0.015% | **buy only** |
| GST | 18% on (brokerage + txn + SEBI) | both |
| DP charge | ₹15.93 flat per scrip | **sell only** (delivery) |

### 5b. Equity Intraday (MIS) — squared off same day
| Charge | Rate | Side |
|---|---|---|
| Brokerage (Zerodha) | ₹20 **or** 0.03% per executed order, **whichever is lower** | each order |
| STT | 0.025% | **sell only** |
| Exchange txn (NSE) | 0.00345% | both |
| SEBI charge | ₹10 / crore (= 0.0001%) | both |
| Stamp duty | 0.003% | **buy only** |
| GST | 18% on (brokerage + txn + SEBI) | both |
| DP charge | none | — |

> **Acceptance test:** hand-compute the charges for a worked example and assert the module
> matches to the paisa; then, once Kite is connected, reconcile against a **real Zerodha
> contract note**. Stamp duty and GST base are the usual sources of error — verify both.

### 5c. Worked example (delivery) — for the unit test
Buy ₹1,00,000, later sell ₹1,10,000 (same scrip):
- Buy: STT 100.00 + txn 3.45 + SEBI 0.10 + stamp 15.00 + GST 18%×(0+3.45+0.10)=0.64 → **≈ ₹119.19**
- Sell: STT 110.00 + txn 3.80 + SEBI 0.11 + DP 15.93 + GST 18%×(0+3.80+0.11)=0.70 → **≈ ₹130.54**
- Total ≈ **₹249.7**; gross ₹10,000 → **net ≈ ₹9,750**.

---

## 6. Indian market rules (bake into `constants.py` / `timeutil.py`)

- **Market hours:** 09:15–15:30 IST, Mon–Fri. Pre-open 09:00–09:15.
- **Holidays:** NSE/BSE holiday calendar — fetch/maintain a list; never assume a weekday is a trading day.
- **Settlement:** **T+1** (shares and cash settle next trading day).
- **No overnight short in cash equity.** Shorts must be intraday (MIS) and closed same day.
- **Intraday auto square-off:** MIS positions are force-squared-off by the broker around
  **15:20 IST** if not closed. The engine must flatten MIS before then (target 15:10).
- **Leverage (SEBI peak-margin, mandatory since Sep 2021):** intraday equity is capped at
  **5× (20% margin)** across ALL brokers. **There is no 11×.** Delivery (CNC) is **1×**
  (full cash, no leverage). Design all sizing to the 5× cap; treat anything higher as wrong.
- **Tick size:** ₹0.05. **Lot size:** 1 for cash equity (no lots, unlike F&O).
- **Circuit limits:** stocks have daily price bands (commonly 5%/10%/20%); a halted/locked
  stock can't fill — handle gracefully.

---

## 7. Data model / ledger schema (SQLite — `ledger/schema.sql`)

Append-only where noted. Use UTC timestamps (`ts_utc`) plus an IST helper.

- **bars** are in Parquet (not SQLite). SQLite holds *state and money*, not bulk bars.
- **config**(id PK, key, value, provenance, ts_utc) — every tunable, with where it came from.
- **instrument**(symbol PK, exchange, isin, segment, tick_size, active)
- **signal**(id PK, ts_utc, symbol, side, strategy, timeframe, strength, meta_json)
- **order_intent**(id PK, ts_utc, signal_id FK, symbol, side, qty, order_type, limit_price, segment, status)
- **fill**(id PK, ts_utc, intent_id FK, symbol, side, qty, price, segment)  *(simulated until go-live)*
- **position**(id PK, symbol, segment, qty, avg_price, opened_ts, status, closed_ts)
- **ledger_entry**(id PK, ts_utc, kind, symbol, amount, balance_after, ref_id, meta_json) — **APPEND-ONLY**;
  `kind` ∈ {deposit, withdraw, fee, stt, stamp, gst, dp, brokerage, realized_pnl, sweep, ...}
- **cycle**(id PK, started_ts, start_equity, status, ended_ts) — wallet cycles (sweep plan)
- **recon_report**(id PK, ts_utc, broker_json, book_json, drift, ok)

**Invariant:** `position` and `ledger_entry` must always reconcile; a nightly job asserts it.

---

## 8. Architecture (target — but build as a clean monolith first)

Logical services with **hard module boundaries** inside one repo/process for now. Do NOT
split into separate deployables in Phase 0–2 (over-engineering). Keep the boundaries so they
*can* be split later.

```
[Kite Connect] --bars/quotes--> [data lake (Parquet)]
                                      |
[lab: backtest + stats] <-------------+        (offline, never in live path)
                                      |
                            [decision: signals + risk caps]   (only writer of order_intents)
                                      |
                            [execution: fills + reconciliation + kill switch]
                                      |
                            [ledger: append-only] --> [dashboard (read-only)]
```

- **Collector/data:** ingest bars; stateless; restart freely.
- **Lab:** backtests + statistics; offline; never imported by the live path.
- **Decision:** turns data into `order_intent`s; enforces risk caps; the ONLY writer of intents.
- **Execution:** turns intents into fills (simulated now, real after gate); reconciles positions.
- **Ledger:** append-only source of truth; everyone reads it.
- **Dashboard:** read-only observability; must survive restarts (watchdog).

**Risk caps enforced in `decision` (code, not convention):** per-trade risk, per-side exposure
cap, daily-loss kill switch, max open positions. (Crypto's 9% side-risk cap correctly blocked
an over-exposure — keep that pattern.)

---

## 9. Risk & sizing defaults (starting point — tune via backtest, store in `config`)

- **Per-trade risk:** 0.5%–1% of equity to start (smaller than crypto — equities gap on
  results/news and a stop is not a guarantee).
- **Max open positions:** e.g. 5.
- **Per-side exposure cap:** e.g. 60% of equity (delivery is 1×; intraday ≤5× but we won't max it).
- **Daily-loss kill switch:** halt new entries if day P&L ≤ −2% of equity.
- **Position sizing:** qty = floor((equity × risk%) / (entry − stop)). Respect tick and capital.

---

## 10. Go-live gate (ALL must be true before any real order)

1. ≥ **3 months** and ≥ **60** forward (paper) trades logged.
2. Out-of-sample / walk-forward expectancy **95% CI excludes zero** (positive).
3. Position reconciliation clean for the whole period (no orphans/drift).
4. **CA / tax opinion** on business-income treatment + audit applicability.
5. Explicit, recorded **human "yes"**.
6. Start at **minimum size**; scale only on continued proof.

---

## 11. Options (rare, major events only) — guardrails

- Off by default. Each options trade needs explicit per-trade human approval.
- **Beware IV crush:** buying options into a known event (earnings, budget, election) often
  loses even when the directional call is right, because implied volatility collapses after
  the event. Prefer defined-risk structures; size tiny; never sell naked options.
- This is a later-phase concern; do not build options plumbing in Phase 0–2.

---

## 12. Lessons imported from the crypto project (the "why" behind the rules)

- **C1 orphan positions** → reconciliation loop from day 1 (rule 3).
- **H4 unverified DB TLS** → TLS verified always (rule 7).
- **L5 loose key file at repo root** → secrets in env/keyring, gitignored (rule 6).
- **H5 in-sample constants in live path** → config with provenance (rule 8).
- **H1 AI gate worthless (AUC 0.499)** → AI off by default, must prove lift (rule 10).
- **C3 in-sample edge** → null tests + walk-forward + CI before believing (rule 9).
- **Free hosted DB latency (~1.4s/q)** → local SQLite/Parquet first (§3).
- **8,131-line god file (`health.py`)** → keep modules small and single-purpose (§8).
- **Monitor died on network lag** → dashboard read-only + watchdog + keep-last-good (§8).

---

## 13. How to work (for Claude Code)

- Work in **small, tested increments**. One module + its tests at a time.
- Run `ruff check --fix`, `ruff format`, `mypy`, and `pytest` before considering a task done.
- Keep `main` green. Use feature branches + PRs.
- Update `PHASE0_TASKS.md` checkboxes as tasks complete.
- When a number is needed (cost, hour, cap), use this file's value — do not invent one.
- If a requirement is ambiguous, choose the conservative option and note the assumption.
