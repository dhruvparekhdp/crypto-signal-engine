import os

# Default to in-memory SQLite during automated test runs so tests do not attempt
# to connect to remote VPC databases like AWS RDS when running locally.
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

# AI call budget (collectors/llm_budget.py) keeps its counts in a file; tests must not touch the real one.
import tempfile as _tempfile

os.environ.setdefault("LLM_BUDGET_PATH", os.path.join(_tempfile.mkdtemp(prefix="llm_budget_"), "budget.json"))

import pytest as _pytest


@_pytest.fixture(autouse=True)
def _fresh_llm_budget(tmp_path, monkeypatch):
    """Each test starts with an empty AI budget: one test's simulated 429 must not cool down the next test."""
    monkeypatch.setenv("LLM_BUDGET_PATH", str(tmp_path / "llm_budget.json"))
    try:
        from collectors import llm_budget
        monkeypatch.setattr(llm_budget, "_budget", None)
    except ImportError:
        pass
