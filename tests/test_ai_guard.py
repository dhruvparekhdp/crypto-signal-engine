import time

import pytest

from scripts import _ai_guard as g


def test_refused_in_the_day_unless_forced(monkeypatch):
    monkeypatch.setattr(g.time, "gmtime", lambda: time.struct_time((2026, 10, 18, 9, 0, 0, 6, 291, 0)))
    with pytest.raises(SystemExit):
        g.require_quota_window(["x"])
    g.require_quota_window(["x", "--force"])


def test_allowed_before_the_reset(monkeypatch):
    monkeypatch.setattr(g.time, "gmtime", lambda: time.struct_time((2026, 10, 18, 21, 0, 0, 6, 291, 0)))
    g.require_quota_window(["x"])
