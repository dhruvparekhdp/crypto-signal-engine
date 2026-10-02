"""Mac side of the dashboard: every 15s send machine stats, job status, logs and result summaries
to the monitoring server. Standard library only.

    python -m scripts.statpush --to dhruv@100.71.216.94 --name mac
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import time
from pathlib import Path


def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True).stdout


def stats(name):
    top = sh("top -l 1 -n 0 | grep -E 'CPU usage|PhysMem'")
    cpu = re.search(r"([\d.]+)% idle", top)
    mem = re.search(r"PhysMem: (\d+)([MG]) used", top)
    used = (float(mem.group(1)) / (1024 if mem.group(2) == "M" else 1)) if mem else None
    total = int(sh("sysctl -n hw.memsize").strip() or 0) / 2**30
    batt = sh("pmset -g batt")
    pct = re.search(r"(\d+)%", batt)
    return {"name": name, "cpu_pct": round(100 - float(cpu.group(1))) if cpu else None,
            "ram_used_gb": round(used, 1) if used else None, "ram_total_gb": round(total, 1), "ts": time.time(),
            "load": sh("sysctl -n vm.loadavg").split()[1] if sh("sysctl -n vm.loadavg") else "",
            "battery_pct": int(pct.group(1)) if pct else None, "ac": "AC Power" in batt}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--to", required=True)
    ap.add_argument("--name", default="mac")
    ap.add_argument("--remote-dir", default="lab/remote")
    a = ap.parse_args()
    while True:
        Path("status").mkdir(exist_ok=True)
        Path("machine.json").write_text(json.dumps(stats(a.name)))
        files = ["machine.json", "status", "logs"] + [str(p) for p in Path("data/lab/runs").glob("*/summary.csv")]
        cmd = (f"COPYFILE_DISABLE=1 tar --no-xattrs -cf - {' '.join(files)} 2>/dev/null | "
               f"ssh -o BatchMode=yes -o ConnectTimeout=8 {a.to} 'mkdir -p {a.remote_dir}/{a.name} && cd {a.remote_dir}/{a.name} && tar -xf -'")
        subprocess.run(cmd, shell=True, capture_output=True)
        time.sleep(15)


if __name__ == "__main__":
    main()
