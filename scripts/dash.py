"""Phone-friendly progress dashboard. Standard library only.

    python -m scripts.dash --host 100.71.216.94 --port 8765

Bind it to the Tailscale address so only your own devices can open it. Reads status/*.json,
logs/*.log, remote/<machine>/ (pushed by scripts.statpush) and the AI queue's progress file.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(os.environ.get("LAB_ROOT", "."))
_cpu_prev = [None]


def cpu_pct():
    with open("/proc/stat") as f:
        v = list(map(int, f.readline().split()[1:8]))
    idle, tot = v[3] + v[4], sum(v)
    prev, _cpu_prev[0] = _cpu_prev[0], (idle, tot)
    if not prev:
        return None
    di, dt = idle - prev[0], tot - prev[1]
    return round(100 * (1 - di / dt), 0) if dt else None


def machine():
    mem = {l.split(":")[0]: int(l.split()[1]) for l in open("/proc/meminfo")}
    out = {"name": os.uname().nodename, "cpu_pct": cpu_pct(), "load": open("/proc/loadavg").read().split()[0],
           "ram_used_gb": round((mem["MemTotal"] - mem["MemAvailable"]) / 1048576, 1),
           "ram_total_gb": round(mem["MemTotal"] / 1048576, 1), "ts": time.time()}
    try:
        out["temp_c"] = round(max(int(p.read_text()) for p in Path("/sys/class/thermal").glob("thermal_zone*/temp")) / 1000)
    except (ValueError, OSError):
        pass
    try:
        out["battery_pct"] = int(Path("/sys/class/power_supply/BAT0/capacity").read_text())
        out["ac"] = Path(next(Path("/sys/class/power_supply").glob("AC*")) / "online").read_text().strip() == "1"
    except (OSError, StopIteration):
        pass
    return out


def tail(path, n=14):
    try:
        data = Path(path).read_bytes()[-6000:].decode(errors="replace")
        return [l for l in data.splitlines() if l.strip()][-n:]
    except OSError:
        return []


def progress_from_log(lines):
    done = sum(1 for l in lines if re.search(r"^\s+[A-Z0-9]+USDT done", l))
    return done


def job_view(st, logdir):
    log = Path(logdir) / f"{st['name']}.log"
    lines = tail(log, 400)
    stale = time.time() - st.get("beat", 0) > 60 and st["state"] == "running"
    now = st.get("finished") or time.time()
    return {"name": st["name"], "host": st.get("host"), "state": "stalled?" if stale else st["state"],
            "elapsed_s": round(now - st["started"]), "symbols_done": progress_from_log(lines),
            "tail": lines[-8:], "cmd": st.get("cmd", "")[:160]}


def load_jobs():
    jobs = []
    for base in [ROOT] + [p for p in (ROOT / "remote").glob("*") if p.is_dir()]:
        for sp in sorted((base / "status").glob("*.json")):
            try:
                d = json.loads(sp.read_text())
                if "stage" in d:          # an AI-queue progress file, shown in its own section
                    continue
                jobs.append(job_view(d, base / "logs"))
            except (OSError, json.JSONDecodeError):
                pass
    return jobs


def ai_view():
    out = []
    for sp in sorted((ROOT / "status").glob("ai_*.json")):
        try:
            d = json.loads(sp.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if "stage" not in d:
            continue
        rv = []
        f = Path(d["results"]) if d.get("results") else None
        if f is not None and f.is_file():
            for l in f.read_text().splitlines()[-6:]:
                try:
                    r = json.loads(l)
                    rv.append({k: r.get(k) for k in ("model", "strategy", "symbol", "cause", "signal_quality", "wall_s", "ok", "consistent", "hypothesis")})
                except json.JSONDecodeError:
                    pass
        d["recent"] = rv
        out.append(d)
    return out


def results_view():
    out = []
    for base in [ROOT] + list((ROOT / "remote").glob("*")):
        for sp in sorted((base / "data" / "lab" / "runs").glob("*/summary.csv"), key=lambda p: p.stat().st_mtime)[-3:]:
            try:
                rows = list(csv.DictReader(open(sp)))
                rows.sort(key=lambda r: -float(r["expectancy_r"]))
                out.append({"run": f"{base.name}/{sp.parent.name}", "top": [
                    {k: (round(float(r[k]), 3) if k not in ("strategy", "params") and r.get(k) not in (None, "") else r.get(k)) for k in
                     ("strategy", "params", "n", "win_rate", "expectancy_r", "profit_factor", "null_p") if k in r}
                    for r in rows[:6]]})
            except (OSError, KeyError, ValueError):
                pass
    return out


def state():
    machines = [machine()]
    for mp in (ROOT / "remote").glob("*/machine.json"):
        try:
            m = json.loads(mp.read_text())
            m["age_s"] = round(time.time() - m.get("ts", 0))
            machines.append(m)
        except (OSError, json.JSONDecodeError):
            pass
    return {"now": time.time(), "machines": machines, "jobs": load_jobs(), "ai": ai_view(), "results": results_view()}


PAGE = (Path(__file__).parent / "dash.html").read_text() if (Path(__file__).parent / "dash.html").exists() else "dash.html missing"


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/api/state"):
            body, ct = json.dumps(state()).encode(), "application/json"
        else:
            body, ct = PAGE.encode(), "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ct)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()
    cpu_pct()
    print(f"dashboard on http://{a.host}:{a.port}  root={ROOT.resolve()}", flush=True)
    ThreadingHTTPServer((a.host, a.port), H).serve_forever()
