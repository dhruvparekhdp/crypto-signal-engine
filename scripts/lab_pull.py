"""Pull ThinkPad lab status/logs onto the Mac so the lab monitor shows live progress.

Survives Tailscale / Wi‑Fi lag: retries SSH, never exits on a failed pull, keeps last good copy.

    python -m scripts.lab_pull --from dhruv@100.71.216.94 --every 8
"""
from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SSH_OPTS = [
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=12",
    "-o", "ServerAliveInterval=5",
    "-o", "ServerAliveCountMax=3",
    "-o", "TCPKeepAlive=yes",
    "-o", "StrictHostKeyChecking=accept-new",
]


def pull_once(host: str, name: str, remote_root: str) -> tuple[bool, str]:
    dest = ROOT / "remote" / name
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "status").mkdir(exist_ok=True)
    (dest / "logs").mkdir(exist_ok=True)
    remote = (
        f"cd {remote_root} && "
        "python3 - <<'PY'\n"
        "import json, time\n"
        "from pathlib import Path\n"
        "mem = open('/proc/meminfo').read().split()\n"
        "kv = {mem[i].rstrip(':'): int(mem[i+1]) for i in range(0, len(mem)-1, 2) if mem[i].endswith(':')}\n"
        "used = (kv.get('MemTotal', 0) - kv.get('MemAvailable', 0)) / 1048576\n"
        "total = kv.get('MemTotal', 0) / 1048576\n"
        "load = open('/proc/loadavg').read().split()[0]\n"
        f"Path('machine.json').write_text(json.dumps({{'name': '{name}', 'ts': time.time(), "
        "'load': load, 'ram_used_gb': round(used, 1), 'ram_total_gb': round(total, 1), 'cpu_pct': None}))\n"
        "PY\n"
        "tar cf - machine.json status "
        "logs/gate_live_run.log logs/non_ai_queue.log logs/gate_beat.log 2>/dev/null"
    )
    try:
        p = subprocess.run(["ssh", *SSH_OPTS, host, remote], capture_output=True, timeout=55)
        if p.returncode != 0:
            return False, (p.stderr or b"ssh failed")[:220].decode(errors="replace")
        u = subprocess.run(["tar", "xf", "-", "-C", str(dest)], input=p.stdout, capture_output=True, timeout=30)
        if u.returncode != 0:
            return False, (u.stderr or b"untar failed")[:160].decode(errors="replace")
    except (subprocess.TimeoutExpired, OSError) as e:
        return False, f"timeout/os: {e}"[:160]

    (dest / "pull.json").write_text(json.dumps({"ts": time.time(), "ok": True, "host": host, "name": name}))
    return True, "ok"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--from", dest="host", default="dhruv@100.71.216.94")
    ap.add_argument("--name", default="dhruv-ai")
    ap.add_argument("--remote-root", default="~/projects/crypto-signal-engine")
    ap.add_argument("--every", type=int, default=8)
    a = ap.parse_args()
    print(f"pulling {a.host} -> remote/{a.name} every {a.every}s (lag-tolerant)", flush=True)
    fail_streak = 0
    while True:
        t0 = time.time()
        ok, msg = False, ""
        for attempt in range(1, 4):
            ok, msg = pull_once(a.host, a.name, a.remote_root)
            if ok:
                break
            time.sleep(min(2 ** attempt, 8))
        dest = ROOT / "remote" / a.name
        dest.mkdir(parents=True, exist_ok=True)
        if ok:
            if fail_streak:
                print(f"pull recovered after {fail_streak} failures", flush=True)
            fail_streak = 0
        else:
            fail_streak += 1
            (dest / "pull.json").write_text(json.dumps({
                "ts": time.time(), "ok": False, "error": msg, "fail_streak": fail_streak, "host": a.host}))
            print(f"pull failed #{fail_streak}: {msg}", flush=True)
        wait = a.every if ok else min(60, a.every * (1 + fail_streak))
        time.sleep(max(3, wait - (time.time() - t0)))


if __name__ == "__main__":
    main()
