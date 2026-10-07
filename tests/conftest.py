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


@_pytest.fixture(autouse=True)
def _no_llm_call_log(monkeypatch):
    """AI call logging writes training data to data/llm_calls/; tests must not."""
    try:
        from config.settings import settings
        monkeypatch.setattr(settings, "llm_call_log", False)
    except Exception:  # noqa: BLE001
        pass


@_pytest.fixture(autouse=True)
def _no_live_perp_prices(monkeypatch):
    """Swing pricing fetches Binance futures prices live; tests must not touch the network (one test opts in)."""
    try:
        from config.settings import settings
        monkeypatch.setattr(settings, "swing_perp_prices", False)
        # Binance min orders skip most trades of the small test wallets; tests opt in (test_swing_sizing_binance)
        monkeypatch.setattr(settings, "swing_binance_rules", False)
    except Exception:  # noqa: BLE001
        pass
