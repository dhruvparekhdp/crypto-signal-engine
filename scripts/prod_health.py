"""Production health in plain words, every run: is the app up, is data fresh, are signals and paper trades flowing,
are AI calls answering. Writes status/prod_health.json for the lab monitor.

    python -m scripts.prod_health --base http://52.62.37.4:8080 [--ssh ubuntu@52.62.37.4 --key crypto-key.pem]

With --ssh it also reads the last 6 hours of the server log for AI failures and errors.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import time
import urllib.request
from pathlib import Path


def get(base: str, path: str, timeout: float = 20):
    t0 = time.time()
    with urllib.request.urlopen(base + path, timeout=timeout) as r:
        return json.loads(r.read()), time.time() - t0


def journal_counts(ssh: str, key: str) -> dict:
    cmd = ["ssh", "-i", key, "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", ssh,
           "sudo journalctl -u crypto-engine --since '6 hours ago' --no-pager -o cat"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60).stdout
    c: dict = {}
    for line in out.splitlines():
        if not line.startswith("{"):
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        e = d.get("event", "")
        if e in ("llm_provider_failed", "llm_no_provider_answered", "llm_served_by_fallback", "llm_circuit_breaker_tripped",
                 "swing_signal", "swing_trade_opened", "paper_trade_closed", "crypto_signal_engine_starting",
                 "swing_scan_failed", "live_monitor_failed", "swing_outcomes_failed"):
            key_ = e + (":" + d["role"] if d.get("role") else "")
            c[key_] = c.get(key_, 0) + 1
        if d.get("level") == "error":
            c["errors"] = c.get("errors", 0) + 1
    return c


def check(base: str, ssh: str | None, key: str | None) -> dict:
    items = []

    def add(name, ok, detail, state=None):
        items.append({"label": name, "state": state or ("ok" if ok else "problem"), "detail": detail})

    try:
        st, dt = get(base, "/api/status")
        up = st.get("uptime_seconds", 0)
        add("App reachable", True, f"answered in {dt:.1f}s · up {up / 3600:.1f} h"
            + (" · restarted in the last 10 min" if up < 600 else ""), "ok" if up >= 600 else "warn")
        c = st.get("crypto", {})
        add("Live price stream", bool(c.get("binance_ws_connected")),
            f"Binance stream {'connected' if c.get('binance_ws_connected') else 'DISCONNECTED'} · "
            f"{c.get('binance_messages_received', 0):,} messages · {c.get('signals_today', 0)} signals today")
    except Exception as e:  # noqa: BLE001
        add("App reachable", False, f"no answer: {str(e)[:120]}")
        return {"ts": time.time(), "items": items, "overall": "down"}
    try:
        p, _ = get(base, "/api/predict")
        ages = [m.get("data_age_minutes") or 0 for m in p.get("markets", [])]
        stale = [m["symbol"] for m in p.get("markets", []) if m.get("stale")]
        add("Price data fresh", not stale, f"newest candle {max(ages) if ages else '?'} min old"
            + (f" · stale: {', '.join(stale)}" if stale else ""))
    except Exception as e:  # noqa: BLE001
        add("Price data fresh", False, str(e)[:120])
    try:
        sw, _ = get(base, "/api/swing")
        g = sw.get("regime_filter", {})
        sig = g.get("signals", {})
        seen = sum(sig.get(k, {}).get("n", 0) + sig.get(k, {}).get("running", 0) for k in ("take", "skip"))
        scan = sw.get("scan", {})
        add("Swing book scanning", bool(scan), f"{len(scan)} coin/timeframes scanned · {len(sw.get('open', []))} open · "
            f"{sw.get('closed', {}).get('n', 0)} closed · filter {g.get('mode')} · "
            f"BTC volatility rank {round((g.get('now') or {}).get('btc_vol_rank') or 0, 2)}")
        add("Swing signals", True, f"{seen} signals tracked in 14 days: {sig.get('take', {}).get('n', 0)} taken finished, "
            f"{sig.get('skip', {}).get('n', 0)} skipped finished, "
            f"{sig.get('take', {}).get('running', 0) + sig.get('skip', {}).get('running', 0)} running", "ok")
    except Exception as e:  # noqa: BLE001
        add("Swing book scanning", False, str(e)[:120])
    if ssh and key:
        try:
            j = journal_counts(ssh, key)
            failed = sum(v for k, v in j.items() if k.startswith("llm_no_provider_answered"))
            fb = sum(v for k, v in j.items() if k.startswith("llm_served_by_fallback"))
            add("AI calls (6 h)", failed <= 3, f"{failed} jobs got no answer from any provider · {fb} answered by a fallback · "
                f"{sum(v for k, v in j.items() if k.startswith('llm_provider_failed'))} single-provider failures",
                "ok" if failed <= 3 else "warn" if failed <= 15 else "problem")
            restarts = j.get("crypto_signal_engine_starting", 0)
            add("Restarts (6 h)", restarts <= 2, f"{restarts} restarts · {j.get('errors', 0)} logged errors · "
                f"{j.get('swing_signal', 0)} swing signals · {j.get('swing_trade_opened', 0)} swing trades opened",
                "ok" if restarts <= 2 and j.get("errors", 0) == 0 else "warn")
        except Exception as e:  # noqa: BLE001
            add("Server log", False, f"could not read: {str(e)[:100]}", "warn")
    worst = "problem" if any(i["state"] == "problem" for i in items) else "warn" if any(i["state"] == "warn" for i in items) else "ok"
    return {"ts": time.time(), "items": items, "overall": worst}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default="http://52.62.37.4:8080")
    ap.add_argument("--ssh", default=None)
    ap.add_argument("--key", default=None)
    ap.add_argument("--out", default="status/prod_health.json")
    ap.add_argument("--every", type=int, default=0, help="repeat every N seconds (0 = once)")
    a = ap.parse_args()
    while True:
        r = check(a.base, a.ssh, a.key)
        Path(a.out).parent.mkdir(exist_ok=True)
        Path(a.out).write_text(json.dumps(r))
        print(time.strftime("%H:%M"), r["overall"], *(f"\n  [{i['state']}] {i['label']}: {i['detail']}" for i in r["items"]), flush=True)
        if not a.every:
            break
        time.sleep(a.every)


if __name__ == "__main__":
    main()
