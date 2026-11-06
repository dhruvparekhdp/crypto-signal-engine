# Handoff AUD-2

Read-only baseline of crypto-signal-engine. The only deliverable is `docs/audit/crypto-baseline.md`. No trading code, settings, or dependencies were changed.

## Summary

The process started by `main.py` paper-trades a 4h/8h swing book and keeps the older 15-minute detectors in shadow. It can place, amend, and cancel **Delta Exchange India** orders when `delta_india_mode` is `live` and `delta_india_live_orders` is true. Both default off, and both can be flipped from the database without a code change. This audit did not read production.

The **Binance USD-M** client in `execution/binance_futures.py` can POST `/fapi/v1/order`, but `main.py` and `scheduler/runner.py` never construct it. `live_trading_mode=off` is only displayed. Flipping it does not enable Binance orders.

There is no withdrawal or transfer call. The HTTP server is aiohttp on `0.0.0.0:$PORT` with a large unauthenticated surface. Proposed home for a read-only Jarvis API: new module `scheduler/integration_v1.py`, prefix `/integration/v1/`, fail-closed bearer, registered from `make_app`, and forbidden from importing `execution`.

## Files changed

- `docs/audit/crypto-baseline.md` — the audit (architecture, data, scheduler, strategies, backtests, signal audit, notifications, AI, database, every order site, live-trading gates, HTTP routes, env names, tests, findings AUD2-1–AUD2-10, `/integration/v1` placement).
- `docs/handoff/AUD-2.md` — this note.

## Commands run and results

| Command | Result |
|---|---|
| `/tmp/cse-venv/bin/python -m pytest -q --tb=line` (venv with `requirements-dev.txt`, Python 3.12.3 / pytest 9.2.0) | **2082 passed, 3 skipped, 24 warnings, 1208 subtests passed, exit 0**, 77.40 s. Skips: missing `backups/neon_backup_latest.json.gz` (2 tests) and missing local lake (`test_live_check_matches_the_backtest_series`). |
| `ruff check .` (ruff 0.17.4) | **Exit 1, 6065 pre-existing findings.** 5735 are `E501`. Not introduced by this branch (no Python edits). Not auto-fixed. |
| `mypy` on `config scheduler execution analysis storage collectors notifications main.py` (mypy 2.4.1, repo `pyproject.toml` config) | **Exit 1, 253 pre-existing errors.** Not introduced by this branch. Not edited. |

The virtualenv was `/tmp/cse-venv`. `python3.12-venv` was installed on the machine so the suite could run. Nothing was added to `requirements.txt` or `pyproject.toml`.

## Assumptions

- Code defaults are the baseline. Production `app_settings` can override `delta_india_mode` and `delta_india_live_orders`. Not queried.
- `.env` was not opened. Names only.
- "Main engine" means `python main.py`. Scripts under `scripts/delta_*.py` are operator processes. The rehearsal and testnet scripts refuse a non-testnet URL; they still send order calls when someone runs them.
- `docs/INTEGRATION_PLAN.md` findings L1–L11 were not re-scored. HANDOFF says phase 1 of that list was deployed. Current line numbers in the audit are from `2429e2c`.

## Known gaps

- No check of the live EC2 process, its database, or its `/keys` rows. AUD2-1 stays "latent" until someone reads `delta_india_mode` and `delta_india_live_orders` on the box (`GET /api/portal/meta` shows them).
- Ruff and mypy are red on `main`. Cleaning them was out of scope.
- `pyproject.toml` lists `ddgs`; `requirements.txt` does not. Tests passed without it. The import is optional.
- Three tests skip without the backup gzip and the Binance lake. They were not forced green.
- `india_equity_engine/` and `ios-port/` were not audited beyond confirming `main.py` does not start them.

## Security impact

Documentation only. No new endpoint, no change to order code, no secret written.

The audit records existing risk: Delta orders are reachable from the scheduler when two flags are on (AUD2-1); `POST /api/delta/shadow-now` mutates those flags with no auth (AUD2-2); simulator start/save/config are unauthenticated and can store caller-supplied keys (AUD2-3); three mutating routes fail open when `API_AUTH_TOKEN` is empty (AUD2-4); the HTTP port is broadly readable, including a redacted database dump (AUD2-5); database TLS is unverified under `sslmode=require` (AUD2-7). Binance USD-M order methods are not on the engine call graph (AUD2-8).

## Rollback

Delete the branch, or revert the two docs commits. No schema, config, or runtime change to undo.

```bash
git checkout main
git branch -D jarvis/AUD-2
git push origin --delete jarvis/AUD-2
```
