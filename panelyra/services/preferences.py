"""Small durable preference store for first-run and retention settings."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

from panelyra.services.paths import CONFIG_DIR


PREFERENCES_FILE = CONFIG_DIR / "preferences.json"
DEFAULT_PREFERENCES: dict[str, Any] = {
    "onboarding_complete": False,
    "authorization_confirmed": False,
    "retention_days": 30,
}


def load_preferences() -> dict[str, Any]:
    values = dict(DEFAULT_PREFERENCES)
    if not PREFERENCES_FILE.exists():
        return values
    try:
        payload = json.loads(PREFERENCES_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return values
    if isinstance(payload, dict):
        values.update(payload)
    return values


def save_preferences(values: dict[str, Any]) -> bool:
    temporary: Path | None = None
    temporary = PREFERENCES_FILE.with_name(f".{PREFERENCES_FILE.name}.{uuid.uuid4().hex}.tmp")
    try:
        PREFERENCES_FILE.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps(values, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, PREFERENCES_FILE)
    except OSError:
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass
        return False
    return True
