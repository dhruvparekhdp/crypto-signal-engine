# Real-money readiness: Delta India (review of main @ 2cb90de, 9 Oct 2026)

**Verdict: NOT ready for real orders, not even ₹500.** The strategy code, signing, endpoints and the
two live gates are fine. The order path around them is not safe yet: it can leave a real position
with no stop, it never closes or reconciles, and nobody is told when something goes wrong.

Once P0 is done and a testnet run passes, ₹500 is a **plumbing test only**. It cannot say anything
about the edge (see S-1).

> **REAL MONEY rule (HANDOFF.md):** any change to `execution/`, Delta keys or order placement needs a
> strict REAL MONEY alert to the owner and an explicit yes before it ships. Never push to `main`
> without approval, because it auto-deploys.

Test suite on 2cb90de: **1601 passed, 3 skipped, 0 failed.**

---

## P0: must fix before any real order

| # | Problem | Where | Fix |
|---|---|---|---|
| DX-1 | Entry, stop and TP are 3 separate orders. If the stop or TP is rejected, or httpx times out after the fill, the result is a **naked position recorded nowhere**. Only `DeltaError` is caught. | `execution/delta_book.py` ~149-163 | Send `bracket_stop_loss_price` and `bracket_take_profit_price` on the entry order (Delta treats them as OCO). Add a `client_order_id`. Catch `Exception`. After any error, query positions; if one exists without confirmed protection, flatten it at once (same pattern as `execution/live_book.py:186-195`). Alert at error level. |
| DX-2 | Stop and TP prices are not rounded to `tick_size`. `tick_size` is loaded but never used, and prices are sent as `f"{x:.8f}"`. | `execution/delta_india.py` | Round to tick: stop away from entry, TP toward entry. Add a unit test. |
| DX-3 | No lifecycle. Paper closes and the 7-day expiry never reach Delta. `state['positions']` is never removed, so after 3 trades the book is permanently "full" and nothing says so. Leftover stop/TP legs stay resting. | `delta_book.py:121,124,163`, `scheduler/runner.py` `_mirror_delta_india` | Add a `close(symbol)` hook, called from the paper close and expiry paths. It sends a reduce-only market order, then `cancel_all(product_id)`, then removes the state entry. |
| DX-4 | No reconciliation. `positions()` has no caller and swallows errors, so it would *fail open*. | `delta_india.py` | Add a job every 1-5 min that compares exchange positions and orders with `delta_state.json`. An unknown position means alert and flatten. A position missing on the exchange means drop the state and alert. **On an API error, do nothing and alert.** Never treat an error as "no position". |
| DX-5 | Leverage and margin mode are never set. The account default (can be 100-200x) can put the liquidation price **inside** the 3xATR stop. | `delta_india.py` | Before entry, `POST /v2/products/{id}/orders/leverage` with isolated margin and low leverage (e.g. 5x). Check liquidation price is beyond the stop, otherwise skip. |
| DX-6 | Wallet is probably read as ₹0. `inr_available()` only reads INR/RUPEE rows, but the Delta India wallet may report in USD, so every signal becomes `below_one_contract`. Shadow sizing uses `max(inr, 5000)`, which hides this. | `delta_india.py`, `delta_book.py` | Parse the USD balance × rate. Remove the 5000 floor, or log it loudly. |
| DX-7 | At ₹500 and 1% risk (₹5) almost no signal reaches one contract, so the test may place **zero orders**, or only on one cheap coin. | sizing | Decide on purpose: for the plumbing test, allow min-1-contract only if the 1-contract risk is ≤ an explicit ₹ cap (e.g. ₹25), and log every skip. |
| OPS-3 | No Telegram alert for Delta fills or failures. The paper "trade opened" message is sent even when the real order failed. | runner | Send separate Delta messages: filled, protected (stop+TP ids), rejected, flattened, closed. |
| OPS-6 | No kill switch or daily-loss limit for Delta. DB settings override `.env`, so an SSH/.env kill does not work. | settings, `delta_book.py` | Add a kill flag that blocks new orders and flattens everything. Reachable from Telegram and the admin page. Add a daily-loss stop. |
| SEC-1 | `POST /api/delta/shadow-now` is **unauthenticated**. It flips the global live gates and then restores old values, which can undo a kill. `GET /api/delta` is also open. | `scheduler/delta_api.py` | Require admin. Never mutate global flags: pass shadow as a parameter. |
| SEC-2 | Admin cookie travels over plain HTTP on a public IP (52.62.37.4:8080). `delta_india_base_url` is free text, so a hijacked session can point signed requests at another host. Other writes (settings toggle, watchlist) are open when `API_AUTH_TOKEN` is empty. | portal, settings | Put the portal behind Tailscale or Caddy+TLS and close 8080 to the world. Whitelist the base URL to the 2 known Delta hosts. Make `API_AUTH_TOKEN` mandatory. |
| OPS-9 | Trading keys are IP-whitelisted. If the EC2 public IP changes, every order fails. | infra | Attach an **Elastic IP** and whitelist only that. |
| TN-1 | The order path has **never run** against the exchange. | — | Write `scripts/delta_testnet_smoke.py` (testnet `https://cdn-ind.testnet.deltaex.org`, keys from demo.delta.exchange). It should open a bracket, verify the stop and TP exist, run reconcile, close, verify flat, then force a stop rejection and verify flatten. Must pass before live. |

## P1: fix soon (first week of live)

- **S-1 Data drift.** `delta_only_mode=True`, so signals and paper exits now run on Delta India bars. The 5-year validation used Binance USD-M. Forward R from before and after 8 Oct is mixed under one `live_from`. Fix: bump `live_from` for the swing specs, and run a quick backtest or parity check of Delta vs Binance 4h bars.
- **S-2** The 4h→8h aggregation doesn't sort bars first, so the result depends on the order Delta returns rows. Sort by open time.
- **S-3** The perp price cache has no age limit (`runner.py` ~980-993). Reject prices older than about 2 bars and alert.
- **S-4** Before entry, check the stop is still valid against the current Delta mark (a fast move after the bar close puts it on the wrong side, so it gets rejected).
- **OPS-4 Dead-man switch.** The heartbeat only logs, and the health check runs on the part-time ThinkPad/Mac. Add an external ping (e.g. healthchecks.io free tier) from EC2 every 5 min.
- **OPS-5** Every push to `main` restarts the trader with `shutdown(wait=False)` and no drain, and CI deploys `origin/main` rather than the tested SHA. Fix: graceful stop (finish the in-flight order), `TimeoutStopSec`, deploy the tested SHA.
- **OPS-8** Live switches load once at startup. If the DB is down then, live stays off silently. Alert when that happens.
- **OPS-10** Orders are signed with the local clock (5 s window). Check chrony/NTP offset at start and alert.
- **OPS-11** Send a daily Delta P&L and position summary on Telegram.
- `delta_india_usd_inr` is 83; Delta uses a fixed 85. Read the rate from the exchange or set 85.
- `reduce_only` is sent as a bool; confirm the API wants the string `"true"`. `cancel_all` sends `product_id` as a query param; confirm it belongs in the body.
- Paper and Delta risk differ (3% vs 1%), so paper ROE cannot be used to judge the real leg. Report the Delta leg in R.

## P1: portal / accounting bugs (the "small silly mistakes")

| Bug | Cause | Fix |
|---|---|---|
| **NET / unrealised** ignores the entry fee and funding | `analysis/paper_trading.py` `unrealised()` ~892 returns `gross_pnl(mark) - exit_fee` only | Subtract `pos.entry_fee` and accrued funding so far. |
| **Wallet** doesn't drop when a trade opens | entry fee computed at open (~1002) but charged only in `close_position` (~1095) | Show the entry fee in equity while the trade is open (or debit at open and don't double-count at close). |
| **COSTS ₹0** | `scheduler/portal/hubs/book.js:87` sums `trading_fees + funding_paid` from **closed** trades only | Add open-position entry fees + accrued funding. Label it "paid + accrued". |
| **Funding is fake** | a constant `0.0000655` per 8 h (`analysis/instruments.py:45`), charged only at close, same sign for long and short | Use Delta's real funding rate per product, accrued at each 00/08/16 UTC. Longs pay positive funding and shorts receive it. |
| Fees don't include GST | Delta charges 18% GST on fees | Fee = 0.05% taker / 0.02% maker × 1.18. |
| **TARGET ₹1,00,00,000** | `paper_target_wallet=10_000_000` is a sentinel (profit is swept as a withdrawal) | Show the next withdrawal milestone, or hide the card. |
| **FEED 17h ago** | `collectors/delta_market.py:176` calls `store.update_price`, which `CryptoStateStore` doesn't have; the error is swallowed at debug. `replace_candles` doesn't set the timestamp. | Use the right setter, set `timestamp` on candle replace, and log at warning. |
| WORST CASE −9.0% | correct: 3 shorts × 3% under the 9% side cap (`swing_max_side_risk_pct`), by design | Nothing to fix. Consider a tooltip. |

## Exits: trailing stop question (answer: don't add one now)

From the lab exit test (`docs/BACKTEST_REPORT.md:61-73`, trial ledger runs `d90b39d7b69f75dc` 4h and
`b33ab7dd20d1bd4f` 8h, numbers re-checked):

- The current exit (3xATR stop, 3R target, 7-day limit) had the best mean R on **all 4 specs at 8h** and 2 of 4 at 4h.
- Every breakeven, trail or scale-out variant **lost** on all 8 specs. For example, BE at 1R plus a 3xATR trail: 4h +0.169 and 8h +0.282, against +0.209 and +0.314 now. "The money is in the few trades that run to 3R."
- Only a tighter 1.75xATR stop (no trail) won, narrowly, on 4h ichimoku and 4h vol_breakout.
- The open shorts are at about +0.8-0.9R. Giving back unrealised profit at that level is normal for a 3R-target system.
- Not yet tested: BE at 1.5-2R **with the 3R target kept**, or a 4xATR chandelier (EX-2). The exit runs also used unfiltered trades (EX-3). If wanted, test those in the lab first. Any exit change must bump `live_from` (EX-5).
- Small bug: the trial ledger records `trades=0` for every config because of a wrong column name (EX-4).

## Strategy readiness (ROADMAP §7 go-live checklist)

- There are about **0 closed forward trades** in cycle 2, which started 7 Oct. Forward trust needs ≥30 trades and t ≥ 1.645.
- Checklist items not met for Delta: testnet run, reconciliation, kill switch, two clean weeks.
- ₹500 at 1% risk is ₹5 per trade. Even 10 real trades cannot confirm or reject the edge.

## Tax note

Delta says INR-settled crypto futures are "not VDA, so not 30%". That is Delta's position, and the law
is not settled. Get a CA's view, keep full trade records, and budget for the 30% + 1% TDS worst case.

## Verified OK (don't redo)

- HMAC signing (method + timestamp + path + query + body), endpoints, field names and fee rates.
- Two live gates (`delta_india_mode`, `delta_india_live_orders`) are off by default.
- Keys are never returned or logged, and `/api/tables` redacts them. Keys have no withdraw permission.
- `data/` survives `git reset --hard`. `delta_state.json` is written atomically.
- CI test gate blocks the deploy. `/health` is polled after restart. systemd `Restart=always`. Crash report on the next start. journald is capped.
- Phase-1 fixes from INTEGRATION_PLAN are real: L1, L2 (partial), L3, L4, L6-L9, L11.

## Suggested order

1. SEC-1, SEC-2, OPS-9 (Elastic IP, Tailscale/TLS). No real-money risk, so do them now.
2. Accounting/portal bugs (fees, funding, COSTS, FEED, TARGET).
3. DX-1, DX-2, DX-5, DX-6, DX-7, then DX-3, DX-4, OPS-3, OPS-6, each with tests that include the failure paths.
4. TN-1 testnet smoke passes 3 times in a row, plus 3-5 days of shadow mode with reconcile running clean.
5. REAL MONEY alert to the owner, then ₹500 with `delta_india_max_open=1` and a ₹ loss cap. Watch the first 5 fills by hand.
