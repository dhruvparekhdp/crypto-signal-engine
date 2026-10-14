"""End-to-end order flow on Delta India TESTNET (fake money): open -> exchange stop + target -> ladder amend ->
reconcile -> engine close -> flat. Every step is checked against the exchange's own records.

    PYTHONPATH=. python scripts/delta_testnet_flow.py            # keys from /keys (delta_india_testnet_*)

Refuses to run unless the base URL is the testnet. Always cleans up (closes and cancels) at the end.
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

import httpx

SYMBOL = "xrpusdt"


async def main() -> int:
    from config.overrides import apply
    from config.settings import settings
    from execution.delta_book import DeltaBook
    from execution.delta_india import USER_AGENT, DeltaIndia
    try:
        from scheduler.keys_page import _stored
        apply(settings, await _stored())
    except Exception as e:  # noqa: BLE001 - running without the database: env / .env keys only
        print("note: keys from env only:", str(e)[:80])
    base = settings.delta_india_testnet_url
    if "testnet" not in base:
        print("REFUSED: base URL is not a testnet:", base)
        return 2
    k, s = settings.delta_india_testnet_api_key, settings.delta_india_testnet_api_secret
    if not k or not s:
        print("No testnet key saved: add 'Delta India TESTNET key/secret' on /keys")
        return 2
    results: list[tuple[str, bool, str]] = []

    def step(name, ok, detail=""):
        results.append((name, bool(ok), detail))
        print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")

    tmp = Path(tempfile.mkdtemp(prefix="delta_testnet_"))
    async with httpx.AsyncClient(timeout=20, headers={"User-Agent": USER_AGENT}) as http:
        client = DeltaIndia(k.get_secret_value(), s.get_secret_value(), base_url=base, client=http)
        book = DeltaBook(client, mode="live", live_orders=True, risk_pct=0.01, max_open=1, usd_inr=86.0,
                         shadow_path=tmp / "log.jsonl", state_path=tmp / "state.json", backstop_r=0.5)
        product = None
        try:
            inr = await client.inr_available()
            step("1 auth + wallet", True, f"INR available {inr:.2f}")
            product = await client.product("XRPUSD")
            step("2 product XRPUSD", product is not None, f"id {product.id if product else '-'}")
            t = (await http.get(f"{base}/v2/tickers/XRPUSD")).json().get("result") or {}
            px = float(t.get("mark_price") or 0)
            step("3 mark price", px > 0, f"{px}")
            entry, stop, target = px, px * 1.05, px * 0.85           # short: stop +5%, target -15% (3R)
            plan = await book.on_paper_open(SYMBOL, "short", entry, stop, target, "testnet_flow")
            step("4 open short (entry + stop + target)", plan.ok, f"{plan.size} contracts, reason={plan.reason}")
            if not plan.ok:
                return 1
            orders = await client.open_orders(product.id)
            kinds = sorted(str(o.get("stop_order_type")) for o in orders)
            step("5 exchange shows stop + target", "stop_loss_order" in kinds and "take_profit_order" in kinds,
                 f"{kinds}")
            size = await client.position_size(product.id)
            step("6 exchange position", size == -plan.size, f"size {size}")
            # a realistic first ladder step: the engine moves its stop to break-even, so the exchange backstop
            # goes 0.5R behind it (between the market and the original stop, never at the market price)
            lock = entry
            res = await book.on_stop_change(SYMBOL, lock)
            orders = await client.open_orders(product.id)
            sl = [float(o.get("stop_price") or 0) for o in orders if o.get("stop_order_type") == "stop_loss_order"]
            step("7 ladder amend moved the exchange stop", res.get("reason") == "amended" and sl
                 and abs(sl[0] - res["exchange_stop"]) <= (product.tick_size or 1e-4) * 1.01,
                 f"want {res.get('exchange_stop')} on exchange {sl} | {res} | log: "
                 + (tmp / "log.jsonl").read_text()[-600:].replace(chr(10), " ") if (tmp / "log.jsonl").exists() else "")
            ev = await book.reconcile()
            step("8 reconcile clean", ev == [], f"{ev}")
            res = await book.on_paper_close(SYMBOL, "testnet_flow")
            await asyncio.sleep(1.5)
            size = await client.position_size(product.id)
            left = await client.open_orders(product.id)
            step("9 engine close -> flat, no leftover orders", res.get("ok") and size == 0 and not left,
                 f"size {size}, open orders {len(left)}")
        except Exception as e:  # noqa: BLE001
            step("unexpected error", False, str(e)[:200])
        finally:
            if product is not None:
                try:
                    size = await client.position_size(product.id)
                    if size:
                        await client.place_market(product.id, "buy" if size < 0 else "sell", abs(size), reduce_only=True)
                    await client.cancel_all(product.id)
                except Exception as e:  # noqa: BLE001
                    print("cleanup warning:", str(e)[:120])
    passed = sum(ok for _, ok, _ in results)
    print(f"\n{passed}/{len(results)} steps passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
