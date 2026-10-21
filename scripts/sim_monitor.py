"""Live monitor for the swing-book test (owner, 20 Oct 2026: "give me a live dashboard to monitor").

    python -m scripts.sim_monitor            # http://127.0.0.1:8766

One page that refreshes itself: the qwen3:14b download and its copy to the ThinkPad, whether the ThinkPad answers on the
cable, the local-LLM reviewer's progress (speed, time left, TAKE / SKIP so far and how those signals turned out), the
simulator's results (every risk level, every strategy, the best combinations), and the real live trades on production.
Standard library + pandas; reads files and runs ssh, never changes anything.
"""
from __future__ import annotations

import json
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = Path("/private/tmp/claude-501/-Users-dhruv-personal-Tennis-bet-crypto-signal-engine/"
               "5fe50565-6e25-409a-bc23-c5b74f73cac9/scratchpad/qwen3-14b")
TP = "dhruv@fe80::40c5:74aa:b07d:c11a%en5"
PROD = ["-i", str(Path.home() / ".ssh/crypto-ec2"), "ubuntu@52.62.37.4"]
MODEL_BYTES = 9_276_184_896
LLM_DIR = "~/lab/llm_review/out_v2"          # version 2 (21 Oct 2026); v1 had a biased prompt
STATE: dict = {"started": time.time()}
LOCK = threading.Lock()


def _ssh(target: list[str], cmd: str, timeout: int = 12) -> str | None:
    try:
        p = subprocess.run(["ssh", "-o", "ConnectTimeout=6", "-o", "BatchMode=yes", *target, cmd],
                           capture_output=True, timeout=timeout, text=True)
        return p.stdout if p.returncode == 0 else None
    except (subprocess.SubprocessError, OSError):
        return None


def _set(key: str, value) -> None:
    with LOCK:
        STATE[key] = value


def _sim_summary() -> dict | None:
    p = ROOT / "data/lab/live_sim.json"
    if not p.exists():
        return None
    d = json.loads(p.read_text())
    labels = d["meta"]["labels"]
    runs = d["runs"]
    idx = {(tuple(r["combo"]), r["risk"], r["wallet"], r["period"]): r for r in runs}
    keep = ("taken", "won", "lost", "win_pct", "avg_r", "end_inr", "x", "cagr_pct", "max_dd_pct", "losing_streak", "busted")
    risks = {}
    for period in d["meta"]["periods"]:
        risks[period] = [{"risk": rk, **{k: idx[(tuple(range(8)), rk, 500, period)][k] for k in keep}}
                         for rk in d["meta"]["risks"]]
    singles = [{"name": labels[i], "r3": {k: idx[((i,), 0.03, 500, "all")][k] for k in keep},
                "r5": {k: idx[((i,), 0.05, 500, "all")][k] for k in keep},
                "after": {k: idx[((i,), 0.03, 500, "2025-26")][k] for k in keep}} for i in range(8)]
    # best combinations chosen on 2021-24 only, then shown on 2025-26 (the honest way to pick)
    best = []
    for rk in (0.03, 0.05):
        cand = sorted((r for r in runs if r["risk"] == rk and r["wallet"] == 500 and r["period"] == "2021-24"
                       and r["taken"] >= 40), key=lambda r: (-(r["x"]) / max(r["max_dd_pct"], 5)))[:5]
        for r in cand:
            c = tuple(r["combo"])
            best.append({"risk": rk, "combo": " + ".join(labels[i] for i in c), "n": len(c),
                         "train": {k: r[k] for k in keep}, "after": {k: idx[(c, rk, 500, "2025-26")][k] for k in keep}})
    return {"meta": {k: d["meta"][k] for k in ("data_from", "data_to", "signals", "signals_after_filter", "coins")},
            "risks": risks, "singles": singles, "best": best, "n_runs": len(runs)}


def _llm_outcomes(path: Path) -> dict | None:
    """Version 2 records carry their own result: TAKE / SKIP against how the signal ended, the model's probability in
    bands, and the post-mortem's 'why' categories for wins and for losses."""
    try:
        rows = [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]
        rows = list({r["key"]: r for r in rows if r.get("key")}.values())      # a redone post-mortem replaces the old row
        rows = [r for r in rows if r.get("pre") and "r" in r]
        if not rows:
            return None
        out = {}
        for dec in ("TAKE", "SKIP"):
            rs = [r["r"] for r in rows if r["pre"].get("decision") == dec]
            if rs:
                out[dec] = {"n": len(rs), "win_pct": round(100 * sum(v > 0 for v in rs) / len(rs), 1),
                            "avg_r": round(sum(rs) / len(rs), 3)}
        bands = {}
        for r in rows:
            p = r["pre"].get("p_profit")
            if p is None:
                continue
            b = "<40" if p < 40 else "40-47" if p < 48 else "48-55" if p <= 55 else ">55"
            bands.setdefault(b, []).append(r["r"])
        out["bands"] = [{"band": b, "n": len(v), "win_pct": round(100 * sum(x > 0 for x in v) / len(v), 1),
                         "avg_r": round(sum(v) / len(v), 3)} for b, v in sorted(bands.items(), key=lambda kv: kv[0])]
        why = {}
        for r in rows:
            c = (r.get("post") or {}).get("category")
            if c:
                w = why.setdefault(c, {"won": 0, "lost": 0})
                w["won" if r["outcome"] == "WON" else "lost"] += 1
        out["why"] = why
        out["all"] = {"n": len(rows), "win_pct": round(100 * sum(r["r"] > 0 for r in rows) / len(rows), 1),
                      "avg_r": round(sum(r["r"] for r in rows) / len(rows), 3)}
        out["recent"] = [{"coin": r.get("coin"), "decision": r["pre"].get("decision"), "p": r["pre"].get("p_profit"),
                          "reason": r["pre"].get("reason"), "outcome": r.get("outcome"), "r": r.get("r"),
                          "category": (r.get("post") or {}).get("category"), "why": (r.get("post") or {}).get("why"),
                          "seconds": round((r.get("pre_seconds") or 0) + (r.get("post_seconds") or 0))}
                         for r in rows[-8:]][::-1]
        return out
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:120]}


def loop_fast() -> None:
    last = (0, time.time())
    while True:
        f = SCRATCH / "model.gguf"
        size = f.stat().st_size if f.exists() else 0
        now = time.time()
        speed = (size - last[0]) / max(now - last[1], 1e-6)
        last = (size, now)
        _set("download", {"bytes": size, "total": MODEL_BYTES, "mb_s": round(speed / 1e6, 1),
                          "done": size >= MODEL_BYTES})
        prog = ROOT / "data/lab/live_sim_progress.json"
        if prog.exists():
            _set("sim_progress", json.loads(prog.read_text()))
        tr = ROOT / "data/lab/llm_review/transfer.json"
        if tr.exists():
            _set("transfer", json.loads(tr.read_text()))
        time.sleep(3)


def loop_thinkpad() -> None:
    while True:
        out = _ssh([TP], f"echo up; cat /proc/loadavg; free -m | awk '/Mem/ {{print $3\" \"$2}}'; "
                         f"cat /sys/class/thermal/thermal_zone*/temp 2>/dev/null | sort -n | tail -1; "
                         f"ollama list 2>/dev/null | awk 'NR>1 {{print $1}}' | tr '\\n' ' '; echo; "
                         f"cat {LLM_DIR}/progress.json 2>/dev/null")
        if out and out.startswith("up"):
            lines = out.splitlines()
            load = lines[1].split()[:3] if len(lines) > 1 else []
            mem = lines[2].split() if len(lines) > 2 else []
            temp = int(lines[3]) / 1000 if len(lines) > 3 and lines[3].strip().isdigit() else None
            models = lines[4].split() if len(lines) > 4 else []
            prog = None
            if len(lines) > 5:
                try:
                    prog = json.loads("\n".join(lines[5:]))
                except ValueError:
                    prog = None
            _set("thinkpad", {"up": True, "seen": time.time(), "load": load,
                              "mem_used_gb": round(int(mem[0]) / 1024, 1) if mem else None,
                              "mem_total_gb": round(int(mem[1]) / 1024, 1) if len(mem) > 1 else None,
                              "temp_c": temp, "models": models})
            if prog:
                _set("llm", prog)
                local = ROOT / "data/lab/llm_review/results_v2_qwen3_14b.jsonl"
                try:
                    subprocess.run(["scp", "-q", "-6", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6",
                                    f"dhruv@[fe80::40c5:74aa:b07d:c11a%en5]:{LLM_DIR}/results_v2_qwen3_14b.jsonl",
                                    str(local)], timeout=30, capture_output=True)
                    if local.exists():
                        _set("llm_outcomes", _llm_outcomes(local))
                except (subprocess.SubprocessError, OSError):
                    pass
        else:
            with LOCK:
                tp = dict(STATE.get("thinkpad") or {})
            tp["up"] = False
            _set("thinkpad", tp)
        time.sleep(20)


def loop_slow() -> None:
    while True:
        try:
            _set("sim", _sim_summary())
        except Exception as e:  # noqa: BLE001
            _set("sim", {"error": str(e)[:160]})
        out = _ssh(PROD, "curl -s http://localhost:8080/api/swing", timeout=20)
        if out:
            try:
                d = json.loads(out)
                _set("live", {"at": time.time(), "open": [
                    {"symbol": p["symbol"], "side": p["side"], "strategy": p["strategy"], "opened_at": p["opened_at"],
                     "r_now": (p.get("lock") or {}).get("r_now"), "state": (p.get("lock") or {}).get("state")}
                    for p in d.get("open", [])], "closed": {k: d.get("closed", {}).get(k) for k in ("n", "wins", "avg_r", "total_r")}})
            except ValueError:
                pass
        time.sleep(60)


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/status.json"):
            with LOCK:
                body = json.dumps({**STATE, "now": time.time()}, default=str).encode()
            ctype = "application/json"
        else:
            body = (ROOT / "scripts/sim_monitor.html").read_bytes()
            ctype = "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def main(port: int = 8766) -> None:
    for fn in (loop_fast, loop_thinkpad, loop_slow):
        threading.Thread(target=fn, daemon=True).start()
    print(f"monitor on http://127.0.0.1:{port}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()


if __name__ == "__main__":
    main()
