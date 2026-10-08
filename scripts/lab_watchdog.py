"""Keep the Mac lab monitor + ThinkPad status puller alive through Tailscale lag and crashes.

    python -m scripts.lab_watchdog

Checks every 10s: restarts `scripts.dash` on 127.0.0.1:8765 and `scripts.lab_pull` if missing or stuck.
"""
from __future__ import annotations

import argparse
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = str(ROOT / ".venv" / "bin" / "python")
LOG = ROOT / "logs"


def alive(pattern: str) -> bool:
    r = subprocess.run(["pgrep", "-f", pattern], capture_output=True)
    return r.returncode == 0


def dash_ok(port: int = 8765) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/live", timeout=4) as r:
            return r.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def pull_stale(name: str = "dhruv-ai", max_age: float = 90.0) -> bool:
    """True if pull.json missing or older than max_age (puller hung on SSH)."""
    import json

    p = ROOT / "remote" / name / "pull.json"
    try:
        d = json.loads(p.read_text())
        return (time.time() - float(d.get("ts", 0))) > max_age
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return True


def start(cmd: list[str], log_name: str) -> None:
    LOG.mkdir(exist_ok=True)
    log = open(LOG / log_name, "ab", buffering=0)
    subprocess.Popen(cmd, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    print(f"started {' '.join(cmd)}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--from", dest="host", default="dhruv@100.71.216.94")
    a = ap.parse_args()
    print(f"watchdog: dash :{a.port} + lab_pull from {a.host}", flush=True)
    while True:
        if not alive("scripts.lab_pull") or pull_stale():
            if alive("scripts.lab_pull"):
                print("lab_pull stale (>90s) — restarting", flush=True)
                subprocess.run(["pkill", "-f", "scripts.lab_pull"], capture_output=True)
                time.sleep(1)
            start([PY, "-u", "-m", "scripts.lab_pull", "--from", a.host, "--every", "8"], "lab_pull.log")
            time.sleep(2)
        if not alive("scripts.dash") or not dash_ok(a.port):
            # only kill our dash, never Tailscale
            subprocess.run(["pkill", "-f", "scripts.dash --host 127.0.0.1"], capture_output=True)
            time.sleep(1)
            start([PY, "-u", "-m", "scripts.dash", "--host", "127.0.0.1", "--port", str(a.port)], "dash.log")
            time.sleep(2)
        time.sleep(10)


if __name__ == "__main__":
    main()
