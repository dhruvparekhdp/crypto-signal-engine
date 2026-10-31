"""Market moves styling (31 Oct 2026): tool pages load portal.css, whose bare class rules collided with the page's
own classes, and the body grid let the coin-card row grow to ~11,700px."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "scheduler" / "portal"


def _bare_classes(css: str) -> set[str]:
    out = set()
    for sel in re.findall(r"(?:^|})\s*([^{}@]+)\{", css):
        for part in sel.split(","):
            m = re.match(r"\s*\.([A-Za-z0-9_-]+)\s*$", part)
            if m:
                out.add(m.group(1))
    return out


def test_every_portal_class_the_moves_page_reuses_is_overridden_for_tool_pages():
    from scheduler.health import _MOVES_HTML
    portal = _bare_classes((ROOT / "portal.css").read_text())
    tools = (ROOT / "command-tools.css").read_text()
    used = set(re.findall(r'class="([^"{}]+)"', _MOVES_HTML)) | set(re.findall(r"class='([^'{}]+)'", _MOVES_HTML))
    used = {c for s in used for c in s.split()} | {"coin", "pos", "neg", "hist", "gauge"}
    harmless = {"dim", "mono", "up", "dn", "wrap", "muted", "btn"}
    clash = sorted((portal & used) - harmless)
    missing = [c for c in clash if f".cmd-tool .{c}" not in tools and f".cmd-tool .grid .{c}" not in tools]
    assert not missing, f"portal.css styles these Market moves classes with no tool-page override: {missing}"


def test_the_tool_body_grid_cannot_grow_past_the_screen():
    assert ".cmd-tool #main-body{grid-template-columns:minmax(0,1fr)}" in (ROOT / "command-tools.css").read_text()
