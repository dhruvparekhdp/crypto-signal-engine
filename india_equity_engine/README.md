# India Equity Engine

Patient swing + intraday trading engine for Indian equities on **Zerodha (Kite Connect)**.
Equity delivery (CNC) + intraday (MIS) only. **No futures. Options rarely (major events).**
Nifty-50 blue-chip universe. **Paper/POC only** until a formal go-live gate is passed.

> Read **`CLAUDE.md`** first — it is the founding spec (rules, cost model, architecture, phases).
> Then **`PHASE0_TASKS.md`** for the current work.

## Quickstart

```bash
git clone <this-repo> && cd india_equity_engine
uv sync                      # create venv + install deps (uses uv.lock)
cp .env.example .env         # then fill in your Kite credentials
uv run pytest -q             # run the test suite
uv run ruff check && uv run ruff format --check && uv run mypy src
```

## Kite Connect setup (manual, one-time)

1. Create an app at <https://developers.zerodha.com/> (paid). Note `api_key` / `api_secret`.
2. Put them in `.env` as `KITE_API_KEY` / `KITE_API_SECRET`.
3. Run the auth flow (see `india_engine.data.kite_auth`) to get a daily `access_token`.
   Tokens expire each day; the engine refreshes/re-prompts. **Never commit tokens.**

## Ground rules (summary — full list in CLAUDE.md)

- Paper-first; no real orders until the go-live gate (CLAUDE.md §10) + explicit human yes.
- Ledger-first, append-only; reconciliation from day 1.
- Full Indian cost model netted into every backtest/fill.
- Tests + CI green before any merge. No secrets in git. TLS verified.
- Leverage: SEBI cap is **5× intraday**, **1× delivery**. There is no 11×.

## Status

**Phase 0 — Foundation** (see `PHASE0_TASKS.md`). Later phases: 1 lab/backtest ·
2 paper engine · 3 execution spine · 4 go-live gate · 5 scale.
