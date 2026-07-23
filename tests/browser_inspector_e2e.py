#!/usr/bin/env python3
"""Visible-Chrome smoke test for the optional Copilot DOM/screenshot inspector."""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import quote

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

import bot
from panelyra.assistant.browser import inspect_chrome_page


def main() -> int:
    session = bot.build_driver()
    try:
        html = """
        <html>
          <head><title>Panelyra Inspector Fixture</title></head>
          <body>
            <main>
              <h1>Inspector fixture</h1>
              <textarea placeholder="Comment text"></textarea>
              <button aria-label="Submit fixture">Send</button>
            </main>
          </body>
        </html>
        """
        session.driver.get(f"data:text/html,{quote(html)}")
        time.sleep(1)
        with tempfile.TemporaryDirectory() as tmpdir:
            snapshot = inspect_chrome_page(
                include_screenshot=True,
                screenshot_dir=Path(tmpdir),
            )
            assert snapshot.get("available"), snapshot
            assert snapshot.get("title") == "Panelyra Inspector Fixture"
            assert any(item.get("text") == "Send" for item in snapshot.get("buttons", []))
            assert any(
                item.get("placeholder") == "Comment text"
                for item in snapshot.get("inputs", [])
            )
            screenshot_path = Path(str(snapshot.get("screenshot_path", "")))
            assert screenshot_path.is_file()
            assert screenshot_path.stat().st_size > 0
        print("Browser inspector end-to-end check passed.")
        return 0
    finally:
        bot.close_driver_session(session)


if __name__ == "__main__":
    raise SystemExit(main())
