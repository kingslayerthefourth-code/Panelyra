"""Named Engage selector profiles stored as local JSON."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

from panelyra.services.paths import CONFIG_DIR
from reader_routing import (
    DEFAULT_CHAPTER_LIST_SELECTOR,
    DEFAULT_NEXT_CHAPTER_SELECTOR,
    DEFAULT_NEXT_PAGE_SELECTOR,
    DEFAULT_PAGE_COUNTER_SELECTOR,
)


SELECTOR_PROFILES_FILE = CONFIG_DIR / "selector_profiles.json"

DEFAULT_SELECTOR_PROFILES: dict[str, dict[str, str]] = {
    "Generic chapter reader": {
        "next_link": DEFAULT_NEXT_CHAPTER_SELECTOR,
        "next_page": DEFAULT_NEXT_PAGE_SELECTOR,
        "page_counter": DEFAULT_PAGE_COUNTER_SELECTOR,
        "chapter_list": DEFAULT_CHAPTER_LIST_SELECTOR,
        "comment_editor": (
            "textarea[name='comment'], textarea#comment, textarea.comment, textarea, "
            "[contenteditable='true'], [role='textbox'], [placeholder*='comment' i], "
            "[aria-label*='comment' i], [data-testid*='comment' i]"
        ),
        "submit": (
            "button[type='submit'], input[type='submit'], button.submit-comment, "
            "button[name='submit'], #submit, .comment-submit, .submit-comment, "
            "button[aria-label*='send' i], button[title*='send' i], "
            "button[aria-label*='post' i], button[title*='post' i]"
        ),
        "thanks": "button, a",
        "confirmation": ".comment-success, .notice-success, .toast-success, [data-testid='comment-success']",
    },
    "Minimal HTML form": {
        "next_link": "a[rel='next'], a.next",
        "next_page": "a.next-page, button.next-page, [aria-label*='next page' i]",
        "page_counter": ".page-counter, [aria-label*='page' i]",
        "chapter_list": "a[href*='chapter' i], a[href*='episode' i]",
        "comment_editor": "textarea[name='comment'], textarea",
        "submit": "button[type='submit'], input[type='submit']",
        "thanks": "button, a",
        "confirmation": ".success, [role='status']",
    },
    "Paginated manga reader": {
        "next_link": DEFAULT_NEXT_CHAPTER_SELECTOR,
        "next_page": DEFAULT_NEXT_PAGE_SELECTOR,
        "page_counter": DEFAULT_PAGE_COUNTER_SELECTOR,
        "chapter_list": DEFAULT_CHAPTER_LIST_SELECTOR,
        "comment_editor": (
            "textarea[name='comment'], textarea, [contenteditable='true'], "
            "[role='textbox'], [placeholder*='comment' i]"
        ),
        "submit": (
            "button[type='submit'], input[type='submit'], "
            "button[aria-label*='send' i], button[aria-label*='post' i]"
        ),
        "thanks": "button, a",
        "confirmation": ".comment-success, .toast-success, [role='status']",
    },
}

PROFILE_KEYS = (
    "next_link",
    "next_page",
    "page_counter",
    "chapter_list",
    "comment_editor",
    "submit",
    "thanks",
    "confirmation",
)


def load_selector_profiles() -> dict[str, dict[str, str]]:
    if not SELECTOR_PROFILES_FILE.exists():
        save_selector_profiles(DEFAULT_SELECTOR_PROFILES)
        return {name: dict(values) for name, values in DEFAULT_SELECTOR_PROFILES.items()}
    try:
        payload = json.loads(SELECTOR_PROFILES_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {name: dict(values) for name, values in DEFAULT_SELECTOR_PROFILES.items()}
    if not isinstance(payload, dict):
        return {name: dict(values) for name, values in DEFAULT_SELECTOR_PROFILES.items()}
    profiles: dict[str, dict[str, str]] = {}
    for name, values in payload.items():
        if not isinstance(values, dict):
            continue
        defaults = DEFAULT_SELECTOR_PROFILES.get(str(name), DEFAULT_SELECTOR_PROFILES["Generic chapter reader"])
        profiles[str(name)] = {
            key: str(values.get(key, defaults.get(key, ""))).strip()
            for key in PROFILE_KEYS
        }
    return profiles or {name: dict(values) for name, values in DEFAULT_SELECTOR_PROFILES.items()}


def save_selector_profiles(profiles: dict[str, dict[str, Any]]) -> None:
    SELECTOR_PROFILES_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = SELECTOR_PROFILES_FILE.with_name(
        f".{SELECTOR_PROFILES_FILE.name}.{uuid.uuid4().hex}.tmp"
    )
    temporary.write_text(json.dumps(profiles, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, SELECTOR_PROFILES_FILE)
