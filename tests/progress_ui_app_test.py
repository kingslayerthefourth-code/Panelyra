#!/usr/bin/env python3
"""Focused Streamlit integration checks for Panelyra's Mac progress surfaces."""

from __future__ import annotations

import sys
from pathlib import Path

from streamlit.testing.v1 import AppTest


APP_PATH = Path(__file__).resolve().parents[1] / "app.py"
sys.path.insert(0, str(APP_PATH.parent))


def html_bodies(app: AppTest) -> str:
    return "\n".join(str(getattr(element, "body", "")) for element in app.get("html"))


def main() -> int:
    app = AppTest.from_file(str(APP_PATH), default_timeout=30).run()
    assert not app.exception, app.exception

    for page, bot_name in (("Engage", "engage"), ("Capture", "capture")):
        app.session_state["page"] = page
        app.session_state[f"{page.lower()}_view"] = "Live"
        app.run()
        assert not app.exception, f"{page} Live raised: {app.exception}"
        bodies = html_bodies(app)
        assert "panelyra-progress" in bodies, f"{page} Live did not render the custom rail"
        assert f"p-workspace-{bot_name}" in bodies
        assert 'aria-label="Run stages"' not in bodies
    assert "def render_floating_chat_widget" not in APP_PATH.read_text()

    app.session_state["page"] = "Settings"
    app.run()
    assert not app.exception, app.exception
    labels = {widget.label for widget in app.selectbox}
    assert "Progress animation" in labels
    toggle_labels = {widget.label for widget in app.toggle}
    assert "Show progress history" in toggle_labels
    assert "Mac completion notifications" in toggle_labels

    source = APP_PATH.read_text(encoding="utf-8")
    assert "render_run_dock()" in source
    assert "auto_advance_completed_workspace(page)" not in source
    assert "st.progress(" not in source
    print("Progress UI AppTest checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
