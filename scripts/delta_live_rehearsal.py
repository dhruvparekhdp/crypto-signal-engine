"""Go-live rehearsal on Delta India TESTNET (fake money, 17 Oct 2026): every safety path a real Rs500 trade can take,
checked against the exchange's own records, short and long.

    PYTHONPATH=. python scripts/delta_live_rehearsal.py [ADAUSD]      # on the server (testnet keys live there)

Refuses to run unless the base URL is the testnet or when the contract already has a demo position. Always cleans up
(closes, cancels, restores the contract's leverage). Writes data/delta_rehearsal.json.
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

import httpx


async def main() -> int:
    from config.overrides import apply
    from config.settings import settings
    from execution.delta_book import DeltaBook, leverage_for
    from execution.delta_india import USER_AGENT, DeltaIndia, binance_to_delta_symbol
    from scheduler.keys_page import _stored
    apply(settings, await _stored())
    base = settings.delta_india_testnet_url
    if "testnet" not in base:
        print("REFUSED: not a testnet URL:", base)
        return 2
    dsym = (sys.argv[1] if len(sys.argv) > 1 else "ADAUSD").upper()
    bsym = dsym.replace("USD", "usdt").lower()
    assert binance_to_delta_symbol(bsym) == dsym
    results: list[tuple[str, bool, str]] = []

    def step(name, ok, detail=""):
        results.append((name, bool(ok), str(detail)[:300]))
        print(f"[{'PASS' if ok else 'FAIL'}] {name} {str(detail)[:300]}", flush=True)

    tmp = Path(tempfile.mkdtemp(prefix="rehearsal_"))
    async with httpx.AsyncClient(timeout=20, headers={"User-Agent": USER_AGENT}) as http:
        c = DeltaIndia(settings.delta_india_testnet_api_key.get_secret_value(),
                       settings.delta_india_testnet_api_secret.get_secret_value(), base_url=base, client=http)

        def book(**kw):
            b = DeltaBook(c, mode="live", live_orders=True, risk_pct=0.005, max_open=3, usd_inr=85.0,
                          shadow_path=tmp / "log.jsonl", state_path=tmp / "state.json", backstop_r=0.5, **kw)
            b.retry_delay_s = 1.0
            return b

        p = await c.product(dsym)
        if p is None:
            print("no product", dsym)
            return 2
        if await c.position_size(p.id):
            print(f"{dsym} already has a demo position; refusing")
            return 2
        lev_before = (await c._request("GET", f"/v2/products/{p.id}/orders/leverage")).get("leverage")

        async def mark() -> float:
            t = (await http.get(f"{base}/v2/tickers/{dsym}")).json().get("result") or {}
            return float(t.get("mark_price") or 0)

        async def exchange_orders():
            return await c.open_orders(p.id)

        try:
            inr, rate = await c.wallet_inr()
            total = await c.wallet_total_inr()
            step("1 auth + wallet", inr > 0 and total >= inr, f"available Rs{inr:,.0f}, whole wallet Rs{total:,.0f}, rate {rate}")
            px = await mark()
            step("2 product + mark", px > 0, f"{dsym} id {p.id} contract {p.contract_value} tick {p.tick_size} mark {px}")

            # --- guards refuse before any order
            r = await book(kill_switch=True).on_paper_open(bsym, "short", px, px * 1.05, px * 0.85, "rehearsal")
            step("3 kill switch blocks the order", not r.ok and r.reason == "kill_switch" and not await c.position_size(p.id), r.reason)
            r = await book(min_wallet_inr=total * 10).on_paper_open(bsym, "short", px, px * 1.05, px * 0.85, "rehearsal")
            step("4 wallet floor blocks the order", not r.ok and r.reason == "wallet_floor" and not await c.position_size(p.id), r.reason)
            b = book(daily_loss_pct=0.10)
            st = b._load_state()
            st["day"] = {"date": time.strftime("%Y-%m-%d", time.gmtime()), "start_inr": total * 1.5}   # "down 33% today"
            b._save_state(st)
            r = await b.on_paper_open(bsym, "short", px, px * 1.05, px * 0.85, "rehearsal")
            step("5 daily loss limit blocks the order", not r.ok and r.reason == "daily_loss_limit" and not await c.position_size(p.id), r.reason)
            (tmp / "state.json").unlink(missing_ok=True)

            for side in ("short", "long"):
                s = 1 if side == "long" else -1
                b = book(daily_loss_pct=0.10)
                px = await mark()
                stop, target = px * (1 - s * 0.05), px * (1 + s * 0.15)
                plan = await b.on_paper_open(bsym, side, px, stop, target, "rehearsal")
                step(f"{side}: 6 open (entry + stop + target)", plan.ok, f"{plan.size} contracts, leverage {plan.leverage}x, reason={plan.reason}")
                if not plan.ok:
                    continue
                lev = (await c._request("GET", f"/v2/products/{p.id}/orders/leverage")).get("leverage")
                step(f"{side}: 7 exchange leverage = planned", str(lev).split(".")[0] == str(plan.leverage) and plan.leverage == leverage_for(0.05),
                     f"exchange {lev}x, planned {plan.leverage}x")
                liq = await c.liquidation_price(p.id)
                entry = float(json.loads((tmp / "log.jsonl").read_text().splitlines()[-1]).get("entry_order", {}).get("average_fill_price") or px)
                beyond = liq is not None and ((s < 0 and liq > stop) or (s > 0 and liq < stop))
                step(f"{side}: 8 liquidation beyond the stop", beyond,
                     f"entry {entry} stop {stop:.5f} liquidation {liq} ({(liq / entry - 1) if liq else 0:+.1%} from entry)")
                orders = await exchange_orders()
                kinds = sorted(str(o.get("stop_order_type")) for o in orders)
                ro = all(bool(o.get("reduce_only")) for o in orders)
                step(f"{side}: 9 stop + target on the exchange, reduce-only", "stop_loss_order" in kinds and "take_profit_order" in kinds and ro,
                     f"{kinds} reduce_only={ro}")
                size = await c.position_size(p.id)
                step(f"{side}: 10 exchange position size", size == s * plan.size, f"exchange {size}, ours {s * plan.size}")
                res = await b.on_stop_change(bsym, px)                      # ladder: lock at break-even
                orders = await exchange_orders()
                sl = [float(o.get("stop_price") or 0) for o in orders if o.get("stop_order_type") == "stop_loss_order"]
                step(f"{side}: 11 ladder move -> exchange stop moved (backstop 0.5R behind the lock)",
                     res.get("reason") == "amended" and sl and abs(sl[0] - res["exchange_stop"]) <= (p.tick_size or 1e-4) * 1.01,
                     f"lock {px:.5f} -> exchange {sl} | {res.get('reason')}")
                ev = await b.reconcile()
                step(f"{side}: 12 reconcile clean", ev == [], ev)
                sid = b._load_state()["positions"][dsym]["stop_order_id"]
                await c.cancel_order(sid, p.id)                              # simulate a stop that vanished
                ev = await b.reconcile()
                orders = await exchange_orders()
                has_sl = any(o.get("stop_order_type") == "stop_loss_order" for o in orders)
                step(f"{side}: 13 missing stop is restored by reconcile", any(e.get("event") == "stop_restored" for e in ev) and has_sl, ev)
                res = await b.on_paper_close(bsym, "rehearsal")
                await asyncio.sleep(1.5)
                left, size = await exchange_orders(), await c.position_size(p.id)
                step(f"{side}: 14 engine close -> flat, no leftover orders", res.get("ok") and size == 0 and not left,
                     f"size {size}, open orders {len(left)}")
        except Exception as e:  # noqa: BLE001
            step("unexpected error", False, repr(e))
        finally:
            try:
                size = await c.position_size(p.id)
                if size:
                    await c.place_market(p.id, "buy" if size < 0 else "sell", abs(size), reduce_only=True)
                await c.cancel_all(p.id)
                await c._request("POST", f"/v2/products/{p.id}/orders/leverage", body={"leverage": str(lev_before)})
                print("cleanup: size", await c.position_size(p.id), "| leverage restored to", lev_before)
            except Exception as e:  # noqa: BLE001
                print("cleanup warning:", repr(e)[:160])
    passed = sum(ok for _, ok, _ in results)
    print(f"\n{passed}/{len(results)} steps passed")
    Path("data").mkdir(exist_ok=True)
    Path("data/delta_rehearsal.json").write_text(json.dumps(
        {"at": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()), "contract": dsym, "passed": passed,
         "steps": len(results), "results": [{"step": n, "ok": ok, "detail": d} for n, ok, d in results]}))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
