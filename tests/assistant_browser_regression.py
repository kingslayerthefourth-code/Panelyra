#!/usr/bin/env python3
"""Regression coverage for Lyra browser-port routing."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

from panelyra.assistant.browser import list_debug_tabs


class Response:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return json.dumps([
            {"type": "page", "url": "https://reader.test/chapter-1", "title": "Chapter 1"},
            {"type": "other", "url": "devtools://devtools"},
        ]).encode()


def main() -> int:
    requested: list[str] = []

    def fake_urlopen(url: str, timeout: float = 0):
        requested.append(url)
        assert timeout == 2.0
        return Response()

    with patch("panelyra.assistant.browser.urlopen", side_effect=fake_urlopen):
        tabs = list_debug_tabs(debug_port=9223)
    assert requested == ["http://127.0.0.1:9223/json/list"]
    assert len(tabs) == 1 and tabs[0]["title"] == "Chapter 1"
    print("Assistant browser regression checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
