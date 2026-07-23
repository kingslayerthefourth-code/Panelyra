#!/usr/bin/env python3
"""Regression coverage for Panelyra navigation and sequential workspaces."""

from __future__ import annotations

from pathlib import Path
import sys

from streamlit.testing.v1 import AppTest


APP_PATH = Path(__file__).resolve().parents[1] / "app.py"
sys.path.insert(0, str(APP_PATH.parent))


def button_map(app: AppTest) -> dict[str, object]:
    return {button.label: button for button in app.button}


def assert_ready(app: AppTest, page: str) -> None:
    assert not app.exception, f"{page} raised a Streamlit exception: {app.exception}"
    rendered = "\n".join(str(block.value) for block in app.markdown)
    assert "Panelyra" in rendered, f"{page} did not render Panelyra branding"
    repeated_warnings = {
        "I own, administer, or have permission to use this target",
        "I have permission to archive this target",
        "I own, administer, or have permission to use this editor",
    }
    labels = {checkbox.label for checkbox in app.checkbox}
    assert not labels.intersection(repeated_warnings), f"{page} repeated the onboarding acknowledgment"


def open_workspace(app: AppTest, dashboard_label: str) -> None:
    buttons = button_map(app)
    assert dashboard_label in buttons
    buttons[dashboard_label].click().run()
    assert_ready(app, dashboard_label)


def main() -> int:
    app = AppTest.from_file(str(APP_PATH), default_timeout=20).run()
    assert_ready(app, "Dashboard")

    for label in ("Start Comment Testing", "Start Chapter Capture", "Start Creative Studio"):
        assert label in button_map(app), f"Dashboard is missing {label}"

    open_workspace(app, "Start Comment Testing")
    assert "Continue to behavior" in button_map(app)
    app.session_state["start_url"] = "http://127.0.0.1:8766/content/example/chapter-1"
    app.session_state["authorization_confirmed"] = True
    app.run()
    button_map(app)["Continue to behavior"].click().run()
    assert "Continue to review" in button_map(app)
    button_map(app)["Continue to review"].click().run()
    assert "Open browser and prepare" in button_map(app)
    assert "Start exact URL" in button_map(app)
    assert_ready(app, "Engage review")

    app.session_state["page"] = "Dashboard"
    app.run()
    open_workspace(app, "Start Chapter Capture")
    capture_url = (
        "https://en-thunderscans.com/"
        "since-im-immune-to-corruption-i-start-with-a-disaster-class-demon-sword-chapter-1/"
    )
    app.text_input(key="ocr_start_url").set_value(capture_url).run()
    assert "Continue to output" in button_map(app)
    button_map(app)["Continue to output"].click().run()
    assert "Continue to review" in button_map(app)
    button_map(app)["Continue to review"].click().run()
    assert app.session_state["ocr_start_url"] == capture_url
    assert "Start capture" in button_map(app)
    assert not button_map(app)["Start capture"].disabled
    assert_ready(app, "Capture review")

    app.session_state["page"] = "Dashboard"
    app.run()
    open_workspace(app, "Start Creative Studio")
    assert "Continue to design" in button_map(app)
    assert_ready(app, "Studio source")

    for page in ("Assistant", "Run History", "Settings", "Help"):
        app.session_state["page"] = page
        app.run()
        assert_ready(app, page)
        if page == "Assistant":
            assert app.radio
            assert app.radio[0].label == "Assistant mode"
            assert app.chat_input

    print("Dashboard and workspace readiness checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
