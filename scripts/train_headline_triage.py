"""Train our headline triage model (owner, 18 Oct 2026, AI plan step 6) - relevance, event type and direction from the
headline's bge-small embedding (the same vectors the duplicate filter already gets from our HF Space).

    python -m scripts.train_headline_triage DATASET.jsonl            # rolling evaluation + writes the model file

DATASET.jsonl: one labelled headline per line with "vec" (bge-small, 384 floats), "relevant", "event_type", "score"
(labels from scripts/label_headlines.py, Groq gpt-oss-120b) and "rules" (what FinBERT + rules said, for the baseline).
Writes analysis/models/headline_triage.json only if the model beats FinBERT + rules on BOTH mistakes (noise let
through, real news dropped) averaged over five rolling time splits. Retrain monthly as labels grow.
"""
from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

OUT = Path("analysis/models/headline_triage.json")
EMBEDDER = "BAAI/bge-small-en-v1.5"
VOCAB = ["rate_decision", "inflation_data", "jobs_data", "war", "tariff", "regulation", "etf_flow", "liquidation",
         "hack", "adoption", "exchange_outage", "macro_other", "crypto_other", "noise"]
MAP = {"security": "hack", "liquidity": "macro_other", "political": "macro_other"}
THRESHOLD = 0.35          # relevance cut-off: fewer real headlines dropped (13% vs 26% at 0.5), still far less noise
C_REL, C_EV, C_DIR = 1.0, 8.0, 1.0


def event_label(r: dict) -> str:
    if not r.get("relevant"):
        return "noise"
    e = MAP.get(r.get("event_type"), r.get("event_type"))
    return e if e in VOCAB else "macro_other"


def direction_label(r: dict) -> int:
    s = float(r.get("score") or 0)
    return 0 if not r.get("relevant") else (1 if s > 0.1 else -1 if s < -0.1 else 0)


def arrays(rows: list[dict]):
    X = np.array([r["vec"] for r in rows], dtype=float)
    X /= np.linalg.norm(X, axis=1, keepdims=True)
    return (X, np.array([bool(r.get("relevant")) for r in rows]), np.array([event_label(r) for r in rows]),
            np.array([direction_label(r) for r in rows]))


def fit(X, y_rel, y_ev, y_dir):
    from sklearn.linear_model import LogisticRegression
    kw = dict(max_iter=3000, class_weight="balanced")
    return (LogisticRegression(C=C_REL, **kw).fit(X, y_rel), LogisticRegression(C=C_EV, **kw).fit(X, y_ev),
            LogisticRegression(C=C_DIR, **kw).fit(X, y_dir))


def rolling(rows: list[dict]) -> dict:
    """Train on everything before each of five 10% slices, test on the slice; mean mistake rates, ours vs baseline."""
    X, yr, ye, yd = arrays(rows)
    br = np.array([r["rules"]["event_type"] != "noise" for r in rows])
    be = np.array([r["rules"]["event_type"] for r in rows])
    n, res = len(rows), []
    for frac in (0.5, 0.6, 0.7, 0.8, 0.9):
        a, b = int(n * frac), min(n, int(n * (frac + 0.1)))
        mr, me, _ = fit(X[:a], yr[:a], ye[:a], yd[:a])
        p = mr.predict_proba(X[a:b])[:, 1] >= THRESHOLD
        t, bt = yr[a:b], br[a:b]
        res.append({"noise_through": float((p & ~t).sum() / max((~t).sum(), 1)),
                    "news_dropped": float((~p & t).sum() / max(t.sum(), 1)),
                    "event_acc": float((me.predict(X[a:b]) == ye[a:b]).mean()),
                    "base_noise_through": float((bt & ~t).sum() / max((~t).sum(), 1)),
                    "base_news_dropped": float((~bt & t).sum() / max(t.sum(), 1)),
                    "base_event_acc": float((be[a:b] == ye[a:b]).mean())})
    return {k: round(float(np.mean([r[k] for r in res])), 3) for k in res[0]}


def head(model) -> dict:
    return {"classes": [c.item() if hasattr(c, "item") else c for c in model.classes_],
            "coef": np.round(model.coef_, 5).tolist(), "intercept": np.round(model.intercept_, 5).tolist()}


def main(path: str) -> int:
    rows = sorted((json.loads(x) for x in open(path)), key=lambda r: r["published_at"])
    ev = rolling(rows)
    print(json.dumps(ev, indent=1))
    beats = ev["noise_through"] < ev["base_noise_through"] and ev["news_dropped"] < ev["base_news_dropped"]
    if not beats:
        print("does NOT beat FinBERT + rules on both mistakes - model file left as it is")
        return 1
    mr, me, md = fit(*arrays(rows))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "version": 1, "embedder": EMBEDDER, "dim": len(rows[0]["vec"]), "threshold": THRESHOLD,
        "trained_at": datetime.now(UTC).isoformat(timespec="seconds"), "n_train": len(rows),
        "labels_from": "groq/openai/gpt-oss-120b (scripts/label_headlines.py)",
        "rolling_eval": ev, "relevance": head(mr), "event": head(me), "direction": head(md)}))
    print(f"wrote {OUT} ({OUT.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
