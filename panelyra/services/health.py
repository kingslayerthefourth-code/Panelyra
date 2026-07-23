"""Local environment health checks for the Panelyra control plane."""

from __future__ import annotations

import importlib.util
import os
import platform
import shutil
import sys
from pathlib import Path

from panelyra.services.paths import PROJECT_ROOT


def chrome_application_path() -> Path:
    return Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")


def chrome_profile_path() -> Path:
    chrome_root = Path.home() / "Library/Application Support/Google/Chrome"
    preferred = chrome_root / "PanelyraProfile"
    legacy = chrome_root / "AutomationProfile"
    if legacy.exists() and not preferred.exists():
        return legacy
    return preferred


def environment_health() -> list[dict[str, str]]:
    checks: list[dict[str, str]] = []

    python_ok = sys.version_info >= (3, 9)
    checks.append(
        {
            "check": "Python",
            "status": "Ready" if python_ok else "Action needed",
            "detail": platform.python_version(),
        }
    )

    for dependency in ("streamlit", "selenium", "webdriver_manager", "requests", "PIL"):
        available = importlib.util.find_spec(dependency) is not None
        checks.append(
            {
                "check": dependency,
                "status": "Ready" if available else "Missing",
                "detail": "Installed" if available else "Install from requirements.txt",
            }
        )

    chrome_path = chrome_application_path()
    checks.append(
        {
            "check": "Google Chrome",
            "status": "Ready" if chrome_path.exists() else "Missing",
            "detail": str(chrome_path),
        }
    )

    profile_path = chrome_profile_path()
    checks.append(
        {
            "check": "Browser profile",
            "status": "Ready" if profile_path.exists() else "Not initialized",
            "detail": str(profile_path),
        }
    )

    writable = os.access(PROJECT_ROOT, os.W_OK)
    checks.append(
        {
            "check": "Workspace",
            "status": "Ready" if writable else "Read only",
            "detail": str(PROJECT_ROOT),
        }
    )

    free_bytes = shutil.disk_usage(PROJECT_ROOT).free
    checks.append(
        {
            "check": "Free storage",
            "status": "Ready" if free_bytes >= 1_000_000_000 else "Low",
            "detail": format_bytes(free_bytes),
        }
    )
    return checks


def format_bytes(value: int) -> str:
    size = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"
