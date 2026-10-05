"""Write status/banner.json from the job status files, so the dashboard top card is always current."""
from __future__ import annotations

import glob
import json
import sys
import time
from pathlib import Path

PLAN = [("full2_12h", "12h bars"), ("full2_8h", "8h bars"), ("full2_4h", "4h bars"), ("full2_2h", "2h bars"), ("full2_1h", "1h bars"),
        ("full2_15m", "15m bars (defaults)"), ("full2_report", "aggregate every run"), ("full2_null", "null test on the best settings"),
        ("full2_wallet", "25 to 100 wallet simulations"), ("full2_final", "final tables")]


def main():
    items = []
    for name, label in PLAN:
        f = Path("status") / f"{name}.json"
        if f.exists():
            d = json.loads(f.read_text())
            st = {"running": "running", "done": "done", "failed": "failed"}.get(d["state"], "running")
            el = (d.get("finished") or time.time()) - d["started"]
            items.append({"label": f"Without AI, {label}", "state": st, "detail": f"{el / 60:.0f} min" + (" so far" if st == "running" else "")})
        else:
            items.append({"label": f"Without AI, {label}", "state": "paused", "detail": "waiting in the queue"})
    items.append({"label": "With AI (local thinking models)", "state": "paused", "detail": "stopped until you say go; the ThinkPad is switched off"})
    json.dump({"title": "5-year test without AI (all strategies, mirrors, ensembles, wallet)", "updated": time.strftime("%H:%M"), "items": items},
              open("status/banner.json", "w"))


if __name__ == "__main__":
    main()
