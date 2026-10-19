#!/usr/bin/env python3
"""Regression coverage for user-facing browser disconnect handling."""

import sys
from pathlib import Path

from selenium.common.exceptions import WebDriverException

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

import bot
import manhwa_ocr_downloader as downloader


def main() -> int:
    lost = WebDriverException("invalid session id: session deleted as the browser has closed")
    ordinary = WebDriverException("element click intercepted")

    assert bot.driver_session_lost(lost)
    assert not bot.driver_session_lost(ordinary)
    assert "completed work is safe" in bot.friendly_browser_error(lost).lower()
    assert "stacktrace" not in bot.friendly_browser_error(lost).lower()

    assert downloader.driver_session_lost(lost)
    assert not downloader.driver_session_lost(ordinary)
    assert "completed downloads are safe" in downloader.friendly_browser_error(lost).lower()

    print("Browser-loss regression checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
