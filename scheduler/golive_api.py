"""
Go-live dashboard data (owner, 15 Oct 2026): every check that stands between the demo and the Rs500 real trial,
read live from the server.

    GET /api/golive      verdict, real-money gates, system checks (cached 20 s)

The verdict only says whether the engineering gates are met. Real orders still need delta_india_mode=live, the
second gate (delta_india_live_orders), and the owner's explicit yes after the REAL MONEY alert.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from aiohttp import web

TESTNET_JOURNAL = Path("data/delta_testnet.jsonl")
TESTNET_STATE = Path("data/delta_testnet_state.json")
STOP_TEST = Path("data/delta_stop_test.json")       # written by the demo stop-fill test
DAY_MS = 86_400_000
MIN_DEMO_DAYS = 5
_cache: dict = {"t": 0.0, "body": None}


def _tail_jsonl(path: Path, max_rows: int = 2000) -> list[dict]:
    try:
        lines = path.read_text().splitlines()[-max_rows:]
    except OSError:
        return []
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except ValueError:
            continue
    return out


def journal_stats(rows: list[dict], now_ms: int) -> dict:
    """What the demo account has actually done, from the order journal."""
    opens = [r for r in rows if r.get("placed") and r.get("entry_order")]
    amends = [r for r in rows if r.get("event") == "amend_stop" and r.get("placed")]
    closes = [r for r in rows if r.get("event") == "close" and r.get("placed")]
    on_exchange = [r for r in rows if r.get("event") == "closed_on_exchange"]
    restored = [r for r in rows if r.get("event") == "stop_restored"]
    errors_24h = [r for r in rows if r.get("error") and now_ms - int(r.get("t") or 0) <= DAY_MS]
    first = min((int(r["t"]) for r in rows if r.get("t")), default=None)
    return {"opens": len(opens), "amends": len(amends), "engine_closes": len(closes),
            "exchange_closes": len(on_exchange), "stops_restored": len(restored),
            "errors_24h": len(errors_24h), "last_error": (errors_24h[-1].get("error", "")[:160] if errors_24h else ""),
            "days_running": round((now_ms - first) / DAY_MS, 1) if first else 0.0,
            "last": [{k: r.get(k) for k in ("t", "event", "delta_symbol", "side", "size", "reason", "placed", "error")}
                     for r in rows[-12:]]}


def evaluate(s: dict) -> dict:
    """Pure: the snapshot of the server's state -> verdict, gates and checks. `s` keys documented in _snapshot."""
    j = s["journal"]
    gates, checks = [], []

    def gate(name, ok, detail, who):
        gates.append({"name": name, "status": "ok" if ok is True else ("wait" if ok is None else "fail"),
                      "detail": detail, "who": who})

    def check(area, name, status, detail):
        checks.append({"area": area, "name": name, "status": status, "detail": detail})

    gate("Tax view from the CA", True, "Owner, 15 Oct: fine up to Rs1-2 lakh per FY while Delta deducts no 1% TDS "
         "(TDS report empty)", "owner")
    guard_on = s["daily_loss_pct"] > 0 and s["min_wallet_inr"] > 0
    gate("Automatic loss limit live", guard_on if s["guard_deployed"] else False,
         (f"no new orders after -{s['daily_loss_pct']:.0%} in a day or below Rs{s['min_wallet_inr']:,.0f}"
          if s["guard_deployed"] else "not deployed yet"), "claude")
    st = s.get("stop_test") or {}
    stop_seen = j["exchange_closes"] > 0 or bool(st.get("passed"))
    gate("Delta's own stop has filled a demo trade", stop_seen,
         (f"{j['exchange_closes']} exchange-side closes in the demo journal" if j["exchange_closes"]
          else ("stop-fill test passed " + str(st.get("at", "")) if st.get("passed") else "not seen yet - run the demo stop test")),
         "claude")
    gate(f"Demo running {MIN_DEMO_DAYS}+ days", j["days_running"] >= MIN_DEMO_DAYS,
         f"{j['days_running']} days, {j['opens']} opens, {j['amends']} stop moves, {j['engine_closes']} closes", "system")
    gate("No demo order errors in 24 h", j["errors_24h"] == 0,
         "clean" if j["errors_24h"] == 0 else f"{j['errors_24h']} errors: {j['last_error']}", "system")
    # a saved key proves nothing about its permissions (the 13 Oct key is read-only): green only once live orders run
    gate("Real-money key with trading permission (IP 52.62.37.4)", True if (s["live_key_set"] and s["mode"] == "live"
                                                                            and s["live_orders"]) else None,
         ("a key is saved - owner confirms it has trading permission and the 52.62.37.4 whitelist (the 13 Oct key "
          "was read-only)") if s["live_key_set"]
         else "owner: create on Delta India with trading permission, whitelist 52.62.37.4, save on /keys", "owner")
    w = s.get("live_wallet_inr")
    gate("Rs500 deposited", (w >= 450) if isinstance(w, (int, float)) else None,
         f"live wallet Rs{w:,.0f}" if isinstance(w, (int, float)) else "live wallet not readable yet", "owner")
    live_now = s["mode"] == "live" and s["live_orders"]
    gate("REAL MONEY alert + owner's explicit yes", True if live_now else None,
         "live orders ON" if live_now else "last step: Claude sends the alert, owner replies yes", "both")

    # --- system checks
    check("engine", "Server responding", "ok", f"up {s['uptime_s'] // 3600}h {s['uptime_s'] % 3600 // 60}m")
    check("delta", "Delta book mode", "ok" if s["mode"] in ("testnet", "shadow", "live") else "warn",
          f"{s['mode']}" + (" · live orders ON" if s["live_orders"] else " · live orders OFF"))
    check("delta", "Kill switch", "warn" if s["kill_switch"] else "ok", "ON - no new orders" if s["kill_switch"] else "off")
    check("delta", "Demo positions", "ok", f"{s['demo_open']} open on the demo account")
    hf = s.get("hf") or {}
    if hf.get("error"):
        check("ai", "HF Space", "fail", hf["error"][:120])
    else:
        slow = (hf.get("last_seconds") or 0) > 60
        check("ai", "HF Space", "warn" if slow or not hf.get("classifier_loaded") else "ok",
              f"FinBERT {'loaded' if hf.get('classifier_loaded') else 'NOT loaded'} · analyst model "
              f"{hf.get('last_seconds', '?')} s per answer" + (" (too slow for live use)" if slow else ""))
    for m in s.get("models") or []:
        served, skipped = m.get("served", 0), m.get("skipped", 0)
        bad = served == 0 or (skipped > 3 * max(served, 1))
        check("ai", m.get("model", "?"), "warn" if bad else "ok",
              f"served {served} · skipped {skipped}" + (f" · {str(m.get('last_error'))[:80]}" if m.get("last_error") else ""))
    n6, src24, fut = s["news_6h"], s["news_sources_24h"], s["news_future"]
    check("news", "Fresh headlines (6 h)", "ok" if n6 >= 5 else ("warn" if n6 else "fail"), f"{n6} headlines")
    check("news", "Sources seen (24 h)", "ok" if len(src24) >= 4 else "warn",
          ", ".join(f"{k} {v}" for k, v in sorted(src24.items(), key=lambda kv: -kv[1])) or "none")
    check("news", "Future-dated items", "ok" if fut == 0 else "warn",
          "none" if fut == 0 else f"{fut} stored (ignored by the score since 15 Oct fix)")
    fam = s["families"]
    check("book", "Books opening paper trades", "warn" if "intraday" in fam else "ok",
          fam + (" · intraday detectors lose in the audit (-0.3R per signal); swing only recommended"
                 if "intraday" in fam else ""))

    engineering = [g for g in gates if g["who"] in ("claude", "system")]
    if any(g["status"] == "fail" for g in engineering):
        verdict, tone = "NOT READY", "fail"
    elif any(g["status"] != "ok" for g in gates):
        verdict, tone = "ENGINEERING READY · OWNER STEPS LEFT", "wait"
    else:
        verdict, tone = "LIVE", "ok"
    return {"verdict": verdict, "tone": tone, "gates": gates, "checks": checks, "journal": j}


async def _snapshot(runner) -> dict:
    import httpx

    from config.settings import settings
    now_ms = int(time.time() * 1000)
    rows = _tail_jsonl(TESTNET_JOURNAL)
    try:
        state = json.loads(TESTNET_STATE.read_text())
    except (OSError, ValueError):
        state = {}
    try:
        stop_test = json.loads(STOP_TEST.read_text())
    except (OSError, ValueError):
        stop_test = None
    base = (settings.hf_base_url or "").rstrip("/")
    hf = {} if base else {"error": "HF Space URL not set"}
    if base:
        try:
            tok = settings.hf_api_token.get_secret_value() if settings.hf_api_token else ""
            async with httpx.AsyncClient(timeout=6) as c:
                r = await c.get(base + "/health", headers={"Authorization": f"Bearer {tok}"} if tok else {})
                hf = r.json() if r.status_code == 200 else {"error": f"HTTP {r.status_code}"}
        except Exception as e:  # noqa: BLE001
            hf = {"error": str(e)[:120] or type(e).__name__}
    models = []
    try:
        from collectors.llm_budget import budget
        models = budget().snapshot()
    except Exception:  # noqa: BLE001
        pass
    news_6h, src24, fut = 0, {}, 0
    try:
        from datetime import UTC, datetime, timedelta

        from storage.database import AsyncSessionFactory
        from storage.repository import Repository
        async with AsyncSessionFactory() as session:
            news = await Repository(session).news_sentiment_since(24)
        now = datetime.now(UTC).replace(tzinfo=None)
        for r in news:
            if r.published_at is None:
                continue
            if r.published_at > now + timedelta(minutes=15):
                fut += 1
                continue
            src = (r.source or "?").split(":")[0]
            src24[src] = src24.get(src, 0) + 1
            if r.published_at >= now - timedelta(hours=6):
                news_6h += 1
    except Exception:  # noqa: BLE001
        pass
    live_wallet = None
    if settings.delta_india_api_key and settings.delta_india_api_secret:
        try:                                                     # read-only balance of the real account
            from execution.delta_india import USER_AGENT, DeltaIndia
            async with httpx.AsyncClient(timeout=8, headers={"User-Agent": USER_AGENT}) as http:
                live_wallet = await DeltaIndia(settings.delta_india_api_key.get_secret_value(),
                                               settings.delta_india_api_secret.get_secret_value(),
                                               base_url=settings.delta_india_base_url, client=http).wallet_total_inr()
        except Exception:  # noqa: BLE001
            live_wallet = None
    started = getattr(runner, "started_at", None) or _START
    return {
        "mode": (settings.delta_india_mode or "off").lower(), "live_orders": bool(settings.delta_india_live_orders),
        "kill_switch": bool(settings.delta_india_kill_switch),
        "guard_deployed": hasattr(settings, "delta_india_daily_loss_pct"),
        "daily_loss_pct": float(getattr(settings, "delta_india_daily_loss_pct", 0) or 0),
        "min_wallet_inr": float(getattr(settings, "delta_india_min_wallet_inr", 0) or 0),
        "live_key_set": bool(settings.delta_india_api_key and settings.delta_india_api_secret),
        "families": settings.paper_open_families or "swing",
        "journal": journal_stats(rows, now_ms), "demo_open": len(state.get("positions") or {}),
        "stop_test": stop_test, "hf": hf, "models": models, "news_6h": news_6h, "news_sources_24h": src24,
        "news_future": fut, "live_wallet_inr": live_wallet, "uptime_s": int(time.time() - started),
    }


_START = time.time()


def register(app: web.Application, runner) -> None:
    async def golive(request: web.Request) -> web.Response:
        if _cache["body"] is None or time.time() - _cache["t"] > 20:
            snap = await _snapshot(runner)
            body = evaluate(snap)
            body["generated_ms"] = int(time.time() * 1000)
            body["mode"] = snap["mode"]
            body["live_wallet_inr"] = snap["live_wallet_inr"]
            _cache.update(t=time.time(), body=body)
        return web.json_response(_cache["body"])

    app.router.add_get("/api/golive", golive)
