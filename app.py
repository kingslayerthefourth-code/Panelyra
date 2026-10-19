#!/usr/bin/env python3
"""
Local Streamlit dashboard for running the sequential form-submission QA suite.

Run:
    streamlit run app.py
"""

from __future__ import annotations

import html
import base64
import json
import os
import random
import re
import signal
import socket
import subprocess
import sys
import time
import uuid
import zipfile
from collections import Counter
from datetime import datetime
from pathlib import Path
from statistics import median
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

import streamlit as st
from PIL import Image
from streamlit.components import v1 as components
from streamlit.runtime.scriptrunner import get_script_run_ctx

from automation_runtime import (
    RUNS_DIR,
    RunRecord,
    RunStore,
    TrackedProcessRef,
    active_run_records,
    browser_lock_owner,
    launch_macos_sleep_guard,
    list_run_records,
    normalize_web_url,
    preflight_url,
    reconcile_stale_run_records,
    update_run_record_by_pid,
)
from history_utils import append_jsonl_record, read_jsonl_records
from panelyra.assistant import (
    CONVERSATIONS,
    DEFAULT_LOCAL_MODEL,
    PANELYRA_SYSTEM_PROMPT,
    AssistantTurnPlan,
    CopilotContext,
    LocalModelClient,
    LocalModelError,
    PageContext,
    PanelyraKnowledgeService,
    WorkspaceRun,
    assistant_requests_browser_inspection,
    build_model_prompt,
    extract_log_events,
    needs_external_research,
    plan_assistant_turn,
)
from panelyra.assistant.browser import inspect_chrome_page
from panelyra.models.product import (
    NAVIGATION_GROUPS,
    PRODUCT_MODE,
    PRODUCT_NAME,
    PRODUCT_TAGLINE,
    PRODUCT_VERSION,
    WORKSPACES,
    runtime_type_for_page,
)
from panelyra.progress_ui import (
    PROGRESS_CSS,
    RunProgressModel,
    build_progress_html,
    extract_activity_items,
    resolve_health,
    resolve_progress_state,
)
from panelyra.services.health import environment_health, format_bytes
from panelyra.services.local_reader import start_local_reader_server
from panelyra.services.reader_progress import ReaderProgressStore
from panelyra.services.manhwa_cleaner import clean_chapter, detect_boundary_promos
from panelyra.services.paths import (
    DOWNLOADS_DIR,
    LEGACY_DOWNLOAD_DIR,
    PANELYRA_CACHE_DIR,
    PANELYRA_LOG_DIR,
    READER_PROGRESS_FILE,
    RESEARCH_CONTEXT_FILE,
    ensure_runtime_directories,
)
from panelyra.services.preferences import load_preferences, save_preferences
from panelyra.services.presets import load_presets, save_presets
from panelyra.services.selector_profiles import load_selector_profiles, save_selector_profiles
from panelyra.services.storage import prune_generated_files, storage_report
from panelyra.services.support import (
    TICKET_SEVERITIES,
    TICKET_STATUSES,
    create_ticket,
    read_tickets,
    update_ticket,
)


APP_DIR = Path(__file__).resolve().parent

# Keep the local tablet endpoint available while Panelyra is open. Pairing and
# certificate state remain on disk; do not require reopening Settings each run.
from tablet_ui import ensure_companion
# LAN listeners and USB watching are explicit connection tools, not app startup.
ensure_runtime_directories()
reconcile_stale_run_records()
ENV_FILE = APP_DIR / ".env"
BOT_PATH = APP_DIR / "bot.py"
NOTE_ART_BOT_PATH = APP_DIR / "note_art_bot.py"
MANHWA_OCR_BOT_PATH = APP_DIR / "manhwa_ocr_downloader.py"
BOT_PYTHON = APP_DIR / ".venv" / "bin" / "python"
HISTORY_FILE = APP_DIR / "data/history/comment_history.jsonl"
READ_MARKER_FILE = APP_DIR / "data/history/read_markers.jsonl"
RESEARCH_FILE = RESEARCH_CONTEXT_FILE
OWNER_REQUESTS_FILE = APP_DIR / "data/history/owner_requests.jsonl"
ASSISTANT_FEEDBACK_FILE = APP_DIR / "data/history/assistant_feedback.jsonl"
LOG_DIR = PANELYRA_LOG_DIR
LOG_DIR.mkdir(parents=True, exist_ok=True)
PYTHON_CACHE_DIR = PANELYRA_CACHE_DIR
PYTHON_CACHE_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR = APP_DIR / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
OLD_OCR_DOWNLOAD_DIR = LEGACY_DOWNLOAD_DIR
OCR_DOWNLOAD_DIR = DOWNLOADS_DIR
OCR_DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
MAX_CONSOLE_CHARS = 60_000
CONTENT_CONTAINER_SELECTOR = "article, main, .chapter-content, .entry-content, .post-content, .content"
MANHWA_CONTAINER_SELECTOR = "body"
LEGACY_MANHWA_CONTAINER_SELECTOR = ".reading-content, .chapter-content, .entry-content, article, main, .content"
MANHWA_IMAGE_SELECTOR = (
    "img.wp-manga-chapter-img, img.chapter-img, img[data-src], "
    "img[data-lazy-src], img[data-original], img[src], img"
)
OCR_CONFIDENCE = 0.45
NOTE_ART_SELECTOR_DEFAULTS = {
    "note_upload_input_selector": "input[type='file'][accept*='image'], input[type='file']",
    "note_upload_trigger_selector": (
        "button[aria-label*='upload'], button[title*='upload'], "
        "button[data-testid*='upload'], button[class*='upload'], "
        "label[for*='upload'], [role='button'][aria-label*='upload']"
    ),
    "note_editor_selector": (
        "textarea, [contenteditable='true'], [role='textbox'], "
        ".ProseMirror, [data-slate-editor='true']"
    ),
    "note_submit_selector": (
        "button[type='submit'], input[type='submit'], "
        "button[aria-label*='save'], button[title*='save'], "
        "button[aria-label*='publish'], button[title*='publish'], "
        "button[data-testid*='save'], button[data-testid*='publish']"
    ),
}


def display_path(value: Path | str | None) -> str:
    """Return a neutral, workspace-safe label for a local path shown in the UI."""
    if value is None:
        return "Not available"
    path = Path(str(value)).expanduser()
    try:
        relative = path.resolve().relative_to(APP_DIR.resolve())
    except (OSError, ValueError):
        return path.name or "Local location"
    if str(relative) == ".":
        return "Panelyra workspace"
    return f"Workspace/{relative.as_posix()}"


def redact_private_paths(text: str) -> str:
    """Prevent the local account name and absolute filesystem paths reaching the UI."""
    redacted = str(text)
    for private_root, replacement in (
        (str(APP_DIR), "[Panelyra workspace]"),
        (str(Path.home()), "[home]"),
    ):
        if private_root:
            redacted = redacted.replace(private_root, replacement)
    redacted = re.sub(r"/Users/[^/\s]+", "[home]", redacted)
    redacted = re.sub(r"[A-Za-z]:\\\\Users\\\\[^\\\s]+", "[home]", redacted)
    return redacted


READER_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


def reader_sort_key(path: Path) -> tuple[int, int | str, str]:
    """Sort zero-padded page/chapter folders numerically when possible."""
    number = re.search(r"(?:chapter|ch|episode)?[-_ ]*(\d+(?:\.\d+)?)", path.stem, re.I)
    if number:
        return (0, float(number.group(1)), path.name.lower())
    return (1, path.stem.lower(), path.name.lower())


def reader_catalog() -> dict[str, dict[str, object]]:
    from panelyra.services.reading_catalog import catalog
    return catalog(APP_DIR)


def _reader_cover_data(entry: dict[str, object]) -> str:
    """Return a small disposable data thumbnail without touching chapter files."""
    candidates: list[Path] = []
    saved_cover = APP_DIR / "data/reader_covers" / (str(entry.get("series_key", "")) + ".cover")
    if saved_cover.is_file():
        candidates.append(saved_cover)
    editions = entry.get("editions", {})
    if isinstance(editions, dict):
        for chapters in editions.values():
            if not isinstance(chapters, dict):
                continue
            for chapter in chapters.values():
                if isinstance(chapter, dict):
                    metadata = chapter.get("metadata", {})
                    if isinstance(metadata, dict):
                        raw_cover = metadata.get("cover") or metadata.get("cover_path")
                        if raw_cover:
                            cover_path = Path(str(raw_cover))
                            if cover_path.is_file():
                                candidates.append(cover_path)
                    candidates.extend(list(chapter.get("images", []))[:1])
    for path in candidates:
        try:
            with Image.open(path) as image:
                image.thumbnail((320, 420))
                from io import BytesIO
                output = BytesIO()
                image.convert("RGB").save(output, "JPEG", quality=78, optimize=True)
                return "data:image/jpeg;base64," + base64.b64encode(output.getvalue()).decode("ascii")
        except (OSError, ValueError):
            continue
    return ""


def _chapter_number(value: str) -> float:
    match = re.search(r"\d+(?:\.\d+)?", value)
    return float(match.group()) if match else -1.0


def _chapter_integrity(chapter: dict[str, object]) -> tuple[bool, str]:
    if 'status' in chapter:
        return (True, 'Ready') if chapter.get('healthy') and chapter.get('verified') else (False, str(chapter.get('error') or chapter['status']))
    images = [Path(item) for item in chapter.get("images", [])]
    if not images or any(not image.is_file() for image in images):
        return False, "Missing image"
    archive = Path(chapter["chapter_dir"]) / "chapter-pages.cbz"
    if archive.is_file():
        try:
            with zipfile.ZipFile(archive) as bundle:
                if bundle.testzip():
                    return False, "Invalid CBZ"
        except (OSError, zipfile.BadZipFile):
            return False, "Invalid CBZ"
    return True, "Ready"






@st.cache_data(ttl=15, show_spinner=False)
def cached_capture_page_count(root_value: str) -> int:
    """Count captured image files without rescanning the library on every rerun."""
    root = Path(root_value)
    total = 0
    if not root.exists():
        return total
    for metadata_path in root.rglob("metadata.json"):
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        images_dir = Path(str(metadata.get("images_dir") or metadata_path.parent / "images"))
        if not images_dir.is_absolute():
            images_dir = APP_DIR / images_dir
        if images_dir.exists():
            total += sum(
                1
                for item in images_dir.iterdir()
                if item.is_file() and item.suffix.lower() in READER_IMAGE_SUFFIXES
            )
    return total


@st.cache_resource(show_spinner=False)
def local_reader_service():
    """Start one loopback-only reader service for this local workspace."""
    return start_local_reader_server(APP_DIR)


def load_local_env_file(path: Path) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            os.environ.setdefault(key, value)


load_local_env_file(ENV_FILE)
PREFERENCES = load_preferences()
LOCAL_MODEL_CLIENT = None  # AI interface retired; do not connect to a model at startup.
KNOWLEDGE_SERVICE = None


def save_local_preferences(values: dict[str, object]) -> bool:
    """Merge local Mac preferences so one settings panel cannot erase another."""
    preferences = load_preferences()
    preferences.update(values)
    return save_preferences(preferences)


def clean_report_for_series(series: str) -> dict[str, object]:
    report_path = APP_DIR / "manhwa_clean" / f"{series}-clean" / "cleaning-report.json"
    if not report_path.is_file():
        return {}
    try:
        payload = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def render_cleaning_preview(image_path: Path, *, top: int = 0, bottom: int = 0, label: str) -> None:
    with Image.open(image_path) as image:
        width, height = image.size
        top = max(0, min(top, height - 1))
        bottom = max(0, min(bottom, height - top - 1))
        preview_height = min(height - top - bottom, max(1, int(width * 2.2)))
        if bottom:
            start = max(top, height - bottom - preview_height)
        else:
            start = top
        preview = image.crop((0, start, width, min(height - bottom, start + preview_height)))
    st.image(preview, caption=label, width=280)


def start_clean_review(series: str, chapter: str, chapter_dir: Path, *, reuse_previous: bool = False) -> None:
    images = sorted(
        (path for path in (chapter_dir / "images").iterdir() if path.suffix.lower() in READER_IMAGE_SUFFIXES),
        key=reader_sort_key,
    )
    if not images:
        st.session_state.reader_clean_review = {}
        return
    suggestion = detect_boundary_promos(images[0], images[-1])
    if reuse_previous:
        report = clean_report_for_series(series)
        chapters = report.get("chapters", {}) if isinstance(report, dict) else {}
        if isinstance(chapters, dict):
            previous = None
            for name in sorted(chapters, key=lambda value: (0, int(value)) if str(value).isdigit() else (1, str(value))):
                if name != chapter and isinstance(chapters[name], dict):
                    previous = chapters[name]
            if isinstance(previous, dict):
                first = previous.get("first_page", {})
                last = previous.get("last_page", {})
                if isinstance(first, dict) and isinstance(last, dict):
                    suggestion = type(suggestion)(
                        int(first.get("top_crop_pixels", suggestion.first_top) or 0),
                        int(last.get("bottom_crop_pixels", suggestion.last_bottom) or 0),
                        "reused",
                        "Reused crop values from an earlier cleaned chapter in this series.",
                    )
    st.session_state.reader_clean_review = {
        "series": series,
        "chapter": chapter,
        "first_top": int(suggestion.first_top),
        "last_bottom": int(suggestion.last_bottom),
        "reason": suggestion.reason,
        "confidence": suggestion.confidence,
    }


def original_reader_selection(catalog: dict[str, dict[str, object]]) -> tuple[str, dict[str, object], dict[str, dict[str, object]], list[str]] | None:
    if not catalog:
        return None
    series_options = sorted(catalog, key=lambda key: str(catalog[key]["title"]).lower())
    if st.session_state.reader_series not in series_options:
        st.session_state.reader_series = series_options[0]
    selected_series = str(st.session_state.reader_series)
    selected_entry = catalog[selected_series]
    editions = selected_entry["editions"]
    if not isinstance(editions, dict) or "Original" not in editions:
        return None
    original_chapters = editions["Original"]
    if not isinstance(original_chapters, dict):
        return None
    chapter_options = sorted(original_chapters, key=lambda value: (0, int(value)) if value.isdigit() else (1, value))
    if st.session_state.reader_chapter not in chapter_options:
        st.session_state.reader_chapter = chapter_options[0]
    return selected_series, selected_entry, original_chapters, chapter_options


def render_cleaner_review(catalog: dict[str, dict[str, object]]) -> None:
    catalog = {key: entry for key, entry in catalog.items() if entry.get("editions", {}).get("Original")}
    selection = original_reader_selection(catalog)
    if selection is None:
        st.info("Trimming needs an Original chapter with image files.")
        return

    selected_series, selected_entry, original_chapters, chapter_options = selection
    series_options = sorted(catalog, key=lambda key: str(catalog[key]["title"]).lower())
    selected_series = st.selectbox(
        "Choose a series",
        options=series_options,
        key="reader_series",
        format_func=lambda key: str(catalog[key]["title"]),
        help="Open this list and scroll to the series you want to clean.",
    )
    selected_entry = catalog[selected_series]
    editions = selected_entry["editions"]
    original_chapters = editions["Original"] if isinstance(editions, dict) else {}
    chapter_options = sorted(original_chapters, key=lambda value: (0, int(value)) if value.isdigit() else (1, value))
    if st.session_state.reader_chapter not in chapter_options:
        st.session_state.reader_chapter = chapter_options[0]

    picker_columns = st.columns([2, 1])
    picker_columns[0].caption(f"Chapter {st.session_state.reader_chapter}")
    clean_report = clean_report_for_series(selected_series)
    clean_chapters = editions.get("Clean", {}) if isinstance(editions, dict) else {}
    clean_exists = st.session_state.reader_chapter in clean_chapters
    picker_columns[1].metric("Clean status", "Ready" if clean_exists else "Not created")

    current_chapter = st.session_state.reader_chapter
    current_entry = original_chapters[current_chapter]
    actions = st.columns(4)
    if actions[0].button("Review Chapter", key="cleaner_review_current", type="primary", width="stretch"):
        start_clean_review(selected_series, current_chapter, Path(current_entry["chapter_dir"]))
        st.rerun()
    if actions[1].button("Reuse Last Crop", key="cleaner_reuse_current", width="stretch"):
        start_clean_review(selected_series, current_chapter, Path(current_entry["chapter_dir"]), reuse_previous=True)
        st.rerun()
    if actions[2].button("Back to Library", key="cleaner_open_reader", width="stretch"):
        set_page("Reader")
        st.rerun()
    if actions[3].button("View Report", key="cleaner_report", disabled=not bool(clean_report), width="stretch"):
        st.session_state.reader_clean_review = {
            "series": selected_series,
            "chapter": current_chapter,
            "show_report": True,
        }
        st.rerun()

    selected_batch = st.multiselect(
        "Cleaning queue",
        options=chapter_options,
        default=st.session_state.reader_clean_batch,
        key="reader_clean_batch",
        format_func=lambda value: f"Chapter {value}",
        help="Each queued chapter opens in this review page before Panelyra writes a clean copy.",
    )
    if st.button("Start Queue", key="cleaner_batch_start", disabled=not selected_batch, width="stretch"):
        first_chapter = selected_batch[0]
        st.session_state.reader_chapter = first_chapter
        start_clean_review(selected_series, first_chapter, Path(original_chapters[first_chapter]["chapter_dir"]))
        st.rerun()

    review = st.session_state.get("reader_clean_review", {})
    if isinstance(review, dict) and review.get("series") == selected_series:
        if review.get("show_report"):
            with st.expander("Cleaning report", expanded=True):
                st.json(clean_report or {"status": "No cleaning report found yet."})
            return
        if review.get("chapter") not in original_chapters:
            return
        review_chapter = str(review["chapter"])
        review_entry = original_chapters[review_chapter]
        review_images = review_entry["images"]
        first_image = Path(review_images[0])
        last_image = Path(review_images[-1])
        with Image.open(first_image) as first:
            first_height = first.height
        with Image.open(last_image) as last:
            last_height = last.height
        st.subheader(f"Review cleaning for Chapter {review_chapter}")
        st.caption(str(review.get("reason") or "Move the crop handles, preview, then apply."))
        first_top = st.slider(
            "First page top crop",
            min_value=0,
            max_value=max(0, first_height - 1),
            value=min(int(review.get("first_top", 0) or 0), max(0, first_height - 1)),
            step=10,
            key=f"reader_clean_first_top_{selected_series}_{review_chapter}",
            help="Removes pixels from the top of the first image.",
        )
        last_bottom = st.slider(
            "Last page bottom crop",
            min_value=0,
            max_value=max(0, last_height - 1),
            value=min(int(review.get("last_bottom", 0) or 0), max(0, last_height - 1)),
            step=10,
            key=f"reader_clean_last_bottom_{selected_series}_{review_chapter}",
            help="Removes pixels from the bottom of the last image.",
        )
        preview_columns = st.columns(4)
        with preview_columns[0]:
            render_cleaning_preview(first_image, label="First page before")
        with preview_columns[1]:
            render_cleaning_preview(first_image, top=first_top, label="First page after")
        with preview_columns[2]:
            render_cleaning_preview(last_image, label="Last page before")
        with preview_columns[3]:
            render_cleaning_preview(last_image, bottom=last_bottom, label="Last page after")

        apply_columns = st.columns(3)
        if apply_columns[0].button("Apply Clean Copy", key="reader_clean_apply", type="primary", width="stretch"):
            try:
                result = clean_chapter(
                    Path(review_entry["chapter_dir"]).parent,
                    APP_DIR / "manhwa_clean",
                    review_chapter,
                    first_top=first_top,
                    last_bottom=last_bottom,
                    app_dir=APP_DIR,
                )
                st.success(f"Cleaned Chapter {review_chapter}. CBZ rebuilt at {display_path(result.archive_file)}.")
                st.cache_data.clear()
                queue = [value for value in st.session_state.reader_clean_batch if value != review_chapter]
                st.session_state.reader_clean_batch = queue
                if queue:
                    next_chapter = queue[0]
                    st.session_state.reader_chapter = next_chapter
                    start_clean_review(selected_series, next_chapter, Path(original_chapters[next_chapter]["chapter_dir"]))
                else:
                    st.session_state.reader_clean_review = {}
                st.rerun()
            except Exception as error:
                st.error(f"Could not clean this chapter: {error}")
        if apply_columns[1].button("Reset Suggestion", key="reader_clean_reset", width="stretch"):
            start_clean_review(selected_series, review_chapter, Path(review_entry["chapter_dir"]))
            st.rerun()
        if apply_columns[2].button("Cancel", key="reader_clean_cancel", width="stretch"):
            st.session_state.reader_clean_review = {}
            st.rerun()

DEFAULT_SETTINGS = {
    "default_landing_page": str(PREFERENCES.get("default_landing_page", "Library")),
    "compact_ui": bool(PREFERENCES.get("compact_ui", False)),
    "compact_sidebar": bool(PREFERENCES.get("compact_sidebar", False)),
    "show_sidebar_paths": False,
    "console_max_chars": int(PREFERENCES.get("console_max_chars", 60_000)),
    "auto_refresh_seconds": int(PREFERENCES.get("auto_refresh_seconds", 2)),
    "progress_animation": str(PREFERENCES.get("progress_animation", "Subtle")),
    "progress_history": bool(PREFERENCES.get("progress_history", True)),
    "mac_notifications": bool(PREFERENCES.get("mac_notifications", False)),
    "history_rows": int(PREFERENCES.get("history_rows", 30)),
    "top_keywords": int(PREFERENCES.get("top_keywords", 10)),
    "reflection_every_n_comments": int(PREFERENCES.get("reflection_every_n_comments", 5)),
    "assistant_enabled": True,
    "assistant_internet_endpoint": str(PREFERENCES.get("assistant_internet_endpoint", "")),
    "assistant_history_limit": int(PREFERENCES.get("assistant_history_limit", 40)),
    "assistant_save_history": bool(PREFERENCES.get("assistant_save_history", True)),
    "assistant_local_model": str(PREFERENCES.get("assistant_local_model", DEFAULT_LOCAL_MODEL)),
    "assistant_model": str(PREFERENCES.get("assistant_model", os.getenv("OPENAI_MODEL", "gpt-4.1-mini"))),
    "assistant_web_search_enabled": bool(PREFERENCES.get("assistant_web_search_enabled", False)),
    "assistant_use_openai_web": bool(PREFERENCES.get("assistant_use_openai_web", False)),
    "assistant_use_model": bool(PREFERENCES.get("assistant_use_model", True)),
    "assistant_show_evidence": bool(PREFERENCES.get("assistant_show_evidence", True)),
    "assistant_allow_browser_inspection": bool(PREFERENCES.get("assistant_allow_browser_inspection", True)),
    "assistant_generated_plan": "",
    "show_lyra_in_immersive_reader": bool(PREFERENCES.get("show_lyra_in_immersive_reader", False)),
    "onboarding_complete": bool(PREFERENCES.get("onboarding_complete", False)),
    "authorization_confirmed": bool(PREFERENCES.get("authorization_confirmed", False)),
    "retention_days": int(PREFERENCES.get("retention_days", 30)),
    "selected_workflow_preset": "Balanced review",
    "saved_preset_name": "",
    "selected_selector_profile": "Generic chapter reader",
    "selector_profile_name": "",
    "run_history_search": "",
}

WORKSPACE_DRAFT_KEYS = (
    "start_url",
    "max_chapters",
    "selected_workflow_preset",
    "selected_selector_profile",
    "scroll_preset",
    "custom_scroll_min_px",
    "custom_scroll_max_px",
    "custom_scroll_min_delay",
    "custom_scroll_max_delay",
    "custom_type_min_delay",
    "custom_type_max_delay",
    "custom_cooldown_min",
    "custom_cooldown_max",
    "custom_submit_wait",
    "chapter_routing",
    "use_thanks_marker",
    "content_mode",
    "content_container_selector",
    "ocr_image_selector",
    "ocr_confidence",
    "research_file",
    "research_endpoint",
    "review_first_comment",
    "comment_preview_subject",
    "comment_preview_text",
    "ocr_start_url",
    "ocr_custom_output_dir",
    "ocr_max_chapters",
    "ocr_save_images",
    "ocr_create_archive",
    "ocr_save_text",
    "ocr_reader_layout",
    "ocr_selector_profile",
    "ocr_container_selector",
    "ocr_download_image_selector",
    "ocr_download_confidence",
    "note_url",
    "studio_image_path",
    "studio_preview_path",
    "word_art_text",
    "word_art_caption",
    "word_art_style_mode",
    "word_art_text_color",
    "word_art_panel_color",
    "word_art_text_align",
    "word_art_panel_placement",
    "note_skip_submit",
)

WIZARD_STATE_KEYS = tuple(
    dict.fromkeys(
        (
            *WORKSPACE_DRAFT_KEYS,
            "authorization_confirmed",
            "selected_selector_profile",
            "engage_next_link_selector",
            "engage_next_page_selector",
            "engage_page_counter_selector",
            "engage_chapter_list_selector",
            "engage_comment_editor_selector",
            "engage_submit_button_selector",
            "engage_thanks_button_selector",
            "engage_confirmation_selector",
            "ocr_output_dir",
            "ocr_scroll_min_delay",
            "ocr_scroll_max_delay",
            "ocr_image_fetch_timeout",
            "ocr_next_chapter_selector",
            "ocr_next_page_selector",
            "ocr_page_counter_selector",
            "ocr_chapter_list_selector",
            "note_upload_input_selector",
            "note_upload_trigger_selector",
            "note_editor_selector",
            "note_submit_selector",
        )
    )
)


def preserve_wizard_state() -> None:
    """Keep hidden step widgets from being removed by Streamlit's cleanup pass."""
    for key in WIZARD_STATE_KEYS:
        if key in st.session_state:
            st.session_state[key] = st.session_state[key]


def is_process_running(process: subprocess.Popen | None) -> bool:
    return process is not None and process.poll() is None


def read_log(log_path: Path, max_console_chars: int = MAX_CONSOLE_CHARS) -> str:
    if not log_path.exists():
        return "Waiting for automation output..."
    max_console_chars = max(1_000, int(max_console_chars))
    byte_budget = max_console_chars * 4
    with log_path.open("rb") as log_file:
        log_file.seek(0, os.SEEK_END)
        file_size = log_file.tell()
        start_offset = max(0, file_size - byte_budget)
        log_file.seek(start_offset)
        text = log_file.read().decode("utf-8", errors="replace")

    if start_offset == 0 and len(text) <= max_console_chars:
        return redact_private_paths(text)
    return redact_private_paths(
        f"... showing latest {max_console_chars:,} characters ...\n\n{text[-max_console_chars:]}"
    )


def parse_bot_progress(log_text: str) -> dict[str, int | str]:
    """Extract the latest structured run counters from a bot log snapshot."""

    def latest_pair(pattern: str) -> tuple[int, int]:
        matches = re.findall(pattern, log_text, flags=re.IGNORECASE)
        if not matches:
            return 0, 0
        current, total = matches[-1]
        return int(current), int(total)

    current, target = latest_pair(r"Processing chapter\s+(\d+)\s*/\s*(\d+)")
    scanned, scan_target = latest_pair(r"Chapter scan completed:\s*(\d+)\s*/\s*(\d+)")
    submitted, submit_target = latest_pair(r"Chapter submission completed:\s*(\d+)\s*/\s*(\d+)")
    unverified, unverified_target = latest_pair(r"Chapter submission unverified:\s*(\d+)\s*/\s*(\d+)")

    # Keep progress useful for logs created by older bot versions.
    if scanned == 0:
        scanned = len(re.findall(r"Extracted chapter context length:", log_text, flags=re.IGNORECASE))
    if submitted == 0:
        submitted = len(
            re.findall(
                r"Posted comment verified on the page\.|treating submit as accepted\.",
                log_text,
                flags=re.IGNORECASE,
            )
        )

    failures = len(
        re.findall(r"Chapter\s+\d+\s+failed gracefully", log_text, flags=re.IGNORECASE)
    )
    stop_reasons = re.findall(r"RUN_STOP_REASON:\s*(.+)", log_text, flags=re.IGNORECASE)
    return {
        "current": current or scanned or submitted or unverified,
        "target": target or scan_target or submit_target or unverified_target,
        "scanned": scanned,
        "submitted": submitted,
        "unverified": unverified,
        "failures": failures,
        "stop_reason": stop_reasons[-1].strip() if stop_reasons else "",
    }


def parse_ocr_progress(log_text: str) -> dict[str, int | str]:
    matches = re.findall(r"OCR downloading chapter\s+(\d+)\s*/\s*(\d+)", log_text, re.IGNORECASE)
    current, total = (map(int, matches[-1]) if matches else (0, 0))
    saved = sum(int(value) for value in re.findall(r"Saved\s+(\d+)/\d+\s+raw image", log_text, re.IGNORECASE))
    failures = len(re.findall(r"Chapter\s+\d+\s+OCR download failed", log_text, re.IGNORECASE))
    return {"current": current, "target": total, "saved": saved, "failures": failures}


def parse_word_art_progress(log_text: str) -> dict[str, int | str]:
    stages = {
        "upload": "WORD_ART_UPLOAD_VERIFIED" in log_text,
        "editor": "WORD_ART_EDITOR_VERIFIED" in log_text,
        "save": "WORD_ART_SAVE_VERIFIED" in log_text,
    }
    completed = sum(1 for value in stages.values() if value)
    return {"current": completed, "target": 3, "stage": next((key for key, value in stages.items() if not value), "completed")}


def read_history() -> list[dict[str, object]]:
    return read_jsonl_records(HISTORY_FILE)


def read_owner_requests() -> list[dict[str, object]]:
    return read_tickets(OWNER_REQUESTS_FILE)


def submitted_records(records: list[dict[str, object]]) -> list[dict[str, object]]:
    return [record for record in records if record.get("status") == "submitted"]


def failed_records(records: list[dict[str, object]]) -> list[dict[str, object]]:
    return [record for record in records if record.get("status") == "failed"]


def jsonl_metric_values(records: list[dict[str, object]]) -> dict[str, str | int]:
    submitted = submitted_records(records)
    failed = failed_records(records)
    emoji_count = sum(1 for record in records if record.get("emoji_used"))
    reflection_count = sum(1 for record in records if record.get("reflection_used"))
    style_counts = Counter(str(record.get("style", "unknown")) for record in records if record.get("style"))
    keyword_counts = Counter(
        str(record.get("keyword", "")).lower()
        for record in records
        if str(record.get("keyword", "")).strip()
    )
    return {
        "Total records": len(records),
        "Submitted": len(submitted),
        "Failed": len(failed),
        "Emoji rate": f"{(emoji_count / max(1, len(records))) * 100:.0f}%",
        "Quote comments": reflection_count,
        "Styles used": len(style_counts),
        "Top keyword": keyword_counts.most_common(1)[0][0] if keyword_counts else "none",
        "Latest chapter": str(records[-1].get("chapter_index", "none")) if records else "none",
    }


COMMENT_STYLES = [
    "question",
    "theory",
    "reaction",
    "joke",
    "prediction",
    "detail",
    "foreshadowing",
    "character_focus",
    "pacing",
    "cliffhanger",
    "worldbuilding",
    "mystery",
    "emotional",
    "comparison",
    "callback",
    "suspicion",
    "appreciation",
    "tension",
    "confusion",
    "reread",
    "favorite_moment",
    "next_chapter_hook",
    "plot_twist",
    "panel_art",
    "dialogue",
    "power_scaling",
    "villain_watch",
    "hero_moment",
    "side_character",
    "relationship",
    "strategy",
    "lore_question",
    "mood_shift",
    "slow_burn",
    "payoff",
    "setup",
    "reader_poll",
    "wild_guess",
    "respect",
    "quiet_detail",
    "symbolism",
    "stakes",
    "motivation",
    "betrayal_watch",
    "comedy_timing",
    "visual_cue",
    "theme_check",
    "parallel",
    "hidden_agenda",
    "battle_tactics",
    "training_arc",
    "crew_dynamics",
    "moral_conflict",
    "identity",
    "reputation",
    "turning_point",
    "aftershock",
    "calm_before_storm",
    "final_panel",
    "micro_reaction",
]


def generate_comment_preview(subject: str = "") -> str:
    focus = "this turning point" if not subject.strip() else " ".join(subject.split())
    templates = (
        f"Did anyone else notice how much attention this chapter gave to {focus}?",
        f"I have a feeling {focus} is going to matter much more than it seems right now.",
        f"The pacing around {focus} made this chapter land harder than I expected.",
        f"If {focus} is foreshadowing, the payoff could completely change the direction of the story.",
        f"The quiet detail around {focus} might be the most important part of this chapter.",
    )
    return random.choice(templates)


def style_recommendation(records: list[dict[str, object]]) -> str:
    if not records:
        return "No comment history yet. Run a short test first so the analyzer has data."

    counts = Counter(str(record.get("style", "unknown")) for record in records)
    lowest_count = min(counts.get(style, 0) for style in COMMENT_STYLES)
    underused = [style for style in COMMENT_STYLES if counts.get(style, 0) == lowest_count]

    emoji_rate = sum(1 for record in records if record.get("emoji_used")) / max(1, len(records))
    advice = [f"Next comment should lean toward: {', '.join(underused)}."]
    if emoji_rate > 0.35:
        advice.append("Emoji use is getting high; prefer no emoji for the next few comments.")
    elif emoji_rate < 0.08 and len(records) >= 10:
        advice.append("Emoji use is very low; one subtle emoji is acceptable if the chapter tone fits.")

    repeated_keywords = Counter(
        str(record.get("keyword", "")).strip().lower()
        for record in records
        if str(record.get("keyword", "")).strip()
    )
    repeated_keyword_warnings = [
        f"{keyword} ({count})"
        for keyword, count in repeated_keywords.most_common()
        if count >= 3
    ]
    if repeated_keyword_warnings:
        advice.append(
            "Structural warning: repeated keyword pressure detected for "
            f"{', '.join(repeated_keyword_warnings)}; vary the phrasing and comment angle."
        )

    return " ".join(advice)


def survey_internet(endpoint: str, prompt: str) -> str:
    if not endpoint.strip():
        return (
            "**Internet Survey Not Configured**\n\n"
            "Add an endpoint in Settings -> Assistant. "
            "The app will call it with `?q=<your question>` and show the returned text or JSON summary."
        )

    separator = "&" if "?" in endpoint else "?"
    url = f"{endpoint}{separator}{urlencode({'q': prompt})}"
    try:
        with urlopen(url, timeout=20) as response:
            raw_text = response.read().decode("utf-8", errors="replace")
    except (OSError, URLError, TimeoutError) as exc:
        return f"Internet survey failed: {exc}"

    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError:
        return raw_text.strip() or "Internet survey returned an empty response."

    if isinstance(payload, dict):
        for key in ("answer", "summary", "text", "result", "content"):
            if payload.get(key):
                return str(payload[key])
    return json.dumps(payload, indent=2, ensure_ascii=False)


def openai_api_key_available() -> bool:
    return bool(os.getenv("OPENAI_API_KEY", "").strip())


def extract_response_text(payload: dict[str, object]) -> str:
    output_text = payload.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()

    pieces: list[str] = []
    output = payload.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if isinstance(part, dict):
                    text = part.get("text") or part.get("content")
                    if isinstance(text, str) and text.strip():
                        pieces.append(text.strip())
    return "\n".join(pieces).strip()


def call_openai_assistant(prompt: str, *, use_web: bool) -> str:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        return openai_api_key_help()

    system_context = (
        "You are the embedded assistant for an owned Streamlit/Selenium QA dashboard and website operations console. "
        "Help with the whole site/server workflow: user replies, support triage, dashboard settings, run setup, logs, selector failures, content/comment drafting, style variety, owner requests, and safe operational next steps. "
        "Be accurate and explicit about evidence. Use local dashboard context when supplied; if evidence is missing, say what you cannot verify. "
        "Do not invent chapter facts, quotes, web findings, successful posts, server status, or actions taken. "
        "If web search is unavailable or sources are weak, say so and offer a safer draft or troubleshooting path. "
        "For posting/commenting, tell the user to review the generated result. Never claim that a comment was posted. "
        "For user-support replies, write clear, respectful responses the site owner can send to a user, and include practical next steps. "
        "For bulk chapter or comment work, return compact tables when useful with confidence and verification notes. "
        "Format responses with clean Markdown, short bold headers, bullets, tables when useful, and fenced code blocks for exact messages or commands."
    )
    payload: dict[str, object] = {
        "model": st.session_state.assistant_model,
        "input": [
            {"role": "system", "content": system_context},
            {"role": "user", "content": prompt},
        ],
    }
    if use_web:
        payload["tools"] = [{"type": "web_search_preview"}]

    request = Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=45) as response:
            raw_text = response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        return f"Model request failed: HTTP {exc.code}. {error_body}"
    except (OSError, URLError, TimeoutError) as exc:
        return f"Model request failed: {exc}"

    try:
        response_payload = json.loads(raw_text)
    except json.JSONDecodeError:
        return raw_text.strip() or "Model request returned an empty response."

    text = extract_response_text(response_payload)
    return text or json.dumps(response_payload, indent=2, ensure_ascii=False)


def stream_openai_assistant(
    prompt: str,
    *,
    use_web: bool,
    conversation: list[dict[str, object]],
):
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        yield openai_api_key_help()
        return

    system_context = (
        "I'm Lyra, Panelyra's built-in assistant, grounded in the supplied local application context. "
        "Help users use Library, Downloads, Capture, Engage, Lyra, Activity and Settings. Studio is retired. "
        "Treat run records, log evidence, browser state, output counts, and the Panelyra knowledge base as authoritative. "
        "Never invent actions, successful submissions, downloaded files, browser state, support updates, or web findings. "
        "Do not say an action was completed unless the context explicitly confirms it. "
        "Keep answers concise, practical, and tied to evidence. Sensitive or state-changing actions require user confirmation."
    )
    input_items: list[dict[str, str]] = [{"role": "system", "content": system_context}]
    for message in conversation[-12:]:
        role = str(message.get("role", ""))
        content = str(message.get("content", "")).strip()
        if role in {"user", "assistant"} and content:
            input_items.append({"role": role, "content": content[:8_000]})
    input_items.append({"role": "user", "content": prompt})

    payload: dict[str, object] = {
        "model": st.session_state.assistant_model,
        "input": input_items,
        "stream": True,
    }
    if use_web:
        payload["tools"] = [{"type": "web_search_preview"}]

    request = Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        },
        method="POST",
    )
    emitted = False
    try:
        with urlopen(request, timeout=90) as response:
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                event_text = line[5:].strip()
                if not event_text or event_text == "[DONE]":
                    continue
                try:
                    event = json.loads(event_text)
                except json.JSONDecodeError:
                    continue
                event_type = str(event.get("type", ""))
                if event_type == "response.output_text.delta":
                    delta = str(event.get("delta", ""))
                    if delta:
                        emitted = True
                        yield delta
                elif event_type in {"response.failed", "error"}:
                    response_value = event.get("response")
                    response_error = (
                        response_value.get("error")
                        if isinstance(response_value, dict)
                        else None
                    )
                    error = event.get("error") or response_error
                    if error:
                        emitted = True
                        yield f"\n\nModel request failed: {error}"
    except HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        yield f"Model request failed: HTTP {exc.code}. {error_body}"
        return
    except (OSError, URLError, TimeoutError) as exc:
        yield f"Model request failed: {exc}"
        return
    if not emitted:
        yield "The model returned no text. The grounded local recommendation is shown instead."


def extract_first_url(text: str) -> str:
    match = re.search(r"https?://[^\s)>\]]+", text)
    return match.group(0).rstrip(".,") if match else ""


def extract_chapter_range(text: str) -> tuple[int | None, int | None]:
    match = re.search(
        r"chapter(?:s)?\s+(\d{1,5})(?:\s*(?:-|to|through|until)\s*(\d{1,5}))?",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        return None, None
    start = int(match.group(1))
    end = int(match.group(2) or match.group(1))
    if end < start:
        start, end = end, start
    return start, min(end, start + 50)


def assistant_task_intent(prompt: str) -> bool:
    normalized = prompt.lower()
    task_terms = ("make a comment", "draft", "quote", "quotes", "chapter", "bulk", "range", "lookism", "research")
    return any(term in normalized for term in task_terms)


def build_comment_task_prompt(prompt: str, records: list[dict[str, object]], status_label: str) -> str:
    start, end = extract_chapter_range(prompt)
    target_link = extract_first_url(prompt)
    recent_comments = [
        str(record.get("comment", "")).strip()
        for record in records[-10:]
        if str(record.get("comment", "")).strip()
    ]
    if target_link:
        st.session_state.start_url = target_link
    if start is not None and end is not None:
        st.session_state.max_chapters = max(1, end - start + 1)

    local_context = {
        "bot_status": status_label,
        "target_link": target_link or "not provided",
        "chapter_start": start,
        "chapter_end": end,
        "recent_comments_to_avoid_copying": recent_comments,
        "quote_cadence": st.session_state.reflection_every_n_comments,
    }
    return (
        "User request:\n"
        f"{prompt}\n\n"
        "Local dashboard context:\n"
        f"{json.dumps(local_context, indent=2, ensure_ascii=False)}\n\n"
        "Produce an actionable response. If no target link is provided, ask for it. If a target link is provided, say it has been placed into Engage Setup. "
        "If the user asks for chapter-specific facts or quotes, use web search if available and cite/describe the basis. "
        "If you cannot verify facts, say what is unverified and provide a safe comment draft based on the user's wording instead. "
        "Do not tell the user the app posted anything."
    )


def run_model_backed_task(prompt: str, records: list[dict[str, object]], status_label: str) -> str:
    task_prompt = build_comment_task_prompt(prompt, records, status_label)
    if openai_api_key_available():
        response = call_openai_assistant(task_prompt, use_web=bool(st.session_state.assistant_use_openai_web))
    elif st.session_state.assistant_internet_endpoint.strip():
        response = survey_internet(st.session_state.assistant_internet_endpoint, task_prompt)
    else:
        response = (
            "I can still route the workflow locally, but model/web research is not configured yet.\n\n"
            f"{openai_api_key_help()}\n\n"
            "After the key is available, ask again with the target chapter/link or bulk range."
        )

    target_link = extract_first_url(prompt)
    start, end = extract_chapter_range(prompt)
    routing = []
    if target_link:
        routing.append(f"Target link placed into Engage Setup: {target_link}")
    else:
        routing.append("Target link still needed. Include it in your message or paste it in Engage Setup.")
    if start is not None and end is not None:
        routing.append(f"Chapter range detected: {start}-{end}. Max chapters set to {max(1, end - start + 1)}.")
    routing.append("Next step: open Engage Setup, review the link/count, then use Open Browser / Pick Chapter so the user can navigate to the desired page before submitting.")
    st.session_state.assistant_generated_plan = response
    return f"{response}\n\nWorkflow routing:\n" + "\n".join(f"- {item}" for item in routing)


def build_site_assistant_prompt(prompt: str, records: list[dict[str, object]], status_label: str) -> str:
    recent_comments = [
        str(record.get("comment", "")).strip()
        for record in records[-8:]
        if str(record.get("comment", "")).strip()
    ]
    local_context = {
        "bot_status": status_label,
        "current_page": st.session_state.page,
        "metrics": jsonl_metric_values(records),
        "latest_log_findings": latest_log_findings(st.session_state.log_path, max_lines=6),
        "settings": {
            "start_url": st.session_state.start_url,
            "max_chapters": st.session_state.max_chapters,
            "chapter_routing": st.session_state.chapter_routing,
            "scroll_preset": st.session_state.scroll_preset,
            "quote_cadence": st.session_state.reflection_every_n_comments,
            "history_rows": st.session_state.history_rows,
            "top_keywords": st.session_state.top_keywords,
            "assistant_web_search": st.session_state.assistant_use_openai_web,
        },
        "available_comment_styles": COMMENT_STYLES,
        "recent_comments": recent_comments,
        "support_request_count": len(read_owner_requests()),
    }
    return (
        "User request:\n"
        f"{prompt}\n\n"
        "Local dashboard/site context:\n"
        f"{json.dumps(local_context, indent=2, ensure_ascii=False)}\n\n"
        "Answer as the site's operations assistant. Help with comments, site/server workflow, support replies, settings, logs, owner requests, and user-facing responses. "
        "If the user asks for a message to send to a site user, write the reply clearly and politely. "
        "If the user asks for an action the app cannot safely perform, explain the manual next step."
    )


def run_site_assistant_task(prompt: str, records: list[dict[str, object]], status_label: str) -> str:
    site_prompt = build_site_assistant_prompt(prompt, records, status_label)
    if openai_api_key_available():
        return call_openai_assistant(site_prompt, use_web=bool(st.session_state.assistant_use_openai_web))
    if st.session_state.assistant_internet_endpoint.strip() and any(
        term in prompt.lower() for term in ("internet", "web", "online", "survey", "search", "research")
    ):
        return survey_internet(st.session_state.assistant_internet_endpoint, site_prompt)
    return ""


def latest_log_findings(log_path: Path | None, max_lines: int = 8) -> list[str]:
    if log_path is None:
        return ["No run log is available yet."]
    if not log_path.exists():
        return [f"Configured log is unavailable: {display_path(log_path)}"]

    text = read_log(log_path, 40_000)
    keywords = ("error", "warning", "exception", "traceback", "failed", "timeout", "not found", "blocked")
    findings = [
        line.strip()
        for line in text.splitlines()
        if any(keyword in line.lower() for keyword in keywords)
    ]
    return findings[-max_lines:] if findings else ["No obvious warning/error lines found in the latest log."]


def create_owner_request(prompt: str, page: str, status_label: str) -> dict[str, object]:
    recent_findings = latest_log_findings(st.session_state.log_path, max_lines=5)
    return create_ticket(
        OWNER_REQUESTS_FILE,
        {
            "status": "open",
            "severity": "medium",
            "category": "assistant-escalation",
            "page": page,
            "bot_status": status_label,
            "request": prompt.strip(),
            "latest_log_findings": recent_findings,
        },
    )


def summarize_comment_history(records: list[dict[str, object]], limit: int) -> str:
    if not records:
        return "**Engage Insights Summary**\n\nNo comment history yet. Run a short test, then I can analyze style balance, repeated keywords, emoji use, and quote cadence."

    recent_records = records[-limit:]
    submitted = [record for record in records if record.get("status") == "submitted"]
    style_counts = Counter(str(record.get("style", "unknown")) for record in records if record.get("style"))
    keyword_counts = Counter(
        str(record.get("keyword", "")).lower()
        for record in records
        if str(record.get("keyword", "")).strip()
    )
    reflection_count = sum(1 for record in records if record.get("reflection_used"))
    emoji_count = sum(1 for record in records if record.get("emoji_used"))
    recent_comments = [
        str(record.get("comment", "")).strip()
        for record in recent_records[-5:]
        if str(record.get("comment", "")).strip()
    ]

    lines = [
        "**Engage Insights Summary**",
        "",
        f"History has {len(records)} record(s), with {len(submitted)} submitted comment(s).",
        f"Most used styles: {', '.join(f'{style} ({count})' for style, count in style_counts.most_common(5)) or 'none yet'}.",
        f"Top keywords: {', '.join(f'{keyword} ({count})' for keyword, count in keyword_counts.most_common(5)) or 'none yet'}.",
        f"Emoji rate: {(emoji_count / max(1, len(records))) * 100:.0f}%. Quote/philosophy comments: {reflection_count}.",
        style_recommendation(records),
    ]
    if recent_comments:
        lines.append("Recent comments:")
        lines.extend(f"- {comment}" for comment in recent_comments)
    return "\n".join(lines)


def diagnose_user_problem(prompt: str, records: list[dict[str, object]], status_label: str) -> str:
    findings = latest_log_findings(st.session_state.log_path)
    failed_records = [record for record in records if record.get("status") == "failed"]
    submitted_records = [record for record in records if record.get("status") == "submitted"]
    lines = [
        "**Local Diagnosis**",
        "",
        "Based only on dashboard data:",
        f"- Bot state: {status_label}.",
        f"- Submitted comments in history: {len(submitted_records)}.",
        f"- Failed history records: {len(failed_records)}.",
        f"- Latest log: {display_path(st.session_state.log_path) if st.session_state.log_path else 'none yet'}.",
        "",
        "Latest log findings:",
        *[f"- {finding}" for finding in findings],
        "",
        "Next steps:",
    ]
    lowered_findings = " ".join(findings).lower()
    if "cloudflare" in lowered_findings or "challenge" in lowered_findings:
        lines.append("- The page appears blocked by a challenge. Use Open Browser / Pick Chapter, solve/login manually, then click Start Comments.")
    elif "no matching element" in lowered_findings or "selector" in lowered_findings or "not found" in lowered_findings:
        lines.append("- The bot likely cannot find the comment input or submit button. Check the target page and update selectors in bot.py if the site changed.")
    elif "timeout" in lowered_findings:
        lines.append("- A page or element timed out. Try slower scroll settings, confirm login, and rerun from the exact chapter URL.")
    elif status_label == "Idle" and not st.session_state.log_path:
        lines.append("- No run has started yet. Paste a chapter URL in Engage Setup and use Open Browser / Pick Chapter for login-gated pages.")
    else:
        lines.append("- Open Engage Live for full logs. If this affects a real user, ask the assistant to contact the owner so it lands in the Settings support log.")
    lines.append("- I will not claim internet/current-site facts unless an internet survey endpoint is configured and returns data.")
    return "\n".join(lines)


def owner_request_intent(prompt: str) -> bool:
    normalized = prompt.lower()
    direct_terms = ("request owner", "ask owner", "contact owner", "tell owner", "send to owner", "admin", "support ticket")
    problem_terms = ("bug", "broken", "not working", "issue", "problem", "error", "failed", "stuck")
    return any(term in normalized for term in direct_terms) or (
        any(term in normalized for term in problem_terms) and "owner" in normalized
    )


def openai_api_key_setup_intent(prompt: str) -> bool:
    normalized = prompt.lower().replace("open api", "openai").replace("open ai", "openai")
    key_terms = ("openai_api_key", "openai api key", "openai key", "api key")
    setup_terms = ("set", "setup", "configure", "add", "where", "how", "put", "env", "environment", "key")
    return any(term in normalized for term in key_terms) and any(term in normalized for term in setup_terms)


def openai_api_key_help() -> str:
    key_status = "available" if openai_api_key_available() else "missing"
    return (
        "**Set The OpenAI API Key**\n\n"
        f"Current app status: `OPENAI_API_KEY` is **{key_status}**.\n\n"
        "Recommended for this project:\n\n"
        "```bash\n"
        "cd /path/to/Panelyra\n"
        "printf 'OPENAI_API_KEY=your-key-here\\n' > .env\n"
        "```\n\n"
        "Then restart Streamlit or the Panelyra launcher. The app loads `.env` only when it starts.\n\n"
        "One-time Terminal option:\n\n"
        "```bash\n"
        "cd /path/to/Panelyra\n"
        "export OPENAI_API_KEY=\"your-key-here\"\n"
        "streamlit run app.py\n"
        "```\n\n"
        "Keep the key private. Do not paste the real key into the assistant chat, comments, GitHub, screenshots, or support requests. "
        "After restart, open `Settings` and check `OpenAI key`; it should say `available`."
    )


def app_help_intent(prompt: str) -> bool:
    normalized = prompt.lower().replace("live room", "live console").replace("live tab", "live console")
    app_terms = (
        "automation",
        "automations",
        "dashboard",
        "run suite",
        "live console",
        "live log",
        "intelligence",
        "settings",
        "help page",
        "open browser",
        "pick chapter",
        "start comments",
        "run immediately",
        "stop current run",
        "scroll",
        "chapter routing",
        "research endpoint",
        "research file",
        "read marker",
        "say thanks",
        "owner request",
        "support request",
        "assistant",
        "comment style",
        "comment styles",
        "quote cadence",
        "history",
        "logs",
        "log",
        "server",
        "site workflow",
    )
    question_terms = (
        "how",
        "what",
        "where",
        "when",
        "why",
        "explain",
        "use",
        "using",
        "setup",
        "set up",
        "configure",
        "guide",
        "show me",
        "tell me",
        "help",
        "function",
        "functions",
        "page",
        "tab",
        "button",
        "buttons",
        "do i",
        "should i",
    )
    return any(term in normalized for term in app_terms) and any(term in normalized for term in question_terms)


def automation_setup_help(status_label: str) -> str:
    return (
        "**Set Up An Automation Run**\n\n"
        f"Current bot state: `{status_label}`.\n\n"
        "1. Open `Engage`, then stay on `Configure`.\n"
        "2. Paste the starting URL. Use a chapter URL for a direct run, or a series/reader URL if you need to pick the chapter manually.\n"
        "3. Set `How many chapters?` as the maximum number the bot is allowed to process.\n"
        "4. Choose `Reader routing`:\n"
        "- `Auto detect`: recommended for most manga, manhwa, and manhua readers.\n"
        "- `Next chapter button/link`: one chapter per URL.\n"
        "- `Continuous chapters`: multiple chapter/comment sections on one long page.\n"
        "- `Next page reader`: multiple internal pages inside one chapter.\n"
        "- `Paginated pages + next chapter`: separate page and chapter controls.\n"
        "5. Choose `Scroll speed`. Start with `Balanced`; use `Performance` or `Maximum` only after the page works reliably.\n"
        "6. Review the summary, then use `Open browser and prepare` if login or manual chapter selection is needed. Chrome opens and the bot pauses.\n"
        "7. After Chrome is on the exact chapter, click `Start Comments`.\n"
        "8. Use `Start exact URL` only when the pasted URL is already the exact page you want.\n"
        "9. Open `Live` for progress, errors, generated comments, and selector problems.\n"
        "10. Open `Results` after a run to review history, repeated keywords, comment styles, emoji rate, and quote cadence."
    )


def live_console_help(status_label: str) -> str:
    log_status = "available" if st.session_state.log_path else "not created yet"
    return (
        "**Dedicated Live Rooms**\n\n"
        "Each bot has its own Live Room with only its metrics, controls, and runtime log.\n\n"
        f"- Bot state: `{status_label}`.\n"
        f"- Log file: `{log_status}`.\n"
        "- `Start Comments` appears there when Chrome is paused after `Open browser and prepare`.\n"
        "- `Stop` sends a stop signal to the running bot.\n"
        "- `Refresh Console` reloads the latest text from the run log.\n"
        "- The log shows routing decisions, selector failures, login waits, generated comments, and submission status.\n\n"
        "If something fails, copy the exact warning/error line from that bot's Live Room and ask the assistant what it means."
    )


def page_overview_help() -> str:
    return (
        "**Dashboard Pages**\n\n"
        "- `Dashboard`: three workspace launch choices and recent-run status.\n"
        "- `Library` is the home for saved books; `Downloads` finds titles; `Capture` saves a chapter URL.\n"
        "- `Configure`: a short sequential setup that shows one decision stage at a time.\n"
        "- `Live`: isolated progress, controls, and runtime diagnostics.\n"
        "- `Results`: records and artifacts for that workspace only.\n"
        "- `Settings`: UI, console, assistant, history, and support-request controls.\n"
        "- `Help`: reference guide for buttons, fields, research endpoints, API key setup, and workflow."
    )


def local_app_help_response(prompt: str, records: list[dict[str, object]], status_label: str) -> str:
    normalized = prompt.lower().replace("live room", "live console").replace("live tab", "live console")
    sections: list[str] = []

    if any(term in normalized for term in ("automation", "automations", "set up", "setup", "run suite", "start run", "start automation")):
        sections.append(automation_setup_help(status_label))

    if any(term in normalized for term in ("live console", "live log", "logs", "log", "live room")):
        sections.append(live_console_help(status_label))

    if "intelligence" in normalized or "analytics" in normalized or "history" in normalized:
        sections.append(
            "**Intelligence Page**\n\n"
            f"It reads `{HISTORY_FILE.name}` and summarizes the automation history.\n\n"
            "- JSONL metrics show totals, failures, emoji rate, quote comments, top keyword, and latest chapter.\n"
            "- Charts show style balance, research source usage, and frequent keywords.\n"
            "- The recent-records table shows generated comments and whether reflection/emoji was used.\n"
            f"- Current history records: `{len(records)}`."
        )

    if "settings" in normalized or "configure" in normalized:
        sections.append(
            "**Settings Page**\n\n"
            "- `Compact UI`: reduces spacing.\n"
            "- `Show workspace details in sidebar`: shows neutral workspace/log labels for debugging.\n"
            "- `Console characters`: controls how much log text each Live Room displays.\n"
            "- `Auto-refresh seconds`: controls refresh speed while the bot is running.\n"
            "- `Quote/philosophy cadence`: controls how often a quote/philosophical style is added.\n"
            "- `AI`: configures Lyra’s local model, browser inspection and optional web search.\n"
            "- `Help`: instructions and local diagnostics. Support-ticket management is retired."
        )

    if "research endpoint" in normalized or "research file" in normalized:
        sections.append(
            "**Research Context**\n\n"
            "- `Local research file` points to a JSON file with staging-safe context such as title, tags, summary, notes, or context.\n"
            "- `Research endpoint` is optional. If configured, the bot calls it as `endpoint?q=<chapter title and keywords>`.\n"
            "- Leave it blank unless you run your own local/private context API.\n"
            "- Model-backed internet research requires `OPENAI_API_KEY` or a configured private endpoint."
        )

    if any(term in normalized for term in ("button", "buttons", "open browser", "pick chapter", "start comments", "run immediately", "stop current run")):
        sections.append(
            "**Main Buttons**\n\n"
            "- `Open browser and prepare`: opens Chrome and pauses so you can log in or manually pick the exact chapter.\n"
            "- `Start Comments`: resumes the paused bot from the page currently open in Chrome.\n"
            "- `Start exact URL`: starts right away from the pasted URL. Use it only for exact chapter URLs.\n"
            "- `Stop Current Run`: stops the bot process if the wrong page opens or something breaks."
        )

    if any(term in normalized for term in ("assistant", "reply", "user", "support", "owner request", "support request")):
        sections.append(
            "**Assistant And User Replies**\n\n"
            "- Open Lyra from the sidebar for help with settings, logs and workflows.\n"
            "- If a user reports a problem, ask: `write a reply to a user who says ...` and include the problem details.\n"
            "- To make a local owner/support request, ask the assistant to contact the owner and describe the issue.\n"
            "- Lyra requires the configured local Ollama model; open Settings → AI if it is disconnected."
        )

    if any(term in normalized for term in ("comment style", "comment styles", "quote cadence", "emoji", "keyword")):
        sections.append(
            "**Engage Insights**\n\n"
            f"- The generator currently has `{len(COMMENT_STYLES)}` style modes.\n"
            "- The app rotates underused styles to reduce repetitive comments.\n"
            "- `Quote/philosophy cadence` controls how often a famous-person/philosophical reflection is added.\n"
            "- Intelligence warns when emoji use is too high/low or when keywords repeat too much."
        )

    if not sections:
        sections.append(page_overview_help())
        sections.append(automation_setup_help(status_label))

    return "\n\n".join(sections)


def assistant_response(prompt: str, records: list[dict[str, object]], status_label: str) -> str:
    normalized = prompt.lower()
    if owner_request_intent(prompt):
        return "Support-ticket management is retired. Open Settings → Help for diagnostics."
        return (
            "**Owner Request Created**\n\n"
            f"- Request ID: {record['id']}\n"
            f"- Status: {record['status']}\n"
            f"- Page: {record['page']}\n"
            "The owner can review it in Settings under Support Requests."
        )
    if openai_api_key_setup_intent(prompt):
        return openai_api_key_help()
    if app_help_intent(prompt):
        return local_app_help_response(prompt, records, status_label)
    if assistant_task_intent(prompt):
        return run_model_backed_task(prompt, records, status_label)
    model_response = run_site_assistant_task(prompt, records, status_label)
    if model_response:
        return model_response
    if any(term in normalized for term in ("problem", "issue", "bug", "broken", "error", "failed", "stuck", "not working", "diagnose", "fix")):
        return diagnose_user_problem(prompt, records, status_label)
    if any(term in normalized for term in ("internet", "web", "online", "survey", "search")):
        return survey_internet(st.session_state.assistant_internet_endpoint, prompt)
    if any(term in normalized for term in ("setting", "configure", "cadence", "quote", "ui", "ux")):
        return (
            "**Recommended Settings**\n\n"
            f"- Quote/philosophy cadence: every {int(st.session_state.reflection_every_n_comments)} comments.\n"
            f"- History rows shown: {int(st.session_state.history_rows)}.\n"
            f"- Top keywords shown: {int(st.session_state.top_keywords)}.\n"
            f"- Console length: {int(st.session_state.console_max_chars):,} characters.\n"
            f"- Auto-refresh: {int(st.session_state.auto_refresh_seconds)} seconds.\n"
            f"Current bot state: {status_label}."
        )
    if any(term in normalized for term in ("recent", "history", "comment", "style", "keyword", "emoji", "analyze", "analyse")):
        return summarize_comment_history(records, int(st.session_state.assistant_history_limit))
    if any(term in normalized for term in ("what can you do", "help", "tasks", "task", "server", "site", "reply", "user")):
        return (
            "**What I Can Help With**\n\n"
            "- Help write clear replies to site users and support messages.\n"
            "- Review local run history, comment variety, settings, and recent generated comments.\n"
            "- Troubleshoot site workflow issues from local logs and bot state.\n"
            "- Create owner requests for issues users cannot solve.\n"
            "- Use the configured internet survey endpoint or model-backed web search when available."
        )
    return (
        "**Assistant Ready**\n\n"
        "Ask me anything about the site, server workflow, user replies, comments, settings, logs, support requests, or internet research. "
        "For model-backed general answers, set `OPENAI_API_KEY`; without it, I can still use local dashboard data and the configured internet endpoint."
    )


def stop_bot(process: subprocess.Popen | None, timeout_seconds: float = 8.0) -> None:
    if not is_process_running(process):
        return

    update_run_record_by_pid(process.pid, status='stopped', stage='stopped',
                             stop_reason='Stopped from the Panelyra dashboard.')

    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except Exception:
        process.terminate()

    try:
        process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        except Exception:
            process.kill()
    update_run_record_by_pid(
        process.pid,
        status="stopped",
        stage="stopped",
        stop_reason="Stopped from the Panelyra dashboard.",
    )


def pause_bot(process: subprocess.Popen | None) -> bool:
    if not is_process_running(process):
        return False
    try:
        os.killpg(process.pid, signal.SIGSTOP)
    except (OSError, ProcessLookupError):
        return False
    return True


def resume_bot(process: subprocess.Popen | None) -> bool:
    if not is_process_running(process):
        return False
    try:
        os.killpg(process.pid, signal.SIGCONT)
    except (OSError, ProcessLookupError):
        return False
    return True


def subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env.setdefault("PYTHONPYCACHEPREFIX", str(PYTHON_CACHE_DIR))
    return env


def launch_tracked_process(
    command: list[str],
    *,
    bot_type: str,
    log_path: Path,
    settings: dict[str, object],
) -> subprocess.Popen:
    environment = subprocess_env()
    if bot_type == 'download':
        from panelyra.engine import policy
        from panelyra.services.worker_runtime import engine_environment
        if policy(APP_DIR):
            raise ValueError('Background work is paused in Settings. Resume it before starting Capture.')
        environment.update(engine_environment(APP_DIR))
    record = RunRecord.create(bot_type, settings, log_path)
    store = RunStore(record)
    if store.capture:
        store.capture.command(record.run_id, command)
    environment["PANELYRA_RUN_RECORD"] = str(store.path)
    environment["ASURA_RUN_RECORD"] = str(store.path)
    log_handle = log_path.open("a", encoding="utf-8", buffering=1)
    process = subprocess.Popen(
        command,
        cwd=str(APP_DIR),
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        env=environment,
    )
    sleep_guard = (
        launch_macos_sleep_guard(process.pid)
        if bot_type in {"comment", "download"}
        else None
    )
    if sleep_guard is not None:
        log_handle.write(
            f"macOS sleep protection active for {bot_type} process {process.pid}.\n"
        )
    log_handle.close()
    store.update(pid=process.pid)
    st.session_state.run_record_paths[bot_type] = str(store.path)
    return process


def start_bot(
    start_url: str,
    max_chapters: int,
    wait_for_login: bool,
    research_file: str,
    research_endpoint: str,
    use_thanks_marker: bool,
    chapter_routing: str,
    scroll_min_px: int,
    scroll_max_px: int,
    scroll_min_delay: float,
    scroll_max_delay: float,
    reflection_every_n_comments: int,
    content_mode: str,
    content_container_selector: str,
    ocr_image_selector: str,
    ocr_confidence: float,
    type_min_delay: float,
    type_max_delay: float,
    visible_type_chars: int,
    reuse_loaded_images: bool,
    cooldown_min_seconds: float,
    cooldown_max_seconds: float,
    post_submit_wait_seconds: float,
    next_link_selector: str,
    next_page_selector: str,
    page_counter_selector: str,
    chapter_list_selector: str,
    comment_editor_selector: str,
    submit_button_selector: str,
    thanks_button_selector: str,
    confirmation_selector: str,
    first_comment_override: str = "",
) -> tuple[subprocess.Popen, Path, Path | None]:
    start_url = normalize_web_url(start_url)
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    log_path = LOG_DIR / f"sequential-qa-{timestamp}.log"
    signal_path = LOG_DIR / f"start-signal-{timestamp}-{uuid.uuid4().hex}.txt" if wait_for_login else None
    python_executable = str(BOT_PYTHON if BOT_PYTHON.exists() else Path(sys.executable))
    command = [
        python_executable,
        str(BOT_PATH),
        "--start-url",
        start_url,
        "--max-chapters",
        str(max_chapters),
        "--history-file",
        str(HISTORY_FILE),
        "--read-marker-file",
        str(READ_MARKER_FILE),
        "--chapter-routing",
        chapter_routing,
        "--scroll-min-px",
        str(scroll_min_px),
        "--scroll-max-px",
        str(scroll_max_px),
        "--scroll-min-delay",
        str(scroll_min_delay),
        "--scroll-max-delay",
        str(scroll_max_delay),
        "--reflection-every-n-comments",
        str(reflection_every_n_comments),
        "--content-mode",
        content_mode,
        "--content-container-selector",
        content_container_selector,
        "--ocr-image-selector",
        ocr_image_selector,
        "--ocr-confidence",
        str(ocr_confidence),
        "--type-min-delay",
        str(type_min_delay),
        "--type-max-delay",
        str(type_max_delay),
        "--visible-type-chars",
        str(visible_type_chars),
        "--reuse-loaded-images",
        str(reuse_loaded_images).lower(),
        "--cooldown-min-seconds",
        str(cooldown_min_seconds),
        "--cooldown-max-seconds",
        str(cooldown_max_seconds),
        "--post-submit-wait-seconds",
        str(post_submit_wait_seconds),
        "--next-link-selector",
        next_link_selector,
        "--next-page-selector",
        next_page_selector,
        "--page-counter-selector",
        page_counter_selector,
        "--chapter-list-selector",
        chapter_list_selector,
        "--comment-editor-selector",
        comment_editor_selector,
        "--submit-button-selector",
        submit_button_selector,
        "--thanks-button-selector",
        thanks_button_selector,
        "--confirmation-selector",
        confirmation_selector,
    ]
    if research_file.strip():
        command.extend(["--research-file", research_file.strip()])
    if research_endpoint.strip():
        command.extend(["--research-endpoint", research_endpoint.strip()])
    if use_thanks_marker:
        command.append("--use-thanks-marker")
    if first_comment_override.strip():
        command.extend(["--first-comment-override", first_comment_override.strip()])
    if signal_path:
        command.extend(["--start-signal-file", str(signal_path)])

    startup_banner = (
        "Starting bot process...\n"
        "Working directory: Panelyra workspace\n"
        f"Python: {python_executable}\n"
        f"Command: {' '.join(command)}\n\n"
    )
    log_path.write_text(startup_banner, encoding="utf-8")
    process = launch_tracked_process(
        command,
        bot_type="comment",
        log_path=log_path,
        settings={"start_url": start_url, "max_chapters": max_chapters, "routing": chapter_routing},
    )
    st.session_state.bot_runtimes["comment"] = {
        "process": process, "log_path": log_path, "signal_path": signal_path, "paused": False
    }
    return process, log_path, signal_path


def start_note_art_bot(
    note_url: str,
    image_path: Path,
    word_art_text: str,
    caption: str,
    style_mode: str,
    text_color: str,
    panel_color: str,
    text_align: str,
    panel_placement: str,
    wait_for_setup: bool,
    upload_input_selector: str,
    upload_trigger_selector: str,
    editor_selector: str,
    submit_selector: str,
    skip_submit: bool,
) -> tuple[subprocess.Popen, Path, Path | None]:
    note_url = normalize_web_url(note_url)
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    log_path = LOG_DIR / f"word-art-{timestamp}.log"
    signal_path = LOG_DIR / f"word-art-start-{timestamp}-{uuid.uuid4().hex}.txt" if wait_for_setup else None
    python_executable = str(BOT_PYTHON if BOT_PYTHON.exists() else Path(sys.executable))
    command = [
        python_executable,
        str(NOTE_ART_BOT_PATH),
        "--note-url",
        note_url,
        "--image-path",
        str(image_path),
        "--word-art-text",
        word_art_text,
        "--caption",
        caption,
        "--style-mode",
        style_mode,
        "--text-color",
        text_color,
        "--panel-color",
        panel_color,
        "--text-align",
        text_align,
        "--panel-placement",
        panel_placement,
        "--upload-input-selector",
        upload_input_selector.strip() or NOTE_ART_SELECTOR_DEFAULTS["note_upload_input_selector"],
        "--upload-trigger-selector",
        upload_trigger_selector.strip() or NOTE_ART_SELECTOR_DEFAULTS["note_upload_trigger_selector"],
        "--editor-selector",
        editor_selector.strip() or NOTE_ART_SELECTOR_DEFAULTS["note_editor_selector"],
        "--submit-selector",
        submit_selector.strip() or NOTE_ART_SELECTOR_DEFAULTS["note_submit_selector"],
        "--log-file",
        str(log_path),
    ]
    if skip_submit:
        command.append("--skip-submit")
    if signal_path:
        command.extend(["--start-signal-file", str(signal_path)])

    startup_banner = (
        "Starting word-art note process...\n"
        "Working directory: Panelyra workspace\n"
        f"Python: {python_executable}\n"
        f"Command: {' '.join(command)}\n\n"
    )
    log_path.write_text(startup_banner, encoding="utf-8")
    process = launch_tracked_process(
        command,
        bot_type="word_art",
        log_path=log_path,
        settings={
            "note_url": note_url,
            "style": style_mode,
            "skip_submit": skip_submit,
            "image_path": str(image_path),
        },
    )
    st.session_state.bot_runtimes["word_art"] = {
        "process": process, "log_path": log_path, "signal_path": signal_path, "paused": False
    }
    return process, log_path, signal_path


def start_manhwa_ocr_bot(
    start_url: str,
    max_chapters: int,
    wait_for_inspection: bool,
    output_dir: str,
    content_container_selector: str,
    ocr_image_selector: str,
    ocr_confidence: float,
    scroll_min_delay: float,
    scroll_max_delay: float,
    image_fetch_timeout: float,
    save_images: bool,
    create_archive: bool,
    save_ocr: bool,
    reader_layout: str,
    next_chapter_selector: str,
    next_page_selector: str,
    page_counter_selector: str,
    chapter_list_selector: str,
    performance_profile: str,
) -> tuple[subprocess.Popen, Path, Optional[Path]]:
    start_url = normalize_web_url(start_url)
    max_chapters = max(1, min(10000, int(max_chapters)))
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    log_path = LOG_DIR / f"manhwa-ocr-{timestamp}.log"
    signal_path = (
        LOG_DIR / f"capture-start-{timestamp}-{uuid.uuid4().hex}.txt"
        if wait_for_inspection
        else None
    )
    python_executable = str(BOT_PYTHON if BOT_PYTHON.exists() else Path(sys.executable))
    command = [
        python_executable,
        str(MANHWA_OCR_BOT_PATH),
        "--start-url",
        start_url,
        "--max-chapters",
        str(max_chapters),
        "--output-dir",
        output_dir,
        "--content-container-selector",
        content_container_selector,
        "--ocr-image-selector",
        ocr_image_selector,
        "--ocr-confidence",
        str(ocr_confidence),
        "--scroll-min-delay",
        str(scroll_min_delay),
        "--scroll-max-delay",
        str(scroll_max_delay),
        "--image-fetch-timeout",
        str(image_fetch_timeout),
        "--reader-layout",
        reader_layout,
        "--next-chapter-selector",
        next_chapter_selector,
        "--next-page-selector",
        next_page_selector,
        "--page-counter-selector",
        page_counter_selector,
        "--chapter-list-selector",
        chapter_list_selector,
        "--performance-profile",
        performance_profile.lower(),
    ]
    if save_images:
        command.append("--save-images")
    if signal_path:
        command.extend(["--start-signal-file", str(signal_path)])
    if not create_archive:
        command.append("--skip-cbz")
    if not save_ocr:
        command.append("--skip-ocr")

    command[1:2] = ["-m", "panelyra.automation.managed_capture", "--root", str(APP_DIR)]

    startup_banner = (
        "Starting Panelyra Capture process...\n"
        "Working directory: Panelyra workspace\n"
        f"Python: {python_executable}\n"
        f"Command: {' '.join(command)}\n\n"
    )
    log_path.write_text(startup_banner, encoding="utf-8")
    process = launch_tracked_process(
        command,
        bot_type="download",
        log_path=log_path,
        settings={
            "start_url": start_url,
            "max_chapters": max_chapters,
            "output_dir": output_dir,
            "reader_layout": reader_layout,
            "wait_for_inspection": wait_for_inspection,
            "save_images": save_images,
            "create_archive": create_archive,
            "save_ocr": save_ocr,
        },
    )
    st.session_state.bot_runtimes["download"] = {
        "process": process, "log_path": log_path, "signal_path": signal_path, "paused": False
    }
    return process, log_path, signal_path


def inject_styles() -> None:
    compact_css = ""
    if st.session_state.get("compact_ui", False):
        compact_css = """
        .block-container { padding-top: 5.25rem; padding-bottom: 1.25rem; }
        .app-shell, .panel, .metric-card, .primary-action { border-radius: 12px; padding: 0.82rem 0.9rem; }
        .app-shell { margin-bottom: 0.75rem; }
        .primary-action { min-height: 6.5rem; }
        div.stButton > button { min-height: 2.35rem; }
        .page-title { font-size: 1.65rem; }
        .page-subtitle, .muted, .primary-action-copy { font-size: 0.84rem; }
        """
    sidebar_css = ""
    if st.session_state.get("compact_sidebar", False):
        sidebar_css = """
        section[data-testid="stSidebar"] { min-width: 13rem !important; width: 13rem !important; }
        section[data-testid="stSidebar"] .brand-tagline { display: none; }
        section[data-testid="stSidebar"] button[kind] { min-height: 2.3rem; }
        """
    st.markdown(
        """
        <style>
        :root {
            --bg: #0B0C10;
            --bg-2: #0D0E12;
            --surface: #1A1C23;
            --surface-2: #1F2128;
            --surface-3: #242733;
            --line: rgba(255,255,255,0.08);
            --line-strong: #2F323E;
            --text: #E5E7EB;
            --muted: #9CA3AF;
            --muted-2: #6B7280;
            --accent: #00B4D8;
            --success: #2EC4B6;
            --warning: #E5E7EB;
            --accent-blue: #00B4D8;
            --accent-violet: #9B8AFB;
            --accent-emerald: #2EC4B6;
            --accent-crimson: #9D174D;
            --glass-bg: rgba(31, 33, 40, 0.88);
            --glass-border: rgba(255, 255, 255, 0.08);
            --shadow-lg: 0 30px 90px rgba(0,0,0,0.48);
            --shadow-md: 0 20px 54px rgba(0,0,0,0.34);
            --radius-sm: 10px;
            --radius-md: 16px;
            --radius-lg: 22px;
            --focus-ring: 0 0 0 3px rgba(38, 198, 218, 0.24);
            --motion-fast: 160ms;
            --motion-base: 320ms;
        }
        [data-testid="stHeader"], [data-testid="stToolbar"], .stDeployButton {
            display: none !important;
        }
        html, body, [data-testid="stAppViewContainer"], .main {
            background:
                linear-gradient(135deg, #0B0C10 0%, #0D0E12 52%, #11131A 100%);
            color: var(--text);
            font-family: Manrope, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        }
        .block-container {
            /* Streamlit's fixed toolbar overlays the top of the app canvas. */
            padding: 5.25rem 1.75rem 2.25rem 1.75rem;
            max-width: 1360px;
            position: relative;
            /* A transform animation here becomes the containing block for fixed UI. */
            animation: none;
        }
        .block-container::before {
            content: "";
            position: absolute;
            top: 0;
            left: 1.75rem;
            right: 1.75rem;
            height: 1px;
            background: linear-gradient(90deg, transparent, rgba(0,180,216,0.55), rgba(123,44,191,0.42), transparent);
            pointer-events: none;
            animation: topLineSweep 1300ms cubic-bezier(0.16, 1, 0.3, 1) both;
        }
        section[data-testid="stSidebar"] {
            background:
                linear-gradient(180deg, rgba(31,33,40,0.96) 0%, rgba(20,22,29,0.92) 100%),
                rgba(26,28,35,0.88);
            border-right: 1px solid rgba(255,255,255,0.08);
            backdrop-filter: blur(26px) saturate(145%);
            -webkit-backdrop-filter: blur(26px) saturate(145%);
            box-shadow: inset -1px 0 rgba(255,255,255,0.05), 18px 0 64px rgba(0,0,0,0.30);
            animation: sidebarSlideIn 520ms cubic-bezier(0.16, 1, 0.3, 1) both;
        }
        section[data-testid="stSidebar"] * {
            color: var(--text);
        }
        section[data-testid="stSidebar"] [data-testid="stSidebarContent"] { padding-top: 1.15rem; }
        section[data-testid="stSidebar"] [data-testid="stCaptionContainer"] p {
            color: #687386 !important;
            font-size: 0.67rem !important;
            font-weight: 760 !important;
            letter-spacing: 0.12em !important;
            text-transform: uppercase;
        }
        section[data-testid="stSidebar"] code {
            color: var(--text);
            background: #11131A;
            border: 1px solid var(--line);
            border-radius: 8px;
        }
        section[data-testid="stSidebar"][aria-expanded="false"] [data-testid="stSidebarUserContent"] {
            display: none !important;
        }
        h1, h2, h3, h4, h5, h6, p, label, span {
            color: var(--text);
            letter-spacing: 0;
        }
        h1, h2, h3 {
            font-family: "Space Grotesk", Manrope, ui-sans-serif, system-ui, sans-serif;
        }
        div[data-testid="stMetric"] {
            background:
                linear-gradient(180deg, rgba(255,255,255,0.052) 0%, rgba(0,180,216,0.018) 100%),
                var(--surface-2);
            border: 1px solid var(--line-strong);
            border-radius: 12px;
            padding: 1rem 1.05rem;
            box-shadow:
                inset 0 1px rgba(255,255,255,0.07),
                0 24px 68px rgba(0,0,0,0.36),
                0 8px 24px rgba(0,0,0,0.25);
        }
        div[data-testid="stMetric"] [data-testid="stMetricLabel"],
        div[data-testid="stMetric"] [data-testid="stMetricValue"] {
            color: var(--text);
            font-family: Manrope, ui-sans-serif, system-ui, sans-serif;
        }
        div.stButton > button {
            border-radius: 8px;
            min-height: 3rem;
            font-weight: 650;
            border: 1px solid var(--line-strong);
            background:
                linear-gradient(180deg, rgba(255,255,255,0.075) 0%, rgba(255,255,255,0.026) 100%),
                #1A1C23;
            color: var(--text);
            box-shadow:
                inset 0 1px rgba(255,255,255,0.09),
                inset 0 -1px rgba(0,0,0,0.35),
                0 18px 42px rgba(0,0,0,0.30);
            position: relative;
            overflow: hidden;
            transition: transform 180ms cubic-bezier(0.16, 1, 0.3, 1), border-color 180ms ease, background 180ms ease, box-shadow 180ms ease;
        }
        div.stButton > button::after {
            content: "";
            position: absolute;
            top: -55%;
            bottom: -55%;
            left: -45%;
            width: 38%;
            background: linear-gradient(90deg, transparent, rgba(255,255,255,0.22), transparent);
            transform: translateX(-140%) rotate(18deg);
            transition: transform 520ms cubic-bezier(0.16, 1, 0.3, 1);
            pointer-events: none;
        }
        div.stButton > button:hover {
            background:
                radial-gradient(circle at 50% -40%, rgba(0,180,216,0.22), transparent 48%),
                linear-gradient(180deg, rgba(255,255,255,0.10) 0%, rgba(255,255,255,0.038) 100%),
                #1F2128;
            border-color: rgba(0,180,216,0.46);
            color: var(--text);
            transform: translateY(-2px);
            box-shadow:
                inset 0 1px rgba(255,255,255,0.11),
                inset 0 -1px rgba(0,0,0,0.35),
                0 24px 54px rgba(0,0,0,0.42),
                0 0 24px rgba(0,180,216,0.14),
                0 0 42px rgba(123,44,191,0.10);
        }
        div.stButton > button:hover::after {
            transform: translateX(360%) rotate(18deg);
        }
        div.stButton > button:active {
            transform: scale(0.96) translateY(1px);
        }
        button:focus-visible, input:focus-visible, textarea:focus-visible,
        [role="radio"]:focus-visible, [role="checkbox"]:focus-visible {
            outline: 2px solid #67E8F9 !important;
            outline-offset: 3px !important;
            box-shadow: var(--focus-ring) !important;
        }
        section[data-testid="stSidebar"] button[kind] {
            background: transparent !important;
            color: var(--text) !important;
            border: 1px solid transparent;
            justify-content: flex-start;
            box-shadow: none;
            position: relative;
            overflow: hidden;
        }
        section[data-testid="stSidebar"] button[kind]::before {
            content: "";
            position: absolute;
            inset: 4px 6px;
            border-radius: 8px;
            background: transparent;
            opacity: 0;
            transform: translateY(12px) scale(0.96);
            transition: opacity 360ms cubic-bezier(0.16, 1, 0.3, 1), transform 360ms cubic-bezier(0.16, 1, 0.3, 1);
            z-index: -1;
        }
        section[data-testid="stSidebar"] button[kind]:hover {
            background: transparent;
            border-color: var(--line);
            color: var(--text);
        }
        section[data-testid="stSidebar"] button[kind]:hover::before {
            opacity: 1;
            transform: translateY(0) scale(1);
        }
        section[data-testid="stSidebar"] button[kind][kind="primary"] {
            color: #ECFEFF !important;
            border-color: rgba(103,232,249,.42) !important;
            background: #183d38 !important;
            box-shadow: inset 3px 0 #67E8F9, 0 10px 24px rgba(0,0,0,.18) !important;
        }
        input, textarea, select, div[data-baseweb="select"] > div {
            background-color: #11131A !important;
            color: var(--text) !important;
            border-color: var(--line-strong) !important;
            border-radius: 8px !important;
            transition: border-color 180ms ease, box-shadow 180ms ease, transform 180ms cubic-bezier(0.16, 1, 0.3, 1);
        }
        div[data-testid="stNumberInput"] input,
        div[data-testid="stTextInput"] input {
            background-color: #11131A !important;
            color: var(--text) !important;
        }
        div[data-testid="stNumberInput"] input:focus,
        div[data-testid="stTextInput"] input:focus,
        textarea:focus {
            border-color: rgba(0,180,216,0.48) !important;
            box-shadow: 0 0 0 3px rgba(0,180,216,0.12) !important;
            transform: translateY(-1px);
        }
        div[data-testid="stExpander"] {
            background: var(--surface);
            border: 1px solid var(--line);
            border-radius: 10px;
        }
        div[data-testid="stCodeBlock"] {
            border: 1px solid var(--line);
            border-radius: 10px;
            box-shadow: var(--shadow-md);
        }
        div[data-testid="stVerticalBlockBorderWrapper"] {
            border-color: rgba(255,255,255,0.08) !important;
            border-radius: 14px !important;
            background:
                linear-gradient(180deg, rgba(255,255,255,0.045), rgba(0,180,216,0.014)),
                rgba(31,33,40,0.86);
            box-shadow: var(--shadow-md);
        }
        .app-shell {
            border: 1px solid var(--line);
            border-radius: 16px;
            padding: 1.45rem 1.55rem;
            background:
                linear-gradient(180deg, rgba(255,255,255,0.06) 0%, rgba(123,44,191,0.03) 100%),
                var(--surface);
            margin-bottom: 1.2rem;
            box-shadow: var(--shadow-lg);
            position: relative;
            overflow: hidden;
            animation: cardEnter 520ms cubic-bezier(0.16, 1, 0.3, 1) both;
        }
        .app-shell::after {
            content: "";
            position: absolute;
            inset: 0;
            background: linear-gradient(115deg, transparent 0%, rgba(0,180,216,0.08) 42%, transparent 58%);
            transform: translateX(-120%);
            animation: headerShimmer 1900ms 260ms cubic-bezier(0.16, 1, 0.3, 1) both;
            pointer-events: none;
        }
        .page-title {
            font-size: clamp(1.75rem, 2.4vw, 2.85rem);
            font-weight: 760;
            color: var(--text);
            letter-spacing: 0;
            margin-bottom: 0.25rem;
            font-family: "Space Grotesk", Manrope, ui-sans-serif, system-ui, sans-serif;
        }
        .page-subtitle {
            color: var(--muted);
            font-size: 1rem;
            line-height: 1.55;
            max-width: 760px;
        }
        .panel {
            border: 1px solid var(--line);
            border-radius: 16px;
            padding: 1.15rem;
            background:
                linear-gradient(180deg, rgba(255,255,255,0.048) 0%, rgba(0,180,216,0.016) 100%),
                var(--surface);
            margin-bottom: 1rem;
            box-shadow: var(--shadow-md);
            animation: cardEnter 560ms cubic-bezier(0.16, 1, 0.3, 1) both;
            transition: transform 180ms cubic-bezier(0.16, 1, 0.3, 1), border-color 180ms ease, box-shadow 180ms ease;
        }
        .panel:hover {
            transform: translateY(-2px);
            border-color: rgba(0,180,216,0.22);
            box-shadow:
                inset 0 1px rgba(255,255,255,0.08),
                0 28px 64px rgba(0,0,0,0.44);
        }
        .journey-card {
            height: 12.8rem;
            min-height: 12.8rem;
            border: 1px solid rgba(255,255,255,0.085);
            border-radius: var(--radius-md);
            padding: 1.1rem 1.15rem;
            background: linear-gradient(145deg, rgba(255,255,255,.06), rgba(11,12,16,.72));
            position: relative;
            overflow: hidden;
        }
        .journey-card::before {
            content: "";
            position: absolute;
            inset: 0 auto 0 0;
            width: 3px;
            background: linear-gradient(180deg, #67E8F9, rgba(155,138,251,.72));
        }
        .journey-icon { font-size: 1.25rem; margin-bottom: .65rem; }
        .journey-title { font: 720 1rem/1.2 "Space Grotesk", sans-serif; }
        .journey-copy { color: var(--muted); font-size: .82rem; line-height: 1.55; margin-top: .42rem; }
        .journey-output { color: #CFFAFE; font-size: .72rem; font-weight: 700; margin-top: .65rem; }
        div[data-testid="stHorizontalBlock"]:has(.journey-card) > div[data-testid="stColumn"] > div[data-testid="stVerticalBlock"] {
            height: 100%;
            display: grid;
            grid-template-rows: 1fr auto;
            align-content: stretch;
        }
        div[data-testid="stHorizontalBlock"]:has(.journey-card) div.stButton > button {
            margin-top: .65rem;
        }
        .st-key-dashboard_engage button,
        .st-key-dashboard_capture button,
        .st-key-dashboard_reader button {
            min-height: 3rem !important;
        }
        div[data-testid="stHorizontalBlock"]:has(.st-key-dashboard_continue) {
            align-items: stretch;
            margin-top: .8rem;
        }
        div[data-testid="stHorizontalBlock"]:has(.st-key-dashboard_continue) div[data-testid="stAlert"],
        .st-key-dashboard_continue,
        .st-key-dashboard_continue button {
            height: 100%;
            min-height: 3.35rem;
        }
        .setup-stepper { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:.5rem; margin:-.25rem 0 1.35rem; }
        .setup-step { border:1px solid var(--line); border-radius:12px; padding:.72rem .78rem; background:rgba(18,20,27,.76); color:var(--muted); font-size:.75rem; font-weight:680; }
        .setup-step strong { display:block; color:inherit; font-size:.67rem; margin-bottom:.15rem; opacity:.72; }
        .setup-step.is-complete { color:#A7F3D0; border-color:rgba(46,196,182,.24); }
        .setup-step.is-current { color:#ECFEFF; border-color:rgba(103,232,249,.52); background:linear-gradient(145deg,rgba(0,180,216,.14),rgba(155,138,251,.08)); box-shadow:0 0 28px rgba(0,180,216,.09); }
        .autosave-note { display:flex; align-items:center; gap:.4rem; margin:.2rem 0 1rem; color:var(--muted); font-size:.78rem; }
        .autosave-note::before { content:"✓"; color:var(--success); font-weight:800; }
        .onboarding-grid { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:.7rem; margin:.8rem 0; }
        .onboarding-item { padding:.85rem; border-radius:12px; background:rgba(255,255,255,.035); border:1px solid var(--line); }
        .onboarding-item strong { display:block; margin-bottom:.25rem; }
        .onboarding-item span { color:var(--muted); font-size:.78rem; line-height:1.45; }
        .mac-traffic {
            display: flex;
            gap: 8px;
            align-items: center;
            margin: 0.15rem 0 1.25rem 0;
            height: 18px;
        }
        .traffic-dot {
            width: 12px;
            height: 12px;
            border-radius: 50%;
            display: inline-grid;
            place-items: center;
            font-size: 8px;
            line-height: 1;
            color: rgba(0,0,0,0);
            box-shadow: inset 0 1px rgba(255,255,255,0.42), 0 2px 8px rgba(0,0,0,0.35);
            transition: color 150ms ease, transform 150ms ease;
            font-family: Manrope, ui-sans-serif, system-ui, sans-serif;
            font-weight: 760;
        }
        .mac-traffic:hover .traffic-dot {
            color: rgba(0,0,0,0.56);
        }
        .traffic-dot:hover {
            transform: scale(1.08);
        }
        .traffic-red { background: #FF5F56; }
        .traffic-yellow { background: #FFBD2E; }
        .traffic-green { background: #27C93F; }
        .brand-lockup {
            margin: 0 0 1.05rem;
        }
        .brand-name {
            color: var(--text);
            font-family: "Space Grotesk", Manrope, ui-sans-serif, system-ui, sans-serif;
            font-size: 1.34rem;
            font-weight: 720;
            line-height: 1.1;
        }
        .brand-tagline {
            color: var(--muted);
            font-size: 0.76rem;
            margin-top: 0.32rem;
        }
        .global-bar {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 1rem;
            padding: 0.68rem 0.85rem;
            margin-bottom: 0.85rem;
            border: 1px solid var(--line);
            border-radius: 12px;
            background: rgba(26, 28, 35, 0.72);
            box-shadow: inset 0 1px rgba(255,255,255,0.06), 0 14px 34px rgba(0,0,0,0.24);
            backdrop-filter: blur(18px) saturate(125%);
        }
        .global-breadcrumb {
            color: var(--muted);
            font-size: 0.82rem;
        }
        .global-breadcrumb strong {
            color: var(--text);
        }
        .global-status {
            display: flex;
            align-items: center;
            gap: 0.45rem;
            color: var(--muted);
            font-size: 0.8rem;
            white-space: nowrap;
        }
        .global-status-dot {
            width: 7px;
            height: 7px;
            border-radius: 50%;
            background: var(--success);
            box-shadow: 0 0 14px rgba(46,196,182,0.45);
        }
        .module-kicker {
            color: var(--accent);
            font-size: 0.72rem;
            font-weight: 720;
            text-transform: uppercase;
            margin-bottom: 0.42rem;
        }
        @property --metric-value {
            syntax: '<integer>';
            initial-value: 0;
            inherits: false;
        }
        @keyframes countUpMetric {
            from { --metric-value: 0; }
            to { --metric-value: var(--metric-target); }
        }
        @keyframes pageFadeUp {
            from { opacity: 0; transform: translateY(12px); }
            to { opacity: 1; transform: translateY(0); }
        }
        @keyframes cardEnter {
            from { opacity: 0; transform: translateY(14px) scale(0.985); }
            to { opacity: 1; transform: translateY(0) scale(1); }
        }
        @keyframes sidebarSlideIn {
            from { opacity: 0; transform: translateX(-18px); }
            to { opacity: 1; transform: translateX(0); }
        }
        @keyframes topLineSweep {
            from { opacity: 0; transform: scaleX(0.18); }
            to { opacity: 1; transform: scaleX(1); }
        }
        @keyframes headerShimmer {
            0% { opacity: 0; transform: translateX(-120%); }
            18% { opacity: 1; }
            100% { opacity: 0; transform: translateX(120%); }
        }
        @keyframes statusPulse {
            0%, 100% { box-shadow: inset 0 1px rgba(255,255,255,0.08), 0 0 0 rgba(0,180,216,0); }
            50% { box-shadow: inset 0 1px rgba(255,255,255,0.12), 0 0 24px rgba(46,196,182,0.14); }
        }
        @keyframes trafficBreathe {
            0%, 100% { transform: translateY(0); filter: saturate(1); }
            50% { transform: translateY(-1px); filter: saturate(1.22); }
        }
        @keyframes ambientIdlePulse {
            0%, 100% {
                box-shadow:
                    inset 0 1px rgba(255,255,255,0.08),
                    0 26px 70px rgba(0,0,0,0.40),
                    0 0 0 rgba(46,196,182,0.00);
            }
            50% {
                box-shadow:
                    inset 0 1px rgba(255,255,255,0.12),
                    0 30px 82px rgba(0,0,0,0.48),
                    0 0 34px rgba(46,196,182,0.12);
            }
        }
        .metric-card {
            border: 1px solid var(--line-strong);
            border-radius: 12px;
            padding: 1rem 1.08rem;
            min-height: 112px;
            background:
                linear-gradient(180deg, rgba(255,255,255,0.055) 0%, rgba(0,180,216,0.016) 100%),
                var(--surface-2);
            box-shadow:
                inset 0 1px rgba(255,255,255,0.08),
                0 30px 74px rgba(0,0,0,0.38),
                0 8px 24px rgba(0,0,0,0.28);
            animation: cardEnter 620ms cubic-bezier(0.16, 1, 0.3, 1) both;
            transition: transform 180ms cubic-bezier(0.16, 1, 0.3, 1), border-color 180ms ease, box-shadow 180ms ease;
            position: relative;
            overflow: hidden;
        }
        div[data-testid="column"]:nth-of-type(2) .metric-card { animation-delay: 55ms; }
        div[data-testid="column"]:nth-of-type(3) .metric-card { animation-delay: 110ms; }
        div[data-testid="column"]:nth-of-type(4) .metric-card { animation-delay: 165ms; }
        .metric-card:hover {
            transform: translateY(-3px);
            border-color: rgba(0,180,216,0.28);
            box-shadow:
                inset 0 1px rgba(255,255,255,0.12),
                0 34px 84px rgba(0,0,0,0.46),
                0 0 32px rgba(0,180,216,0.08);
        }
        .metric-card.idle {
            animation: ambientIdlePulse 4.8s ease-in-out infinite;
        }
        .metric-label {
            color: var(--muted);
            font-size: 0.82rem;
            margin-bottom: 0.62rem;
        }
        .metric-value {
            color: var(--text);
            font-size: 1.8rem;
            font-weight: 760;
            letter-spacing: 0;
        }
        .metric-value.count-up {
            --metric-value: 0;
            animation: countUpMetric 900ms cubic-bezier(0.16, 1, 0.3, 1) forwards;
            counter-reset: metric var(--metric-value);
        }
        .metric-value.count-up::after {
            content: counter(metric);
        }
        .metric-helper {
            color: var(--muted-2);
            font-size: 0.78rem;
            margin-top: 0.45rem;
        }
        .panel-title {
            font-weight: 720;
            color: var(--text);
            margin-bottom: 0.35rem;
        }
        .muted {
            color: var(--muted);
            font-size: 0.92rem;
            line-height: 1.5;
        }
        .pill-row {
            display: flex;
            flex-wrap: wrap;
            gap: 0.45rem;
            margin-top: 0.85rem;
        }
        .pill {
            border: 1px solid var(--line);
            border-radius: 8px;
            padding: 0.28rem 0.62rem;
            color: var(--muted);
            background: rgba(31,33,40,0.72);
            font-size: 0.82rem;
            animation: cardEnter 500ms cubic-bezier(0.16, 1, 0.3, 1) both;
        }
        .status-waiting {
            border: 1px solid rgba(0,180,216,0.20);
            background: #1A1C23;
            padding: 0.9rem 1rem;
            border-radius: 10px;
            color: var(--text);
            animation: statusPulse 2.4s ease-in-out infinite;
        }
        .status-ok {
            border: 1px solid rgba(46,196,182,0.20);
            background: #1A1C23;
            padding: 0.9rem 1rem;
            border-radius: 10px;
            color: var(--text);
            animation: cardEnter 460ms cubic-bezier(0.16, 1, 0.3, 1) both;
        }
        .primary-action {
            border: 1px solid var(--line-strong);
            border-radius: 12px;
            padding: 1.15rem;
            background:
                linear-gradient(180deg, rgba(255,255,255,0.048) 0%, rgba(123,44,191,0.018) 100%),
                var(--surface);
            min-height: 8.4rem;
            box-shadow:
                inset 0 1px rgba(255,255,255,0.08),
                0 30px 72px rgba(0,0,0,0.36),
                0 8px 22px rgba(0,0,0,0.30);
            transition: transform 180ms cubic-bezier(0.16, 1, 0.3, 1), box-shadow 180ms ease;
            animation: cardEnter 620ms cubic-bezier(0.16, 1, 0.3, 1) both;
        }
        .primary-action:hover {
            transform: translateY(-2px);
            box-shadow:
                inset 0 1px rgba(255,255,255,0.11),
                0 34px 82px rgba(0,0,0,0.44),
                0 0 30px rgba(0,180,216,0.08);
        }
        .primary-action-title {
            font-weight: 720;
            color: var(--text);
            margin-bottom: 0.35rem;
        }
        .primary-action-copy {
            color: var(--muted);
            font-size: 0.9rem;
            line-height: 1.45;
        }
        [data-testid="stDataFrame"] {
            border: 1px solid var(--line);
            border-radius: 10px;
            overflow: hidden;
            animation: cardEnter 520ms cubic-bezier(0.16, 1, 0.3, 1) both;
        }
        div[data-testid="stVegaLiteChart"],
        div[data-testid="stTable"] {
            animation: cardEnter 560ms cubic-bezier(0.16, 1, 0.3, 1) both;
        }
        .traffic-dot {
            animation: trafficBreathe 3.2s ease-in-out infinite;
        }
        .traffic-yellow { animation-delay: 120ms; }
        .traffic-green { animation-delay: 240ms; }
        .assistant-shell {
            border: 1px solid var(--line-strong);
            border-radius: 18px;
            padding: 1rem;
            margin: 0.9rem 0 1.1rem;
            background:
                linear-gradient(180deg, rgba(255,255,255,0.052), rgba(0,180,216,0.016)),
                #1A1C23;
            box-shadow: var(--shadow-md);
            animation: assistantSlideUp 360ms cubic-bezier(0.16, 1, 0.3, 1) both;
        }
        .assistant-title {
            font-weight: 760;
            margin-bottom: 0.25rem;
            color: var(--text);
        }
        .assistant-copy {
            color: var(--muted);
            font-size: 0.9rem;
            line-height: 1.5;
        }
        .floating-chat-header {
            border: 1px solid rgba(255,255,255,0.08);
            border-radius: 12px;
            padding: 0.75rem 0.85rem;
            margin-bottom: 0.75rem;
            background: rgba(31,33,40,0.78);
        }
        .floating-chat-title {
            color: var(--text);
            font-weight: 760;
            margin-bottom: 0.15rem;
        }
        .floating-chat-copy {
            color: var(--muted);
            font-size: 0.82rem;
            line-height: 1.45;
        }
        .floating-chat-scroll {
            max-height: 320px;
            overflow: auto;
            padding-right: 0.2rem;
        }
        .chat-shell {
            max-width: 860px;
            margin: 0 auto 1rem auto;
            border: 1px solid var(--line);
            border-radius: 18px;
            padding: 1.1rem 1.2rem;
            background:
                linear-gradient(180deg, rgba(255,255,255,0.055), rgba(123,44,191,0.018)),
                #1A1C23;
            box-shadow: var(--shadow-md);
            animation: assistantSlideUp 360ms cubic-bezier(0.16, 1, 0.3, 1) both;
        }
        .chat-kicker {
            color: var(--muted-2);
            font-size: 0.78rem;
            text-transform: uppercase;
            letter-spacing: 0.08em;
            margin-bottom: 0.25rem;
        }
        .chat-title {
            color: var(--text);
            font-size: 2rem;
            font-weight: 760;
            font-family: "Space Grotesk", Manrope, ui-sans-serif, system-ui, sans-serif;
            letter-spacing: 0;
        }
        .chat-copy {
            color: var(--muted);
            line-height: 1.5;
            max-width: 680px;
        }
        .chat-status {
            max-width: 860px;
            margin: 0 auto 1rem auto;
            display: flex;
            flex-wrap: wrap;
            gap: 0.45rem;
        }
        .chat-status span {
            border: 1px solid var(--line);
            border-radius: 8px;
            padding: 0.25rem 0.58rem;
            background: rgba(31,33,40,0.72);
            color: var(--muted);
            font-size: 0.8rem;
        }
        .chat-empty {
            max-width: 860px;
            margin: 1rem auto;
            border: 1px dashed rgba(255,255,255,0.18);
            border-radius: 12px;
            padding: 1rem;
            color: var(--muted);
            background: rgba(31,33,40,0.72);
        }
        div[data-testid="stChatMessage"] {
            max-width: 860px;
            margin-left: auto;
            margin-right: auto;
            border: 1px solid rgba(255,255,255,0.08);
            border-radius: 12px;
            padding: 0.9rem 1rem;
            background:
                linear-gradient(180deg, rgba(255,255,255,0.045), rgba(0,180,216,0.014)),
                #1A1C23;
            animation: cardEnter 260ms cubic-bezier(0.16, 1, 0.3, 1) both;
        }
        div[data-testid="stChatInput"] {
            max-width: 860px;
            margin-left: auto;
            margin-right: auto;
        }
        .app-shell,
        .panel,
        .metric-card,
        .primary-action,
        .assistant-shell,
        .chat-shell,
        .floating-chat-header,
        .chat-empty,
        div[data-testid="stMetric"],
        div[data-testid="stVerticalBlockBorderWrapper"],
        div[data-testid="stExpander"],
        div[data-testid="stCodeBlock"],
        [data-testid="stDataFrame"],
        div[data-testid="stPopoverBody"] {
            background:
                linear-gradient(180deg, rgba(255,255,255,0.045) 0%, rgba(0,180,216,0.014) 100%),
                rgba(31, 33, 40, 0.88) !important;
            border: 1px solid rgba(255, 255, 255, 0.08) !important;
            backdrop-filter: blur(10px) saturate(118%);
            -webkit-backdrop-filter: blur(10px) saturate(118%);
            box-shadow:
                inset 0 1px rgba(255,255,255,0.07),
                0 24px 72px rgba(0,0,0,0.36) !important;
        }
        .app-shell,
        .panel,
        .metric-card,
        .primary-action,
        .assistant-shell,
        .chat-shell,
        .floating-chat-header,
        .chat-empty,
        div[data-testid="stMetric"],
        div[data-testid="stVerticalBlockBorderWrapper"] {
            position: relative;
            overflow: hidden;
            isolation: isolate;
        }
        .metric-card.active::before,
        div[data-testid="stMetric"]::before {
            content: "";
            position: absolute;
            top: 0;
            left: 0;
            right: 0;
            height: 2px;
            background: linear-gradient(90deg, var(--accent-violet), var(--accent-blue), var(--accent-emerald));
            box-shadow:
                0 0 18px rgba(0,180,216,0.34),
                0 0 28px rgba(123,44,191,0.20);
            opacity: 0.76;
            pointer-events: none;
            z-index: 2;
        }
        .metric-card.idle::before {
            content: "";
            position: absolute;
            top: 0;
            left: 0;
            right: 0;
            height: 2px;
            background: linear-gradient(90deg, rgba(46,196,182,0.55), rgba(0,180,216,0.26));
            box-shadow: 0 0 14px rgba(46,196,182,0.20);
            opacity: 0.64;
            pointer-events: none;
            z-index: 2;
        }
        .primary-action:hover,
        .panel:hover,
        .metric-card:hover {
            border-color: rgba(0,180,216,0.28) !important;
            box-shadow:
                inset 0 1px rgba(255,255,255,0.10),
                0 28px 78px rgba(0,0,0,0.42),
                0 0 26px rgba(0,180,216,0.10) !important;
        }
        @keyframes aiFloat {
            0%, 100% { transform: translateY(0); }
            50% { transform: translateY(-5px); }
        }
        @keyframes assistantSlideUp {
            from { opacity: 0; transform: translateY(18px) scale(0.99); }
            to { opacity: 1; transform: translateY(0) scale(1); }
        }
        @media (prefers-reduced-motion: reduce) {
            *,
            *::before,
            *::after {
                animation-duration: 0.001ms !important;
                animation-iteration-count: 1 !important;
                scroll-behavior: auto !important;
                transition-duration: 0.001ms !important;
            }
        }
        /* Quiet production layer: preserve hierarchy without decorative noise. */
        .app-shell::after,
        div.stButton > button::after {
            display: none !important;
        }
        .traffic-dot,
        div[data-testid="stPopover"] button {
            animation: none !important;
        }
        .sidebar-status {
            display: flex;
            align-items: center;
            gap: 0.55rem;
            color: var(--muted);
            font-size: 0.82rem;
            padding: 0.45rem 0.15rem;
        }
        .global-status-dot {
            width: 0.48rem;
            height: 0.48rem;
            border-radius: 50%;
            background: var(--success);
            box-shadow: 0 0 0 3px rgba(46,196,182,0.10);
        }
        .app-shell,
        .panel,
        .metric-card,
        .primary-action,
        div[data-testid="stMetric"],
        div[data-testid="stVerticalBlockBorderWrapper"],
        div[data-testid="stExpander"] {
            box-shadow: inset 0 1px rgba(255,255,255,0.055), 0 12px 32px rgba(0,0,0,0.22) !important;
        }
        .primary-action:hover,
        .panel:hover,
        .metric-card:hover,
        div.stButton > button:hover {
            transform: none !important;
            box-shadow: inset 0 1px rgba(255,255,255,0.075), 0 14px 36px rgba(0,0,0,0.26) !important;
        }
        .page-title {
            font-size: clamp(1.55rem, 2.35vw, 2.15rem) !important;
            line-height: 1.12;
            max-width: 980px;
        }
        .app-shell {
            padding: 1.25rem 1.35rem !important;
            margin-bottom: 1rem !important;
        }
        div.stButton > button {
            min-height: 2.65rem;
            box-shadow: inset 0 1px rgba(255,255,255,0.07), 0 8px 20px rgba(0,0,0,0.20);
        }
        /* Keep comic panels continuous while leaving image spacing elsewhere intact. */
        .st-key-reader_pages,
        .st-key-reader_pages > div[data-testid="stVerticalBlock"] {
            gap: 0 !important;
        }
        .st-key-reader_pages div[data-testid="stElementContainer"],
        .st-key-reader_pages div[data-testid="stImage"],
        .st-key-reader_pages [data-testid="stImage"] > div {
            width: 100% !important;
            margin: 0 !important;
            padding: 0 !important;
            line-height: 0 !important;
        }
        .st-key-reader_pages div[data-testid="stElementContainer"] {
            display: flex !important;
            justify-content: center !important;
        }
        .st-key-reader_pages img {
            display: block !important;
            width: 100% !important;
            height: auto !important;
            margin: 0 auto !important;
        }
        @media (max-width: 760px) {
            .block-container {
                padding-left: 0.85rem !important;
                padding-right: 0.85rem !important;
            }
            .app-shell {
                padding: 1rem !important;
            }
            div[data-testid="stHorizontalBlock"] {
                gap: 0.65rem;
            }
            .setup-stepper { grid-template-columns:repeat(2,minmax(0,1fr)); }
            .onboarding-grid { grid-template-columns:1fr; }
            .journey-card { height:auto; min-height:10.5rem; }
        }
        __COMPACT_CSS__
        __SIDEBAR_CSS__
        </style>
        """.replace("__COMPACT_CSS__", compact_css).replace("__SIDEBAR_CSS__", sidebar_css),
        unsafe_allow_html=True,
    )


def render_page_header(title: str, subtitle: str, pills: list[str] | None = None) -> None:
    st.markdown(f'<header class="app-shell"><h1 class="page-title">{html.escape(title)}</h1><p class="page-subtitle">{html.escape(subtitle)}</p></header>', unsafe_allow_html=True)


def render_global_bar(page: str, status: str) -> None:
    breadcrumb = PRODUCT_NAME
    if page != "Dashboard":
        breadcrumb += f" / <strong>{page}</strong>"
    st.markdown(
        f"""
        <div class="global-bar">
          <div class="global-breadcrumb">{breadcrumb}</div>
          <div class="global-status"><span class="global-status-dot"></span>{PRODUCT_MODE} · {status}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_panel(title: str, body: str) -> None:
    st.markdown(
        f"""
        <div class="panel">
          <div class="panel-title">{title}</div>
          <div class="muted">{body}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_journey_card(icon: str, title: str, body: str, output: str) -> None:
    st.markdown(
        f"""
        <section class="journey-card" aria-label="{html.escape(title)}">
          <div class="journey-icon" aria-hidden="true">{html.escape(icon)}</div>
          <div class="journey-title">{html.escape(title)}</div>
          <div class="journey-copy">{html.escape(body)}</div>
          <div class="journey-output">Creates: {html.escape(output)}</div>
        </section>
        """,
        unsafe_allow_html=True,
    )


def render_setup_stepper(current: int, labels: tuple[str, str, str, str]) -> None:
    current_step = max(1, min(4, int(current)))
    items = []
    for index, label in enumerate(labels, start=1):
        state = "is-current" if index == current_step else "is-complete" if index < current_step else ""
        state_text = "Current" if index == current_step else "Complete" if index < current_step else "Upcoming"
        items.append(
            f'<div class="setup-step {state}" aria-label="Step {index}: {html.escape(label)}, {state_text}">'
            f'<strong>STEP {index}</strong>{html.escape(label)}</div>'
        )
    st.markdown(
        '<nav class="setup-stepper" aria-label="Workflow progress">' + "".join(items) + "</nav>",
        unsafe_allow_html=True,
    )


def render_draft_note() -> None:
    st.markdown(
        '<div class="autosave-note" role="status">Changes are kept automatically on this Mac.</div>',
        unsafe_allow_html=True,
    )


def render_metric_card(label: str, value: int | str, helper: str = "", idle: bool = False) -> None:
    state_class = " idle" if idle else " active"
    if isinstance(value, int):
        value_html = f"<div class='metric-value count-up' style='--metric-target: {value}'></div>"
    else:
        value_html = f"<div class='metric-value'>{value}</div>"
    st.markdown(
        f"""
        <div class="metric-card{state_class}">
          <div class="metric-label">{label}</div>
          {value_html}
          <div class="metric-helper">{helper}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_jsonl_metric_grid(records: list[dict[str, object]], columns: int = 4) -> None:
    metrics = jsonl_metric_values(records)
    items = list(metrics.items())
    for start in range(0, len(items), columns):
        cols = st.columns(columns)
        for col, (label, value) in zip(cols, items[start : start + columns]):
            col.metric(label, value)


def build_copilot_context(records: list[dict[str, object]], query: str = "") -> CopilotContext:
    run_records = list_run_records(limit=100)
    bot_to_workspace = {
        "comment": "engage",
        "download": "capture",
        "word_art": "studio",
    }
    runs: dict[str, WorkspaceRun] = {}
    for bot_type, workspace in bot_to_workspace.items():
        latest = next(
            (record for record in run_records if str(record.get("bot_type", "")) == bot_type),
            {},
        )
        runtime = st.session_state.bot_runtimes.get(bot_type, {})
        runtime_log = runtime.get("log_path")
        log_path = Path(runtime_log) if runtime_log else Path(str(latest.get("log_path", "")))
        log_text = read_log(log_path, 50_000) if str(log_path) and log_path.is_file() else ""
        process = runtime.get("process")
        if is_process_running(process):
            status = "running"
        else:
            status = str(latest.get("status", "idle") or "idle")
        runs[workspace] = WorkspaceRun(
            workspace=workspace,
            status=status,
            stage=str(latest.get("stage", "") or ""),
            message=str(latest.get("message", "") or latest.get("stop_reason", "") or ""),
            progress_current=int(latest.get("progress_current", 0) or 0),
            progress_total=int(latest.get("progress_total", 0) or 0),
            log_path=str(log_path) if log_text else "",
            settings=dict(latest.get("settings", {})) if isinstance(latest.get("settings"), dict) else {},
            events=extract_log_events(log_text),
        )

    capture_metadata = list(OCR_DOWNLOAD_DIR.rglob("metadata.json")) if OCR_DOWNLOAD_DIR.exists() else []
    capture_images = (
        sum(
            1
            for path in OCR_DOWNLOAD_DIR.rglob("*")
            if path.is_file() and path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".gif"}
        )
        if OCR_DOWNLOAD_DIR.exists()
        else 0
    )
    current_page = str(st.session_state.page)
    current_view = str(st.session_state.get(f"{current_page.lower()}_view", "Overview"))
    selected_run_context = st.session_state.get("assistant_run_context", {})
    if current_page == "Assistant" and isinstance(selected_run_context, dict):
        selected_workspace = str(selected_run_context.get("workspace", "")).strip().lower()
        if selected_workspace in {"engage", "capture", "studio"}:
            current_page = selected_workspace.title()
            current_view = "Live"
    recent_comments = tuple(
        str(record.get("comment", "")).strip()
        for record in records[-12:]
        if str(record.get("comment", "")).strip()
    )
    settings = {
        "engage": {
            "url": st.session_state.start_url,
            "max_chapters": int(st.session_state.max_chapters),
            "routing": st.session_state.chapter_routing,
            "speed": st.session_state.scroll_preset,
            "selector_profile": st.session_state.selected_selector_profile,
        },
        "capture": {
            "url": st.session_state.ocr_start_url,
            "max_chapters": int(st.session_state.ocr_max_chapters),
            "routing": st.session_state.ocr_reader_layout,
            "output_dir": st.session_state.ocr_output_dir,
            "selector_profile": st.session_state.ocr_selector_profile,
        },
        "studio": {
            "url": st.session_state.note_url,
            "style": st.session_state.word_art_style_mode,
            "skip_submit": bool(st.session_state.note_skip_submit),
        },
    }
    outputs = {
        "comment_history_records": len(records),
        "capture_chapters": len(capture_metadata),
        "capture_images": capture_images,
        "capture_root": str(OCR_DOWNLOAD_DIR),
        "studio_preview": str(st.session_state.studio_preview_path or ""),
    }
    catalog = reader_catalog()
    chapter_availability: dict[str, list[str]] = {}
    original_chapters = 0
    clean_chapters = 0
    reader_series: list[dict[str, object]] = []
    for series_key, entry in catalog.items():
        editions = entry.get("editions", {}) if isinstance(entry, dict) else {}
        edition_summary: dict[str, list[str]] = {}
        if isinstance(editions, dict):
            for edition, chapters in editions.items():
                chapter_names = sorted(str(name) for name in chapters) if isinstance(chapters, dict) else []
                edition_summary[str(edition)] = chapter_names
                if str(edition) == "Original":
                    original_chapters += len(chapter_names)
                elif str(edition) == "Clean":
                    clean_chapters += len(chapter_names)
                for chapter in chapter_names:
                    chapter_availability.setdefault(chapter, []).append(str(edition))
        reader_series.append(
            {
                "key": str(series_key),
                "title": str(entry.get("title", series_key)) if isinstance(entry, dict) else str(series_key),
                "editions": edition_summary,
            }
        )
    history_summary = tuple(
        {
            "workspace": {"comment": "Engage", "download": "Capture", "word_art": "Studio"}.get(
                str(record.get("bot_type", "")), "Unknown"
            ),
            "status": str(record.get("status", "unknown")),
            "stage": str(record.get("stage", "")),
            "message": str(record.get("message", "") or record.get("stop_reason", ""))[:500],
        }
        for record in run_records[:12]
    )
    health_checks = environment_health()
    page_context = PageContext(
        page=current_page,
        view=current_view,
        workspace=current_page.lower() if current_page.lower() in {"engage", "capture", "studio"} else "",
        series=str(st.session_state.get("reader_series", "")),
        edition=str(st.session_state.get("reader_edition", "Original")),
        chapter=str(st.session_state.get("reader_chapter", "")),
        attached_run=str(selected_run_context.get("run_id", "")) if isinstance(selected_run_context, dict) else "",
    )
    grounding = KNOWLEDGE_SERVICE.retrieve(query, page_context) if query else None
    return CopilotContext(
        current_page=current_page,
        current_view=current_view,
        browser_owner=browser_lock_owner(),
        runs=runs,
        settings=settings,
        outputs=outputs,
        recent_comments=recent_comments,
        tickets=tuple(read_owner_requests()),
        browser_snapshot=dict(st.session_state.assistant_browser_snapshot),
        reader={
            "series": reader_series,
            "original_chapters": original_chapters,
            "clean_chapters": clean_chapters,
            "chapter_availability": chapter_availability,
            "selected": {
                "series": str(st.session_state.get("reader_series", "")),
                "edition": str(st.session_state.get("reader_edition", "Original")),
                "chapter": str(st.session_state.get("reader_chapter", "")),
            },
            "resume": "Stored privately by the local Reader in browser storage.",
        },
        run_history=history_summary,
        environment={
            str(check.get("check", "Check")): {
                "status": str(check.get("status", "")),
                "detail": str(check.get("detail", "")),
            }
            for check in health_checks
        },
        product={
            "name": PRODUCT_NAME,
            "version": PRODUCT_VERSION,
            "mode": PRODUCT_MODE,
            "navigation": NAVIGATION_GROUPS,
            "workspaces": ["Library", "Downloads", "Capture", "Engage", "Lyra", "Activity", "Settings"],
        },
        grounding=(json.loads(grounding.prompt_text) if grounding and grounding.relevant else {}),
        grounding_sources=grounding.source_labels if grounding else (),
    )


def copilot_message_from_plan(
    plan: AssistantTurnPlan,
    content: str,
    provider: str,
) -> dict[str, object]:
    return plan.to_message(content, provider)


def action_workspace_record(workspace: str) -> dict[str, object]:
    bot_type = {"engage": "comment", "capture": "download", "studio": "word_art"}[workspace]
    return next(
        (
            record
            for record in list_run_records(limit=100)
            if str(record.get("bot_type", "")) == bot_type
        ),
        {},
    )


def prepare_capture_review_from_lyra(payload: dict[str, object]) -> str:
    """Prepare Capture and open Chrome, but never release the user's start signal."""
    if not bool(payload.get("explicit_request")):
        return "Lyra blocked the preparation because the request was not an explicit Capture command."
    values = payload.get("values", {})
    values = dict(values) if isinstance(values, dict) else {}
    url = normalize_web_url(str(values.get("url", "")))
    issues = preflight_url(url)
    if issues:
        return "Capture was not prepared: " + "; ".join(issues) + ". Your draft was kept."
    download_runtime = st.session_state.bot_runtimes.get("download", {})
    if is_process_running(download_runtime.get("process")):
        return "Capture already has an active run. Stop or finish it before Lyra prepares another one."
    owner = browser_lock_owner()
    if owner:
        return (
            f"Chrome is currently reserved by {owner.get('bot_type', 'another Panelyra workflow')}. "
            "Lyra kept the Capture draft but did not open a second browser."
        )

    apply_ocr_download_defaults()
    routing = {
        "infinite-scroll": "continuous",
        "next-link": "next-chapter",
    }.get(str(values.get("routing", "auto")), str(values.get("routing", "auto")))
    st.session_state.ocr_start_url = url
    st.session_state.ocr_max_chapters = max(1, min(10000, int(values.get("max_chapters", 1))))
    st.session_state.ocr_reader_layout = routing if routing in {
        "auto", "continuous", "next-chapter", "paginated", "mixed"
    } else "auto"
    st.session_state.ocr_performance_profile = str(values.get("performance", "Balanced"))
    st.session_state.ocr_save_images = bool(values.get("save_images", True))
    st.session_state.ocr_create_archive = bool(values.get("create_archive", True))
    st.session_state.ocr_save_text = bool(values.get("save_ocr", False))
    st.session_state.capture_setup_step = 3

    selector_issues = []
    if not str(st.session_state.ocr_next_chapter_selector).strip():
        selector_issues.append("the next-chapter selector is missing")
    if st.session_state.ocr_reader_layout in {"paginated", "mixed"} and not str(
        st.session_state.ocr_next_page_selector
    ).strip():
        selector_issues.append("the next-page selector is missing")
    if selector_issues:
        set_page("Capture Setup")
        return "Lyra filled the Capture draft but could not open browser review because " + "; ".join(selector_issues) + "."

    output_dir = str(
        Path(str(st.session_state.ocr_custom_output_dir).strip() or str(OCR_DOWNLOAD_DIR)).expanduser()
    )
    process, log_path, signal_path = start_manhwa_ocr_bot(
        url,
        int(st.session_state.ocr_max_chapters),
        True,
        output_dir,
        str(st.session_state.ocr_container_selector).strip() or MANHWA_CONTAINER_SELECTOR,
        str(st.session_state.ocr_download_image_selector).strip() or MANHWA_IMAGE_SELECTOR,
        float(st.session_state.ocr_download_confidence),
        float(st.session_state.ocr_scroll_min_delay),
        float(st.session_state.ocr_scroll_max_delay),
        float(st.session_state.ocr_image_fetch_timeout),
        bool(st.session_state.ocr_save_images),
        bool(st.session_state.ocr_create_archive),
        bool(st.session_state.ocr_save_text),
        str(st.session_state.ocr_reader_layout),
        str(st.session_state.ocr_next_chapter_selector).strip(),
        str(st.session_state.ocr_next_page_selector).strip(),
        str(st.session_state.ocr_page_counter_selector).strip(),
        str(st.session_state.ocr_chapter_list_selector).strip(),
        str(st.session_state.ocr_performance_profile),
    )
    st.session_state.bot_process = process
    st.session_state.log_path = log_path
    st.session_state.start_signal_path = signal_path
    st.session_state.lyra_capture_prepared = True
    st.session_state.lyra_capture_review_status = "waiting for browser review"
    st.session_state.lyra_capture_browser_snapshot = {}
    st.session_state.lyra_capture_expected_url = url
    set_page("Capture Live")
    return (
        f"Capture is prepared for `{url}` and Chrome is opening in review mode. "
        "Inspect the reader, then click **Begin Capture**. Lyra has not started downloading."
    )


def execute_copilot_action(action: dict[str, object]) -> str:
    kind = str(action.get("kind", ""))
    payload = action.get("payload", {})
    payload = dict(payload) if isinstance(payload, dict) else {}
    if str(payload.get("workspace", "")).lower() == "studio" and kind != "navigate":
        return "Studio is retired. Its saved files and history remain available; no configuration was changed."
    if kind in {"create_ticket", "create_owner_request"}:
        return "Support-ticket management has been retired. Open Settings → Help for diagnostics."
    if kind == "prepare_capture_review":
        return prepare_capture_review_from_lyra(payload)
    if kind == "navigate":
        page = str(payload.get("page", "Dashboard"))
        view = str(payload.get("view", ""))
        set_page(page)
        if view and page in {"Engage", "Capture"}:
            st.session_state[f"{page.lower()}_view"] = view
        return f"Opened {page}{f' {view}' if view else ''}."
    if kind == "prepare_retry":
        workspace = str(payload.get("workspace", "engage"))
        record = action_workspace_record(workspace)
        if not record:
            return f"No previous {workspace.title()} run is available to restore."
        restore_run_configuration(record)
        page = workspace.title()
        st.session_state[f"{workspace}_view"] = "Configure"
        return f"Restored the latest {page} settings. Review them before starting."
    if kind == "apply_configuration":
        workspace = str(payload.get("workspace", "engage"))
        values = payload.get("values", {})
        values = dict(values) if isinstance(values, dict) else {}
        if workspace == "engage":
            if values.get("url"):
                st.session_state.start_url = str(values["url"])
            if values.get("max_chapters"):
                st.session_state.max_chapters = int(values["max_chapters"])
            if values.get("speed"):
                st.session_state.scroll_preset = str(values["speed"])
            if values.get("routing"):
                st.session_state.chapter_routing = str(values["routing"])
            st.session_state.engage_setup_step = 1
        elif workspace == "capture":
            if values.get("url"):
                st.session_state.ocr_start_url = str(values["url"])
            if values.get("max_chapters"):
                st.session_state.ocr_max_chapters = int(values["max_chapters"])
            if values.get("routing"):
                capture_routing = {
                    "infinite-scroll": "continuous",
                    "next-link": "next-chapter",
                }.get(str(values["routing"]), str(values["routing"]))
                st.session_state.ocr_reader_layout = capture_routing
            st.session_state.capture_setup_step = 1
        set_page(workspace.title())
        st.session_state[f"{workspace}_view"] = "Configure"
        return f"Applied the proposal to {workspace.title()} Configure. No workflow was started."
    if kind == "validate_configuration":
        workspace = str(payload.get("workspace", "engage"))
        values = payload.get("values", {})
        values = dict(values) if isinstance(values, dict) else {}
        current_urls = {
            "engage": str(st.session_state.start_url),
            "capture": str(st.session_state.ocr_start_url),
            "studio": str(st.session_state.note_url),
        }
        url = str(values.get("url") or current_urls.get(workspace, ""))
        issues = preflight_url(url)
        selector_fields = {
            "engage": (
                "engage_comment_editor_selector",
                "engage_submit_button_selector",
            ),
            "capture": (
                "ocr_container_selector",
                "ocr_download_image_selector",
            ),
            "studio": (
                "note_upload_input_selector",
                "note_editor_selector",
            ),
        }
        missing = [
            field
            for field in selector_fields.get(workspace, ())
            if not str(st.session_state.get(field, "")).strip()
        ]
        if issues or missing:
            details = [*issues]
            if missing:
                details.append("Missing selector fields: " + ", ".join(missing))
            return f"{workspace.title()} validation needs attention: {'; '.join(details)}"
        return f"{workspace.title()} validation passed for `{url}`. No settings were changed."
    if kind == "restore_selector_profile":
        workspace = str(payload.get("workspace", ""))
        profile_name = str(payload.get("profile", "Generic chapter reader"))
        if workspace not in {"engage", "capture"}:
            return "That workspace does not support selector-profile repair."
        profiles = load_selector_profiles()
        if profile_name not in profiles:
            return f"Selector profile `{profile_name}` is not available. No settings were changed."
        profile = profiles[profile_name]
        if workspace == "engage":
            st.session_state.selected_selector_profile = profile_name
            field_map = {
                "engage_next_link_selector": "next_link",
                "engage_next_page_selector": "next_page",
                "engage_page_counter_selector": "page_counter",
                "engage_chapter_list_selector": "chapter_list",
                "engage_comment_editor_selector": "comment_editor",
                "engage_submit_button_selector": "submit",
                "engage_thanks_button_selector": "thanks",
                "engage_confirmation_selector": "confirmation",
            }
        else:
            st.session_state.ocr_selector_profile = profile_name
            field_map = {
                "ocr_next_chapter_selector": "next_link",
                "ocr_next_page_selector": "next_page",
                "ocr_page_counter_selector": "page_counter",
                "ocr_chapter_list_selector": "chapter_list",
            }
        for field, profile_key in field_map.items():
            st.session_state[field] = str(profile.get(profile_key, ""))
        set_page(workspace.title())
        st.session_state[f"{workspace}_view"] = "Configure"
        return (
            f"Restored `{profile_name}` selectors for {workspace.title()}. "
            "Review and validate them before starting a workflow."
        )
    if kind == "inspect_browser":
        if not st.session_state.assistant_allow_browser_inspection:
            return "Browser inspection is disabled in Settings."
        snapshot = inspect_chrome_page(
            include_screenshot=bool(payload.get("include_screenshot", False)),
            screenshot_dir=APP_DIR / "outputs" / "assistant",
        )
        st.session_state.assistant_browser_snapshot = snapshot
        if snapshot.get("available"):
            return (
                f"Inspected `{snapshot.get('title', 'active tab')}`. "
                f"Found {len(snapshot.get('buttons', []))} visible buttons and "
                f"{len(snapshot.get('inputs', []))} visible inputs."
            )
        return f"Browser inspection was unavailable: {snapshot.get('error', 'no active tab')}."
    if kind == "create_support_ticket":
        ticket = create_ticket(
            OWNER_REQUESTS_FILE,
            {
                **payload,
                "page": st.session_state.page,
                "bot_status": "running" if running else "idle",
            },
        )
        st.session_state.assistant_selected_ticket = str(ticket["id"])
        return f"Created support ticket {ticket['id']}."
    return "That assistant action is not supported."


def execute_contextual_fix(action: dict[str, object]) -> str:
    kind = str(action.get("kind", ""))
    if kind not in {"apply_configuration", "restore_selector_profile", "prepare_retry"}:
        return "Panelyra blocked an unsupported Assistant fix."
    result = execute_copilot_action(action)
    payload = action.get("payload", {})
    payload = dict(payload) if isinstance(payload, dict) else {}
    workspace = str(payload.get("workspace", ""))
    if workspace not in {"engage", "capture", "studio"}:
        return result
    validation = execute_copilot_action(
        {
            "kind": "validate_configuration",
            "payload": {"workspace": workspace, "values": {}},
        }
    )
    return f"{result}\n\nValidation: {validation}"


def render_copilot_details(message: dict[str, object], key_prefix: str) -> None:
    evidence = message.get("evidence", [])
    provider = str(message.get("provider", "Local AI"))
    st.caption(provider)
    if st.session_state.assistant_show_evidence and isinstance(evidence, list) and evidence:
        visible_evidence = [item for item in evidence if isinstance(item, dict)]
        if visible_evidence:
            with st.expander(f"Sources · {len(visible_evidence)}", expanded=False):
                for item in visible_evidence:
                    label = str(item.get("label") or item.get("source") or "Local context")
                    value = str(item.get("value", "")).strip()
                    st.markdown(f"**{label}**")
                    st.caption(value or str(item.get("source", "Panelyra context")))

    action = message.get("action")
    if isinstance(action, dict) and str(action.get("kind", "")) in {
        "apply_configuration",
        "restore_selector_profile",
        "prepare_retry",
    }:
        with st.container(border=True, key=f"{key_prefix}_single_fix"):
            st.markdown(f"#### {action.get('label', 'Suggested change')}")
            st.caption("Suggested configuration change")
            st.write(str(action.get("description", "Review this local change before applying it.")))
            confirmed = st.checkbox(
                "I reviewed this change",
                key=f"{key_prefix}_confirm_{action.get('action_id', 'safe')}",
                help="Lyra never changes automation settings without your confirmation.",
            )
            if st.button(
                "Apply fix",
                key=f"{key_prefix}_apply_fix_{action.get('action_id', 'safe')}",
                type="primary",
                width="stretch",
                disabled=not confirmed,
            ):
                outcome = execute_contextual_fix(action)
                st.session_state.assistant_messages.append(
                    {
                        "id": f"assistant-fix-{uuid.uuid4().hex[:10]}",
                        "role": "assistant",
                        "content": outcome,
                        "intent": "Fix result",
                        "confidence": "high",
                        "provider": "Panelyra validation",
                        "evidence": [],
                        "action": None,
                    }
                )
                CONVERSATIONS.replace(st.session_state.lyra_conversation_token, st.session_state.assistant_messages)
                st.rerun()


def render_copilot_message(message: dict[str, object], key_prefix: str) -> None:
    role = str(message.get("role", "assistant"))
    content = str(message.get("content", ""))
    with st.chat_message("assistant" if role == "assistant" else "user"):
        st.markdown(content)
        if role == "assistant":
            render_copilot_details(message, key_prefix)
            actions = st.columns([1, 1, 6])
            if actions[0].button("Copy", key=f"{key_prefix}_copy", help="Copy answer"):
                components.html(
                    f"<script>navigator.clipboard.writeText({json.dumps(content)});</script>",
                    height=0,
                )
                st.session_state.lyra_copy_notice = "Answer copied"
            if actions[1].button("Retry", key=f"{key_prefix}_retry", help="Generate this answer again"):
                messages = list(st.session_state.assistant_messages)
                message_index = next(
                    (index for index, item in enumerate(messages) if item.get("id") == message.get("id")),
                    -1,
                )
                previous = next(
                    (item for item in reversed(messages[:message_index]) if item.get("role") == "user"),
                    None,
                )
                if previous:
                    st.session_state.lyra_retry_prompt = str(previous.get("content", ""))
                    st.session_state.assistant_messages = messages[:message_index]
                    CONVERSATIONS.replace(
                        st.session_state.lyra_conversation_token,
                        st.session_state.assistant_messages,
                    )
                    st.rerun()


def run_copilot_prompt(
    prompt: str,
    records: list[dict[str, object]],
    *,
    key_prefix: str,
) -> None:
    prior_messages = list(st.session_state.assistant_messages)
    user_message = {
        "id": f"user-{uuid.uuid4().hex[:10]}",
        "role": "user",
        "content": prompt,
    }
    st.session_state.assistant_messages.append(user_message)
    CONVERSATIONS.replace(st.session_state.lyra_conversation_token, st.session_state.assistant_messages)
    if (
        assistant_requests_browser_inspection(prompt)
        and st.session_state.assistant_allow_browser_inspection
    ):
        st.session_state.assistant_browser_snapshot = inspect_chrome_page(
            include_screenshot="screenshot" in prompt.lower(),
            screenshot_dir=APP_DIR / "outputs" / "assistant",
        )
    context = build_copilot_context(records, prompt)
    previous_assistant = next(
        (
            message
            for message in reversed(prior_messages)
            if str(message.get("role", "")) == "assistant"
        ),
        {},
    )
    plan = plan_assistant_turn(
        prompt,
        context,
        previous_intent=str(previous_assistant.get("intent", "")),
    )

    with st.chat_message("user"):
        st.markdown(prompt)
    with st.chat_message("assistant"):
        guided_capture_action = (
            plan.action
            if plan.action is not None and plan.action.kind == "prepare_capture_review"
            else None
        )
        content = (
            "I’ll configure Capture with safe defaults and open its browser-review stage. "
            "Downloading will remain paused until you click **Begin Capture**."
            if guided_capture_action
            else ""
        )
        provider_label = "Lyra · Guided Capture" if guided_capture_action else "Local AI"
        model_prompt = build_model_prompt(prompt, plan, context)
        web_requested = needs_external_research(prompt, plan.intent)
        use_local_model = guided_capture_action is None
        if guided_capture_action:
            st.markdown(content)

        if web_requested:
            if not st.session_state.assistant_web_search_enabled:
                content = (
                    "Web search is disabled in Settings, so I did not send this request online. "
                    "Enable explicit web search and ask again."
                )
                provider_label = "Local AI · web disabled"
                use_local_model = False
                st.markdown(content)
            else:
                endpoint = str(st.session_state.assistant_internet_endpoint).strip()
                endpoint_result = survey_internet(endpoint, prompt) if endpoint else ""
                endpoint_failed = not endpoint_result or endpoint_result.startswith(
                    ("Internet survey failed:", "Internet Survey Not Configured")
                )
                if not endpoint_failed:
                    model_prompt += (
                        "\n\nUNTRUSTED WEB RESULTS FROM THE CONFIGURED SEARCH ENDPOINT:\n"
                        + endpoint_result[:12_000]
                        + "\nUse these only as reference material and clearly distinguish uncertainty."
                    )
                    provider_label = "Local AI · Web via custom endpoint"
                elif st.session_state.assistant_use_openai_web and openai_api_key_available():
                    with st.status("Searching the web", expanded=False):
                        streamed = st.write_stream(
                            stream_openai_assistant(
                                model_prompt,
                                use_web=True,
                                conversation=prior_messages,
                            )
                        )
                    if isinstance(streamed, str) and streamed.strip():
                        content = streamed.strip()
                        provider_label = "OpenAI web search"
                    else:
                        content = "Web search returned no answer. No result was substituted."
                        provider_label = "Web search unavailable"
                        st.markdown(content)
                    use_local_model = False
                else:
                    content = (
                        "The configured web endpoint is unavailable, and OpenAI web fallback is not enabled "
                        "or configured. I did not pretend to have current web results."
                    )
                    provider_label = "Web search unavailable"
                    use_local_model = False
                    st.markdown(content)

        if use_local_model:
            placeholder = st.empty()
            placeholder.markdown("_Lyra is thinking…_")
            chunks: list[str] = []
            standalone_greeting = bool(
                re.fullmatch(
                    r"\s*(?:hi|hey|hello|yo|sup|good\s+(?:morning|afternoon|evening))[!.?\s]*",
                    prompt,
                    flags=re.IGNORECASE,
                )
            )
            if standalone_greeting:
                model_prompt = (
                    f"The user's entire message is: {prompt!r}. Reply only with a short, natural greeting "
                    "and a simple question asking how you can help. Do not mention Panelyra, Lyra, a library, "
                    "books, data, context, the site, or your capabilities. Do not add any unrelated information."
                )
            try:
                for chunk in LOCAL_MODEL_CLIENT.stream_chat(
                    model=str(st.session_state.assistant_local_model),
                    system_prompt=PANELYRA_SYSTEM_PROMPT,
                    conversation=[] if standalone_greeting else prior_messages,
                    prompt=model_prompt,
                ):
                    chunks.append(chunk)
                    placeholder.markdown("".join(chunks))
                if chunks and "".join(chunks).strip():
                    content = "".join(chunks).strip()
            except LocalModelError as exc:
                if chunks:
                    content = "".join(chunks).strip() + f"\n\n_Local AI stopped early: {exc}_"
                    provider_label += " · partial"
                    placeholder.markdown(content)
                else:
                    content = "Local AI became unavailable before it could answer. Retry the connection and ask again."
                    provider_label = "Local AI unavailable"
                    placeholder.markdown(content)
                    st.caption(str(exc))

        message = copilot_message_from_plan(plan, content, provider_label)
        message["status"] = "partial" if "partial" in provider_label.lower() else "complete"
        st.session_state.assistant_messages.append(message)
        CONVERSATIONS.replace(st.session_state.lyra_conversation_token, st.session_state.assistant_messages)
        if st.session_state.page != "Assistant":
            CONVERSATIONS.mark_unread(st.session_state.lyra_conversation_token)
            st.session_state.lyra_unread = True
        render_copilot_details(message, f"{key_prefix}_{message['id']}")
        if guided_capture_action is not None:
            outcome = execute_copilot_action(
                {"kind": guided_capture_action.kind, "payload": dict(guided_capture_action.payload)}
            )
            outcome_message = {
                "id": f"assistant-capture-{uuid.uuid4().hex[:10]}",
                "role": "assistant",
                "content": outcome,
                "intent": "Configure",
                "confidence": "high",
                "provider": "Panelyra validation",
                "evidence": [],
                "action": None,
                "status": "waiting" if st.session_state.lyra_capture_prepared else "needs-attention",
            }
            st.session_state.assistant_messages.append(outcome_message)
            CONVERSATIONS.replace(
                st.session_state.lyra_conversation_token,
                st.session_state.assistant_messages,
            )
    if guided_capture_action is not None:
        st.rerun()


def render_copilot_chat(
    records: list[dict[str, object]],
    *,
    key_prefix: str,
    history_limit: int,
) -> None:
    notice = str(st.session_state.assistant_action_notice or "")
    if notice:
        st.success(notice)
        st.session_state.assistant_action_notice = ""

    if st.session_state.lyra_copy_notice:
        st.toast(st.session_state.lyra_copy_notice)
        st.session_state.lyra_copy_notice = ""
    model_status = LOCAL_MODEL_CLIENT.status()
    ready = (
        bool(st.session_state.assistant_use_model)
        and model_status.available
        and str(st.session_state.assistant_local_model) in model_status.models
    )
    if not ready:
        st.warning(
            (f"The selected model {st.session_state.assistant_local_model} is not installed. Choose an installed model in Settings → AI."
             if model_status.available else model_status.message)
            if st.session_state.assistant_use_model
            else "Local AI answers are disabled in Settings."
        )
        if st.button("Open AI settings", key=f"{key_prefix}_open_ai_settings"):
            set_page("Settings")
            st.session_state.settings_view = "AI"
            st.rerun()
        if st.button("Retry connection", key=f"{key_prefix}_retry_connection", width="stretch"):
            st.rerun()
        for index, message in enumerate(st.session_state.assistant_messages[-history_limit:]):
            render_copilot_message(message, f"{key_prefix}_offline_{message.get('id', index)}")
        return

    if not st.session_state.assistant_messages:
        st.markdown(
            '<div class="lyra-welcome" data-testid="lyra-welcome">'
            '<div class="lyra-orb"><span class="lyra-glyph" aria-hidden="true"></span></div><h2>How can I help?</h2>'
            '<p>I can explain this page, diagnose a stopped run, or help you choose safe settings.</p></div>',
            unsafe_allow_html=True,
        )
        context_prompts = {
            "Dashboard": ("What can I create with Panelyra?", "Which workspace should I start with?"),
            "Capture": ("What will Capture save?", "Help me choose the Capture options."),
            "Engage": ("How does comment testing work?", "Why did the latest Engage run stop?"),
            "Reader": ("How do I find and read a series?", "Why is a chapter missing?"),
        }
        starters = context_prompts.get(
            str(st.session_state.page),
            ("What does this page do?", "Why did the latest run stop?"),
        ) + ("Help me fix this error.",)
        columns = st.columns(len(starters))
        for index, starter in enumerate(starters):
            if columns[index].button(
                starter,
                key=f"{key_prefix}_starter_{index}",
                width="stretch",
            ):
                run_copilot_prompt(starter, records, key_prefix=key_prefix)
    for index, message in enumerate(st.session_state.assistant_messages[-history_limit:]):
        render_copilot_message(message, f"{key_prefix}_{message.get('id', index)}")

    retry_prompt = str(st.session_state.lyra_retry_prompt).strip()
    if retry_prompt:
        st.session_state.lyra_retry_prompt = ""
        run_copilot_prompt(retry_prompt, records, key_prefix=key_prefix)
    response_slot = st.container(key=f"{key_prefix}_response_slot")
    prompt = st.chat_input(
        f"Ask Lyra about {str(st.session_state.page).lower()}...",
        key=f"{key_prefix}_chat_input",
    )
    if prompt and prompt.strip():
        with response_slot:
            run_copilot_prompt(prompt.strip(), records, key_prefix=key_prefix)


def select_lyra_thread(token: str) -> None:
    selected = CONVERSATIONS.select_thread(token)
    if selected is None:
        return
    st.session_state.lyra_conversation_token = token
    st.session_state.assistant_messages = list(selected.get("messages", []))
    st.session_state.lyra_synced_messages = list(st.session_state.assistant_messages)
    st.session_state.lyra_unread = False


def create_lyra_thread() -> None:
    token = CONVERSATIONS.create_thread(
        context={"page": str(st.session_state.page)},
        model=str(st.session_state.assistant_local_model),
    )
    select_lyra_thread(token)
    st.session_state.assistant_run_context = {}
    st.session_state.assistant_browser_snapshot = {}


def render_lyra_thread_rail() -> None:
    st.markdown('<div data-testid="lyra-thread-list"></div>', unsafe_allow_html=True)
    heading, collapse = st.columns([5, 1])
    heading.markdown("#### Conversations")
    if collapse.button("‹", key="lyra_collapse_threads", help="Hide conversation history"):
        st.session_state.lyra_thread_rail_open = False
        st.rerun()
    if st.button("＋ New conversation", key="lyra_new_thread", type="primary", width="stretch"):
        create_lyra_thread()
        st.rerun()
    threads = CONVERSATIONS.list_threads()
    current = str(st.session_state.lyra_conversation_token)
    for index, thread in enumerate(threads):
        token = str(thread.get("token", ""))
        selected = token == current
        unread = " •" if thread.get("unread") else ""
        label = f"{'● ' if selected else ''}{thread.get('title', 'New conversation')}{unread}"
        if st.button(
            label,
            key=f"lyra_thread_{index}_{token[:8]}",
            width="stretch",
            disabled=selected,
            help=f"{int(thread.get('message_count', 0))} messages",
        ):
            select_lyra_thread(token)
            st.rerun()
    selected_thread = next((thread for thread in threads if thread.get("token") == current), None)
    if selected_thread:
        with st.expander("Manage this conversation", expanded=False):
            title = st.text_input(
                "Conversation name",
                value=str(selected_thread.get("title", "New conversation")),
                key=f"lyra_rename_{current[:8]}",
            )
            manage = st.columns(2)
            if manage[0].button("Rename", key="lyra_rename_thread", width="stretch"):
                CONVERSATIONS.rename_thread(current, title)
                st.rerun()
            if manage[1].button("Delete", key="lyra_delete_thread", width="stretch"):
                CONVERSATIONS.delete_thread(current)
                create_lyra_thread()
                st.rerun()




def render_next_step(status_label: str) -> None:
    if st.session_state.start_signal_path:
        message = "Next step: log in if needed, choose your exact chapter in Chrome, then click Start Comments."
        css_class = "status-waiting"
    elif is_process_running(st.session_state.bot_process):
        message = "Run active: open this bot's Live Room for progress, diagnostics, and status."
        css_class = "status-ok"
    elif status_label.startswith("Finished"):
        message = "Run finished: open the matching Intelligence or Library page to review results."
        css_class = "status-ok"
    else:
        message = "Ready: open a bot's Setup page and configure only that workflow."
        css_class = "status-ok"

    st.markdown(f"<div class='{css_class}'>{message}</div>", unsafe_allow_html=True)


def render_button_guide() -> None:
    st.markdown(
        """
        | Button | What it does | When to use it |
        |---|---|---|
        | `Open Browser / Pick Chapter` | Opens Chrome at the starting URL and pauses the bot. | Use this when you need to log in or manually click the chapter first. |
        | `Start Comments` | Releases the paused bot. | Click this only after Chrome is on the exact chapter you want. |
        | `Run Immediately` | Starts the workflow without pausing. | Use this only when the pasted URL is already the exact target page. |
        | `Apply preset` | Loads a saved Engage speed, routing, chapter-count, and marker configuration. | Use it before reviewing the target and preflight summary. |
        | `Run Capture` | Starts image, CBZ, and OCR collection. | Use it after the Capture preflight passes. |
        | `Open Captures` | Opens the active Capture output folder in Finder. | Use it to inspect downloaded chapter pages. |
        | `Open Note / Prepare` | Opens the editor and pauses Studio for login or manual setup. | Use it when the editor is not already ready. |
        | `Start Word Art` | Continues a prepared Studio run. | Use it after the target editor is visible and ready. |
        | `Pause` / `Resume` | Suspends or continues the active local process. | Use it when you need to inspect the browser without losing the run. |
        | `Stop Current Run` | Stops the running bot process. | Use this if the wrong page opens or selectors fail. |
        | `Retry from saved configuration` | Restores a historical run's available settings and opens its Setup page. | Use it after reviewing a failure or stop reason. |
        | `Apply retention cleanup` | Deletes only old files in a selected generated area after typed confirmation. | Use it from Storage; captures and creative assets stay protected. |
        """,
    )


SCROLL_PRESETS = {
    "Reliable": {
        "min_px": 650,
        "max_px": 1200,
        "min_delay": 0.12,
        "max_delay": 0.35,
        "type_min_delay": 0.015,
        "type_max_delay": 0.04,
        "visible_type_chars": 0,
        "reuse_loaded_images": False,
        "cooldown_min_seconds": 3.0,
        "cooldown_max_seconds": 5.0,
        "post_submit_wait_seconds": 5.0,
        "description": "Prioritizes stable detection and clearly visible browser movement.",
    },
    "Balanced": {
        "min_px": 1400,
        "max_px": 2600,
        "min_delay": 0.05,
        "max_delay": 0.12,
        "type_min_delay": 0.003,
        "type_max_delay": 0.012,
        "visible_type_chars": 0,
        "reuse_loaded_images": False,
        "cooldown_min_seconds": 1.0,
        "cooldown_max_seconds": 2.0,
        "post_submit_wait_seconds": 3.0,
        "description": "A practical default with quick routing and measured verification.",
    },
    "Performance": {
        "min_px": 5000,
        "max_px": 9000,
        "min_delay": 0.01,
        "max_delay": 0.03,
        "type_min_delay": 0.055,
        "type_max_delay": 0.12,
        "visible_type_chars": 320,
        "reuse_loaded_images": False,
        "cooldown_min_seconds": 0.0,
        "cooldown_max_seconds": 0.75,
        "post_submit_wait_seconds": 2.0,
        "description": "Accelerated scrolling with natural visible typing and short waits.",
    },
    "Maximum": {
        "min_px": 12000,
        "max_px": 22000,
        "min_delay": 0.0,
        "max_delay": 0.0,
        "type_min_delay": 0.035,
        "type_max_delay": 0.08,
        "visible_type_chars": 320,
        "reuse_loaded_images": True,
        "cooldown_min_seconds": 0.0,
        "cooldown_max_seconds": 0.0,
        "post_submit_wait_seconds": 1.5,
        "description": "Maximum routing speed and immediate submission with a short verification window.",
    },
    "Custom": {
        "min_px": 650,
        "max_px": 1200,
        "min_delay": 0.12,
        "max_delay": 0.35,
        "type_min_delay": 0.015,
        "type_max_delay": 0.04,
        "visible_type_chars": 0,
        "reuse_loaded_images": False,
        "cooldown_min_seconds": 2.0,
        "cooldown_max_seconds": 4.0,
        "post_submit_wait_seconds": 4.0,
        "description": "Uses the exact values configured in the advanced performance controls.",
    },
}
LEGACY_SPEED_NAMES = {
    "Fast": "Balanced",
    "Ultra fast": "Performance",
    "Super fast": "Maximum",
    "Manual": "Custom",
    "Normal": "Reliable",
}


def normalized_speed_name(value: object) -> str:
    name = str(value)
    if name in SCROLL_PRESETS:
        return name
    return LEGACY_SPEED_NAMES.get(name, "Balanced")

CONTENT_MODE_OPTIONS = {
    "Auto": "auto",
    "Novel text": "novel",
    "Manga / Manhwa / Manhua": "manhwa",
}


def reader_defaults_for_mode(mode_label: str) -> tuple[str, str, float]:
    if mode_label == "Manga / Manhwa / Manhua":
        return MANHWA_CONTAINER_SELECTOR, MANHWA_IMAGE_SELECTOR, OCR_CONFIDENCE
    if mode_label == "Novel text":
        return CONTENT_CONTAINER_SELECTOR, MANHWA_IMAGE_SELECTOR, OCR_CONFIDENCE
    return CONTENT_CONTAINER_SELECTOR, MANHWA_IMAGE_SELECTOR, OCR_CONFIDENCE


def apply_reader_defaults(mode_label: str | None = None, *, force: bool = False) -> None:
    selected_mode = mode_label or str(st.session_state.get("content_mode", "Auto"))
    container_selector, image_selector, confidence = reader_defaults_for_mode(selected_mode)
    current_container_selector = str(st.session_state.get("content_container_selector", "")).strip()

    if (
        force
        or not current_container_selector
        or (
            selected_mode == "Manga / Manhwa / Manhua"
            and current_container_selector == LEGACY_MANHWA_CONTAINER_SELECTOR
        )
    ):
        st.session_state.content_container_selector = container_selector
    if force or not str(st.session_state.get("ocr_image_selector", "")).strip():
        st.session_state.ocr_image_selector = image_selector
    if force or "ocr_confidence" not in st.session_state:
        st.session_state.ocr_confidence = confidence


def apply_selected_reader_mode_defaults() -> None:
    apply_reader_defaults(str(st.session_state.get("content_mode", "Auto")), force=True)


def apply_note_art_selector_defaults(force: bool = False) -> None:
    for key, value in NOTE_ART_SELECTOR_DEFAULTS.items():
        if force or not str(st.session_state.get(key, "")).strip():
            st.session_state[key] = value


def apply_ocr_download_defaults(force: bool = False) -> None:
    output_dir = str(st.session_state.get("ocr_output_dir", "")).strip()
    if force or not output_dir or output_dir == str(OLD_OCR_DOWNLOAD_DIR):
        st.session_state.ocr_output_dir = str(OCR_DOWNLOAD_DIR)
    if force or not str(st.session_state.get("ocr_container_selector", "")).strip():
        st.session_state.ocr_container_selector = MANHWA_CONTAINER_SELECTOR
    if force or not str(st.session_state.get("ocr_download_image_selector", "")).strip():
        st.session_state.ocr_download_image_selector = MANHWA_IMAGE_SELECTOR
    if force or "ocr_download_confidence" not in st.session_state:
        st.session_state.ocr_download_confidence = OCR_CONFIDENCE
    if force or "ocr_scroll_min_delay" not in st.session_state:
        st.session_state.ocr_scroll_min_delay = 0.08
    if force or "ocr_scroll_max_delay" not in st.session_state:
        st.session_state.ocr_scroll_max_delay = 0.18
    if force or "ocr_image_fetch_timeout" not in st.session_state:
        st.session_state.ocr_image_fetch_timeout = 20.0
    if force:
        st.session_state.ocr_reader_layout = "auto"
        profiles = load_selector_profiles()
        profile_name = (
            str(st.session_state.get("ocr_selector_profile", "Generic chapter reader"))
            if str(st.session_state.get("ocr_selector_profile", "Generic chapter reader")) in profiles
            else next(iter(profiles))
        )
        st.session_state.ocr_selector_profile = profile_name
        values = profiles[profile_name]
        st.session_state.ocr_next_chapter_selector = values.get("next_link", "")
        st.session_state.ocr_next_page_selector = values.get("next_page", "")
        st.session_state.ocr_page_counter_selector = values.get("page_counter", "")
        st.session_state.ocr_chapter_list_selector = values.get("chapter_list", "")


PAGES = [page for pages in NAVIGATION_GROUPS.values() for page in pages]
WORKSPACE_VIEW_ROUTES = {
    "Engage": {"Configure": "Engage Setup", "Live": "Engage Live", "Results": "Engage Insights"},
    "Capture": {"Configure": "Capture Setup", "Live": "Capture Live"},
    "Settings": {"General": "Settings", "Connections": "Settings", "Storage": "Storage", "Help": "Settings"},
}
LEGACY_PAGE_ROUTES = {
    "Reader": ("Library", ""), "Dashboard": ("Library", ""), "Overview": ("Library", ""),
    "Capture Library": ("Library", ""), "Run History": ("Activity", ""), "Assistant": ("Library", ""), "Lyra": ("Library", ""),
    "Help": ("Settings", "Help"), "Storage": ("Settings", "Storage"), "Cleaner": ("Library", ""),
    **{route: (workspace, view) for workspace, views in WORKSPACE_VIEW_ROUTES.items()
       for view, route in views.items() if route != "Settings"},
}


def request_scroll_to_top() -> None:
    st.session_state["_scroll_top_request"] = uuid.uuid4().hex


def set_page(page: str) -> None:
    previous_page = str(st.session_state.get("page", ""))
    if page in {"Lyra", "Assistant"}:
        st.session_state.route_notice = "AI has been removed from Panelyra. Saved conversations are preserved on disk."
        page = "Library"
    if page.startswith("Studio"):
        st.session_state.route_notice = "Studio has been retired. Existing files and historical runs are preserved."
        page = "Library"
    st.session_state.library_trim = page == "Cleaner"
    if page in LEGACY_PAGE_ROUTES:
        workspace, view = LEGACY_PAGE_ROUTES[page]
        st.session_state.page = workspace
        if view:
            st.session_state[f"{workspace.lower()}_view"] = view
        if previous_page != workspace:
            request_scroll_to_top()
        return
    destination = page if page in PAGES else "Library"
    st.session_state.page = destination
    if previous_page != destination:
        request_scroll_to_top()


def render_scroll_to_top_trigger() -> None:
    request_id = str(st.session_state.get("_scroll_top_request", ""))
    if not request_id or request_id == st.session_state.get("_scroll_top_rendered"):
        return
    st.session_state._scroll_top_rendered = request_id
    components.html(
        """
        <script>
        (() => {
          const resetScroll = () => {
            try {
              const parentWindow = window.parent;
              const documentRoot = parentWindow.document;
              parentWindow.scrollTo(0, 0);
              [
                documentRoot.scrollingElement,
                documentRoot.documentElement,
                documentRoot.body,
                documentRoot.querySelector('[data-testid="stAppViewContainer"]'),
                documentRoot.querySelector('[data-testid="stMain"]'),
                documentRoot.querySelector('section[data-testid="stMain"]'),
              ].filter(Boolean).forEach((element) => {
                element.scrollTop = 0;
                element.scrollLeft = 0;
              });
            } catch (_) {
              // The dashboard still renders normally if an embedded host blocks parent access.
            }
          };
          resetScroll();
          window.requestAnimationFrame(resetScroll);
          window.setTimeout(resetScroll, 120);
        })();
        </script>
        """,
        height=0,
        width=0,
    )


def render_workspace_view(page: str) -> str:
    views = WORKSPACE_VIEW_ROUTES.get(page)
    if not views:
        return page
    state_key = f"{page.lower()}_view"
    selected = str(st.session_state.get(state_key, next(iter(views))))
    if selected not in views:
        selected = next(iter(views))
        st.session_state[state_key] = selected
    with st.container(key="workspace_views"):
        chosen = st.radio(
            "View", list(views), key=state_key,
            horizontal=True, label_visibility="collapsed",
            index=0,  # Keep widget identity stable when the selected view changes.
            format_func=lambda value: "Setup" if value == "Configure" else value,
            on_change=request_scroll_to_top,
        )
        selected = chosen or next(iter(views))
    return views.get(selected, next(iter(views.values())))


def initialize_session_state() -> None:
    defaults = {
        "bot_process": None,
        "log_path": None,
        "start_signal_path": None,
        "bot_runtimes": {
            "comment": {"process": None, "log_path": None, "signal_path": None, "paused": False},
            "word_art": {"process": None, "log_path": None, "signal_path": None, "paused": False},
            "download": {"process": None, "log_path": None, "signal_path": None, "paused": False},
        },
        "run_record_paths": {},
        "start_url": "",
        "max_chapters": 5,
        "engage_setup_step": 1,
        "research_file": str(RESEARCH_FILE),
        "research_endpoint": "",
        "review_first_comment": False,
        "comment_preview_subject": "",
        "comment_preview_text": generate_comment_preview(),
        "scroll_preset": "Maximum",
        "custom_scroll_min_px": 650,
        "custom_scroll_max_px": 1200,
        "custom_scroll_min_delay": 0.12,
        "custom_scroll_max_delay": 0.35,
        "custom_type_min_delay": 0.015,
        "custom_type_max_delay": 0.04,
        "custom_cooldown_min": 2.0,
        "custom_cooldown_max": 4.0,
        "custom_submit_wait": 4.0,
        "chapter_routing": "auto",
        "use_thanks_marker": True,
        "note_url": "",
        "studio_setup_step": 1,
        "studio_image_path": "",
        "studio_preview_path": "",
        "word_art_text": "Chapter mood board",
        "word_art_caption": "",
        "word_art_style_mode": "banner",
        "word_art_text_color": "#F4F4F5",
        "word_art_panel_color": "#101114",
        "word_art_text_align": "center",
        "word_art_panel_placement": "bottom",
        **NOTE_ART_SELECTOR_DEFAULTS,
        "note_skip_submit": True,
        "content_mode": "Auto",
        "content_container_selector": CONTENT_CONTAINER_SELECTOR,
        "ocr_image_selector": MANHWA_IMAGE_SELECTOR,
        "ocr_confidence": OCR_CONFIDENCE,
        "ocr_start_url": "",
        "capture_setup_step": 1,
        "ocr_output_dir": str(OCR_DOWNLOAD_DIR),
        "ocr_custom_output_dir": "",
        "ocr_save_images": True,
        "ocr_create_archive": True,
        "ocr_save_text": False,
        "ocr_performance_profile": "Balanced",
        "ocr_max_chapters": 1,
        "ocr_reader_layout": "auto",
        "ocr_selector_profile": "Generic chapter reader",
        "ocr_container_selector": MANHWA_CONTAINER_SELECTOR,
        "ocr_download_image_selector": MANHWA_IMAGE_SELECTOR,
        "ocr_download_confidence": OCR_CONFIDENCE,
        "ocr_scroll_min_delay": 0.08,
        "ocr_scroll_max_delay": 0.18,
        "ocr_image_fetch_timeout": 20.0,
        "reader_series": "",
        "reader_edition": "Original",
        "reader_chapter": "",
        "reader_clean_review": {},
        "reader_clean_batch": [],
        "assistant_open": False,
        "assistant_messages": [],
        "lyra_conversation_token": "",
        "lyra_synced_messages": [],
        "assistant_prompt": "",
        "assistant_browser_snapshot": {},
        "assistant_action_notice": "",
        "assistant_selected_ticket": "",
        "assistant_run_context": {},
        "lyra_thread_rail_open": True,
        "lyra_unread": False,
        "lyra_copy_notice": "",
        "lyra_retry_prompt": "",
        "lyra_capture_prepared": False,
        "lyra_capture_review_status": "",
        "lyra_capture_browser_snapshot": {},
        "lyra_capture_expected_url": "",
        "progress_samples": {},
        "progress_previous": {},
        "progress_announcements": {},
        "engage_view": "Configure",
        "capture_view": "Configure",
        "studio_view": "Configure",
        "settings_view": "General",
        **DEFAULT_SETTINGS,
    }
    stored_drafts = PREFERENCES.get("workspace_drafts", {})
    if isinstance(stored_drafts, dict):
        for key in WORKSPACE_DRAFT_KEYS:
            if key in stored_drafts:
                defaults[key] = stored_drafts[key]
    defaults["scroll_preset"] = normalized_speed_name(defaults.get("scroll_preset"))
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value
    if str(st.session_state.progress_animation) not in {"Full", "Subtle", "Off"}:
        st.session_state.progress_animation = "Subtle"
    apply_ocr_download_defaults()
    landing = str(st.session_state.default_landing_page)
    st.session_state.default_landing_page = LEGACY_PAGE_ROUTES.get(landing, (landing, ""))[0]
    if st.session_state.default_landing_page not in PAGES:
        st.session_state.default_landing_page = "Library"
    if "page" not in st.session_state:
        st.session_state.page = st.session_state.default_landing_page
    elif st.session_state.page not in PAGES:
        set_page(str(st.session_state.page))


def restore_active_runtime_references() -> None:
    """Reconnect a new Streamlit session to bots launched by another session."""
    for bot_type, record in active_run_records().items():
        runtime = st.session_state.bot_runtimes.get(bot_type)
        if runtime is None or is_process_running(runtime.get("process")):
            continue
        pid = int(record.get("pid", 0) or 0)
        process = TrackedProcessRef(pid)
        runtime["process"] = process
        runtime["log_path"] = Path(str(record.get("log_path", "")))
        runtime["signal_path"] = None
        runtime["paused"] = False
        run_id = str(record.get("run_id", "")).strip()
        if run_id:
            st.session_state.run_record_paths[bot_type] = str(RUNS_DIR / f"{run_id}.json")
        if bot_type == "comment":
            st.session_state.bot_process = process
            st.session_state.log_path = runtime["log_path"]
            st.session_state.start_signal_path = None


st.set_page_config(
    page_title=PRODUCT_NAME,
    page_icon="P",
    layout="wide",
    initial_sidebar_state="expanded",
)
initialize_session_state()
try:
    requested_page = st.query_params.get("page")
    if requested_page:
        set_page(str(requested_page))
    requested_series = st.query_params.get("series")
    requested_chapter = st.query_params.get("chapter")
    requested_edition = st.query_params.get("edition")
    if requested_series:
        st.session_state.reader_series = str(requested_series)
    if requested_chapter:
        st.session_state.reader_chapter = str(requested_chapter)
    if requested_edition in {"Original", "Clean"}:
        st.session_state.reader_edition = str(requested_edition)
    if requested_page:
        st.session_state.pop("reader_book_choice", None)
        for query_key in ("page", "series", "chapter", "edition"):
            if query_key in st.query_params:
                del st.query_params[query_key]
except (AttributeError, TypeError):
    pass
# AI has been removed from the product. Existing conversation files stay untouched.
st.session_state.assistant_use_model = False
st.session_state.show_lyra_in_immersive_reader = False
st.session_state.lyra_conversation_token = ""
restore_active_runtime_references()
preserve_wizard_state()
inject_styles()
from panelyra.screens.theme import inject_theme
inject_theme()
st.html(PROGRESS_CSS)
render_scroll_to_top_trigger()

running_bots = {
    name: runtime
    for name, runtime in st.session_state.bot_runtimes.items()
    if is_process_running(runtime.get("process"))
}
browser_owner = browser_lock_owner()
running = bool(running_bots) or bool(browser_owner)


def runtime_status(bot_type: str) -> str:
    process = st.session_state.bot_runtimes.get(bot_type, {}).get("process")
    if is_process_running(process):
        return "Running"
    if process is not None and process.poll() is not None:
        return f"Finished ({process.poll()})"
    return "Idle"


def load_runtime_record(bot_type: str) -> dict[str, object]:
    runtime = st.session_state.bot_runtimes.get(bot_type, {})
    process = runtime.get("process")
    process_pid = int(getattr(process, "pid", 0) or 0)
    path_value = st.session_state.run_record_paths.get(bot_type)
    payload: dict[str, object] = {}
    if path_value:
        try:
            candidate = json.loads(Path(path_value).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            candidate = {}
        if isinstance(candidate, dict) and str(candidate.get("bot_type", "")) == bot_type:
            payload = candidate

    records = list_run_records(limit=100)
    if process_pid:
        pid_match = next(
            (
                record
                for record in records
                if str(record.get("bot_type", "")) == bot_type
                and int(record.get("pid", 0) or 0) == process_pid
            ),
            None,
        )
        if isinstance(pid_match, dict):
            return pid_match
    if payload:
        return payload
    return next(
        (record for record in records if str(record.get("bot_type", "")) == bot_type),
        {},
    )


def format_elapsed(seconds: int) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, remaining_seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {remaining_seconds}s"
    return f"{remaining_seconds}s"


def estimate_remaining(record: dict[str, object]) -> str:
    current = int(record.get("progress_current", 0) or 0)
    total = int(record.get("progress_total", 0) or 0)
    started_at = int(record.get("started_at", 0) or 0)
    if current <= 0 or total <= current or started_at <= 0:
        return "Calculating"
    elapsed = max(1, int(time.time()) - started_at)
    return format_elapsed(round((elapsed / current) * (total - current)))


PROGRESS_WORKSPACES = {
    "comment": ("engage", "Engage"),
    "download": ("capture", "Capture"),
    "word_art": ("studio", "Studio"),
}


def safe_progress_int(value: object, default: int = 0) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return max(0, int(default))


def parse_local_timestamp(value: object) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").timestamp()
    except ValueError:
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None


def run_history_records(started_at: int) -> list[dict[str, object]]:
    records = []
    for record in read_history():
        timestamp = parse_local_timestamp(record.get("timestamp"))
        if timestamp is not None and timestamp >= max(0, started_at - 1):
            records.append(record)
    return records


def timestamped_progress_events(log_text: str, workspace: str) -> list[float]:
    if workspace == "capture":
        marker = re.compile(r"OCR downloading chapter\s+\d+\s*/\s*\d+", re.IGNORECASE)
    elif workspace == "engage":
        marker = re.compile(
            r"Chapter submission (?:completed|unverified):\s*\d+\s*/\s*\d+",
            re.IGNORECASE,
        )
    else:
        return []
    values: list[float] = []
    for line in log_text.splitlines():
        if not marker.search(line):
            continue
        match = re.match(r"^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s+", line)
        if match:
            timestamp = parse_local_timestamp(match.group(1))
            if timestamp is not None:
                values.append(timestamp)
    return values


def sample_progress_history(run_id: str, current: int, total: int) -> tuple[float, ...]:
    if not run_id or total <= 0:
        return tuple()
    samples_by_run = st.session_state.setdefault("progress_samples", {})
    samples = list(samples_by_run.get(run_id, []))
    now = time.time()
    fraction = min(1.0, max(0.0, current / max(1, total)))
    if not samples or samples[-1][1] != current or now - float(samples[-1][0]) >= 5.0:
        samples.append((now, current, fraction))
        samples = samples[-36:]
        samples_by_run[run_id] = samples
    return tuple(float(sample[2]) for sample in samples)


def estimate_progress_eta(
    *,
    state: str,
    workspace: str,
    current: int,
    total: int,
    started_at: int,
    log_text: str,
    history_records: list[dict[str, object]],
    run_id: str,
) -> tuple[str, str]:
    if state == "completed":
        return "Complete", "Measured from this run"
    if state in {"failed", "stopped"}:
        return "—", "Run is no longer active"
    if workspace == "studio":
        return "Short run", "Stage-based workflow"
    if total <= 0 or current <= 0:
        return "Calculating", "Waiting for a completed step"
    remaining = max(0, total - current)
    if remaining == 0:
        return "Finishing", "Verifying the final step"

    event_times: list[float] = []
    if workspace == "engage":
        event_times = [
            timestamp
            for item in history_records
            if str(item.get("status", "")).lower() in {"submitted", "unverified"}
            for timestamp in [parse_local_timestamp(item.get("timestamp"))]
            if timestamp is not None
        ]
    if len(event_times) < 2:
        event_times = timestamped_progress_events(log_text, workspace)
    if len(event_times) < 2:
        samples = list(st.session_state.get("progress_samples", {}).get(run_id, []))
        event_times = [float(sample[0]) for sample in samples]
    intervals = [
        later - earlier
        for earlier, later in zip(event_times, event_times[1:])
        if later > earlier
    ]
    if not intervals:
        return "Stabilizing", "Learning this run's pace"
    recent_intervals = intervals[-6:]
    remaining_seconds = max(1, round(median(recent_intervals) * remaining))
    confidence = "Stable pace" if len(recent_intervals) >= 3 else "Early estimate"
    return f"About {format_elapsed(remaining_seconds)}", confidence


def format_freshness(age_seconds: float | None) -> str:
    if age_seconds is None:
        return "when the run starts"
    age = max(0, int(age_seconds))
    if age < 5:
        return "just now"
    if age < 60:
        return f"{age}s ago"
    if age < 3600:
        return f"{age // 60}m ago"
    return f"{age // 3600}h ago"


def capture_output_ready(output_path: Path, started_at: int) -> bool:
    if not output_path.is_dir():
        return False
    try:
        metadata_paths = output_path.rglob("metadata.json")
        for metadata_path in metadata_paths:
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            saved_at = safe_progress_int(metadata.get("saved_at_epoch")) if isinstance(metadata, dict) else 0
            if saved_at and saved_at >= max(0, started_at - 1):
                return True
    except OSError:
        return False
    return False


def resolved_progress_output(
    bot_type: str,
    record: dict[str, object],
    started_at: int,
) -> tuple[Path | None, bool]:
    settings = record.get("settings", {})
    if not isinstance(settings, dict):
        settings = {}
    if bot_type == "download":
        path_value = str(
            settings.get("output_dir")
            or st.session_state.get("ocr_custom_output_dir")
            or OCR_DOWNLOAD_DIR
        ).strip()
        output_path = Path(path_value).expanduser()
        if not output_path.is_absolute():
            output_path = APP_DIR / output_path
        return output_path, capture_output_ready(output_path, started_at)
    if bot_type == "word_art":
        image_value = str(settings.get("image_path") or st.session_state.get("studio_preview_path", "")).strip()
        candidates = []
        if image_value:
            image_path = Path(image_value).expanduser()
            candidates.append(image_path if image_path.is_absolute() else APP_DIR / image_path)
        try:
            candidates.extend(
                sorted(UPLOAD_DIR.glob("*-word-art.png"), key=lambda item: item.stat().st_mtime, reverse=True)
            )
        except OSError:
            pass
        for candidate in candidates:
            try:
                if candidate.is_file() and candidate.stat().st_mtime >= max(0, started_at - 1):
                    return candidate, True
            except OSError:
                continue
        return (candidates[0], False) if candidates else (None, False)
    return (HISTORY_FILE, HISTORY_FILE.is_file())


def progress_stage_label(workspace: str, stage: str, message: str, state: str) -> str:
    if state == "completed":
        return "Ready for review" if workspace == "studio" else "Run complete"
    if state == "failed":
        return "Run needs attention"
    if state == "stopped":
        return "Stopped early"
    if state == "paused":
        return "Paused at the current step"
    if state == "waiting":
        return "Your browser is ready for you"
    normalized = stage.strip().lower()
    workspace_labels = {
        "engage": {
            "created": "Preparing the comment run",
            "process-started": "Starting the local runner",
            "opening-browser": "Opening the browser",
            "scanning": "Reading the current chapter",
            "generated": "Preparing a contextual comment",
            "verified": "Submission verified",
            "unverified": "Checking the submitted comment",
            "recovering": "Recovering the browser session",
        },
        "capture": {
            "created": "Preparing chapter capture",
            "process-started": "Starting the local runner",
            "opening-browser": "Opening the reader",
            "waiting-for-user": "Confirm the reader in the browser",
            "scanning": "Scanning reader pages",
            "saving-images": "Saving original reader images",
            "ocr": "Reading page text locally",
        },
        "studio": {
            "created": "Preparing the composition",
            "process-started": "Starting the local runner",
            "opening-browser": "Opening the editor",
            "uploading": "Uploading the rendered composition",
            "editing": "Filling the editor",
        },
    }
    fallback = workspace_labels.get(workspace, {}).get(normalized, normalized.replace("-", " ").title())
    cleaned_message = redact_private_paths(message).strip()
    return cleaned_message or fallback or "Preparing your run"


def build_run_progress_model(
    bot_type: str,
    *,
    surface: str = "live",
) -> tuple[RunProgressModel, dict[str, object]]:
    workspace, workspace_label = PROGRESS_WORKSPACES[bot_type]
    runtime = st.session_state.bot_runtimes.get(bot_type, {})
    process = runtime.get("process")
    process_running = is_process_running(process)
    return_code = process.poll() if process is not None else None
    record = load_runtime_record(bot_type)
    run_id = str(record.get("run_id", "") or f"{bot_type}-pending")
    record_status = str(record.get("status", "") or ("running" if process_running else "starting"))
    record_stage = str(record.get("stage", "") or "created")

    log_value = record.get("log_path") or runtime.get("log_path")
    log_path = Path(str(log_value)).expanduser() if log_value else None
    log_text = (
        read_log(log_path, int(st.session_state.console_max_chars))
        if log_path is not None
        else ""
    )
    lowered_log = log_text.lower()
    wait_markers = {
        "engage": "waiting for dashboard start signal",
        "capture": "browser review is active",
        "studio": "set up the note page manually",
    }
    continue_markers = {
        "engage": "dashboard start signal received",
        "capture": "capture start signal received",
        "studio": "start signal received",
    }
    unmatched_wait = lowered_log.rfind(wait_markers[workspace]) > lowered_log.rfind(continue_markers[workspace])
    waiting = bool(runtime.get("signal_path")) or record_stage == "waiting-for-user" or unmatched_wait
    state = resolve_progress_state(
        paused=bool(runtime.get("paused")) and process_running,
        waiting=waiting and process_running,
        running=process_running,
        status=record_status,
        stage=record_stage,
        return_code=return_code,
    )

    started_at = safe_progress_int(record.get("started_at"))
    updated_candidates = [safe_progress_int(record.get("updated_at"))]
    if log_path is not None:
        try:
            updated_candidates.append(int(log_path.stat().st_mtime))
        except OSError:
            pass
    last_updated_at = max(updated_candidates or [0])
    last_update_age = time.time() - last_updated_at if last_updated_at else None
    reconnecting = process_running and not record and not log_text
    health = resolve_health(
        state,
        last_update_age,
        refresh_seconds=float(st.session_state.auto_refresh_seconds),
        stage=record_stage,
        reconnecting=reconnecting,
    )

    settings = record.get("settings", {}) if isinstance(record.get("settings"), dict) else {}
    history_records = run_history_records(started_at) if workspace == "engage" and started_at else []
    submitted = sum(1 for item in history_records if str(item.get("status", "")).lower() == "submitted")
    unverified = sum(1 for item in history_records if str(item.get("status", "")).lower() == "unverified")
    engage_failures = sum(1 for item in history_records if str(item.get("status", "")).lower() == "failed")

    current = safe_progress_int(record.get("progress_current"))
    total = safe_progress_int(record.get("progress_total"))
    detail_items: list[tuple[str, str]] = []
    stage_markers: dict[str, bool] = {}
    subprogress_label = ""
    subprogress_current: int | None = None
    subprogress_total: int | None = None
    runtime_details: dict[str, object] = {}

    if workspace == "engage":
        fallback = parse_bot_progress(log_text)
        if total <= 0:
            total = safe_progress_int(fallback.get("target"))
        if not record and current <= 0:
            current = submitted + unverified
        failures = engage_failures or safe_progress_int(fallback.get("failures"))
        detail_items = [
            ("Submitted", str(submitted)),
            ("Unverified", str(unverified)),
            ("Failures", str(failures)),
        ]
        if current > 0:
            subprogress_label = "Verified submissions"
            subprogress_current = submitted
            subprogress_total = current
        stage_markers = {
            "scan": "extracted chapter context" in lowered_log,
            "submit": "chapter submission" in lowered_log,
            "verify": submitted > 0,
        }
        runtime_details.update({"submitted": submitted, "unverified": unverified, "failures": failures})
    elif workspace == "capture":
        fallback = parse_ocr_progress(log_text)
        if total <= 0:
            total = safe_progress_int(fallback.get("target"))
        reader_matches = re.findall(
            r"Reader media progress:\s*page\s+(\d+)\s*/\s*(\d+|\?)\s*,\s*(\d+)\s+unique image(?:s)?",
            log_text,
            re.IGNORECASE,
        )
        reader_current = safe_progress_int(reader_matches[-1][0]) if reader_matches else 0
        reader_total = safe_progress_int(reader_matches[-1][1]) if reader_matches and reader_matches[-1][1] != "?" else 0
        reader_images = safe_progress_int(reader_matches[-1][2]) if reader_matches else 0
        saved_matches = re.findall(r"Saved\s+(\d+)\s*/\s*(\d+)\s+raw image", log_text, re.IGNORECASE)
        saved_current = safe_progress_int(saved_matches[-1][0]) if saved_matches else reader_images
        saved_total = safe_progress_int(saved_matches[-1][1]) if saved_matches else 0
        failures = safe_progress_int(fallback.get("failures"))
        reader_value = f"{reader_current} / {reader_total}" if reader_total else (str(reader_current) if reader_current else "Waiting")
        saved_value = f"{saved_current} / {saved_total}" if saved_total else str(saved_current)
        detail_items = [
            ("Reader page", reader_value),
            ("Images found", saved_value),
            ("Failures", str(failures)),
        ]
        if reader_current:
            subprogress_label = "Current reader page"
            subprogress_current = reader_current
            subprogress_total = reader_total or None
        stage_markers = {
            "browser": "opening" in lowered_log or "browser review" in lowered_log,
            "scan": bool(reader_matches),
            "save": bool(saved_matches),
        }
        runtime_details.update(
            {
                "reader_current": reader_current,
                "reader_total": reader_total,
                "saved_current": saved_current,
                "saved_total": saved_total,
                "failures": failures,
            }
        )
    else:
        upload_done = "WORD_ART_UPLOAD_VERIFIED" in log_text
        editor_done = "WORD_ART_EDITOR_VERIFIED" in log_text
        save_done = "WORD_ART_SAVE_VERIFIED" in log_text
        review_only = bool(settings.get("skip_submit", st.session_state.note_skip_submit))
        total = 2 if review_only else 3
        current = int(upload_done) + int(editor_done) + (0 if review_only else int(save_done))
        if state == "completed":
            current = total
        detail_items = [
            ("Upload", "Verified" if upload_done else "Waiting"),
            ("Editor", "Verified" if editor_done else "Waiting"),
            ("Final action", "Review ready" if review_only and state == "completed" else ("Verified" if save_done else "Waiting")),
        ]
        stage_markers = {
            "upload": upload_done,
            "edit": editor_done,
            "save": save_done,
            "review-mode": review_only,
        }
        runtime_details.update(
            {"upload_done": upload_done, "editor_done": editor_done, "save_done": save_done, "review_only": review_only}
        )

    total = max(total, current) if total > 0 else 0
    history = sample_progress_history(run_id, current, total)
    eta, eta_confidence = estimate_progress_eta(
        state=state,
        workspace=workspace,
        current=current,
        total=total,
        started_at=started_at,
        log_text=log_text,
        history_records=history_records,
        run_id=run_id,
    )
    detail_items.append(("ETA confidence", eta_confidence))
    terminal = state in {"completed", "failed", "stopped"}
    elapsed_end = safe_progress_int(record.get("updated_at")) if terminal else int(time.time())
    elapsed = format_elapsed(max(0, elapsed_end - started_at)) if started_at else "—"

    message = str(record.get("message") or record.get("stop_reason") or "")
    stage_label = progress_stage_label(workspace, record_stage, message, state)
    output_path, output_ready = resolved_progress_output(bot_type, record, started_at)
    notice = ""
    if state == "waiting":
        notice = "Finish the browser check, then use the Continue control below."
    elif state == "paused":
        notice = "The local process is safely paused. Resume when you are ready."
    elif state == "failed":
        notice = redact_private_paths(str(record.get("stop_reason") or message or "Review Diagnostics before trying again."))
    elif state == "stopped":
        notice = redact_private_paths(str(record.get("stop_reason") or "This run stopped before every planned step finished."))
    elif health == "slowing":
        notice = "This step is taking longer than recent updates, but the process is still active."
    elif health == "stalled":
        notice = "No recent update was detected. Check the browser and Diagnostics before stopping the run."
    elif health == "reconnecting":
        notice = "The Mac process is still active while Panelyra reconnects to its run record."

    receipt_title = ""
    receipt_body = ""
    receipt_items: list[tuple[str, str]] = []
    if state == "completed":
        if workspace == "engage":
            receipt_title = "Comment run complete"
            receipt_body = "Verified local history is ready in Engage Insights."
            receipt_items = [("Processed", str(current)), ("Verified", str(submitted)), ("Elapsed", elapsed)]
        elif workspace == "capture":
            receipt_title = "Capture complete"
            receipt_body = "The chapter output was verified and is ready in the local library." if output_ready else "The run finished; verify the output from Capture Library."
            receipt_items = [("Chapters", str(current)), ("Output", "Verified" if output_ready else "Check output"), ("Elapsed", elapsed)]
        else:
            review_only = bool(runtime_details.get("review_only"))
            receipt_title = "Ready for review" if review_only else "Studio run complete"
            receipt_body = "The editor remains open for your final review." if review_only else "The configured editor action was verified."
            receipt_items = [("Checks", f"{current} of {total}"), ("Preview", "Verified" if output_ready else "Check preview"), ("Elapsed", elapsed)]

    component_id = f"{run_id}-{surface}"
    previous_values = st.session_state.setdefault("progress_previous", {})
    previous_current = previous_values.get(component_id, current)
    previous_values[component_id] = current
    announcement_state = st.session_state.setdefault("progress_announcements", {})
    announcement_signature = f"{state}:{record_stage}:{current}:{total}"
    announcement = ""
    if announcement_state.get(component_id) != announcement_signature:
        announcement = f"{workspace_label}: {stage_label}."
        announcement_state[component_id] = announcement_signature

    model = RunProgressModel(
        component_id=component_id,
        workspace=workspace,
        title=f"{workspace_label} run",
        state=state,
        health=health,
        current=current,
        total=total or None,
        previous_current=safe_progress_int(previous_current),
        stage=stage_label,
        elapsed=elapsed,
        eta=eta,
        last_updated=format_freshness(last_update_age),
        status_label="Ready for review" if workspace == "studio" and state == "completed" and bool(runtime_details.get("review_only")) else "",
        subprogress_label=subprogress_label,
        subprogress_current=subprogress_current,
        subprogress_total=subprogress_total,
        stages=(),
        activities=extract_activity_items(log_text, workspace, limit=3),
        history=history,
        detail_items=tuple(detail_items),
        notice=notice,
        receipt_title=receipt_title,
        receipt_body=receipt_body,
        receipt_items=tuple(receipt_items),
        aria_label=f"{workspace_label} run progress",
        announcement=announcement,
        motion_phase=time.time() % 3.2,
    )
    runtime_details.update(
        {
            "workspace": workspace,
            "workspace_label": workspace_label,
            "run_id": run_id,
            "record": record,
            "runtime": runtime,
            "running": process_running,
            "state": state,
            "log_path": log_path,
            "log_text": log_text,
            "output_path": output_path,
            "output_ready": output_ready,
        }
    )
    return model, runtime_details


def emit_runtime_notifications() -> None:
    notification_state = st.session_state.setdefault("runtime_notifications", {})
    for bot_type, runtime in st.session_state.bot_runtimes.items():
        process = runtime.get("process")
        if process is None:
            continue
        return_code = process.poll()
        signature = f"{getattr(process, 'pid', 0)}:{return_code}"
        if notification_state.get(bot_type) == signature:
            continue
        notification_state[bot_type] = signature
        if return_code is None:
            continue
        label = {"comment": "Engage", "download": "Capture", "word_art": "Studio"}.get(bot_type, bot_type)
        if return_code == 0:
            st.toast(f"{label} run finished.")
            send_mac_notification(f"{label} run complete", "Panelyra is ready for review.")
        else:
            st.toast(f"{label} run stopped with code {return_code}. Open its Live view for details.")
            send_mac_notification(f"{label} run needs attention", "Open Panelyra to review the run details.")


def send_mac_notification(title: str, message: str) -> None:
    """Send an opt-in local Notification Center alert on macOS."""
    if not bool(st.session_state.get("mac_notifications", False)):
        return
    if sys.platform != "darwin":
        return
    safe_title = str(title).replace("\\", "\\\\").replace('"', '\\"')[:120]
    safe_message = str(message).replace("\\", "\\\\").replace('"', '\\"')[:240]
    try:
        subprocess.run(
            [
                "osascript",
                "-e",
                f'display notification "{safe_message}" with title "{safe_title}"',
            ],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def is_streamlit_test_session() -> bool:
    context = get_script_run_ctx(suppress_warning=True)
    return bool(context and context.session_id == "test session id")


def render_progress_component(model: RunProgressModel, *, compact: bool = False) -> None:
    st.html(
        build_progress_html(
            model,
            motion_mode=str(st.session_state.progress_animation),
            compact=compact,
            show_history=bool(st.session_state.progress_history) and not compact,
        )
    )


def reveal_in_finder(path_value: object) -> None:
    if sys.platform != "darwin":
        st.warning("This action is available in the Mac app.")
        return
    path = Path(str(path_value)).expanduser() if path_value else None
    if path is None or not path.exists():
        st.warning("Panelyra could not verify that output yet.")
        return
    command = ["open", "-R", str(path)] if path.is_file() else ["open", str(path)]
    try:
        subprocess.run(command, check=False)
    except OSError:
        st.warning("Finder could not open that output.")


def focus_assistant_on_run(model: RunProgressModel, details: dict[str, object]) -> None:
    st.session_state.assistant_run_context = {
        "workspace": str(details["workspace"]),
        "bot_type": str(details.get("record", {}).get("bot_type", ""))
        if isinstance(details.get("record"), dict)
        else "",
        "run_id": str(details["run_id"]),
        "state": model.state,
        "stage": model.stage,
    }
    st.session_state.assistant_prompt = (
        f"Help me understand this {details['workspace_label']} run: "
        f"{model.state} at {model.stage}."
    )
    set_page("Assistant")
    request_scroll_to_top()
    st.rerun()


def open_workspace_results(workspace_label: str) -> None:
    if workspace_label == "Capture":
        set_page("Library")
    else:
        set_page(workspace_label)
        st.session_state[f"{workspace_label.lower()}_view"] = "Results"
    request_scroll_to_top()
    st.rerun()


def prepare_workspace_retry(workspace_label: str) -> None:
    step_keys = {"Engage": "engage_setup_step", "Capture": "capture_setup_step", "Studio": "studio_setup_step"}
    if workspace_label in step_keys:
        st.session_state[step_keys[workspace_label]] = 3
    set_page(workspace_label)
    st.session_state[f"{workspace_label.lower()}_view"] = "Configure"
    request_scroll_to_top()
    st.rerun()


def continue_waiting_run(bot_type: str, details: dict[str, object]) -> None:
    runtime = details["runtime"]
    if not isinstance(runtime, dict):
        return
    signal_path = runtime.get("signal_path")
    if signal_path is None:
        st.warning("The browser is still waiting, but this dashboard session cannot recover its Continue signal. Stop and prepare the run again.")
        return
    Path(signal_path).write_text("start\n", encoding="utf-8")
    runtime["signal_path"] = None
    if bot_type == "download":
        st.session_state.start_signal_path = None
        st.session_state.lyra_capture_prepared = False
        st.session_state.lyra_capture_review_status = "capture started"
    st.toast(f"{details['workspace_label']} is continuing from the active browser page.")
    st.rerun()


def inspect_prepared_capture_browser() -> dict[str, object]:
    snapshot = inspect_chrome_page(
        include_screenshot=False,
        screenshot_dir=APP_DIR / "outputs" / "assistant",
        debug_port=9223,
    )
    expected_url = str(st.session_state.lyra_capture_expected_url or st.session_state.ocr_start_url)
    actual_url = str(snapshot.get("url", ""))
    warnings: list[str] = []
    if snapshot.get("available"):
        expected_host = (urlparse(expected_url).hostname or "").lower()
        actual_host = (urlparse(actual_url).hostname or "").lower()
        if expected_host and actual_host and expected_host != actual_host:
            warnings.append(f"Chrome is on {actual_host}, not {expected_host}.")
        page_text = str(snapshot.get("text_excerpt", "")).lower()
        if any(term in page_text for term in ("log in", "sign in", "login", "verify you are human")):
            warnings.append("The reader may require login or a browser verification step.")
        if not snapshot.get("links") and not snapshot.get("buttons"):
            warnings.append("Lyra could not identify reader controls yet; allow the page to finish loading and recheck.")
        ready = not (expected_host and actual_host and expected_host != actual_host)
    else:
        ready = False
        warnings.append(str(snapshot.get("error", "The Capture browser is not inspectable yet.")))
    snapshot["warnings"] = warnings
    snapshot["ready_to_begin"] = ready
    st.session_state.lyra_capture_browser_snapshot = snapshot
    st.session_state.lyra_capture_review_status = (
        "ready to begin" if ready else "browser needs attention"
    )
    return snapshot


def cancel_prepared_capture() -> None:
    runtime = st.session_state.bot_runtimes.get("download", {})
    signal_path = runtime.get("signal_path")
    stop_bot(runtime.get("process"))
    if signal_path:
        Path(signal_path).unlink(missing_ok=True)
    runtime["signal_path"] = None
    runtime["paused"] = False
    st.session_state.start_signal_path = None
    st.session_state.lyra_capture_prepared = False
    st.session_state.lyra_capture_review_status = "cancelled"
    st.session_state.capture_setup_step = 3
    set_page("Capture Setup")


def render_live_run_actions(
    bot_type: str,
    model: RunProgressModel,
    details: dict[str, object],
) -> None:
    runtime = details["runtime"]
    assert isinstance(runtime, dict)
    running_for_workspace = bool(details["running"])
    workspace_label = str(details["workspace_label"])
    output_path = details.get("output_path")
    output_ready = bool(details.get("output_ready"))

    if model.state == "completed":
        columns = st.columns(3)
        if columns[0].button("View results", key=f"progress_results_{bot_type}", type="primary", width="stretch"):
            open_workspace_results(workspace_label)
        if columns[1].button(
            "Show in Finder",
            key=f"progress_output_{bot_type}",
            disabled=not output_ready,
            width="stretch",
        ):
            reveal_in_finder(output_path)
        if columns[2].button("Run again", key=f"progress_again_{bot_type}", width="stretch"):
            prepare_workspace_retry(workspace_label)
        return

    if model.state in {"failed", "stopped"}:
        columns = st.columns(2)
        if columns[0].button(
            "Review and retry",
            key=f"progress_retry_{bot_type}",
            type="primary",
            width="stretch",
        ):
            prepare_workspace_retry(workspace_label)
        if columns[1].button(
            "Show output in Finder",
            key=f"progress_partial_output_{bot_type}",
            disabled=not output_ready,
            width="stretch",
        ):
            reveal_in_finder(output_path)
        return

    columns = st.columns(3)
    if model.state == "waiting":
        capture_review_ready = bool(
            st.session_state.lyra_capture_browser_snapshot.get("ready_to_begin")
        ) if bot_type == "download" and st.session_state.lyra_capture_prepared else True
        signal_ready = runtime.get("signal_path") is not None and capture_review_ready
        if columns[0].button(
            "Begin Capture" if bot_type == "download" and st.session_state.lyra_capture_prepared else (
                "Continue in browser" if signal_ready else "Waiting for browser"
            ),
            key=f"progress_continue_{bot_type}",
            type="primary",
            disabled=not running_for_workspace or not signal_ready,
            width="stretch",
        ):
            continue_waiting_run(bot_type, details)
    else:
        pause_label = "Resume" if runtime.get("paused") else "Pause"
        if columns[0].button(
            pause_label,
            key=f"progress_pause_{bot_type}",
            disabled=not running_for_workspace,
            width="stretch",
        ):
            changed = (
                resume_bot(runtime.get("process"))
                if runtime.get("paused")
                else pause_bot(runtime.get("process"))
            )
            if changed:
                runtime["paused"] = not runtime.get("paused", False)
                st.rerun()
    if model.state == "waiting":
        pause_label = "Resume" if runtime.get("paused") else "Pause"
        if columns[1].button(
            pause_label,
            key=f"progress_wait_pause_{bot_type}",
            disabled=not running_for_workspace,
            width="stretch",
        ):
            changed = (
                resume_bot(runtime.get("process"))
                if runtime.get("paused")
                else pause_bot(runtime.get("process"))
            )
            if changed:
                runtime["paused"] = not runtime.get("paused", False)
                st.rerun()
    else:
        if columns[1].button(
            "Show output",
            key=f"progress_live_output_{bot_type}",
            disabled=not output_ready,
            width="stretch",
        ):
            reveal_in_finder(output_path)
    if columns[2].button(
        "Stop",
        key=f"progress_stop_{bot_type}",
        disabled=not running_for_workspace,
        width="stretch",
    ):
        stop_bot(runtime.get("process"))
        runtime["paused"] = False
        st.rerun()


def _render_live_progress_region(bot_type: str) -> None:
    emit_runtime_notifications()
    model, details = build_run_progress_model(bot_type, surface="live")
    fragment_activity = st.session_state.setdefault("progress_fragment_activity", {})
    was_active = bool(fragment_activity.get(bot_type))
    is_active = bool(details["running"])
    fragment_activity[bot_type] = is_active
    if was_active and not is_active:
        st.rerun()
    render_progress_component(model)
    render_live_run_actions(bot_type, model, details)


def render_live_progress_region(bot_type: str) -> None:
    fragment_api = getattr(st, "fragment", None)
    if callable(fragment_api):
        runtime = st.session_state.bot_runtimes.get(bot_type, {})
        is_active = is_process_running(runtime.get("process"))
        run_every = (
            f"{int(st.session_state.auto_refresh_seconds)}s"
            if is_active and not is_streamlit_test_session()
            else None
        )
        fragment_api(run_every=run_every)(_render_live_progress_region)(bot_type)
        return
    _render_live_progress_region(bot_type)


def _render_run_dock_region() -> None:
    active = [
        bot_type
        for bot_type, runtime in st.session_state.bot_runtimes.items()
        if bot_type in PROGRESS_WORKSPACES and is_process_running(runtime.get("process"))
    ]
    dock_was_active = bool(st.session_state.get("progress_dock_active"))
    st.session_state.progress_dock_active = bool(active)
    if dock_was_active and not active:
        emit_runtime_notifications()
        st.rerun()
    if not active:
        return
    st.markdown('<div class="run-dock-label">Active Mac runs</div>', unsafe_allow_html=True)
    with st.container(border=True):
        for bot_type in active:
            model, details = build_run_progress_model(bot_type, surface="dock")
            render_progress_component(model, compact=True)
            runtime = details["runtime"]
            assert isinstance(runtime, dict)
            pause_column, stop_column, open_column = st.columns(3)
            pause_label = "Resume" if runtime.get("paused") else "Pause"
            if pause_column.button(pause_label, key=f"dock_pause_{bot_type}", width="stretch"):
                changed = (
                    resume_bot(runtime.get("process"))
                    if runtime.get("paused")
                    else pause_bot(runtime.get("process"))
                )
                if changed:
                    runtime["paused"] = not runtime.get("paused", False)
                    st.rerun()
            if stop_column.button("Stop", key=f"dock_stop_{bot_type}", width="stretch"):
                stop_bot(runtime.get("process"))
                runtime["paused"] = False
                st.rerun()
            if open_column.button("Open Live", key=f"dock_open_{bot_type}", width="stretch"):
                workspace_label = str(details["workspace_label"])
                set_page(workspace_label)
                st.session_state[f"{workspace_label.lower()}_view"] = "Live"
                request_scroll_to_top()
                st.rerun()


def render_run_dock() -> None:
    fragment_api = getattr(st, "fragment", None)
    if callable(fragment_api):
        has_active_run = any(
            is_process_running(runtime.get("process"))
            for runtime in st.session_state.bot_runtimes.values()
        )
        run_every = (
            f"{int(st.session_state.auto_refresh_seconds)}s"
            if has_active_run and not is_streamlit_test_session()
            else None
        )
        fragment_api(run_every=run_every)(_render_run_dock_region)()
        return
    _render_run_dock_region()


def persist_workspace_drafts() -> None:
    drafts = {
        key: st.session_state[key]
        for key in WORKSPACE_DRAFT_KEYS
        if key in st.session_state
        and isinstance(st.session_state[key], (str, int, float, bool))
    }
    durable_preferences = {
        "workspace_drafts": drafts,
        "default_landing_page": str(st.session_state.default_landing_page),
        "assistant_use_model": bool(st.session_state.assistant_use_model),
        "progress_animation": str(st.session_state.progress_animation),
        "progress_history": bool(st.session_state.progress_history),
        "mac_notifications": bool(st.session_state.mac_notifications),
        "compact_ui": bool(st.session_state.compact_ui),
        "compact_sidebar": bool(st.session_state.compact_sidebar),
        "assistant_local_model": str(st.session_state.assistant_local_model),
        "assistant_history_limit": int(st.session_state.assistant_history_limit),
        "assistant_save_history": bool(st.session_state.assistant_save_history),
        "assistant_internet_endpoint": str(st.session_state.assistant_internet_endpoint),
        "assistant_web_search_enabled": bool(st.session_state.assistant_web_search_enabled),
        "assistant_use_openai_web": bool(st.session_state.assistant_use_openai_web),
        "assistant_model": str(st.session_state.assistant_model),
        "show_lyra_in_immersive_reader": bool(st.session_state.show_lyra_in_immersive_reader),
    }
    encoded = json.dumps(durable_preferences, sort_keys=True, ensure_ascii=False)
    if st.session_state.get("_saved_workspace_drafts") == encoded:
        return
    preferences = load_preferences()
    preferences.update(durable_preferences)
    save_preferences(preferences)
    st.session_state["_saved_workspace_drafts"] = encoded


def restore_run_configuration(record: dict[str, object]) -> None:
    settings = record.get("settings", {})
    if not isinstance(settings, dict):
        return
    bot_type = str(record.get("bot_type", ""))
    if bot_type == "comment":
        st.session_state.start_url = str(settings.get("start_url", ""))
        st.session_state.max_chapters = int(settings.get("max_chapters", 1))
        st.session_state.chapter_routing = str(settings.get("routing", "auto"))
        set_page("Engage Setup")
    elif bot_type == "download":
        st.session_state.ocr_start_url = str(settings.get("start_url", ""))
        st.session_state.ocr_max_chapters = int(settings.get("max_chapters", 1))
        st.session_state.ocr_reader_layout = str(settings.get("reader_layout", "auto"))
        set_page("Capture Setup")
    else:
        set_page("Studio")


page_bot_type = runtime_type_for_page(st.session_state.page)
status_label = runtime_status(page_bot_type) if page_bot_type else (
    f"{len(running_bots)} running" if running_bots else "Idle"
)

with st.sidebar:
    st.markdown(
        f"""
        <div class="brand-lockup">
          <div class="brand-name">{PRODUCT_NAME}</div>
          <div class="brand-tagline">{PRODUCT_TAGLINE}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.markdown(
        f"""
        <div class="sidebar-status">
          <span class="global-status-dot"></span>
          <span>{status_label}</span>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.divider()
    navigation_help = {
        "Dashboard": "Start here and choose what you want Panelyra to do.",
        "Engage": "Create and verify contextual chapter comments.",
        "Capture": "Save chapter images, CBZ archives, and optional OCR.",
        "Downloads": "Search for a series and download selected chapters.",

        "Library": "Read saved series and continue where you stopped.",
        "Activity": "Review completed, stopped, and failed runs.",

        "Help": "Learn how the app and each workflow work.",
        "Settings": "Control appearance, storage, and local integrations.",
    }
    for group, pages in NAVIGATION_GROUPS.items():
        st.caption(group)
        for navigation_page in pages:
            runtime_type = runtime_type_for_page(navigation_page)
            is_active_run = runtime_type in running_bots
            label = f"{navigation_page}  •" if is_active_run else navigation_page
            if st.button(
                label,
                key=f"nav_{navigation_page}",
                type="primary" if str(st.session_state.page) == navigation_page else "secondary",

                width="stretch",
            ):
                set_page(navigation_page)
                st.rerun()
    if st.session_state.show_sidebar_paths:
        st.divider()
        st.caption("Workspace")
        st.code("Panelyra workspace", language="text")
        active_log = st.session_state.bot_runtimes.get(page_bot_type, {}).get("log_path") if page_bot_type else None
        if active_log:
            st.caption("Active log")
            st.code(display_path(active_log), language="text")

page = st.session_state.page
history_records = read_history()
if page.startswith("Engage"):
    running = is_process_running(st.session_state.bot_runtimes["comment"].get("process"))
elif page.startswith("Capture"):
    running = is_process_running(st.session_state.bot_runtimes["download"].get("process"))
elif page.startswith("Studio"):
    running = is_process_running(st.session_state.bot_runtimes["word_art"].get("process"))

if st.session_state.get("route_notice"):
    st.info(st.session_state.pop("route_notice"))
emit_runtime_notifications()
route_page = render_workspace_view(page)

if route_page == "Library":
    from panelyra.screens.library import render as render_library
    render_library(APP_DIR=APP_DIR, PANELYRA_LOG_DIR=PANELYRA_LOG_DIR, Path=Path, READER_PROGRESS_FILE=READER_PROGRESS_FILE, ReaderProgressStore=ReaderProgressStore, _chapter_integrity=_chapter_integrity, _chapter_number=_chapter_number, _reader_cover_data=_reader_cover_data, html=html, is_process_running=is_process_running, json=json, local_reader_service=local_reader_service, reader_catalog=reader_catalog, render_cleaner_review=render_cleaner_review, render_page_header=render_page_header, set_page=set_page, st=st, start_clean_review=start_clean_review, subprocess=subprocess, sys=sys)

elif route_page == 'Engage Setup':
    from panelyra.screens.engage import render as render_engage
    render_engage(CONTENT_MODE_OPTIONS=CONTENT_MODE_OPTIONS, SCROLL_PRESETS=SCROLL_PRESETS, apply_reader_defaults=apply_reader_defaults, apply_selected_reader_mode_defaults=apply_selected_reader_mode_defaults, generate_comment_preview=generate_comment_preview, load_presets=load_presets, load_selector_profiles=load_selector_profiles, normalized_speed_name=normalized_speed_name, preflight_url=preflight_url, reader_defaults_for_mode=reader_defaults_for_mode, render_page_header=render_page_header, running=running, set_page=set_page, st=st, start_bot=start_bot)

elif route_page == 'Downloads':
    render_page_header("Downloads", "Find a series. Choose chapters. Save to your library.")
    from panelyra.services.manga_backend_ui import render_manga_backend
    render_manga_backend()

elif route_page == 'Capture Setup':
    from panelyra.screens.capture import render as render_capture
    render_capture(MANHWA_CONTAINER_SELECTOR=MANHWA_CONTAINER_SELECTOR, MANHWA_IMAGE_SELECTOR=MANHWA_IMAGE_SELECTOR, OCR_DOWNLOAD_DIR=OCR_DOWNLOAD_DIR, Path=Path, apply_ocr_download_defaults=apply_ocr_download_defaults, display_path=display_path, load_selector_profiles=load_selector_profiles, preflight_url=preflight_url, render_page_header=render_page_header, running=running, set_page=set_page, st=st, start_manhwa_ocr_bot=start_manhwa_ocr_bot)

elif route_page == 'Activity':
    from panelyra.screens.activity import render as render_activity
    render_activity(display_path=display_path, json=json, list_run_records=list_run_records, redact_private_paths=redact_private_paths, render_page_header=render_page_header, restore_run_configuration=restore_run_configuration, set_page=set_page, st=st, time=time, uuid=uuid)


elif route_page == 'Settings':
    from panelyra.screens.settings import render as render_settings
    render_settings(APP_DIR=APP_DIR, DEFAULT_SETTINGS=DEFAULT_SETTINGS, PAGES=PAGES, environment_health=environment_health, load_presets=load_presets, load_selector_profiles=load_selector_profiles, render_page_header=render_page_header, save_local_preferences=save_local_preferences, save_presets=save_presets, save_selector_profiles=save_selector_profiles, st=st)

elif route_page == 'Storage':
    from panelyra.screens.storage import render as render_storage
    render_storage(format_bytes=format_bytes, prune_generated_files=prune_generated_files, render_page_header=render_page_header, save_local_preferences=save_local_preferences, st=st, storage_report=storage_report)

elif route_page == 'Engage Live':
    from panelyra.screens.engage_live import render as render_engage_live
    render_engage_live(Path=Path, parse_bot_progress=parse_bot_progress, read_log=read_log, render_live_progress_region=render_live_progress_region, render_page_header=render_page_header, render_setup_stepper=render_setup_stepper, st=st)

elif route_page == 'Capture Live':
    from panelyra.screens.capture_live import render as render_capture_live
    render_capture_live(Path=Path, cancel_prepared_capture=cancel_prepared_capture, inspect_prepared_capture_browser=inspect_prepared_capture_browser, parse_ocr_progress=parse_ocr_progress, read_log=read_log, render_live_progress_region=render_live_progress_region, render_page_header=render_page_header, render_setup_stepper=render_setup_stepper, st=st)

elif route_page == 'Engage Insights':
    from panelyra.screens.engage_insights import render as render_engage_insights
    render_engage_insights(COMMENT_STYLES=COMMENT_STYLES, Counter=Counter, HISTORY_FILE=HISTORY_FILE, history_records=history_records, render_jsonl_metric_grid=render_jsonl_metric_grid, render_page_header=render_page_header, render_panel=render_panel, st=st, style_recommendation=style_recommendation)

persist_workspace_drafts()

should_auto_refresh = any(
    is_process_running(runtime.get("process"))
    for runtime in st.session_state.bot_runtimes.values()
)
if (
    should_auto_refresh
    and not is_streamlit_test_session()
    and not callable(getattr(st, "fragment", None))
    and route_page in {"Engage Live", "Capture Live"}
):
    time.sleep(int(st.session_state.auto_refresh_seconds))
    st.rerun()
