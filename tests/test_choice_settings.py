"""Choice fields (e.g. delta_india_mode) must render as a dropdown, not a number box."""
from config.overrides import BY_KEY, describe
from config.settings import settings


def test_delta_mode_is_choice_with_options():
    f = BY_KEY["delta_india_mode"]
    assert f.kind == "choice"
    assert f.choices == ("off", "shadow", "live")
    row = next(r for r in describe(settings, {}) if r["key"] == "delta_india_mode")
    assert row["kind"] == "choice"
    assert row["choices"] == ["off", "shadow", "live"]


def test_settings_page_renders_choice_as_select():
    from scheduler.settings_page import PAGE
    assert "kind==='choice'" in PAGE or "f.kind==='choice'" in open(
        "scheduler/settings_page.py").read()
    src = open("scheduler/settings_page.py").read()
    assert "<select" in src and "f.kind==='choice'" in src
