"""
Live Batch Progress Tracker for Multi-Year Backtesting & AI Pipeline.

Maintains atomic JSON state in data/reports/pipeline_progress.json and
renders an informative real-time terminal dashboard showing:
- Progress per phase (Data Download, Cross-TF Cycle, Volume-Event, Backtest, AI Reasoning)
- Batches completed, pending, in-flight
- Categorized AI answers (macro, regulatory, technical breakout, whale, no_correlation)
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path


PROGRESS_FILE = Path("data/reports/pipeline_progress.json")


@dataclass
class PhaseStatus:
    name: str
    total_tasks: int = 0
    completed_tasks: int = 0
    failed_tasks: int = 0
    status: str = "pending"  # "pending" | "running" | "completed" | "failed"
    current_item: str = ""
    details: dict = field(default_factory=dict)


@dataclass
class PipelineProgress:
    started_at: str = ""
    updated_at: str = ""
    total_phases: int = 5
    current_phase_index: int = 0
    phases: dict[str, PhaseStatus] = field(default_factory=dict)
    ai_categories: dict[str, int] = field(default_factory=lambda: {
        "macro_economic": 0,
        "regulatory": 0,
        "technical_breakout": 0,
        "whale_manipulation": 0,
        "exchange_event": 0,
        "news_panic": 0,
        "pattern_found": 0,
        "no_correlation": 0,
        "needs_deeper_analysis": 0,
    })
    ai_batches_total: int = 0
    ai_batches_completed: int = 0
    ai_batches_failed: int = 0


class ProgressTracker:
    def __init__(self, filepath: Path = PROGRESS_FILE):
        self.filepath = filepath
        self.filepath.parent.mkdir(parents=True, exist_ok=True)
        self.state = PipelineProgress(
            started_at=datetime.now(UTC).isoformat(),
            updated_at=datetime.now(UTC).isoformat(),
            phases={
                "1_data_download": PhaseStatus("Data Download & Lake Verification"),
                "2_cross_tf_cycle": PhaseStatus("Cross-Timeframe Cycle Correlation"),
                "3_volume_event": PhaseStatus("Volume-Event Correlation & Discovery"),
                "4_backtest_runs": PhaseStatus("Multi-Year Strategy Backtest Runs"),
                "5_ai_reasoning": PhaseStatus("Hugging Face Serverless Batch Reasoning"),
            }
        )
        self._load()

    def _load(self) -> None:
        if self.filepath.exists():
            try:
                data = json.loads(self.filepath.read_text())
                self.state.started_at = data.get("started_at", self.state.started_at)
                self.state.updated_at = data.get("updated_at", self.state.updated_at)
                self.state.current_phase_index = data.get("current_phase_index", 0)
                self.state.ai_categories = data.get("ai_categories", self.state.ai_categories)
                self.state.ai_batches_total = data.get("ai_batches_total", 0)
                self.state.ai_batches_completed = data.get("ai_batches_completed", 0)
                self.state.ai_batches_failed = data.get("ai_batches_failed", 0)
                for k, p in data.get("phases", {}).items():
                    if k in self.state.phases:
                        self.state.phases[k] = PhaseStatus(**p)
            except Exception:
                pass

    def save(self) -> None:
        self.state.updated_at = datetime.now(UTC).isoformat()
        temp_file = self.filepath.with_suffix(".tmp")
        payload = {
            "started_at": self.state.started_at,
            "updated_at": self.state.updated_at,
            "total_phases": self.state.total_phases,
            "current_phase_index": self.state.current_phase_index,
            "ai_categories": self.state.ai_categories,
            "ai_batches_total": self.state.ai_batches_total,
            "ai_batches_completed": self.state.ai_batches_completed,
            "ai_batches_failed": self.state.ai_batches_failed,
            "phases": {k: asdict(v) for k, v in self.state.phases.items()}
        }
        temp_file.write_text(json.dumps(payload, indent=2))
        temp_file.replace(self.filepath)

    def set_phase(self, phase_key: str, status: str, total_tasks: int = 0, current_item: str = "") -> None:
        if phase_key in self.state.phases:
            p = self.state.phases[phase_key]
            p.status = status
            if total_tasks > 0:
                p.total_tasks = total_tasks
            if current_item:
                p.current_item = current_item
            self.save()

    def advance_phase_task(self, phase_key: str, increment: int = 1, current_item: str = "", details: dict | None = None) -> None:
        if phase_key in self.state.phases:
            p = self.state.phases[phase_key]
            p.completed_tasks += increment
            if current_item:
                p.current_item = current_item
            if details:
                p.details.update(details)
            self.save()

    def record_ai_batch(self, category: str, success: bool = True) -> None:
        if success:
            self.state.ai_batches_completed += 1
            cat_clean = category.lower().strip()
            if cat_clean in self.state.ai_categories:
                self.state.ai_categories[cat_clean] += 1
            else:
                self.state.ai_categories["pattern_found"] += 1
        else:
            self.state.ai_batches_failed += 1
        self.save()

    def render(self) -> str:
        """Render a clean ASCII progress dashboard."""
        lines = []
        border = "═" * 70
        lines.append(f"╔{border}╗")
        lines.append(f"║  MULTI-YEAR BACKTEST & AI PIPELINE — LIVE BATCH MONITOR       ║")
        lines.append(f"╠{border}╣")
        lines.append(f"║  Started: {self.state.started_at[:19]} UTC   Updated: {self.state.updated_at[:19]} UTC  ║")
        lines.append(f"╠{border}╣")

        for key, p in self.state.phases.items():
            pct = (p.completed_tasks / max(1, p.total_tasks)) * 100 if p.total_tasks > 0 else (100 if p.status == "completed" else 0)
            bar_len = 20
            filled = int(bar_len * (pct / 100))
            bar = "█" * filled + "░" * (bar_len - filled)
            status_icon = "✅" if p.status == "completed" else ("🔄" if p.status == "running" else "⏳")
            lines.append(f"║  {status_icon} {p.name:<32} [{bar}] {pct:5.1f}% ║")
            if p.current_item:
                item_str = f"   ↳ {p.current_item}"[:66]
                lines.append(f"║  {item_str:<68}║")

        lines.append(f"╠{border}╣")
        lines.append(f"║  AI REASONING BATCHES & ANSWER CATEGORIZATION:                       ║")
        total_b = self.state.ai_batches_total
        done_b = self.state.ai_batches_completed
        fail_b = self.state.ai_batches_failed
        lines.append(f"║  Batches: {done_b} completed / {total_b} total ({fail_b} retried/failed)                     ║")

        # Render categories
        cat_items = list(self.state.ai_categories.items())
        for i in range(0, len(cat_items), 2):
            c1, v1 = cat_items[i]
            c2, v2 = cat_items[i+1] if i+1 < len(cat_items) else ("", "")
            str1 = f"• {c1}: {v1}"
            str2 = f"• {c2}: {v2}" if c2 else ""
            lines.append(f"║    {str1:<32} {str2:<32} ║")

        lines.append(f"╚{border}╝")
        return "\n".join(lines)


if __name__ == "__main__":
    tracker = ProgressTracker()
    print(tracker.render())
