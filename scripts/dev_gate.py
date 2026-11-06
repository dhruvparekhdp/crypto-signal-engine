#!/usr/bin/env python3
"""Fail CI when ruff or mypy grows a new finding, and run pip-audit.

A cold `ruff check` and `mypy` already fail on this tree. SYSTEM_CONTEXT says
ruff is diffed against a baseline and that unrelated lint is not cleaned up
in passing. This gate keeps that rule: a higher count than the baseline fails,
a lower count does not, so fixing old lint needs no baseline edit.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUFF_BASELINE = ROOT / "quality" / "ruff-baseline.txt"
MYPY_BASELINE = ROOT / "quality" / "mypy-baseline.txt"
AUDIT_IGNORE = ROOT / "quality" / "pip-audit-ignore.txt"

CountMap = dict[tuple[str, str], int]
_MYPY_ERROR = re.compile(
    r"^(?P<path>.+?):\d+: error: .*?(?:\[(?P<code>[A-Za-z0-9-]+)\])?\s*$"
)


def excess(current: CountMap, baseline: CountMap) -> list[str]:
    """Return findings whose count is above the committed baseline."""
    problems: list[str] = []
    for key in sorted(current):
        count = current[key]
        allowed = baseline.get(key, 0)
        if count > allowed:
            path, code = key
            problems.append(f"{path} [{code}] {count} > baseline {allowed}")
    return problems


def load_baseline(path: Path) -> CountMap:
    counts: CountMap = {}
    if not path.exists():
        return counts
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        file_path, code, count_s = line.split("\t")
        counts[(file_path, code)] = int(count_s)
    return counts


def write_baseline(path: Path, counts: CountMap) -> None:
    lines = [
        "# path, rule, count. Regenerated with: python scripts/dev_gate.py write",
        "# CI fails when a count grows. A smaller count is allowed.",
    ]
    for (file_path, code), count in sorted(counts.items()):
        lines.append(f"{file_path}\t{code}\t{count}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _rel(filename: str) -> str:
    candidate = Path(filename)
    if candidate.is_absolute():
        try:
            candidate = candidate.relative_to(ROOT)
        except ValueError:
            return candidate.as_posix()
    return candidate.as_posix()


def ruff_counts() -> CountMap:
    proc = subprocess.run(
        [sys.executable, "-m", "ruff", "check", ".", "--output-format=json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode not in (0, 1):
        sys.stderr.write(proc.stderr)
        raise SystemExit(proc.returncode or 1)
    payload = json.loads(proc.stdout or "[]")
    counts: Counter[tuple[str, str]] = Counter()
    for item in payload:
        code = str(item.get("code") or "UNKNOWN")
        counts[(_rel(str(item.get("filename") or "?")), code)] += 1
    return dict(counts)


def mypy_counts() -> CountMap:
    proc = subprocess.run(
        [
            sys.executable, "-m", "mypy", ".",
            "--no-error-summary", "--no-pretty", "--hide-error-context",
            "--show-error-codes", "--no-color-output",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode not in (0, 1):
        sys.stderr.write(proc.stderr)
        sys.stderr.write(proc.stdout)
        raise SystemExit(proc.returncode or 1)
    counts: Counter[tuple[str, str]] = Counter()
    for raw in proc.stdout.splitlines():
        match = _MYPY_ERROR.match(raw.strip())
        if match is None:
            continue
        code = match.group("code") or "error"
        counts[(_rel(match.group("path")), code)] += 1
    return dict(counts)


def ignore_ids() -> list[str]:
    ids: list[str] = []
    for raw in AUDIT_IGNORE.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            ids.append(line)
    return ids


def run_audit() -> int:
    cmd = [
        sys.executable, "-m", "pip_audit",
        "-r", "requirements.txt",
        "-r", "requirements-dev.txt",
        "--progress-spinner", "off",
        "--desc", "off",
    ]
    for vuln_id in ignore_ids():
        cmd.extend(["--ignore-vuln", vuln_id])
    proc = subprocess.run(cmd, cwd=ROOT, check=False)
    return proc.returncode


def _report(kind: str, problems: list[str]) -> int:
    if not problems:
        print(f"{kind}: no new findings")
        return 0
    print(f"{kind}: {len(problems)} new finding group(s)")
    for line in problems:
        print(line)
    return 1


def main(argv: list[str]) -> int:
    command = argv[1] if len(argv) > 1 else ""
    if command == "lint":
        return _report("ruff", excess(ruff_counts(), load_baseline(RUFF_BASELINE)))
    if command == "typecheck":
        return _report("mypy", excess(mypy_counts(), load_baseline(MYPY_BASELINE)))
    if command == "audit":
        return run_audit()
    if command == "write":
        write_baseline(RUFF_BASELINE, ruff_counts())
        write_baseline(MYPY_BASELINE, mypy_counts())
        print("wrote quality/ruff-baseline.txt and quality/mypy-baseline.txt")
        return 0
    sys.stderr.write("usage: python scripts/dev_gate.py lint|typecheck|audit|write\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
