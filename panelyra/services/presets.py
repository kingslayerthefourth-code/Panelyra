"""Saved workflow presets with durable local JSON storage."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

from panelyra.services.paths import CONFIG_DIR


PRESETS_FILE = CONFIG_DIR / "workflow_presets.json"

DEFAULT_PRESETS: dict[str, dict[str, Any]] = {
    "Balanced review": {
        "scroll_preset": "Balanced",
        "chapter_routing": "auto",
        "max_chapters": 5,
        "use_thanks_marker": True,
    },
    "Fast staging pass": {
        "scroll_preset": "Performance",
        "chapter_routing": "auto",
        "max_chapters": 10,
        "use_thanks_marker": False,
    },
    "Single chapter diagnostic": {
        "scroll_preset": "Reliable",
        "chapter_routing": "auto",
        "max_chapters": 1,
        "use_thanks_marker": False,
    },
}


def load_presets() -> dict[str, dict[str, Any]]:
    if not PRESETS_FILE.exists():
        save_presets(DEFAULT_PRESETS)
        return dict(DEFAULT_PRESETS)
    try:
        payload = json.loads(PRESETS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return dict(DEFAULT_PRESETS)
    if not isinstance(payload, dict):
        return dict(DEFAULT_PRESETS)
    return {
        str(name): dict(settings)
        for name, settings in payload.items()
        if isinstance(settings, dict)
    }


def save_presets(presets: dict[str, dict[str, Any]]) -> None:
    PRESETS_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = PRESETS_FILE.with_name(f".{PRESETS_FILE.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(presets, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, PRESETS_FILE)
