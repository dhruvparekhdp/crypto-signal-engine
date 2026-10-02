"""A worker: pulls review tasks from the coordinator and runs them on THIS machine's Ollama.

Needs only Python 3 (standard library) and Ollama with the job's models. Run it on any laptop:

    python3 work_worker.py --name laptop2 --job gate_vb --coordinator dhruv@100.71.216.94
    python3 work_worker.py --name thinkpad --job gate_vb --coordinator local      # on the coordinator itself
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

OLLAMA = "http://127.0.0.1:11434"


class Coordinator:
    def __init__(self, target: str, lab_dir: str = "lab"):
        self.target, self.lab = target, lab_dir

    def _run(self, args: list[str], stdin: str | None = None) -> str:
        if self.target == "local":
            import os
            env = dict(os.environ, LAB_ROOT=os.path.expanduser(f"~/{self.lab}"))
            cmd = [sys.executable, os.path.expanduser(f"~/{self.lab}/scripts/work_server.py")] + args
            return subprocess.run(cmd, input=stdin, capture_output=True, text=True, check=True, env=env).stdout
        remote = f"LAB_ROOT=$HOME/{self.lab} python3 $HOME/{self.lab}/scripts/work_server.py " + " ".join(args)
        for attempt in range(5):
            try:
                return subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", self.target, remote],
                                      input=stdin, capture_output=True, text=True, check=True, timeout=120).stdout
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                time.sleep(10 * (attempt + 1))
        raise RuntimeError("coordinator unreachable")

    def meta(self, job):
        return json.loads(self._run(["meta", job]))

    def items(self, job):
        return [json.loads(l) for l in self._run(["items", job]).splitlines() if l.strip()]

    def claim(self, job, worker, models):
        return json.loads(self._run(["claim", job, worker, ",".join(models)]))

    def result(self, job, res):
        self._run(["result", job], stdin=json.dumps(res))


THINK = {"temperature": 0.6, "top_p": 0.95, "top_k": 20, "repeat_penalty": 1.1}
THINK_RETRY = {"temperature": 0.8, "top_p": 0.95, "top_k": 40, "repeat_penalty": 1.25}


def chat(model, system, user, schema=None, think=None, num_predict=300, temperature=0.2, num_ctx=8192, timeout=1800, **opts):
    body = {"model": model, "stream": False, "options": {"temperature": temperature, "num_predict": num_predict, "num_ctx": num_ctx, **opts},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    if schema is not None:
        body["format"] = schema
    if think is not None:
        body["think"] = think
    req = urllib.request.Request(f"{OLLAMA}/api/chat", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            out = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{e.code}: {e.read().decode(errors='replace')[:160]}") from e
    m = out.get("message", {})
    return {"text": m.get("content", ""), "thinking": m.get("thinking", "") or "", "out_tokens": out.get("eval_count", 0)}


def chat_think(model, system, user, **kw):
    """Greedy decoding makes thinking models loop; use the recommended sampling, retry once if Ollama aborts a loop."""
    try:
        return chat(model, system, user, think=True, **THINK, **kw)
    except RuntimeError as e:
        if "repeat limit" not in str(e):
            raise
        t = dict(THINK_RETRY)
        return chat(model, system, user, think=True, temperature=t.pop("temperature"), **t, **kw)


def json_from(text):
    a, b = text.find("{"), text.rfind("}")
    if a < 0 or b <= a:
        raise ValueError("no json")
    return json.loads(text[a:b + 1])


def decide(meta, item, model):
    """Bounded think-then-answer. qwen3 can switch thinking off for the answer step; deepseek-r1 cannot (it
    always thinks first), so it gets a bigger single budget and a plain 'stop reasoning, JSON only' follow-up."""
    t0, system, user = time.time(), meta["system"], item["user"]
    valid = lambda d: d.get("decision") in ("take", "skip") and isinstance(d.get("score"), int)
    r1 = model.startswith("deepseek-r1")
    tokens, data = 0, None
    try:
        a = chat_think(model, system, user, schema=None, num_predict=int(meta["think_tokens"] * (1.5 if r1 else 1)))
        tokens += a["out_tokens"]
        try:
            data = json_from(a["text"])
            data = data if valid(data) else None
        except (ValueError, json.JSONDecodeError):
            data = None
        if data is None:
            notes = (a["thinking"] or a["text"])[-1800:]
            if r1:
                follow = user + "\n\nYour reasoning so far (may be cut off):\n" + notes + "\n\nStop reasoning. Output ONLY the final JSON object now."
                b = chat(model, system, follow, schema=None, think=None, num_predict=500, temperature=0.3)
            else:
                follow = user + "\n\nYour analysis so far (may be cut off):\n" + notes + "\n\nNow give the final JSON only."
                try:
                    b = chat(model, system, follow, schema=meta["schema"], think=False, num_predict=200, temperature=0.1)
                except Exception:
                    b = chat(model, system, follow, schema=meta["schema"], think=None, num_predict=200, temperature=0.1)
            tokens += b["out_tokens"]
            data = json_from(b["text"])
        if not valid(data):
            raise ValueError("invalid answer")
    except Exception as e:  # noqa: BLE001 - a failed task is reported, not fatal
        return {"ok": False, "error": str(e)[:100], "wall_s": time.time() - t0}
    return {"ok": True, "decision": data["decision"], "score": int(data["score"]), "reason": str(data.get("reason", ""))[:240],
            "wall_s": time.time() - t0, "out_tokens": tokens, "think_budget": meta["think_tokens"]}


def installed_models():
    with urllib.request.urlopen(f"{OLLAMA}/api/tags", timeout=10) as r:
        return {m["name"] for m in json.loads(r.read())["models"]}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--name", required=True)
    ap.add_argument("--job", required=True)
    ap.add_argument("--coordinator", required=True, help="user@host, or 'local' on the coordinator")
    ap.add_argument("--models", default="", help="comma list; default every job model installed here")
    a = ap.parse_args()
    co = Coordinator(a.coordinator)
    meta = co.meta(a.job)
    have = installed_models()
    models = [m for m in (a.models.split(",") if a.models else meta["models"]) if m in meta["models"]]
    models = [m for m in models if m in have]
    if not models:
        sys.exit(f"none of the job's models {meta['models']} are installed here ({sorted(have)}); run: ollama pull <model>")
    items = co.items(a.job)
    print(f"[{a.name}] job {a.job}: {len(items)} items, models here: {models}", flush=True)
    done = 0
    while True:
        c = co.claim(a.job, a.name, models)
        if c["idx"] is None:
            if c["all_done"]:
                print("all tasks finished", flush=True)
                return
            time.sleep(60)
            continue
        item = items[c["item"]]
        r = decide(meta, item, c["model"])
        r.update(idx=c["idx"], item=c["item"], model=c["model"], worker=a.name, ts=time.time())
        co.result(a.job, r)
        done += 1
        print(f"[{a.name}] #{done} task {c['idx']} {c['model']} {item['symbol']} -> {r.get('decision')} {r.get('score')} "
              f"{r['wall_s']:.0f}s real_R={item['r_net']:+.2f}", flush=True)


if __name__ == "__main__":
    main()
