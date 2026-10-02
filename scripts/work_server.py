"""Coordinator side of the shared work queue. Standard library only; runs on the ThinkPad.

Layout under $LAB_ROOT/work/NAME/: meta.json, items.jsonl, claims/N, results/N.json, lock.
A task is (item, model); its number is item_index * len(models) + model_index. Workers call this file
over SSH (or directly on the ThinkPad):

    work_server.py meta NAME | items NAME | claim NAME WORKER MODEL,MODEL | result NAME < json | status NAME
"""
from __future__ import annotations

import fcntl
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(os.environ.get("LAB_ROOT", str(Path.home() / "lab")))
STALE_S = 2400
MAX_ATTEMPTS = 2          # a task that fails twice is recorded as failed and not offered again            # a claim with no result after 40 minutes goes back in the queue


def base(name: str) -> Path:
    return ROOT / "work" / name


def meta(name: str) -> dict:
    return json.loads((base(name) / "meta.json").read_text())


def n_tasks(m: dict) -> int:
    return m["n_items"] * len(m["models"])


def _claimed(p: Path, idx: int):
    f = p / "claims" / str(idx)
    try:
        return json.loads(f.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def claim(name: str, worker: str, models: list[str], now: float | None = None):
    """Next task this worker can do, or None. Serialised by a file lock so two laptops never get the same one."""
    now = time.time() if now is None else now
    m = meta(name)
    p = base(name)
    (p / "claims").mkdir(exist_ok=True)
    (p / "results").mkdir(exist_ok=True)
    with open(p / "lock", "a+") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        pending = False
        for idx in range(n_tasks(m)):
            model = m["models"][idx % len(m["models"])]
            if (p / "results" / f"{idx}.json").exists():
                continue
            c = _claimed(p, idx)
            if c is not None and now - c["ts"] < STALE_S:
                pending = True
                continue
            if model not in models:
                pending = True
                continue
            (p / "claims" / str(idx)).write_text(json.dumps({"worker": worker, "ts": now}))
            return {"idx": idx, "item": idx // len(m["models"]), "model": model, "all_done": False}
        return {"idx": None, "all_done": not pending}


def result(name: str, res: dict):
    """A good answer is final. A failed one is retried (its claim is released) until MAX_ATTEMPTS."""
    p = base(name)
    (p / "results").mkdir(exist_ok=True)
    (p / "failed").mkdir(exist_ok=True)
    idx = res["idx"]
    if not res.get("ok"):
        (p / "failed" / f"{idx}.{int(time.time() * 1000)}.json").write_text(json.dumps(res))
        if len(list((p / "failed").glob(f"{idx}.*.json"))) < MAX_ATTEMPTS:
            try:
                (p / "claims" / str(idx)).unlink()
            except OSError:
                pass
            return
    tmp = p / "results" / f".{idx}.tmp"
    tmp.write_text(json.dumps(res))
    tmp.replace(p / "results" / f"{idx}.json")


def status(name: str) -> dict:
    m = meta(name)
    p = base(name)
    done = len(list((p / "results").glob("[0-9]*.json"))) if (p / "results").exists() else 0
    return {"name": name, "total": n_tasks(m), "done": done}


def main():
    cmd, name = sys.argv[1], sys.argv[2]
    if cmd == "meta":
        print(json.dumps(meta(name)))
    elif cmd == "items":
        sys.stdout.write((base(name) / "items.jsonl").read_text())
    elif cmd == "claim":
        print(json.dumps(claim(name, sys.argv[3], [x for x in sys.argv[4].split(",") if x])))
    elif cmd == "result":
        result(name, json.loads(sys.stdin.read()))
        print("ok")
    elif cmd == "status":
        print(json.dumps(status(name)))
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
