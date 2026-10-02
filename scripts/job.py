"""Run one command as a tracked background job.

    python -m scripts.job NAME -- python -m scripts.run_lab --all ...

Writes status/NAME.json (state, pid, timings, heartbeat) and logs/NAME.log so the dashboard can show it.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path


def main():
    argv = sys.argv[1:]
    if "--" not in argv:
        raise SystemExit(__doc__)
    i = argv.index("--")
    name, cmd = argv[0], argv[i + 1:]
    root = Path(os.environ.get("LAB_ROOT", "."))
    (root / "status").mkdir(exist_ok=True)
    (root / "logs").mkdir(exist_ok=True)
    sp, lp = root / "status" / f"{name}.json", root / "logs" / f"{name}.log"
    st = {"name": name, "cmd": " ".join(cmd), "host": socket.gethostname(), "state": "running",
          "started": time.time(), "log": str(lp), "pid": None, "exit_code": None, "finished": None, "beat": time.time()}

    def save():
        st["beat"] = time.time()
        tmp = sp.with_suffix(".tmp")
        tmp.write_text(json.dumps(st))
        tmp.replace(sp)

    with open(lp, "ab", buffering=0) as log:
        p = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, cwd=root)
        st["pid"] = p.pid
        save()
        while p.poll() is None:
            time.sleep(10)
            save()
    st.update(state="done" if p.returncode == 0 else "failed", exit_code=p.returncode, finished=time.time())
    save()


if __name__ == "__main__":
    main()
