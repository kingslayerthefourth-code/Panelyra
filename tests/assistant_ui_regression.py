#!/usr/bin/env python3
"""Streamlit regression for Copilot conversation, evidence, and actions."""

from __future__ import annotations

import sys
from pathlib import Path

from streamlit.testing.v1 import AppTest

APP_PATH = Path(__file__).resolve().parents[1] / "app.py"
sys.path.insert(0, str(APP_PATH.parent))


def main() -> int:
    app = AppTest.from_file(str(APP_PATH), default_timeout=20).run()
    app.session_state["page"] = "Assistant"
    app.session_state["assistant_use_model"] = False
    app.run()
    assert not app.exception
    assert app.radio
    assert app.radio[0].label == "Assistant mode"
    assert "Diagnose" in app.radio[0].options
    assert app.chat_input

    app.radio[0].set_value("Diagnose").run()
    app.chat_input[0].set_value("Why did the latest Capture run fail?").run()
    assert not app.exception
    messages = list(app.session_state["assistant_messages"])
    assert messages[-1]["role"] == "assistant"
    assert messages[-1]["mode"] == "Diagnose"
    assert messages[-1]["evidence"]
    assert any(action["kind"] == "prepare_retry" for action in messages[-1]["actions"])

    buttons = {button.label: button for button in app.button}
    assert "Open Capture Live" in buttons
    buttons["Open Capture Live"].click().run()
    assert app.session_state["page"] == "Capture"
    assert app.session_state["capture_view"] == "Live"

    print("Assistant UI regression checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
