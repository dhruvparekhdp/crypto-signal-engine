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
    if not Path("/proc/stat").exists():
        return None
    with open("/proc/stat") as f:
        v = list(map(int, f.readline().split()[1:8]))
    idle, tot = v[3] + v[4], sum(v)
    prev, _cpu_prev[0] = _cpu_prev[0], (idle, tot)
    if not prev:
        return None
    di, dt = idle - prev[0], tot - prev[1]
    return round(100 * (1 - di / dt), 0) if dt else None


_mac_cache = {"t": 0.0, "v": None}


def machine():
    if not Path("/proc/stat").exists():          # macOS: reuse the stats code the pusher uses, cached for 10 seconds
        if time.time() - _mac_cache["t"] > 10 or _mac_cache["v"] is None:
            from scripts.statpush import stats
            _mac_cache.update(t=time.time(), v=stats(os.uname().nodename.split(".")[0]))
        return dict(_mac_cache["v"])
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
                if "stage" in d or "cmd" not in d or "name" not in d:   # an AI-queue file, the banner, or something else
                    continue
                jobs.append(job_view(d, base / "logs"))
            except Exception:  # noqa: BLE001 - one odd file must not take the page down
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
            for l in f.read_text().splitlines()[-8:]:
                try:
                    r = json.loads(l)
                    rv.append({k: r.get(k) for k in ("model", "strategy", "symbol", "cause", "signal_quality", "wall_s", "ok",
                                                       "consistent", "hypothesis", "decision", "score", "r_net", "reason")})
                except json.JSONDecodeError:
                    pass
        d["recent"] = rv
        out.append(d)
    return out


def work_view():
    """Shared work sets (the spare-laptop queue): progress, per-worker rates and the with-AI comparison."""
    out = []
    for mp in sorted((ROOT / "work").glob("*/meta.json")):
        d = mp.parent
        try:
            meta = json.loads(mp.read_text())
            items = [json.loads(l) for l in (d / "items.jsonl").read_text().splitlines() if l.strip()]
        except (OSError, json.JSONDecodeError):
            continue
        res = []
        for f in (d / "results").glob("[0-9]*.json") if (d / "results").exists() else []:
            try:
                res.append(json.loads(f.read_text()))
            except (OSError, json.JSONDecodeError):
                pass
        res.sort(key=lambda r: r.get("ts", 0))
        rows = []
        for r in res:
            it = items[r["item"]] if r.get("item") is not None and r["item"] < len(items) else {}
            rows.append({**r, "r_net": it.get("r_net"), "symbol": it.get("symbol"), "strategy": it.get("strategy")})
        try:
            from analysis.lab_ai import gate
            compare = {m: gate.compare([x for x in rows if x.get("model") == m]) for m in meta["models"]}
        except Exception:  # noqa: BLE001 - the dashboard must never die on a stats error
            compare = {}
        total = meta["n_items"] * len(meta["models"])
        workers = {}
        for r in res:
            w = workers.setdefault(r.get("worker", "?"), {"done": 0, "sum": 0.0, "last": 0, "failed": 0})
            w["done"] += 1
            w["sum"] += r.get("wall_s", 0)
            w["last"] = max(w["last"], r.get("ts", 0))
            w["failed"] += 0 if r.get("ok") else 1
        now = time.time()
        recent_claims = [c.stat().st_mtime for c in (d / "claims").glob("*")] if (d / "claims").exists() else []
        active = bool(recent_claims) and now - max(recent_claims) < 1800
        first = min((r.get("ts", now) for r in res), default=now)
        by_strat = {}
        for m in meta["models"]:
            for st in sorted({x.get("strategy") for x in rows if x.get("model") == m}):
                c = gate_compare_safe([x for x in rows if x.get("model") == m and x.get("strategy") == st])
                if c.get("n", 0) >= 4:
                    by_strat.setdefault(m, {})[st] = {k: c.get(k) for k in ("n", "all_exp_r", "take_exp_r", "take_n")}
        baseline = None
        try:
            baseline = json.loads((d / "baseline.json").read_text())
        except (OSError, json.JSONDecodeError):
            pass
        out.append({"name": meta["name"], "title": meta["title"], "kind": "gate", "state": "done" if len(res) >= total else ("running" if active else "waiting for workers"),
                    "stage": "work queue", "model": " + ".join(meta["models"]), "think": True, "done": len(res), "total": total,
                    "elapsed_s": now - meta["created"], "avg_s": (sum(r.get("wall_s", 0) for r in res) / len(res)) if res else None,
                    "rate_per_hour": (len(res) / max(now - first, 60) * 3600) if len(res) > 1 else None,
                    "population_n": meta["population_n"], "population_exp_r": meta["population_exp_r"],
                    "population_win": meta["population_win"], "sample_n": meta["n_items"], "compare": compare,
                    "by_strategy": by_strat, "baseline": baseline, "failed": sum(1 for r in res if not r.get("ok")),
                    "workers": [{"name": k, "done": v["done"], "avg_s": v["sum"] / v["done"], "ago_s": now - v["last"], "failed": v["failed"]}
                                for k, v in sorted(workers.items())],
                    "recent": [{k: r.get(k) for k in ("model", "symbol", "strategy", "decision", "score", "r_net", "wall_s", "reason", "worker", "ok")}
                               for r in rows[-8:]]})
    return out


def gate_compare_safe(rows):
    try:
        from analysis.lab_ai import gate
        return gate.compare(rows)
    except Exception:  # noqa: BLE001
        return {}


def banner_view():
    """One glance: what is running, what is finished, what is paused. status/banner.json is written by hand or by a job."""
    try:
        return json.loads((ROOT / "status" / "banner.json").read_text())
    except (OSError, json.JSONDecodeError):
        return None


def noai_view():
    """Rules-only findings written by scripts.portfolio_wallet and scripts.breakdown on the Mac."""
    out = {"wallet": {}, "breakdown": {}, "timeframes": []}
    for base in [ROOT] + list((ROOT / "remote").glob("*")):
        lab = base / "data" / "lab"
        f = lab / "noai_report.json"
        if f.exists():
            try:
                out["timeframes"] = json.loads(f.read_text())
            except (OSError, json.JSONDecodeError):
                pass
        for months in (12, 24):
            f = lab / f"wallet_{months}m.csv"
            if f.exists():
                try:
                    rows = list(csv.DictReader(open(f)))
                    for r in rows:
                        for k in r:
                            try:
                                r[k] = round(float(r[k]), 3)
                            except ValueError:
                                pass
                    rows.sort(key=lambda r: (-(r["hit_100"] or 0), -(r["median_end_balance"] or 0)))
                    out["wallet"][f"{months} months"] = rows[:14]
                except (OSError, ValueError):
                    pass
        for name in ("by_year", "by_symbol", "by_side", "by_strategy", "strategy_by_year"):
            f = lab / "breakdown" / f"{name}.csv"
            if f.exists():
                try:
                    rows = list(csv.reader(open(f)))
                    out["breakdown"][name] = [[(round(float(c), 3) if i else c) if c.replace(".", "", 1).replace("-", "", 1).isdigit() or i == 0 else c for i, c in enumerate(r)] for r in rows]
                except (OSError, ValueError):
                    pass
    return out


def results_view():
    out = []
    for base in [ROOT] + list((ROOT / "remote").glob("*")):
        for sp in sorted((base / "data" / "lab" / "runs").glob("*/summary.csv"), key=lambda p: p.stat().st_mtime)[-3:]:
            try:
                rows = list(csv.DictReader(open(sp)))
                rows.sort(key=lambda r: -float(r["expectancy_r"]))
                keys = ("strategy", "params", "exit", "n", "win_rate", "expectancy_r", "ci_lo", "ci_hi", "profit_factor",
                        "max_dd_r", "null_mean_r", "null_p")
                out.append({"run": f"{base.name}/{sp.parent.name}", "configs": len(rows), "positive": sum(float(r["expectancy_r"]) > 0 for r in rows),
                            "top": [{k: (round(float(r[k]), 3) if k not in ("strategy", "params", "exit") and r.get(k) not in (None, "") else r.get(k))
                                     for k in keys if k in r} for r in rows[:30]]})
            except (OSError, KeyError, ValueError):
                pass
    return out


def scrub(o):
    """NaN and Infinity are not valid JSON; browsers reject the whole payload. Send null instead."""
    import math
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, dict):
        return {k: scrub(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [scrub(v) for v in o]
    return o


def state():
    machines = [machine()]
    for mp in (ROOT / "remote").glob("*/machine.json"):
        try:
            m = json.loads(mp.read_text())
            m["age_s"] = round(time.time() - m.get("ts", 0))
            machines.append(m)
        except (OSError, json.JSONDecodeError):
            pass
    def safe(fn, default):
        try:
            return fn()
        except Exception:  # noqa: BLE001 - each section fails on its own, never the whole page
            return default

    return {"now": time.time(), "machines": machines, "jobs": safe(load_jobs, []),
            "ai": safe(ai_view, []) + safe(work_view, []), "results": safe(results_view, []),
            "noai": safe(noai_view, {"wallet": {}, "breakdown": {}, "timeframes": []}), "banner": safe(banner_view, None)}


PAGE = (Path(__file__).parent / "dash.html").read_text() if (Path(__file__).parent / "dash.html").exists() else "dash.html missing"


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        here = Path(__file__).parent
        if self.path.startswith("/api/state"):
            body, ct = json.dumps(scrub(state()), allow_nan=False).encode(), "application/json"
        elif self.path.startswith("/setup.sh"):
            body, ct = (here / "worker_setup.sh").read_bytes(), "text/x-shellscript"
        elif self.path.startswith("/worker.py"):
            body, ct = (here / "work_worker.py").read_bytes(), "text/x-python"
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
