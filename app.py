#!/usr/bin/env python3
"""
Local Streamlit dashboard for running the sequential form-submission QA suite.

Run:
    streamlit run app.py
"""

from __future__ import annotations

import json
import os
import random
import re
import signal
import subprocess
import sys
import time
import uuid
from collections import Counter
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import streamlit as st
from streamlit.components import v1 as components

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
    AssistantReply,
    CopilotContext,
    WorkspaceRun,
    build_model_prompt,
    create_local_reply,
    extract_log_events,
    needs_external_research,
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
from panelyra.services.health import environment_health, format_bytes
from panelyra.services.local_reader import start_local_reader_server
from panelyra.services.paths import (
    DOWNLOADS_DIR,
    LEGACY_DOWNLOAD_DIR,
    PANELYRA_CACHE_DIR,
    PANELYRA_LOG_DIR,
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
ensure_runtime_directories()
reconcile_stale_run_records()
ENV_FILE = APP_DIR / ".env"
BOT_PATH = APP_DIR / "bot.py"
NOTE_ART_BOT_PATH = APP_DIR / "note_art_bot.py"
MANHWA_OCR_BOT_PATH = APP_DIR / "manhwa_ocr_downloader.py"
BOT_PYTHON = APP_DIR / ".venv" / "bin" / "python"
HISTORY_FILE = APP_DIR / "comment_history.jsonl"
READ_MARKER_FILE = APP_DIR / "read_markers.jsonl"
RESEARCH_FILE = RESEARCH_CONTEXT_FILE
OWNER_REQUESTS_FILE = APP_DIR / "owner_requests.jsonl"
ASSISTANT_FEEDBACK_FILE = APP_DIR / "assistant_feedback.jsonl"
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
    if path.stem.isdigit():
        return (0, int(path.stem), path.name.lower())
    return (1, path.stem.lower(), path.name.lower())


def reader_catalog() -> dict[str, dict[str, object]]:
    """Index local original and clean manhwa folders without exposing their paths."""
    catalog: dict[str, dict[str, object]] = {}
    roots = (
        ("Original", DOWNLOADS_DIR),
        ("Clean", APP_DIR / "manhwa_clean"),
    )
    for edition, root in roots:
        if not root.is_dir():
            continue
        for series_dir in sorted((path for path in root.iterdir() if path.is_dir()), key=reader_sort_key):
            series_key = series_dir.name.removesuffix("-clean")
            chapter_map: dict[str, dict[str, object]] = {}
            series_title = series_key.replace("-", " ").title()
            for chapter_dir in sorted((path for path in series_dir.iterdir() if path.is_dir()), key=reader_sort_key):
                images_dir = chapter_dir / "images"
                if not images_dir.is_dir():
                    continue
                images = sorted(
                    (path for path in images_dir.iterdir() if path.suffix.lower() in READER_IMAGE_SUFFIXES),
                    key=reader_sort_key,
                )
                if not images:
                    continue
                metadata_path = chapter_dir / "metadata.json"
                metadata: dict[str, object] = {}
                if metadata_path.is_file():
                    try:
                        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        metadata = {}
                title = str(metadata.get("series_title") or series_title).strip()
                if title:
                    series_title = title
                chapter_map[chapter_dir.name] = {
                    "images": images,
                    "title": str(metadata.get("title") or f"Chapter {chapter_dir.name}").strip(),
                }
            if chapter_map:
                entry = catalog.setdefault(series_key, {"title": series_title, "editions": {}})
                entry["title"] = series_title
                editions = entry["editions"]
                if isinstance(editions, dict):
                    editions[edition] = chapter_map
    return catalog


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

DEFAULT_SETTINGS = {
    "default_landing_page": "Dashboard",
    "compact_ui": False,
    "show_sidebar_paths": False,
    "console_max_chars": 60_000,
    "auto_refresh_seconds": 2,
    "history_rows": 30,
    "top_keywords": 10,
    "reflection_every_n_comments": 5,
    "assistant_enabled": True,
    "assistant_internet_endpoint": "",
    "assistant_history_limit": 40,
    "assistant_model": os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
    "assistant_use_openai_web": False,
    "assistant_use_model": True,
    "assistant_show_evidence": True,
    "assistant_allow_browser_inspection": True,
    "assistant_generated_plan": "",
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
        "You are Panelyra Copilot, grounded in the supplied local application context. "
        "Help users operate Engage, Capture, Studio, settings, diagnostics, and support. "
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
        "- `Engage`, `Capture`, and `Studio`: independent workspaces with `Configure`, `Live`, and `Results` views.\n"
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
            "- `Assistant`: toggles the floating button, model name, web search, and internet endpoint.\n"
            "- `Support Requests`: local requests users ask the assistant to send to the owner."
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
            "- The floating assistant is one chat box. Ask it for help with comments, users, settings, logs, support, or site workflow.\n"
            "- If a user reports a problem, ask: `write a reply to a user who says ...` and include the problem details.\n"
            "- To make a local owner/support request, ask the assistant to contact the owner and describe the issue.\n"
            "- If `OPENAI_API_KEY` is missing, the assistant can still answer built-in app questions like this one, but broad general AI answers require the key."
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
        record = create_owner_request(prompt, st.session_state.page, status_label)
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
    record = RunRecord.create(bot_type, settings, log_path)
    store = RunStore(record)
    environment = subprocess_env()
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
    store.update(status="running", stage="process-started", pid=process.pid)
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
        settings={"note_url": note_url, "style": style_mode, "skip_submit": skip_submit},
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
    max_chapters = max(1, min(20, int(max_chapters)))
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
    st.markdown(
        """
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Manrope:wght@400;520;650;760&family=Space+Grotesk:wght@560;650;720&display=swap');
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
            animation: pageFadeUp 420ms cubic-bezier(0.16, 1, 0.3, 1) both;
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
        section[data-testid="stSidebar"] div.stButton > button {
            background: transparent;
            color: var(--text);
            border: 1px solid transparent;
            justify-content: flex-start;
            box-shadow: none;
            position: relative;
            overflow: hidden;
        }
        section[data-testid="stSidebar"] div.stButton > button::before {
            content: "";
            position: absolute;
            inset: 4px 6px;
            border-radius: 8px;
            background: linear-gradient(180deg, rgba(0,180,216,0.14), rgba(123,44,191,0.075));
            opacity: 0;
            transform: translateY(12px) scale(0.96);
            transition: opacity 360ms cubic-bezier(0.16, 1, 0.3, 1), transform 360ms cubic-bezier(0.16, 1, 0.3, 1);
            z-index: -1;
        }
        section[data-testid="stSidebar"] div.stButton > button:hover {
            background: transparent;
            border-color: var(--line);
            color: var(--text);
        }
        section[data-testid="stSidebar"] div.stButton > button:hover::before {
            opacity: 1;
            transform: translateY(0) scale(1);
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
        div[data-testid="stPopover"] {
            position: fixed !important;
            right: 30px !important;
            bottom: 30px !important;
            z-index: 999999 !important;
            width: 58px !important;
            height: 58px !important;
            min-width: 58px !important;
            max-width: 58px !important;
            display: block !important;
            margin: 0 !important;
            transform: none !important;
        }
        div[data-testid="stElementContainer"]:has(div[data-testid="stPopover"]) {
            position: fixed !important;
            bottom: 30px !important;
            right: 30px !important;
            z-index: 999999 !important;
            width: 58px !important;
            height: 58px !important;
            min-width: 58px !important;
            max-width: 58px !important;
            margin: 0 !important;
            transform: none !important;
        }
        div[data-testid="stPopover"] > div {
            width: 58px !important;
            height: 58px !important;
            min-width: 58px !important;
            max-width: 58px !important;
        }
        div[data-testid="stPopover"] button {
            width: 58px !important;
            height: 58px !important;
            min-width: 58px !important;
            max-width: 58px !important;
            min-height: 58px !important;
            max-height: 58px !important;
            border-radius: 50% !important;
            padding: 0 !important;
            font-size: 0;
            display: grid;
            place-items: center;
            border: 1px solid rgba(0,180,216,0.42) !important;
            background:
                radial-gradient(circle at 34% 26%, rgba(0,180,216,0.35), transparent 34%),
                radial-gradient(circle at 72% 74%, rgba(123,44,191,0.26), transparent 38%),
                linear-gradient(180deg, rgba(255,255,255,0.095), rgba(255,255,255,0.032)),
                #1F2128 !important;
            box-shadow:
                inset 0 1px rgba(255,255,255,0.13),
                0 22px 58px rgba(0,0,0,0.52),
                0 0 34px rgba(0,180,216,0.18) !important;
            animation: aiFloat 3.2s ease-in-out infinite;
        }
        div[data-testid="stPopover"] button p,
        div[data-testid="stPopover"] button span,
        div[data-testid="stPopover"] button svg {
            display: none !important;
        }
        div[data-testid="stPopover"] button::before {
            content: "💬";
            color: var(--text);
            font-size: 1.25rem;
            line-height: 1;
        }
        div[data-testid="stPopover"] button:hover {
            transform: translateY(-3px) scale(1.04);
            box-shadow:
                inset 0 1px rgba(255,255,255,0.16),
                0 28px 68px rgba(0,0,0,0.62),
                0 0 44px rgba(0,180,216,0.26) !important;
        }
        div[data-testid="stPopoverBody"] {
            position: fixed !important;
            right: 30px !important;
            bottom: 104px !important;
            z-index: 999998 !important;
            width: min(420px, calc(100vw - 60px));
            max-height: min(76vh, 720px);
            overflow: auto;
            border: 1px solid var(--line-strong);
            border-radius: 16px;
            background:
                linear-gradient(180deg, rgba(255,255,255,0.055), rgba(0,180,216,0.018)),
                #1A1C23;
            box-shadow: var(--shadow-lg);
        }
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
            font-size: clamp(1.65rem, 3vw, 2.35rem) !important;
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
        }
        __COMPACT_CSS__
        </style>
        """.replace("__COMPACT_CSS__", compact_css),
        unsafe_allow_html=True,
    )


def render_page_header(title: str, subtitle: str, pills: list[str] | None = None) -> None:
    pill_html = ""
    if pills:
        pill_html = "<div class='pill-row'>" + "".join(f"<span class='pill'>{pill}</span>" for pill in pills) + "</div>"
    st.markdown(
        f"""
        <div class="app-shell">
          <div class="page-title">{title}</div>
          <div class="page-subtitle">{subtitle}</div>
          {pill_html}
        </div>
        """,
        unsafe_allow_html=True,
    )


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


def build_copilot_context(records: list[dict[str, object]]) -> CopilotContext:
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
    current_view = str(
        st.session_state.get(f"{current_page.lower()}_view", "Overview")
    )
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
    )


def copilot_message_from_reply(reply: AssistantReply, content: str | None = None) -> dict[str, object]:
    message = reply.to_message()
    if content:
        message["content"] = content
    return message


def record_assistant_feedback(message: dict[str, object], rating: str) -> None:
    append_jsonl_record(
        ASSISTANT_FEEDBACK_FILE,
        {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "message_id": message.get("id", ""),
            "rating": rating,
            "mode": message.get("mode", ""),
            "page": st.session_state.page,
            "response": str(message.get("content", ""))[:4_000],
        },
    )
    message["feedback"] = rating


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


def execute_copilot_action(action: dict[str, object]) -> str:
    kind = str(action.get("kind", ""))
    payload = action.get("payload", {})
    payload = dict(payload) if isinstance(payload, dict) else {}
    if kind == "navigate":
        page = str(payload.get("page", "Dashboard"))
        view = str(payload.get("view", ""))
        set_page(page)
        if view and page in {"Engage", "Capture", "Studio"}:
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
        elif workspace == "studio":
            if values.get("url"):
                st.session_state.note_url = str(values["url"])
            st.session_state.studio_setup_step = 1
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


def render_copilot_details(message: dict[str, object], key_prefix: str) -> None:
    evidence = message.get("evidence", [])
    actions = message.get("actions", [])
    mode = str(message.get("mode", "Ask"))
    confidence = str(message.get("confidence", ""))
    st.caption(f"{mode} mode · {confidence} confidence")
    if st.session_state.assistant_show_evidence and isinstance(evidence, list) and evidence:
        with st.expander("Evidence", expanded=False):
            for item in evidence:
                if not isinstance(item, dict):
                    continue
                st.markdown(
                    f"**{item.get('label', 'Evidence')}**: {item.get('value', '')}  \n"
                    f"Source: `{item.get('source', 'local context')}`"
                )
    if isinstance(actions, list) and actions:
        action_columns = st.columns(min(2, len(actions)))
        for index, action in enumerate(actions):
            if not isinstance(action, dict):
                continue
            label = str(action.get("label", "Run action"))
            description = str(action.get("description", ""))
            if action.get("requires_confirmation"):
                description = f"Confirmation required. {description}".strip()
            if action_columns[index % len(action_columns)].button(
                label,
                key=f"{key_prefix}_action_{action.get('action_id', index)}",
                help=description or None,
                width="stretch",
            ):
                st.session_state.assistant_action_notice = execute_copilot_action(action)
                st.rerun()
    if not message.get("feedback"):
        feedback_columns = st.columns(2)
        if feedback_columns[0].button(
            "Helpful",
            key=f"{key_prefix}_helpful",
            width="stretch",
        ):
            record_assistant_feedback(message, "helpful")
            st.rerun()
        if feedback_columns[1].button(
            "Needs work",
            key=f"{key_prefix}_needs_work",
            width="stretch",
        ):
            record_assistant_feedback(message, "needs_work")
            st.rerun()
    else:
        st.caption(f"Feedback recorded: {message['feedback']}")


def render_copilot_message(message: dict[str, object], key_prefix: str) -> None:
    role = str(message.get("role", "assistant"))
    content = str(message.get("content", ""))
    with st.chat_message("assistant" if role == "assistant" else "user"):
        st.markdown(content)
        if role == "assistant":
            render_copilot_details(message, key_prefix)


def run_copilot_prompt(
    prompt: str,
    records: list[dict[str, object]],
    *,
    selected_mode: str,
    key_prefix: str,
) -> None:
    prior_messages = list(st.session_state.assistant_messages)
    user_message = {
        "id": f"user-{uuid.uuid4().hex[:10]}",
        "role": "user",
        "content": prompt,
        "mode": selected_mode,
    }
    st.session_state.assistant_messages.append(user_message)
    context = build_copilot_context(records)
    effective_mode = selected_mode
    follow_up_terms = ("it", "that", "same", "what about", "then", "this")
    if selected_mode == "Auto" and len(prompt.split()) <= 10 and any(
        term in prompt.lower() for term in follow_up_terms
    ):
        previous_assistant = next(
            (
                message
                for message in reversed(prior_messages)
                if str(message.get("role", "")) == "assistant"
            ),
            {},
        )
        previous_mode = str(previous_assistant.get("mode", "Auto"))
        if previous_mode in {"Ask", "Diagnose", "Configure", "Support"}:
            effective_mode = previous_mode
    baseline = create_local_reply(prompt, effective_mode, context)

    with st.chat_message("user"):
        st.markdown(prompt)
    with st.chat_message("assistant"):
        content = baseline.text
        if st.session_state.assistant_use_model and openai_api_key_available():
            model_prompt = build_model_prompt(prompt, baseline, context)
            use_web = bool(st.session_state.assistant_use_openai_web) and needs_external_research(
                prompt,
                baseline.mode,
            )
            with st.status("Reading Panelyra context", expanded=False):
                streamed = st.write_stream(
                    stream_openai_assistant(
                        model_prompt,
                        use_web=use_web,
                        conversation=prior_messages,
                    )
                )
            if isinstance(streamed, str) and streamed.strip():
                content = streamed.strip()
        elif (
            st.session_state.assistant_internet_endpoint.strip()
            and needs_external_research(prompt, baseline.mode)
        ):
            content = survey_internet(
                st.session_state.assistant_internet_endpoint,
                build_model_prompt(prompt, baseline, context),
            )
            st.markdown(content)
        else:
            st.markdown(content)
        message = copilot_message_from_reply(baseline, content)
        st.session_state.assistant_messages.append(message)
        render_copilot_details(message, f"{key_prefix}_{message['id']}")


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

    st.markdown(
        """
        <style>
        .st-key-assistant_mode div[role="radiogroup"] {
            display: grid;
            grid-template-columns: repeat(5, minmax(0, 1fr));
            gap: 4px;
            padding: 4px;
            border: 1px solid rgba(255,255,255,0.10);
            border-radius: 10px;
            background: rgba(8,10,14,0.78);
        }
        .st-key-assistant_mode div[role="radiogroup"] label {
            justify-content: center;
            min-height: 36px;
            margin: 0;
            padding: 6px 8px;
            border-radius: 7px;
            transition: background 160ms ease, box-shadow 160ms ease;
        }
        .st-key-assistant_mode div[role="radiogroup"] label:has(input:checked) {
            background: linear-gradient(180deg, #30343d, #22252c);
            box-shadow: inset 0 1px rgba(255,255,255,0.12), 0 5px 16px rgba(0,0,0,0.28);
        }
        .st-key-assistant_mode div[role="radiogroup"] label > div:first-child {
            display: none;
        }
        @media (max-width: 720px) {
            .st-key-assistant_mode div[role="radiogroup"] {
                grid-template-columns: repeat(3, minmax(0, 1fr));
            }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    selected_mode = st.radio(
        "Assistant mode",
        options=["Auto", "Ask", "Diagnose", "Configure", "Support"],
        index=0,
        horizontal=True,
        key="assistant_mode",
    )
    if not st.session_state.assistant_messages:
        suggestions = {
            "Auto": "Ask a question or describe what is failing.",
            "Ask": "Try: What does Capture save and where?",
            "Diagnose": "Try: Why did the latest downloader run fail?",
            "Configure": "Try: Configure Capture for this URL and 5 chapters.",
            "Support": "Try: Draft a reply to a user whose download stopped.",
        }
        st.info(suggestions.get(selected_mode, suggestions["Auto"]))
    for index, message in enumerate(st.session_state.assistant_messages[-history_limit:]):
        render_copilot_message(message, f"{key_prefix}_{message.get('id', index)}")

    prompt = st.chat_input(
        "Ask Panelyra, diagnose a run, configure a workflow, or help a user...",
        key=f"{key_prefix}_chat_input",
    )
    if prompt and prompt.strip():
        run_copilot_prompt(
            prompt.strip(),
            records,
            selected_mode=str(selected_mode),
            key_prefix=key_prefix,
        )


def render_floating_chat_widget(records: list[dict[str, object]], status_label: str) -> None:
    if not st.session_state.assistant_enabled or st.session_state.page == "Assistant":
        return

    readiness = "Model ready" if openai_api_key_available() else "Local copilot"
    with st.popover("AI", width="stretch"):
        st.markdown("### Panelyra Copilot")
        st.caption(
            f"{readiness} · page {st.session_state.page} · {status_label.lower()}"
        )
        controls = st.columns(2)
        if controls[0].button("Open full page", key="assistant_open_page", width="stretch"):
            set_page("Assistant")
            st.rerun()
        if controls[1].button("Clear chat", key="assistant_clear_chat", width="stretch"):
            st.session_state.assistant_messages = []
            st.rerun()
        render_copilot_chat(records, key_prefix="floating_copilot", history_limit=6)


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
    "Engage": {
        "Configure": "Engage Setup",
        "Live": "Engage Live",
        "Results": "Engage Insights",
    },
    "Capture": {
        "Configure": "Capture Setup",
        "Live": "Capture Live",
        "Results": "Capture Library",
    },
    "Studio": {
        "Configure": "Studio Setup",
        "Live": "Studio Live",
        "Results": "Studio Library",
    },
    "Settings": {
        "General": "Settings",
        "Storage": "Storage",
    },
}
LEGACY_PAGE_ROUTES = {
    route: (workspace, view)
    for workspace, views in WORKSPACE_VIEW_ROUTES.items()
    for view, route in views.items()
}
LEGACY_PAGE_ROUTES["Overview"] = ("Dashboard", "")


def request_scroll_to_top() -> None:
    st.session_state["_scroll_top_request"] = uuid.uuid4().hex


def set_page(page: str) -> None:
    previous_page = str(st.session_state.get("page", ""))
    if page in LEGACY_PAGE_ROUTES:
        workspace, view = LEGACY_PAGE_ROUTES[page]
        st.session_state.page = workspace
        if view:
            st.session_state[f"{workspace.lower()}_view"] = view
        if previous_page != workspace:
            request_scroll_to_top()
        return
    destination = page if page in PAGES else "Dashboard"
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
    if page == "Dashboard":
        return "Overview"
    views = WORKSPACE_VIEW_ROUTES.get(page)
    if not views:
        return page
    state_key = f"{page.lower()}_view"
    selected = str(st.session_state.get(state_key, next(iter(views))))
    view_columns = st.columns(len(views))
    for column, view in zip(view_columns, views):
        if column.button(
            view,
            key=f"{page.lower()}_view_button_{view.lower()}",
            type="primary" if view == selected else "secondary",
            width="stretch",
        ):
            st.session_state[state_key] = view
            request_scroll_to_top()
            st.rerun()
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
        "assistant_open": False,
        "assistant_messages": [],
        "assistant_prompt": "",
        "assistant_browser_snapshot": {},
        "assistant_action_notice": "",
        "assistant_selected_ticket": "",
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
    apply_ocr_download_defaults()
    if st.session_state.default_landing_page == "Overview":
        st.session_state.default_landing_page = "Dashboard"
    if st.session_state.default_landing_page not in PAGES:
        st.session_state.default_landing_page = "Dashboard"
    if "page" not in st.session_state:
        st.session_state.page = st.session_state.default_landing_page
    elif st.session_state.page in LEGACY_PAGE_ROUTES:
        set_page(str(st.session_state.page))
    elif st.session_state.page not in PAGES:
        st.session_state.page = "Dashboard"


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
    page_title=f"{PRODUCT_NAME} Control",
    page_icon="P",
    layout="wide",
    initial_sidebar_state="expanded",
)
initialize_session_state()
restore_active_runtime_references()
preserve_wizard_state()
inject_styles()
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
    path_value = st.session_state.run_record_paths.get(bot_type)
    if not path_value:
        return {}
    try:
        payload = json.loads(Path(path_value).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


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
        else:
            st.toast(f"{label} run stopped with code {return_code}. Open its Live view for details.")


def auto_advance_completed_workspace(page: str) -> None:
    workspace_runtime = {"Engage": "comment", "Capture": "download", "Studio": "word_art"}
    bot_type = workspace_runtime.get(page)
    if not bot_type or st.session_state.get(f"{page.lower()}_view") != "Live":
        return
    runtime = st.session_state.bot_runtimes[bot_type]
    process = runtime.get("process")
    if process is None or process.poll() is None:
        return
    signature = f"{getattr(process, 'pid', 0)}:{process.returncode}"
    marker_key = f"{page.lower()}_auto_advanced"
    if st.session_state.get(marker_key) == signature:
        return
    st.session_state[marker_key] = signature
    st.session_state[f"{page.lower()}_view"] = "Results"
    request_scroll_to_top()
    st.rerun()


def render_run_dock() -> None:
    active = [
        (bot_type, runtime)
        for bot_type, runtime in st.session_state.bot_runtimes.items()
        if is_process_running(runtime.get("process"))
    ]
    if not active:
        return

    labels = {"comment": "Engage", "download": "Capture", "word_art": "Studio"}
    pages = {"comment": "Engage", "download": "Capture", "word_art": "Studio"}
    st.markdown('<div class="run-dock-label">Active runs</div>', unsafe_allow_html=True)
    with st.container(border=True):
        for bot_type, runtime in active:
            record = load_runtime_record(bot_type)
            started_at = int(record.get("started_at", int(time.time())) or int(time.time()))
            current = int(record.get("progress_current", 0) or 0)
            total = int(record.get("progress_total", 0) or 0)
            stage = str(record.get("message") or record.get("stage") or "Running")
            summary, pause_col, stop_col, open_col = st.columns([3.8, 0.75, 0.75, 0.9])
            with summary:
                st.markdown(f"**{labels[bot_type]}** · {stage}")
                detail = f"Elapsed {format_elapsed(int(time.time()) - started_at)} · ETA {estimate_remaining(record)}"
                if total:
                    detail += f" · {current}/{total}"
                    st.progress(min(1.0, current / max(1, total)))
                st.caption(detail)
            pause_label = "Resume" if runtime.get("paused") else "Pause"
            if pause_col.button(
                pause_label,
                key=f"dock_pause_{bot_type}",
                disabled=not is_process_running(runtime.get("process")),
                width="stretch",
            ):
                changed = resume_bot(runtime.get("process")) if runtime.get("paused") else pause_bot(runtime.get("process"))
                if changed:
                    runtime["paused"] = not runtime.get("paused", False)
                    st.rerun()
            if stop_col.button("Stop", key=f"dock_stop_{bot_type}", width="stretch"):
                stop_bot(runtime.get("process"))
                runtime["paused"] = False
                st.rerun()
            if open_col.button("Open", key=f"dock_open_{bot_type}", width="stretch"):
                set_page(pages[bot_type])
                st.session_state[f"{pages[bot_type].lower()}_view"] = "Live"
                request_scroll_to_top()
                st.rerun()


def persist_workspace_drafts() -> None:
    drafts = {
        key: st.session_state[key]
        for key in WORKSPACE_DRAFT_KEYS
        if key in st.session_state
        and isinstance(st.session_state[key], (str, int, float, bool))
    }
    encoded = json.dumps(drafts, sort_keys=True, ensure_ascii=False)
    if st.session_state.get("_saved_workspace_drafts") == encoded:
        return
    preferences = load_preferences()
    preferences["workspace_drafts"] = drafts
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
        st.session_state.note_url = str(settings.get("note_url", ""))
        st.session_state.word_art_style_mode = str(settings.get("style", "banner"))
        set_page("Studio Setup")


page_bot_type = runtime_type_for_page(st.session_state.page)
status_label = runtime_status(page_bot_type) if page_bot_type else (
    f"{len(running_bots)} running" if running_bots else "Idle"
)

with st.sidebar:
    st.markdown(
        """
        <div class="mac-traffic">
          <span class="traffic-dot traffic-red">x</span>
          <span class="traffic-dot traffic-yellow">-</span>
          <span class="traffic-dot traffic-green">+</span>
        </div>
        """,
        unsafe_allow_html=True,
    )
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
    for group, pages in NAVIGATION_GROUPS.items():
        st.caption(group)
        for navigation_page in pages:
            if st.button(navigation_page, key=f"nav_{navigation_page}", width="stretch"):
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

render_global_bar(page, status_label)
emit_runtime_notifications()
auto_advance_completed_workspace(page)
route_page = render_workspace_view(page)

if route_page == "Overview":
    render_page_header(
        "What do you want to run?",
        "Choose a workspace, configure the target, and follow the run from one screen.",
        [PRODUCT_MODE, f"Version {PRODUCT_VERSION}", "Local automation"],
    )

    if not st.session_state.onboarding_complete:
        st.markdown("### First-run setup")
        with st.container(border=True):
            st.markdown(
                "Panelyra runs locally and controls a visible browser. Use it only with sites and content you own, administer, or have explicit permission to test."
            )
            onboarding_authorized = st.checkbox(
                "I will use Panelyra only on authorized targets.",
                key="onboarding_authorized",
            )
            if st.button(
                "Complete local setup",
                type="primary",
                disabled=not onboarding_authorized,
                width="stretch",
            ):
                st.session_state.onboarding_complete = True
                st.session_state.authorization_confirmed = True
                save_preferences(
                    {
                        "onboarding_complete": True,
                        "authorization_confirmed": True,
                        "retention_days": int(st.session_state.retention_days),
                    }
                )
                st.rerun()

    run_records = list_run_records(limit=100)
    capture_pages = cached_capture_page_count(str(OCR_DOWNLOAD_DIR))
    recent_failures = sum(
        1
        for record in run_records[:20]
        if str(record.get("status", "")).lower() in {"failed", "error"}
        or bool(str(record.get("stop_reason", "")).strip())
    )
    top_metrics = st.columns(4)
    with top_metrics[0]:
        render_metric_card("Active runs", len(running_bots), "Across all workspaces", idle=not running_bots)
    with top_metrics[1]:
        render_metric_card("Submitted comments", len(submitted_records(history_records)), "Verified local records")
    with top_metrics[2]:
        render_metric_card("Captured pages", capture_pages, "Saved image files")
    with top_metrics[3]:
        render_metric_card("Recent failures", recent_failures, "Across the latest 20 runs")

    st.markdown("### Start a workspace")
    st.divider()
    workflow_cards = st.columns(3)
    with workflow_cards[0]:
        render_panel("Panelyra Engage", "Generate, submit, verify, and review contextual chapter comments.")
        if st.button("Start Comment Testing", key="dashboard_engage", type="primary", width="stretch"):
            set_page("Engage Setup")
            st.rerun()
    with workflow_cards[1]:
        render_panel("Panelyra Capture", "Collect chapter pages, build CBZ archives, and create OCR transcripts.")
        if st.button("Start Chapter Capture", key="dashboard_capture", width="stretch"):
            set_page("Capture Setup")
            st.rerun()
    with workflow_cards[2]:
        render_panel("Panelyra Studio", "Compose Word Art, preview the result, and send it to a configured editor.")
        if st.button("Start Creative Studio", key="dashboard_studio", width="stretch"):
            set_page("Studio Setup")
            st.rerun()

    if run_records:
        recent = run_records[0]
        continue_cols = st.columns([3, 1])
        continue_cols[0].info(
            f"Latest run: {str(recent.get('bot_type', 'automation')).replace('_', ' ').title()} · "
            f"{recent.get('status', 'unknown')} · {recent.get('stage', 'unknown')}"
        )
        if continue_cols[1].button("Continue configuration", width="stretch"):
            restore_run_configuration(recent)
            st.rerun()
    else:
        st.info("No runs yet. Choose a workspace above to create the first one.")

    with st.expander("System readiness", expanded=False):
        health_rows = environment_health()
        st.dataframe(health_rows, width="stretch", hide_index=True)

elif route_page == "Engage Setup":
    comment_runtime = st.session_state.bot_runtimes["comment"]
    apply_reader_defaults(str(st.session_state.get("content_mode", "Auto")))
    selector_profiles = load_selector_profiles()
    if st.session_state.selected_selector_profile not in selector_profiles:
        st.session_state.selected_selector_profile = next(iter(selector_profiles))
    active_selector_values = selector_profiles[st.session_state.selected_selector_profile]
    for field_name, profile_key in (
        ("engage_next_link_selector", "next_link"),
        ("engage_next_page_selector", "next_page"),
        ("engage_page_counter_selector", "page_counter"),
        ("engage_chapter_list_selector", "chapter_list"),
        ("engage_comment_editor_selector", "comment_editor"),
        ("engage_submit_button_selector", "submit"),
        ("engage_thanks_button_selector", "thanks"),
        ("engage_confirmation_selector", "confirmation"),
    ):
        if field_name not in st.session_state:
            st.session_state[field_name] = active_selector_values.get(profile_key, "")
    setup_step = max(1, min(3, int(st.session_state.get("engage_setup_step", 1))))
    st.session_state.engage_setup_step = setup_step
    step_titles = {1: "Target", 2: "Behavior", 3: "Review and start"}
    render_page_header(
        "Engage",
        step_titles[setup_step],
        [f"Step {setup_step} of 3"],
    )

    if setup_step == 1:
        st.markdown("### Choose the target")
        with st.expander("Use a saved configuration", expanded=False):
            presets = load_presets()
            preset_columns = st.columns([3, 1])
            selected_preset = preset_columns[0].selectbox(
                "Saved configuration",
                options=list(presets),
                key="selected_workflow_preset",
                label_visibility="collapsed",
            )
            if preset_columns[1].button("Apply", key="engage_apply_preset", width="stretch"):
                selected_values = presets[selected_preset]
                for key in ("scroll_preset", "chapter_routing", "max_chapters", "use_thanks_marker"):
                    if key in selected_values:
                        st.session_state[key] = (
                            normalized_speed_name(selected_values[key])
                            if key == "scroll_preset"
                            else selected_values[key]
                        )
                st.toast(f"Applied {selected_preset}.")
                st.rerun()
        target_columns = st.columns([3, 1])
        target_columns[0].text_input(
            "Starting URL",
            key="start_url",
            placeholder="https://staging.example.com/chapter-1",
        )
        target_columns[1].number_input(
            "Maximum chapters",
            key="max_chapters",
            min_value=1,
            max_value=10_000,
            step=1,
        )
        st.selectbox(
            "Reader layout",
            options=["auto", "next-link", "infinite-scroll", "paginated", "mixed"],
            key="chapter_routing",
            format_func=lambda value: {
                "auto": "Auto detect",
                "next-link": "Next chapter control",
                "infinite-scroll": "Continuous chapters",
                "paginated": "Next page reader",
                "mixed": "Pages plus next chapter",
            }[value],
        )
        target_errors = preflight_url(str(st.session_state.start_url))
        if target_errors:
            st.caption(target_errors[0])
        if st.button(
            "Continue to behavior",
            type="primary",
            disabled=bool(target_errors),
            width="stretch",
        ):
            st.session_state.engage_setup_step = 2
            st.rerun()

    elif setup_step == 2:
        st.markdown("### Choose how the run behaves")
        behavior_columns = st.columns(2)
        with behavior_columns[0]:
            st.selectbox(
                "Performance mode",
                options=list(SCROLL_PRESETS),
                key="scroll_preset",
            )
            st.caption(SCROLL_PRESETS[normalized_speed_name(st.session_state.scroll_preset)]["description"])
            st.checkbox(
                "Thank and mark completed chapters",
                key="use_thanks_marker",
            )
        with behavior_columns[1]:
            st.radio(
                "Content reader",
                options=list(CONTENT_MODE_OPTIONS),
                key="content_mode",
                on_change=apply_selected_reader_mode_defaults,
            )
            st.caption("Auto is recommended for most targets.")

        with st.expander("Review the first comment", expanded=False):
            st.checkbox(
                "Use reviewed text for chapter one",
                key="review_first_comment",
            )
            st.text_input(
                "Preview focus",
                key="comment_preview_subject",
                placeholder="A character, clue, conflict, or turning point",
            )
            if st.button("Regenerate preview", key="regenerate_comment_preview"):
                st.session_state.comment_preview_text = generate_comment_preview(
                    str(st.session_state.comment_preview_subject)
                )
            st.text_area(
                "First comment",
                key="comment_preview_text",
                height=110,
                disabled=not st.session_state.review_first_comment,
            )

        with st.expander("Optional context", expanded=False):
            st.text_input("Local context file", key="research_file")
            st.text_input(
                "Context endpoint",
                key="research_endpoint",
                placeholder="http://127.0.0.1:8000/research",
            )

        with st.expander("Advanced configuration", expanded=False):
            profile_columns = st.columns([3, 1])
            profile_columns[0].selectbox(
                "Selector profile",
                options=list(selector_profiles),
                key="selected_selector_profile",
            )
            if profile_columns[1].button("Apply profile", width="stretch"):
                values = selector_profiles[st.session_state.selected_selector_profile]
                for field_name, profile_key in (
                    ("engage_next_link_selector", "next_link"),
                    ("engage_next_page_selector", "next_page"),
                    ("engage_page_counter_selector", "page_counter"),
                    ("engage_chapter_list_selector", "chapter_list"),
                    ("engage_comment_editor_selector", "comment_editor"),
                    ("engage_submit_button_selector", "submit"),
                    ("engage_thanks_button_selector", "thanks"),
                    ("engage_confirmation_selector", "confirmation"),
                ):
                    st.session_state[field_name] = values.get(profile_key, "")
                st.rerun()

            selector_columns = st.columns(2)
            selector_columns[0].text_input("Next chapter", key="engage_next_link_selector")
            selector_columns[1].text_input("Next page", key="engage_next_page_selector")
            selector_columns[0].text_input("Page counter", key="engage_page_counter_selector")
            selector_columns[1].text_input("Chapter list", key="engage_chapter_list_selector")
            selector_columns[0].text_input("Comment editor", key="engage_comment_editor_selector")
            selector_columns[1].text_input("Submit control", key="engage_submit_button_selector")
            selector_columns[0].text_input("Thanks control", key="engage_thanks_button_selector")
            selector_columns[1].text_input("Confirmation", key="engage_confirmation_selector")

            st.text_input("Content container", key="content_container_selector")
            st.text_input("Chapter image selector", key="ocr_image_selector")
            st.slider(
                "OCR confidence",
                min_value=0.10,
                max_value=0.95,
                step=0.01,
                key="ocr_confidence",
            )
            if normalized_speed_name(st.session_state.scroll_preset) == "Custom":
                performance_columns = st.columns(2)
                performance_columns[0].number_input(
                    "Minimum scroll pixels", min_value=1, max_value=50_000, key="custom_scroll_min_px"
                )
                performance_columns[1].number_input(
                    "Maximum scroll pixels", min_value=1, max_value=50_000, key="custom_scroll_max_px"
                )
                performance_columns[0].number_input(
                    "Minimum scroll pause", min_value=0.0, max_value=10.0, key="custom_scroll_min_delay"
                )
                performance_columns[1].number_input(
                    "Maximum scroll pause", min_value=0.0, max_value=10.0, key="custom_scroll_max_delay"
                )
                performance_columns[0].number_input(
                    "Minimum typing delay", min_value=0.0, max_value=2.0, key="custom_type_min_delay"
                )
                performance_columns[1].number_input(
                    "Maximum typing delay", min_value=0.0, max_value=2.0, key="custom_type_max_delay"
                )
                performance_columns[0].number_input(
                    "Minimum cooldown", min_value=0.0, max_value=60.0, key="custom_cooldown_min"
                )
                performance_columns[1].number_input(
                    "Maximum cooldown", min_value=0.0, max_value=60.0, key="custom_cooldown_max"
                )
                st.number_input(
                    "Submission verification wait",
                    min_value=0.5,
                    max_value=30.0,
                    key="custom_submit_wait",
                )

        navigation = st.columns(2)
        if navigation[0].button("Back", width="stretch"):
            st.session_state.engage_setup_step = 1
            st.rerun()
        if navigation[1].button("Continue to review", type="primary", width="stretch"):
            st.session_state.engage_setup_step = 3
            st.rerun()

    speed_name = normalized_speed_name(st.session_state.scroll_preset)
    speed = SCROLL_PRESETS[speed_name]
    if speed_name == "Custom":
        scroll_min_px = int(st.session_state.custom_scroll_min_px)
        scroll_max_px = int(st.session_state.custom_scroll_max_px)
        scroll_min_delay = float(st.session_state.custom_scroll_min_delay)
        scroll_max_delay = float(st.session_state.custom_scroll_max_delay)
        type_min_delay = float(st.session_state.custom_type_min_delay)
        type_max_delay = float(st.session_state.custom_type_max_delay)
        cooldown_min_seconds = float(st.session_state.custom_cooldown_min)
        cooldown_max_seconds = float(st.session_state.custom_cooldown_max)
        post_submit_wait_seconds = float(st.session_state.custom_submit_wait)
    else:
        scroll_min_px = int(speed["min_px"])
        scroll_max_px = int(speed["max_px"])
        scroll_min_delay = float(speed["min_delay"])
        scroll_max_delay = float(speed["max_delay"])
        type_min_delay = float(speed["type_min_delay"])
        type_max_delay = float(speed["type_max_delay"])
        cooldown_min_seconds = float(speed["cooldown_min_seconds"])
        cooldown_max_seconds = float(speed["cooldown_max_seconds"])
        post_submit_wait_seconds = float(speed["post_submit_wait_seconds"])
    visible_type_chars = int(speed["visible_type_chars"])
    reuse_loaded_images = bool(speed["reuse_loaded_images"])
    content_mode_cli = CONTENT_MODE_OPTIONS[str(st.session_state.content_mode)]
    default_container_selector, default_image_selector, _ = reader_defaults_for_mode(str(st.session_state.content_mode))
    effective_content_container_selector = str(st.session_state.content_container_selector).strip() or default_container_selector
    effective_ocr_image_selector = str(st.session_state.ocr_image_selector).strip() or default_image_selector

    launch_blockers = preflight_url(str(st.session_state.start_url))
    if not str(st.session_state.engage_comment_editor_selector).strip():
        launch_blockers.append("add a comment editor selector")
    if not str(st.session_state.engage_submit_button_selector).strip():
        launch_blockers.append("add a submit selector")
    if st.session_state.chapter_routing in {"paginated", "mixed"} and not str(
        st.session_state.engage_next_page_selector
    ).strip():
        launch_blockers.append("add a next-page selector")
    if scroll_max_px < scroll_min_px or scroll_max_delay < scroll_min_delay:
        launch_blockers.append("fix the scroll range")
    if type_max_delay < type_min_delay or cooldown_max_seconds < cooldown_min_seconds:
        launch_blockers.append("fix the timing range")

    if setup_step == 3:
        st.markdown("### Review the run")
        estimated_seconds = int(
            int(st.session_state.max_chapters)
            * (12 + (cooldown_min_seconds + cooldown_max_seconds) / 2 + post_submit_wait_seconds)
        )
        review_metrics = st.columns(4)
        review_metrics[0].metric("Target", "Ready" if not preflight_url(str(st.session_state.start_url)) else "Missing")
        review_metrics[1].metric("Layout", str(st.session_state.chapter_routing).replace("-", " ").title())
        review_metrics[2].metric("Chapters", int(st.session_state.max_chapters))
        review_metrics[3].metric("Estimated minimum", f"{max(1, estimated_seconds // 60)} min")
        st.code(str(st.session_state.start_url), language="text")
        st.caption(
            f"{speed_name} mode · {st.session_state.content_mode} reader · "
            f"{'read markers on' if st.session_state.use_thanks_marker else 'read markers off'}"
        )
        if st.session_state.review_first_comment:
            st.text_area(
                "Reviewed first comment",
                value=str(st.session_state.comment_preview_text),
                disabled=True,
                height=90,
            )
        if launch_blockers:
            st.warning("Before starting: " + "; ".join(launch_blockers) + ".")
        else:
            st.success("Configuration is ready.")

        back_column, prepare_column, direct_column = st.columns([0.8, 1.3, 1.3])
        if back_column.button("Back", width="stretch"):
            st.session_state.engage_setup_step = 2
            st.rerun()
        prepare_clicked = prepare_column.button(
            "Open browser and prepare",
            type="primary",
            disabled=running or bool(launch_blockers),
            width="stretch",
        )
        direct_clicked = direct_column.button(
            "Start exact URL",
            disabled=running or bool(launch_blockers),
            width="stretch",
        )

        def launch_engage(wait_for_login: bool) -> None:
            st.session_state.bot_process, st.session_state.log_path, st.session_state.start_signal_path = start_bot(
                str(st.session_state.start_url).strip(),
                int(st.session_state.max_chapters),
                wait_for_login=wait_for_login,
                research_file=str(st.session_state.research_file),
                research_endpoint=str(st.session_state.research_endpoint),
                use_thanks_marker=bool(st.session_state.use_thanks_marker),
                chapter_routing=str(st.session_state.chapter_routing),
                scroll_min_px=scroll_min_px,
                scroll_max_px=scroll_max_px,
                scroll_min_delay=scroll_min_delay,
                scroll_max_delay=scroll_max_delay,
                reflection_every_n_comments=int(st.session_state.reflection_every_n_comments),
                content_mode=content_mode_cli,
                content_container_selector=effective_content_container_selector,
                ocr_image_selector=effective_ocr_image_selector,
                ocr_confidence=float(st.session_state.ocr_confidence),
                type_min_delay=type_min_delay,
                type_max_delay=type_max_delay,
                visible_type_chars=visible_type_chars,
                reuse_loaded_images=reuse_loaded_images,
                cooldown_min_seconds=cooldown_min_seconds,
                cooldown_max_seconds=cooldown_max_seconds,
                post_submit_wait_seconds=post_submit_wait_seconds,
                next_link_selector=str(st.session_state.engage_next_link_selector).strip(),
                next_page_selector=str(st.session_state.engage_next_page_selector).strip(),
                page_counter_selector=str(st.session_state.engage_page_counter_selector).strip(),
                chapter_list_selector=str(st.session_state.engage_chapter_list_selector).strip(),
                comment_editor_selector=str(st.session_state.engage_comment_editor_selector).strip(),
                submit_button_selector=str(st.session_state.engage_submit_button_selector).strip(),
                thanks_button_selector=str(st.session_state.engage_thanks_button_selector).strip(),
                confirmation_selector=str(st.session_state.engage_confirmation_selector).strip(),
                first_comment_override=(
                    str(st.session_state.comment_preview_text)
                    if st.session_state.review_first_comment
                    else ""
                ),
            )
            set_page("Engage Live")
            st.rerun()

        if prepare_clicked:
            launch_engage(True)
        if direct_clicked:
            launch_engage(False)

elif route_page == "Capture Setup":
    download_runtime = st.session_state.bot_runtimes["download"]
    apply_ocr_download_defaults()
    capture_profiles = load_selector_profiles()
    if st.session_state.ocr_selector_profile not in capture_profiles:
        st.session_state.ocr_selector_profile = next(iter(capture_profiles))
    capture_profile_values = capture_profiles[st.session_state.ocr_selector_profile]
    for field_name, profile_key in (
        ("ocr_next_chapter_selector", "next_link"),
        ("ocr_next_page_selector", "next_page"),
        ("ocr_page_counter_selector", "page_counter"),
        ("ocr_chapter_list_selector", "chapter_list"),
    ):
        if field_name not in st.session_state:
            st.session_state[field_name] = capture_profile_values.get(profile_key, "")
    capture_step = max(1, min(3, int(st.session_state.get("capture_setup_step", 1))))
    st.session_state.capture_setup_step = capture_step
    capture_step_titles = {1: "Source", 2: "Output", 3: "Review and start"}
    render_page_header("Capture", capture_step_titles[capture_step], [f"Step {capture_step} of 3"])

    if capture_step == 1:
        st.markdown("### Choose the source")
        source_columns = st.columns([3, 1])
        source_columns[0].text_input(
            "Starting URL",
            key="ocr_start_url",
            placeholder="https://staging.example.com/chapter-1",
        )
        source_columns[1].number_input(
            "Maximum chapters",
            min_value=1,
            max_value=20,
            step=1,
            key="ocr_max_chapters",
        )
        st.selectbox(
            "Reader layout",
            options=["auto", "continuous", "next-chapter", "paginated", "mixed"],
            key="ocr_reader_layout",
            format_func=lambda value: {
                "auto": "Auto detect",
                "continuous": "Continuous vertical pages",
                "next-chapter": "Next chapter control",
                "paginated": "Next page reader",
                "mixed": "Pages plus next chapter",
            }[value],
        )
        source_errors = preflight_url(str(st.session_state.ocr_start_url))
        if st.button(
            "Continue to output",
            type="primary",
            disabled=bool(source_errors),
            width="stretch",
        ):
            st.session_state.capture_setup_step = 2
            st.rerun()

    elif capture_step == 2:
        st.markdown("### Choose the output")
        output_columns = st.columns(2)
        with output_columns[0]:
            st.checkbox("Save chapter images", key="ocr_save_images")
            st.checkbox("Create CBZ archive", key="ocr_create_archive")
            st.checkbox("Create OCR transcript", key="ocr_save_text")
            st.text_input(
                "Custom output folder",
                key="ocr_custom_output_dir",
                placeholder="Default: Workspace/manhwa",
            )
            output_path = Path(
                str(st.session_state.ocr_custom_output_dir).strip() or str(OCR_DOWNLOAD_DIR)
            ).expanduser()
            st.caption(f"Output: {display_path(output_path)}")
        with output_columns[1]:
            st.selectbox(
                "Capture performance",
                options=["Balanced", "Rapid", "Reliable"],
                key="ocr_performance_profile",
                help="Balanced is recommended. Rapid reduces completeness waits and uses more parallel downloads; Reliable waits longer for difficult lazy-loaded readers.",
            )
            st.slider(
                "OCR confidence",
                min_value=0.05,
                max_value=0.95,
                step=0.05,
                key="ocr_download_confidence",
            )
            st.caption("Each completed chapter includes metadata and discovered image URLs.")

        with st.expander("Advanced configuration", expanded=False):
            profile_columns = st.columns([3, 1])
            profile_columns[0].selectbox(
                "Selector profile",
                options=list(capture_profiles),
                key="ocr_selector_profile",
            )
            if profile_columns[1].button("Apply profile", key="capture_apply_profile", width="stretch"):
                values = capture_profiles[st.session_state.ocr_selector_profile]
                st.session_state.ocr_next_chapter_selector = values.get("next_link", "")
                st.session_state.ocr_next_page_selector = values.get("next_page", "")
                st.session_state.ocr_page_counter_selector = values.get("page_counter", "")
                st.session_state.ocr_chapter_list_selector = values.get("chapter_list", "")
                st.rerun()
            advanced_columns = st.columns(2)
            advanced_columns[0].text_input("Chapter container", key="ocr_container_selector")
            advanced_columns[1].text_input("Chapter images", key="ocr_download_image_selector")
            advanced_columns[0].text_input("Next chapter", key="ocr_next_chapter_selector")
            advanced_columns[1].text_input("Next page", key="ocr_next_page_selector")
            advanced_columns[0].text_input("Page counter", key="ocr_page_counter_selector")
            advanced_columns[1].text_input("Chapter list", key="ocr_chapter_list_selector")
            advanced_columns[0].number_input(
                "Minimum scroll delay",
                min_value=0.0,
                max_value=5.0,
                step=0.01,
                key="ocr_scroll_min_delay",
            )
            advanced_columns[1].number_input(
                "Maximum scroll delay",
                min_value=0.0,
                max_value=5.0,
                step=0.01,
                key="ocr_scroll_max_delay",
            )
            st.number_input(
                "Image fetch timeout",
                min_value=1.0,
                max_value=120.0,
                step=1.0,
                key="ocr_image_fetch_timeout",
            )
        output_navigation = st.columns(2)
        if output_navigation[0].button("Back", key="capture_output_back", width="stretch"):
            st.session_state.capture_setup_step = 1
            st.rerun()
        if output_navigation[1].button(
            "Continue to review",
            key="capture_output_next",
            type="primary",
            width="stretch",
        ):
            st.session_state.capture_setup_step = 3
            st.rerun()

    effective_ocr_output_dir = str(
        Path(str(st.session_state.ocr_custom_output_dir).strip() or str(OCR_DOWNLOAD_DIR)).expanduser()
    )
    effective_ocr_container_selector = (
        str(st.session_state.ocr_container_selector).strip() or MANHWA_CONTAINER_SELECTOR
    )
    effective_ocr_image_selector = (
        str(st.session_state.ocr_download_image_selector).strip() or MANHWA_IMAGE_SELECTOR
    )
    capture_blockers = preflight_url(str(st.session_state.ocr_start_url))
    if float(st.session_state.ocr_scroll_max_delay) < float(st.session_state.ocr_scroll_min_delay):
        capture_blockers.append("fix the scan timing")
    if not str(st.session_state.ocr_next_chapter_selector).strip():
        capture_blockers.append("add a next-chapter selector")
    if st.session_state.ocr_reader_layout in {"paginated", "mixed"} and not str(
        st.session_state.ocr_next_page_selector
    ).strip():
        capture_blockers.append("add a next-page selector")

    if capture_step == 3:
        st.markdown("### Review the capture")
        capture_metrics = st.columns(4)
        capture_metrics[0].metric("Target", "Ready" if not preflight_url(str(st.session_state.ocr_start_url)) else "Missing")
        capture_metrics[1].metric("Layout", str(st.session_state.ocr_reader_layout).replace("-", " ").title())
        capture_metrics[2].metric("Chapters", int(st.session_state.ocr_max_chapters))
        selected_outputs = [
            label
            for enabled, label in (
                (st.session_state.ocr_save_images, "Images"),
                (st.session_state.ocr_create_archive, "CBZ"),
                (st.session_state.ocr_save_text, "OCR"),
            )
            if enabled
        ]
        capture_metrics[3].metric("Outputs", ", ".join(selected_outputs) or "Metadata")
        st.code(str(st.session_state.ocr_start_url), language="text")
        st.caption(f"Output: {display_path(effective_ocr_output_dir)}")
        st.caption(
            "Capture uses a dedicated persistent Chrome profile. Select Review in Browser "
            "when authentication or manual reader verification is required."
        )
        if capture_blockers:
            st.warning("Before starting: " + "; ".join(capture_blockers) + ".")
        else:
            st.success("Configuration is ready.")
        review_navigation = st.columns([1, 2, 2])
        if review_navigation[0].button("Back", key="capture_review_back", width="stretch"):
            st.session_state.capture_setup_step = 2
            st.rerun()

        def launch_capture(wait_for_inspection: bool) -> None:
            st.session_state.bot_process, st.session_state.log_path, st.session_state.start_signal_path = start_manhwa_ocr_bot(
                str(st.session_state.ocr_start_url).strip(),
                int(st.session_state.ocr_max_chapters),
                wait_for_inspection,
                effective_ocr_output_dir,
                effective_ocr_container_selector,
                effective_ocr_image_selector,
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
            set_page("Capture Live")
            st.rerun()

        inspect_clicked = review_navigation[1].button(
            "Review in Browser",
            key="capture_prepare",
            disabled=running or bool(capture_blockers),
            width="stretch",
        )
        direct_clicked = review_navigation[2].button(
            "Start Capture",
            key="capture_start",
            type="primary",
            disabled=running or bool(capture_blockers),
            width="stretch",
        )
        if inspect_clicked:
            launch_capture(True)
        if direct_clicked:
            launch_capture(False)

elif route_page == "Studio Setup":
    word_art_runtime = st.session_state.bot_runtimes["word_art"]
    apply_note_art_selector_defaults()
    studio_step = max(1, min(3, int(st.session_state.get("studio_setup_step", 1))))
    st.session_state.studio_setup_step = studio_step
    studio_step_titles = {1: "Target", 2: "Design", 3: "Review and start"}
    render_page_header("Studio", studio_step_titles[studio_step], [f"Step {studio_step} of 3"])

    if studio_step == 1:
        st.markdown("### Choose the editor and image")
        st.text_input(
            "Editor URL",
            key="note_url",
            placeholder="https://staging.example.com/notes/new",
        )
        uploaded_image = st.file_uploader(
            "Source image",
            type=["png", "jpg", "jpeg", "webp", "gif"],
            key="studio_upload",
        )
        if uploaded_image is not None:
            safe_upload_name = re.sub(r"[^A-Za-z0-9._-]+", "_", uploaded_image.name).strip("._") or "upload.png"
            saved_image_path = UPLOAD_DIR / safe_upload_name
            saved_image_path.write_bytes(uploaded_image.getbuffer())
            st.session_state.studio_image_path = str(saved_image_path)
        image_path = Path(str(st.session_state.studio_image_path)) if st.session_state.studio_image_path else None
        if image_path and image_path.exists():
            st.image(str(image_path), width=420)
        source_errors = preflight_url(str(st.session_state.note_url))
        if image_path is None or not image_path.exists():
            source_errors.append("Choose an image.")
        if st.button(
            "Continue to design",
            type="primary",
            disabled=bool(source_errors),
            width="stretch",
        ):
            st.session_state.studio_setup_step = 2
            st.rerun()

    elif studio_step == 2:
        st.markdown("### Design the composition")
        design_columns = st.columns([1, 1.2])
        with design_columns[0]:
            st.text_input("Main text", key="word_art_text")
            st.text_area("Caption", key="word_art_caption", height=120)
            st.radio(
                "Style",
                options=["banner", "box", "stack"],
                key="word_art_style_mode",
                horizontal=True,
            )
            color_columns = st.columns(2)
            color_columns[0].color_picker("Text", key="word_art_text_color")
            color_columns[1].color_picker("Panel", key="word_art_panel_color")
            st.radio(
                "Text alignment",
                options=["left", "center", "right"],
                key="word_art_text_align",
                horizontal=True,
            )
            st.radio(
                "Panel placement",
                options=["top", "bottom"],
                key="word_art_panel_placement",
                horizontal=True,
            )
            st.checkbox(
                "Leave the editor open for review",
                key="note_skip_submit",
            )
        with design_columns[1]:
            image_path = Path(str(st.session_state.studio_image_path)) if st.session_state.studio_image_path else None
            if image_path and image_path.exists() and str(st.session_state.word_art_text).strip():
                try:
                    from note_art_bot import render_word_art_image

                    preview_path = render_word_art_image(
                        image_path,
                        str(st.session_state.word_art_text),
                        str(st.session_state.word_art_style_mode),
                        text_color=str(st.session_state.word_art_text_color),
                        panel_color=str(st.session_state.word_art_panel_color),
                        text_align=str(st.session_state.word_art_text_align),
                        panel_placement=str(st.session_state.word_art_panel_placement),
                    )
                    st.session_state.studio_preview_path = str(preview_path)
                    st.image(str(preview_path), caption="Preview", width="stretch")
                except (OSError, ValueError) as exc:
                    st.error(f"Preview could not be rendered: {exc}")
            else:
                st.info("Enter the main text to render the preview.")

        with st.expander("Advanced configuration", expanded=False):
            if st.button("Restore default selectors", key="studio_restore_selectors", width="stretch"):
                apply_note_art_selector_defaults(force=True)
                st.rerun()
            selector_columns = st.columns(2)
            selector_columns[0].text_input("File input", key="note_upload_input_selector")
            selector_columns[1].text_input("Upload trigger", key="note_upload_trigger_selector")
            selector_columns[0].text_input("Editor", key="note_editor_selector")
            selector_columns[1].text_input("Save or publish", key="note_submit_selector")

        design_navigation = st.columns(2)
        if design_navigation[0].button("Back", key="studio_design_back", width="stretch"):
            st.session_state.studio_setup_step = 1
            st.rerun()
        if design_navigation[1].button(
            "Continue to review",
            key="studio_design_next",
            type="primary",
            disabled=not bool(str(st.session_state.word_art_text).strip()),
            width="stretch",
        ):
            st.session_state.studio_setup_step = 3
            st.rerun()

    image_path = Path(str(st.session_state.studio_image_path)) if st.session_state.studio_image_path else None
    studio_blockers = preflight_url(str(st.session_state.note_url))
    if image_path is None or not image_path.exists():
        studio_blockers.append("choose an image")
    if not str(st.session_state.word_art_text).strip():
        studio_blockers.append("add the main text")
    if studio_step == 3:
        st.markdown("### Review the composition")
        preview_path = Path(str(st.session_state.studio_preview_path)) if st.session_state.studio_preview_path else None
        review_columns = st.columns([1.2, 1])
        with review_columns[0]:
            if preview_path and preview_path.exists():
                st.image(str(preview_path), width="stretch")
            else:
                st.info("Return to Design to create a preview.")
        with review_columns[1]:
            st.metric("Style", str(st.session_state.word_art_style_mode).title())
            st.metric("Editor action", "Review only" if st.session_state.note_skip_submit else "Save or publish")
            st.code(str(st.session_state.note_url), language="text")
        if studio_blockers:
            st.warning("Before starting: " + "; ".join(studio_blockers) + ".")
        else:
            st.success("Composition is ready.")

        review_navigation = st.columns([0.8, 1.3, 1.3])
        if review_navigation[0].button("Back", key="studio_review_back", width="stretch"):
            st.session_state.studio_setup_step = 2
            st.rerun()
        prepare_clicked = review_navigation[1].button(
            "Open editor and prepare",
            type="primary",
            disabled=running or bool(studio_blockers),
            width="stretch",
        )
        direct_clicked = review_navigation[2].button(
            "Start exact URL",
            disabled=running or bool(studio_blockers),
            width="stretch",
        )

        def launch_studio(wait_for_setup: bool) -> None:
            assert image_path is not None
            st.session_state.bot_process, st.session_state.log_path, st.session_state.start_signal_path = start_note_art_bot(
                str(st.session_state.note_url).strip(),
                image_path,
                str(st.session_state.word_art_text).strip(),
                str(st.session_state.word_art_caption).strip(),
                str(st.session_state.word_art_style_mode),
                str(st.session_state.word_art_text_color),
                str(st.session_state.word_art_panel_color),
                str(st.session_state.word_art_text_align),
                str(st.session_state.word_art_panel_placement),
                wait_for_setup=wait_for_setup,
                upload_input_selector=str(st.session_state.note_upload_input_selector).strip(),
                upload_trigger_selector=str(st.session_state.note_upload_trigger_selector).strip(),
                editor_selector=str(st.session_state.note_editor_selector).strip(),
                submit_selector=str(st.session_state.note_submit_selector).strip(),
                skip_submit=bool(st.session_state.note_skip_submit),
            )
            set_page("Studio Live")
            st.rerun()

        if prepare_clicked:
            launch_studio(True)
        if direct_clicked:
            launch_studio(False)

elif route_page == "Engage Live":
    runtime = st.session_state.bot_runtimes["comment"]
    log_path = runtime.get("log_path")
    log_text = read_log(Path(log_path), int(st.session_state.console_max_chars)) if log_path else "No comment run started yet."
    progress = parse_bot_progress(log_text)
    record = load_runtime_record("comment")
    render_page_header("Engage", "Live run", ["Comment testing"])
    metrics = st.columns(4)
    metrics[0].metric("State", runtime_status("comment"))
    metrics[1].metric("Chapter", f"{progress['current']} / {progress['target']}" if progress["target"] else "Waiting")
    metrics[2].metric("Submitted", progress["submitted"])
    metrics[3].metric("Failures", progress["failures"])
    completion = min(1.0, float(progress["current"]) / max(1, int(progress["target"])))
    st.progress(completion, text=f"Chapter progress: {progress['current']} of {progress['target'] or 'pending'}")
    st.info(str(record.get("message") or record.get("stage") or "Waiting for a run to start."))
    if progress["stop_reason"]:
        st.warning(f"Last stop reason: {progress['stop_reason']}")
    actions = st.columns(3)
    if actions[0].button(
        "Continue in browser" if runtime.get("signal_path") else "Waiting for browser",
        type="primary",
        disabled=not running or runtime.get("signal_path") is None,
        width="stretch",
    ):
        Path(runtime["signal_path"]).write_text("start\n", encoding="utf-8")
        runtime["signal_path"] = None
        st.toast("Comment workflow started.")
        st.rerun()
    pause_label = "Resume" if runtime.get("paused") else "Pause"
    if actions[1].button(pause_label, disabled=not running, width="stretch"):
        changed = resume_bot(runtime.get("process")) if runtime.get("paused") else pause_bot(runtime.get("process"))
        if changed:
            runtime["paused"] = not runtime.get("paused", False)
            st.rerun()
    if actions[2].button("Stop", disabled=not running, width="stretch"):
        stop_bot(runtime.get("process"))
        runtime["paused"] = False
        st.rerun()
    if not running and not log_path:
        if st.button("Configure a run", width="stretch"):
            set_page("Engage Setup")
            st.rerun()
    with st.expander("Diagnostics", expanded=bool(progress["stop_reason"])):
        if progress["stop_reason"] and st.button("Return to review and retry", width="stretch"):
            st.session_state.engage_setup_step = 3
            set_page("Engage Setup")
            st.rerun()
        st.code(log_text, language="text")

elif route_page == "Capture Live":
    runtime = st.session_state.bot_runtimes["download"]
    log_path = runtime.get("log_path")
    log_text = read_log(Path(log_path), int(st.session_state.console_max_chars)) if log_path else "No download run started yet."
    progress = parse_ocr_progress(log_text)
    record = load_runtime_record("download")
    render_page_header("Capture", "Live run", ["Chapter capture"])
    saved_matches = re.findall(r"Saved\s+(\d+)/(\d+)\s+raw image", log_text, re.IGNORECASE)
    saved_label = f"{saved_matches[-1][0]} / {saved_matches[-1][1]}" if saved_matches else "0"
    reader_matches = re.findall(r"Reader media progress:\s*(\d+)/(\d+)", log_text, re.IGNORECASE)
    reader_label = f"{reader_matches[-1][0]} / {reader_matches[-1][1]}" if reader_matches else "Not detected"
    metrics = st.columns(4)
    metrics[0].metric("State", runtime_status("download"))
    metrics[1].metric("Chapter", f"{progress['current']} / {progress['target']}" if progress["target"] else "Waiting")
    metrics[2].metric("Reader page", reader_label)
    metrics[3].metric("Images", saved_label)
    completion = min(1.0, float(progress["current"]) / max(1, int(progress["target"])))
    st.progress(completion, text=f"Chapter progress: {progress['current']} of {progress['target'] or 'pending'}")
    st.info(str(record.get("message") or record.get("stage") or "Waiting for a run to start."))
    actions = st.columns(4)
    if actions[0].button(
        "Begin Capture" if runtime.get("signal_path") else "Browser Review Complete",
        type="primary",
        disabled=not running or runtime.get("signal_path") is None,
        width="stretch",
    ):
        Path(runtime["signal_path"]).write_text("start\n", encoding="utf-8")
        runtime["signal_path"] = None
        st.session_state.start_signal_path = None
        st.toast("Capture initiated from the active browser page.")
        st.rerun()
    pause_label = "Resume" if runtime.get("paused") else "Pause"
    if actions[1].button(pause_label, disabled=not running, width="stretch"):
        changed = resume_bot(runtime.get("process")) if runtime.get("paused") else pause_bot(runtime.get("process"))
        if changed:
            runtime["paused"] = not runtime.get("paused", False)
            st.rerun()
    if actions[2].button("Stop", disabled=not running, width="stretch"):
        stop_bot(runtime.get("process"))
        runtime["paused"] = False
        st.rerun()
    if actions[3].button("Open output", width="stretch"):
        output_path = Path(st.session_state.ocr_custom_output_dir.strip() or OCR_DOWNLOAD_DIR)
        output_path.mkdir(parents=True, exist_ok=True)
        subprocess.run(["open", str(output_path)], check=False)
    if not running and not log_path:
        if st.button("Configure a capture", width="stretch"):
            set_page("Capture Setup")
            st.rerun()
    with st.expander("Diagnostics", expanded=bool(progress["failures"])):
        if progress["failures"] and st.button("Return to review and retry", key="capture_retry", width="stretch"):
            st.session_state.capture_setup_step = 3
            set_page("Capture Setup")
            st.rerun()
        st.code(log_text, language="text")

elif route_page == "Studio Live":
    runtime = st.session_state.bot_runtimes["word_art"]
    log_path = runtime.get("log_path")
    log_text = read_log(Path(log_path), int(st.session_state.console_max_chars)) if log_path else "No Word Art run started yet."
    progress = parse_word_art_progress(log_text)
    record = load_runtime_record("word_art")
    render_page_header("Studio", "Live run", ["Editor automation"])
    metrics = st.columns(4)
    metrics[0].metric("State", runtime_status("word_art"))
    metrics[1].metric("Stages", f"{progress['current']} / {progress['target']}")
    metrics[2].metric("Upload", "Done" if "WORD_ART_UPLOAD_VERIFIED" in log_text else "Waiting")
    metrics[3].metric("Save", "Done" if "WORD_ART_SAVE_VERIFIED" in log_text else "Waiting")
    st.progress(
        min(1.0, float(progress["current"]) / max(1, int(progress["target"]))),
        text=f"Stage progress: {progress['current']} of {progress['target']}",
    )
    previews = sorted(UPLOAD_DIR.glob("*-word-art.png"), key=lambda item: item.stat().st_mtime, reverse=True)
    if previews:
        st.image(str(previews[0]), caption="Latest rendered Word Art", width=480)
    st.info(str(record.get("message") or record.get("stage") or "Waiting for a run to start."))
    actions = st.columns(3)
    if actions[0].button(
        "Continue in browser" if runtime.get("signal_path") else "Waiting for browser",
        type="primary",
        disabled=not running or runtime.get("signal_path") is None,
        width="stretch",
    ):
        Path(runtime["signal_path"]).write_text("start\n", encoding="utf-8")
        runtime["signal_path"] = None
        st.rerun()
    pause_label = "Resume" if runtime.get("paused") else "Pause"
    if actions[1].button(pause_label, disabled=not running, width="stretch"):
        changed = resume_bot(runtime.get("process")) if runtime.get("paused") else pause_bot(runtime.get("process"))
        if changed:
            runtime["paused"] = not runtime.get("paused", False)
            st.rerun()
    if actions[2].button("Stop", disabled=not running, width="stretch"):
        stop_bot(runtime.get("process"))
        runtime["paused"] = False
        st.rerun()
    if not running and not log_path:
        if st.button("Configure a composition", width="stretch"):
            set_page("Studio Setup")
            st.rerun()
    with st.expander("Diagnostics", expanded=False):
        st.code(log_text, language="text")

elif route_page == "Reader":
    render_page_header(
        "Reader",
        "Read locally collected chapters in their original continuous vertical format.",
        ["Vertical reading", "Original and clean editions", "Local files"],
    )
    catalog = reader_catalog()
    if not catalog:
        st.info("No chapter images are available yet. Run Capture first, then return here to read them.")
    else:
        series_options = sorted(catalog, key=lambda key: str(catalog[key]["title"]).lower())
        if st.session_state.reader_series not in series_options:
            st.session_state.reader_series = series_options[0]
        selected_series = st.selectbox(
            "Series",
            options=series_options,
            key="reader_series",
            format_func=lambda key: str(catalog[key]["title"]),
        )
        selected_entry = catalog[selected_series]
        editions = selected_entry["editions"]
        edition_options = [edition for edition in ("Original", "Clean") if edition in editions]
        if st.session_state.reader_edition not in edition_options:
            st.session_state.reader_edition = edition_options[0]
        st.radio(
            "Edition",
            options=edition_options,
            key="reader_edition",
            horizontal=True,
            help="Clean removes verified promotional bands while preserving the original source files.",
        )
        chapters = editions[st.session_state.reader_edition]
        chapter_options = sorted(chapters, key=lambda value: (0, int(value)) if value.isdigit() else (1, value))
        if st.session_state.reader_chapter not in chapter_options:
            st.session_state.reader_chapter = chapter_options[0]

        navigation = st.columns([1, 2, 1])
        current_index = chapter_options.index(st.session_state.reader_chapter)
        if navigation[0].button("Previous", disabled=current_index == 0, width="stretch"):
            st.session_state.reader_chapter = chapter_options[current_index - 1]
            request_scroll_to_top()
            st.rerun()
        navigation[1].selectbox(
            "Chapter",
            options=chapter_options,
            key="reader_chapter",
            format_func=lambda value: f"Chapter {value}",
        )
        if navigation[2].button("Next", disabled=current_index == len(chapter_options) - 1, width="stretch"):
            st.session_state.reader_chapter = chapter_options[current_index + 1]
            request_scroll_to_top()
            st.rerun()

        chapter = chapters[st.session_state.reader_chapter]
        images = chapter["images"]
        st.caption(
            f"{st.session_state.reader_edition} edition · Chapter {st.session_state.reader_chapter} · {len(images)} image(s)"
        )
        reader_url = local_reader_service().reader_url(
            st.session_state.reader_edition,
            selected_series,
            st.session_state.reader_chapter,
        )
        st.link_button("Open immersive reader", reader_url, type="primary", width="stretch")
        st.caption("Opens a local, distraction-free reader with resume position, width modes, keyboard controls, and zero panel spacing.")

elif route_page == "Capture Library":
    render_page_header(
        "Capture Library",
        "Browse downloaded chapters, real image counts, CBZ archives, OCR output, and storage locations.",
        ["Chapter images", "CBZ archives", "OCR files"],
    )
    output_roots = {OCR_DOWNLOAD_DIR}
    if st.session_state.ocr_custom_output_dir.strip():
        output_roots.add(Path(st.session_state.ocr_custom_output_dir).expanduser())
    metadata_files = sorted(
        (path for root in output_roots if root.exists() for path in root.rglob("metadata.json")),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    if not metadata_files:
        st.info("No downloaded chapters were found. Start from Capture Setup.")
    else:
        library_rows = []
        for metadata_file in metadata_files:
            try:
                metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            images_dir = Path(str(metadata.get("images_dir") or metadata_file.parent / "images"))
            actual_images = len([
                item for item in images_dir.glob("*")
                if item.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".gif"}
            ]) if images_dir.exists() else 0
            archive = str(metadata.get("archive_file") or metadata_file.parent / "chapter-pages.cbz")
            library_rows.append(
                {
                    "Chapter": metadata.get("title", metadata_file.parent.name),
                    "Images": actual_images,
                    "OCR characters": metadata.get("character_count", 0),
                    "Images folder": display_path(images_dir),
                    "CBZ": display_path(Path(archive)) if Path(archive).exists() else "not created",
                    "_images_dir": str(images_dir),
                }
            )
        st.dataframe(
            [{key: value for key, value in row.items() if not key.startswith("_")} for row in library_rows],
            width="stretch",
            hide_index=True,
        )
        latest_images_dir = Path(library_rows[0]["_images_dir"]) if library_rows else OCR_DOWNLOAD_DIR
        actions = st.columns(3)
        if actions[0].button("Open Latest Images", width="stretch"):
            subprocess.run(["open", str(latest_images_dir)], check=False)
        if actions[1].button("Open Download Folder", width="stretch"):
            subprocess.run(["open", str(next(iter(output_roots)))], check=False)
        if actions[2].button("New Download", width="stretch"):
            set_page("Capture Setup")
            st.rerun()

elif route_page == "Studio Library":
    render_page_header(
        "Studio Library",
        "Review generated visual compositions independently from comment and download data.",
        ["Rendered PNGs", "Source uploads", "Note assets"],
    )
    generated_images = sorted(UPLOAD_DIR.glob("*-word-art.png"), key=lambda item: item.stat().st_mtime, reverse=True)
    if not generated_images:
        st.info("No rendered Word Art images were found. Start from Studio Setup.")
    else:
        for row_start in range(0, min(len(generated_images), 18), 3):
            columns = st.columns(3)
            for column, image_path in zip(columns, generated_images[row_start:row_start + 3]):
                with column:
                    st.image(str(image_path), width="stretch")
                    st.caption(image_path.name)
        if st.button("Open Word Art Files", width="stretch"):
            subprocess.run(["open", str(UPLOAD_DIR)], check=False)

elif route_page == "Assistant":
    context = build_copilot_context(history_records)
    render_page_header(
        "Panelyra Copilot",
        "Grounded help, run diagnostics, workflow configuration, and user support.",
        ["Local evidence", "Controlled actions", "Multi-turn context"],
    )
    metrics = st.columns(4)
    metrics[0].metric("Current page", context.current_page)
    metrics[1].metric(
        "Browser",
        "Busy" if context.browser_owner else (
            "Inspected" if context.browser_snapshot.get("available") else "Available"
        ),
    )
    metrics[2].metric(
        "Active runs",
        sum(1 for run in context.runs.values() if run.status in {"running", "starting"}),
    )
    metrics[3].metric(
        "Open tickets",
        sum(1 for ticket in context.tickets if str(ticket.get("status", "open")) == "open"),
    )
    failed_runs = [
        workspace.title()
        for workspace, run in context.runs.items()
        if run.status == "failed"
    ]
    if context.browser_owner:
        st.warning(
            f"Chrome is currently owned by {context.browser_owner.get('bot_type', 'another Panelyra workflow')}. "
            "Diagnose or wait for that workflow before preparing another browser run."
        )
    elif failed_runs:
        st.info(
            f"Recent failed workspace: {', '.join(failed_runs)}. Select Diagnose mode to inspect its evidence and prepare a retry."
        )

    run_rows = [
        {
            "Workspace": workspace.title(),
            "Status": run.status,
            "Stage": run.stage or "not started",
            "Progress": (
                f"{run.progress_current}/{run.progress_total}"
                if run.progress_total
                else "-"
            ),
            "Findings": len(run.events),
        }
        for workspace, run in context.runs.items()
    ]
    with st.expander("Live context", expanded=False):
        st.dataframe(run_rows, width="stretch", hide_index=True)
        if context.browser_snapshot:
            snapshot = context.browser_snapshot
            st.markdown(
                f"**Browser tab:** {snapshot.get('title', 'Unavailable')}  \n"
                f"**URL:** `{snapshot.get('url', '')}`"
            )
            screenshot_path = Path(str(snapshot.get("screenshot_path", "")))
            if screenshot_path.is_file():
                st.image(str(screenshot_path), caption="Latest local browser inspection")
            with st.expander("DOM summary", expanded=False):
                st.json(
                    {
                        "text_excerpt": str(snapshot.get("text_excerpt", ""))[:2_000],
                        "buttons": snapshot.get("buttons", []),
                        "inputs": snapshot.get("inputs", []),
                    }
                )

    toolbar = st.columns([1, 1, 2])
    if toolbar[0].button("Clear conversation", width="stretch"):
        st.session_state.assistant_messages = []
        st.rerun()
    if toolbar[1].button("Inspect Chrome", width="stretch"):
        st.session_state.assistant_action_notice = execute_copilot_action(
            {
                "kind": "inspect_browser",
                "payload": {"include_screenshot": True},
            }
        )
        st.rerun()
    toolbar[2].caption(
        "The copilot can fill forms and prepare retries, but it never starts, stops, submits, deletes, or publishes without a separate user action."
    )
    render_copilot_chat(history_records, key_prefix="assistant_page", history_limit=30)

elif route_page == "Run History":
    render_page_header(
        "Run History",
        "Search, inspect, export, and retry durable records from every Panelyra workspace.",
        ["Searchable records", "JSON export", "Retry setup"],
    )
    records = list_run_records(limit=100)
    filter_columns = st.columns([2, 1, 1])
    history_search = filter_columns[0].text_input(
        "Search",
        key="run_history_search",
        placeholder="Run ID, stage, URL, or stop reason",
    ).strip().lower()
    bot_filter = filter_columns[1].selectbox(
        "Workspace",
        options=["All", "Engage", "Capture", "Studio"],
    )
    status_filter = filter_columns[2].selectbox(
        "Status",
        options=["All", "running", "completed", "partial", "failed", "stopped"],
    )
    bot_values = {"Engage": "comment", "Capture": "download", "Studio": "word_art"}
    if bot_filter != "All":
        records = [record for record in records if record.get("bot_type") == bot_values[bot_filter]]
    if status_filter != "All":
        records = [record for record in records if str(record.get("status", "")).lower() == status_filter]
    if history_search:
        records = [
            record
            for record in records
            if history_search in json.dumps(record, ensure_ascii=False).lower()
        ]
    st.download_button(
        "Export visible run report",
        data=json.dumps(records, indent=2, ensure_ascii=False),
        file_name=f"panelyra-run-report-{time.strftime('%Y%m%d-%H%M%S')}.json",
        mime="application/json",
        disabled=not records,
        width="stretch",
    )
    if not records:
        st.info("No matching runs were found. Start a workspace from the Dashboard.")
    else:
        for record in records:
            with st.expander(
                f"{record.get('bot_type', 'bot').replace('_', ' ').title()} · {record.get('status', 'unknown')} · {record.get('run_id', '')}",
                expanded=False,
            ):
                cols = st.columns(4)
                cols[0].metric("Stage", record.get("stage", "unknown"))
                cols[1].metric("Progress", f"{record.get('progress_current', 0)} / {record.get('progress_total', 0)}")
                cols[2].metric("PID", record.get("pid", 0) or "n/a")
                cols[3].metric("Updated", time.strftime("%Y-%m-%d %H:%M", time.localtime(record.get("updated_at", 0))))
                if record.get("stop_reason"):
                    st.error(str(record["stop_reason"]))
                st.code(redact_private_paths(json.dumps(record.get("settings", {}), indent=2)), language="json")
                if record.get("log_path"):
                    st.caption(display_path(record["log_path"]))
                if st.button(
                    "Retry from saved configuration",
                    key=f"retry_{record.get('run_id', uuid.uuid4().hex)}",
                    width="stretch",
                ):
                    restore_run_configuration(record)
                    st.rerun()

elif route_page == "Engage Insights":
    records = history_records
    render_page_header(
        "Engage Insights",
        "Review generated comment history, balance style variety, and inspect repeated keywords.",
        [f"{len(COMMENT_STYLES)} styles", "History analytics", "Research source tracking"],
    )
    st.markdown("### JSONL Metrics")
    st.divider()
    with st.container(border=True):
        render_jsonl_metric_grid(records, columns=4)
        st.info(style_recommendation(records))

    if records:
        style_counts = Counter(str(record.get("style", "unknown")) for record in records)
        research_counts = Counter(str(record.get("research_source", "chapter-only") or "chapter-only") for record in records)
        keyword_counts = Counter(str(record.get("keyword", "")).lower() for record in records if str(record.get("keyword", "")).strip())
        st.markdown("### Analysis Charts")
        st.divider()
        with st.container(border=True):
            chart_cols = st.columns(3)
            chart_cols[0].bar_chart([{"style": style, "count": count} for style, count in style_counts.items()], x="style", y="count")
            chart_cols[1].bar_chart([{"source": source, "count": count} for source, count in research_counts.items()], x="source", y="count")
            chart_cols[2].bar_chart(
                [{"keyword": keyword, "count": count} for keyword, count in keyword_counts.most_common(int(st.session_state.top_keywords))],
                x="keyword",
                y="count",
            )

        recent_rows = [
            {
                "chapter": record.get("chapter_index"),
                "style": record.get("style"),
                "keyword": record.get("keyword"),
                "emoji": "yes" if record.get("emoji_used") else "no",
                "reflection": "yes" if record.get("reflection_used") else "no",
                "research": record.get("research_source") or "chapter-only",
                "comment": record.get("comment"),
            }
            for record in records[-int(st.session_state.history_rows):]
        ]
        st.markdown("### Recent Records")
        st.divider()
        with st.container(border=True):
            st.dataframe(recent_rows, width="stretch", hide_index=True)
    else:
        render_panel("No History Yet", f"History will appear after a run writes records to {HISTORY_FILE.name}.")

elif route_page == "Storage":
    render_page_header(
        "Storage",
        "Inspect generated artifacts, apply retention policies, and preserve protected content.",
        ["Usage report", "Guarded cleanup", "Legacy compatibility"],
    )
    report = storage_report()
    total_bytes = sum(int(item["bytes"]) for item in report)
    total_files = sum(int(item["files"]) for item in report)
    storage_metrics = st.columns(4)
    storage_metrics[0].metric("Managed storage", format_bytes(total_bytes))
    storage_metrics[1].metric("Managed files", total_files)
    storage_metrics[2].metric("Retention", f"{int(st.session_state.retention_days)} days")
    storage_metrics[3].metric("Rollback snapshot", "Available")

    st.markdown("### Storage Areas")
    st.dataframe(
        [
            {
                "Area": item["label"],
                "Size": item["size"],
                "Files": item["files"],
                "Automatic cleanup": "Allowed" if item["clearable"] else "Protected",
                "Path": item["path"],
            }
            for item in report
        ],
        width="stretch",
        hide_index=True,
    )

    with st.container(border=True):
        st.markdown("#### Retention Policy")
        retention_days = st.number_input(
            "Keep generated logs and records for at least",
            min_value=1,
            max_value=3650,
            step=1,
            key="retention_days",
            help="Cleanup removes only files at least this old from an explicitly selected generated area.",
        )
        cleanup_options = {
            str(item["label"]): str(item["key"])
            for item in report
            if bool(item["clearable"])
        }
        cleanup_label = st.selectbox("Generated area", options=list(cleanup_options))
        cleanup_confirmation = st.text_input(
            "Type CLEAN to confirm",
            key="storage_cleanup_confirmation",
            placeholder="CLEAN",
        )
        if st.button(
            "Apply retention cleanup",
            disabled=cleanup_confirmation != "CLEAN",
            width="stretch",
        ):
            removed_files, removed_bytes = prune_generated_files(
                cleanup_options[cleanup_label],
                int(retention_days),
            )
            save_preferences(
                {
                    "onboarding_complete": bool(st.session_state.onboarding_complete),
                    "authorization_confirmed": bool(st.session_state.authorization_confirmed),
                    "retention_days": int(retention_days),
                }
            )
            st.success(f"Removed {removed_files} file(s), releasing {format_bytes(removed_bytes)}.")
            st.rerun()

    st.info(
        "Capture downloads, Studio assets, history, read markers, and legacy OCR output are protected. "
        "Panelyra never deletes those areas automatically."
    )
    st.caption("Migration rollback snapshot is stored in the Panelyra workspace.")

elif route_page == "Settings":
    render_page_header(
        "Settings",
        "Configure Panelyra Control, reusable workflows, privacy, and local integrations.",
        ["Preferences", "Workflow presets", "Local integrations"],
    )
    settings_left, settings_right = st.columns(2)
    with settings_left:
        st.markdown("#### Dashboard")
        st.selectbox(
            "Default landing page",
            options=PAGES,
            key="default_landing_page",
            help="Used the next time the app opens a fresh session.",
        )
        st.toggle(
            "Compact UI",
            key="compact_ui",
            help="Reduces padding and card height for denser controls.",
        )
        st.toggle(
            "Show paths in sidebar",
            key="show_sidebar_paths",
            help="Shows or hides workspace labels in the left navigation without revealing local paths.",
        )
        st.number_input(
            "Console characters",
            min_value=5_000,
            max_value=200_000,
            step=5_000,
            key="console_max_chars",
            help="How much of the latest run log appears in each bot's Live Room.",
        )
        st.select_slider(
            "Auto-refresh seconds",
            options=[1, 2, 3, 5, 10],
            key="auto_refresh_seconds",
            help="Refresh interval while a bot run is active.",
        )

    with settings_right:
        st.markdown("#### Comments And Intelligence")
        st.number_input(
            "Quote/philosophy cadence",
            min_value=2,
            max_value=20,
            step=1,
            key="reflection_every_n_comments",
            help="Every Nth generated comment gets a famous-person/philosophical reflection.",
        )
        st.number_input(
            "History rows shown",
            min_value=5,
            max_value=200,
            step=5,
            key="history_rows",
            help="How many recent comment records appear in Intelligence.",
        )
        st.number_input(
            "Top keywords shown",
            min_value=3,
            max_value=30,
            step=1,
            key="top_keywords",
            help="How many keyword bars appear in Intelligence.",
        )
        st.markdown("#### Assistant")
        st.toggle(
            "Enable floating AI button",
            key="assistant_enabled",
            help="Shows the compact Panelyra Copilot on every page except the full Assistant workspace.",
        )
        st.toggle(
            "Use configured AI model",
            key="assistant_use_model",
            help="Enhances grounded local answers with the configured model when OPENAI_API_KEY is available.",
        )
        st.toggle(
            "Show answer evidence",
            key="assistant_show_evidence",
            help="Shows run records, log findings, browser state, and knowledge entries supporting each answer.",
        )
        st.toggle(
            "Allow browser inspection",
            key="assistant_allow_browser_inspection",
            help="Allows user-confirmed read-only DOM summaries and local screenshots from Chrome on port 9222.",
        )
        st.number_input(
            "Assistant history limit",
            min_value=10,
            max_value=500,
            step=10,
            key="assistant_history_limit",
            help="How many recent history records the assistant reviews.",
        )
        st.text_input(
            "Internet survey endpoint",
            key="assistant_internet_endpoint",
            placeholder="http://127.0.0.1:8000/search",
            help="Optional. Called as endpoint?q=<question> when the assistant is asked to survey the internet.",
        )
        st.text_input(
            "Assistant model",
            key="assistant_model",
            help="OpenAI model used when OPENAI_API_KEY is set. You can also set OPENAI_MODEL before launching the app.",
        )
        st.toggle(
            "Use model web search",
            key="assistant_use_openai_web",
            help="Used only when Ask mode explicitly requests current external research. Site help and diagnostics remain local.",
        )
        st.caption("For model-backed answers, put `OPENAI_API_KEY=sk-...` in `.env`, then restart Streamlit.")
        st.markdown("#### Current Values")
        st.table(
            [
                {"setting": "Default page", "value": st.session_state.default_landing_page},
                {"setting": "Compact UI", "value": "on" if st.session_state.compact_ui else "off"},
                {"setting": "Sidebar paths", "value": "shown" if st.session_state.show_sidebar_paths else "hidden"},
                {"setting": "Console length", "value": f"{int(st.session_state.console_max_chars):,} chars"},
                {"setting": "Auto-refresh", "value": f"{int(st.session_state.auto_refresh_seconds)}s"},
                {"setting": "Quote cadence", "value": f"1 every {int(st.session_state.reflection_every_n_comments)} comments"},
                {"setting": "History rows", "value": str(int(st.session_state.history_rows))},
                {"setting": "Top keywords", "value": str(int(st.session_state.top_keywords))},
                {"setting": "AI button", "value": "enabled" if st.session_state.assistant_enabled else "disabled"},
                {"setting": "Model enhancement", "value": "enabled" if st.session_state.assistant_use_model else "disabled"},
                {"setting": "Evidence", "value": "shown" if st.session_state.assistant_show_evidence else "hidden"},
                {"setting": "Browser inspection", "value": "allowed" if st.session_state.assistant_allow_browser_inspection else "disabled"},
                {"setting": "Assistant history", "value": str(int(st.session_state.assistant_history_limit))},
                {"setting": "Internet endpoint", "value": st.session_state.assistant_internet_endpoint or "not configured"},
                {"setting": "Model", "value": st.session_state.assistant_model},
                {"setting": ".env file", "value": str(ENV_FILE)},
                {"setting": "OpenAI key", "value": "available" if openai_api_key_available() else "missing"},
                {"setting": "Model web search", "value": "on" if st.session_state.assistant_use_openai_web else "off"},
                {"setting": "Product version", "value": PRODUCT_VERSION},
                {"setting": "Operating mode", "value": PRODUCT_MODE},
            ]
        )

    with st.expander("Saved Workflow Presets", expanded=False):
        st.text_input(
            "Preset name",
            key="saved_preset_name",
            placeholder="My staging reader",
        )
        if st.button(
            "Save current Engage preset",
            disabled=not st.session_state.saved_preset_name.strip(),
            width="stretch",
        ):
            presets = load_presets()
            presets[st.session_state.saved_preset_name.strip()] = {
                "scroll_preset": st.session_state.scroll_preset,
                "chapter_routing": st.session_state.chapter_routing,
                "max_chapters": int(st.session_state.max_chapters),
                "use_thanks_marker": bool(st.session_state.use_thanks_marker),
            }
            save_presets(presets)
            st.session_state.selected_workflow_preset = st.session_state.saved_preset_name.strip()
            st.success("Workflow preset saved locally.")
            st.rerun()

    with st.expander("Named Selector Profiles", expanded=False):
        profiles = load_selector_profiles()
        active_profile_name = (
            st.session_state.selected_selector_profile
            if st.session_state.selected_selector_profile in profiles
            else next(iter(profiles))
        )
        st.caption(
            f"Current source: {active_profile_name}. Edit selectors from Engage Setup, then save them under a reusable name."
        )
        st.text_input(
            "New selector profile name",
            key="selector_profile_name",
            placeholder="Owned staging reader",
        )
        if st.button(
            "Save current selectors",
            disabled=not st.session_state.selector_profile_name.strip(),
            width="stretch",
        ):
            fallback_profile = profiles[active_profile_name]
            profiles[st.session_state.selector_profile_name.strip()] = {
                "next_link": str(
                    st.session_state.get("engage_next_link_selector", fallback_profile["next_link"])
                ),
                "next_page": str(
                    st.session_state.get("engage_next_page_selector", fallback_profile["next_page"])
                ),
                "page_counter": str(
                    st.session_state.get("engage_page_counter_selector", fallback_profile["page_counter"])
                ),
                "chapter_list": str(
                    st.session_state.get("engage_chapter_list_selector", fallback_profile["chapter_list"])
                ),
                "comment_editor": str(
                    st.session_state.get("engage_comment_editor_selector", fallback_profile["comment_editor"])
                ),
                "submit": str(
                    st.session_state.get("engage_submit_button_selector", fallback_profile["submit"])
                ),
                "thanks": str(
                    st.session_state.get("engage_thanks_button_selector", fallback_profile["thanks"])
                ),
                "confirmation": str(
                    st.session_state.get("engage_confirmation_selector", fallback_profile["confirmation"])
                ),
            }
            save_selector_profiles(profiles)
            st.session_state.selected_selector_profile = st.session_state.selector_profile_name.strip()
            st.success("Selector profile saved locally.")
            st.rerun()

    with st.expander("Environment Health", expanded=False):
        st.dataframe(environment_health(), width="stretch", hide_index=True)

    with st.expander("Local Data And Licensing", expanded=False):
        st.markdown(
            f"""
            **{PRODUCT_NAME} runs locally.** Browser sessions, run records, captures, comments, and generated assets remain on this computer unless you configure an external provider or export them.

            Account and license integration is intentionally isolated from the automation engines. This build does not require an online Panelyra account and does not include telemetry.
            """
        )
        if st.button("Save local privacy preferences", width="stretch"):
            save_preferences(
                {
                    "onboarding_complete": bool(st.session_state.onboarding_complete),
                    "authorization_confirmed": bool(st.session_state.authorization_confirmed),
                    "retention_days": int(st.session_state.retention_days),
                }
            )
            st.success("Local preferences saved.")

    with st.expander("Assistant Quality", expanded=False):
        feedback_records = read_jsonl_records(ASSISTANT_FEEDBACK_FILE)
        helpful_count = sum(
            1 for record in feedback_records if str(record.get("rating", "")) == "helpful"
        )
        needs_work_count = sum(
            1 for record in feedback_records if str(record.get("rating", "")) == "needs_work"
        )
        quality_columns = st.columns(4)
        quality_columns[0].metric("Rated answers", len(feedback_records))
        quality_columns[1].metric("Helpful", helpful_count)
        quality_columns[2].metric("Needs work", needs_work_count)
        quality_columns[3].metric(
            "Helpful rate",
            f"{(helpful_count / max(1, len(feedback_records))) * 100:.0f}%",
        )
        if feedback_records:
            st.dataframe(
                [
                    {
                        "time": record.get("timestamp", ""),
                        "rating": record.get("rating", ""),
                        "mode": record.get("mode", ""),
                        "page": record.get("page", ""),
                        "response": str(record.get("response", ""))[:220],
                    }
                    for record in reversed(feedback_records[-50:])
                ],
                width="stretch",
                hide_index=True,
            )
        else:
            st.info("No assistant answers have been rated yet.")

    with st.expander("Support Requests", expanded=False):
        requests = read_owner_requests()
        open_requests = [request for request in requests if str(request.get("status", "open")) == "open"]
        support_cols = st.columns(4)
        support_cols[0].metric("Total requests", len(requests))
        support_cols[1].metric("Open", len(open_requests))
        support_cols[2].metric("Request file", "Ready" if OWNER_REQUESTS_FILE.exists() else "Empty")
        support_cols[3].metric("Bot state", status_label)

        if requests:
            rows = [
                {
                    "id": str(request.get("id", "")),
                    "time": str(request.get("timestamp", "")),
                    "status": str(request.get("status", "")),
                    "page": str(request.get("page", "")),
                    "bot": str(request.get("bot_status", "")),
                    "request": str(request.get("request", "")),
                    "log findings": " | ".join(str(item) for item in request.get("latest_log_findings", [])[:3])
                    if isinstance(request.get("latest_log_findings"), list)
                    else "",
                }
                for request in reversed(requests)
            ]
            st.dataframe(rows, width="stretch", hide_index=True)
            ticket_ids = [str(request.get("id", "")) for request in reversed(requests)]
            if st.session_state.assistant_selected_ticket not in ticket_ids:
                st.session_state.assistant_selected_ticket = ticket_ids[0]
            selected_ticket_id = st.selectbox(
                "Manage ticket",
                options=ticket_ids,
                key="assistant_selected_ticket",
            )
            selected_ticket = next(
                request for request in requests if str(request.get("id", "")) == selected_ticket_id
            )
            ticket_columns = st.columns(3)
            status_value = str(selected_ticket.get("status", "open"))
            severity_value = str(selected_ticket.get("severity", "medium"))
            ticket_status = ticket_columns[0].selectbox(
                "Status",
                options=list(TICKET_STATUSES),
                index=list(TICKET_STATUSES).index(status_value)
                if status_value in TICKET_STATUSES
                else 0,
                key=f"ticket_status_{selected_ticket_id}",
            )
            ticket_severity = ticket_columns[1].selectbox(
                "Severity",
                options=list(TICKET_SEVERITIES),
                index=list(TICKET_SEVERITIES).index(severity_value)
                if severity_value in TICKET_SEVERITIES
                else 1,
                key=f"ticket_severity_{selected_ticket_id}",
            )
            ticket_category = ticket_columns[2].text_input(
                "Category",
                value=str(selected_ticket.get("category", "workflow")),
                key=f"ticket_category_{selected_ticket_id}",
            )
            ticket_reply = st.text_area(
                "User-facing reply",
                value=str(selected_ticket.get("user_reply", "")),
                key=f"ticket_reply_{selected_ticket_id}",
            )
            ticket_note = st.text_area(
                "Internal note",
                value=str(selected_ticket.get("internal_note", "")),
                key=f"ticket_note_{selected_ticket_id}",
            )
            ticket_resolution = st.text_area(
                "Resolution",
                value=str(selected_ticket.get("resolution", "")),
                key=f"ticket_resolution_{selected_ticket_id}",
            )
            if st.button("Update support ticket", width="stretch"):
                update_ticket(
                    OWNER_REQUESTS_FILE,
                    selected_ticket_id,
                    {
                        "status": ticket_status,
                        "severity": ticket_severity,
                        "category": ticket_category,
                        "user_reply": ticket_reply,
                        "internal_note": ticket_note,
                        "resolution": ticket_resolution,
                    },
                )
                st.success(f"Updated {selected_ticket_id}.")
                st.rerun()
            st.caption("Raw request log")
            st.code(display_path(OWNER_REQUESTS_FILE), language="text")
        else:
            render_panel(
                "No Support Requests Yet",
                "When a user asks the assistant to contact the owner, local support requests appear here.",
            )

    with st.expander("Reference paths and styles", expanded=False):
        path_cols = st.columns(2)
        with path_cols[0]:
            st.markdown("#### Files")
            st.code("App folder: Panelyra workspace", language="text")
            st.code(f"Bot script: {display_path(BOT_PATH)}", language="text")
            st.code(f"History file: {display_path(HISTORY_FILE)}", language="text")
            st.code(f"Read marker file: {display_path(READ_MARKER_FILE)}", language="text")
            st.code(f"Research file: {display_path(RESEARCH_FILE)}", language="text")
        with path_cols[1]:
            st.markdown("#### Comment Styles")
            style_cols = st.columns(2)
            for index, style in enumerate(COMMENT_STYLES):
                style_cols[index % 2].code(style, language="text")

elif route_page == "Help":
    render_page_header(
        "Help",
        "Plain-language explanation of buttons, fields, research endpoints, and the recommended workflow.",
        ["Button guide", "Endpoint guide", "Workflow"],
    )
    st.markdown("#### Buttons")
    render_button_guide()
    st.markdown("#### Fields")
    st.markdown(
        """
        | Field | What it controls |
        |---|---|
        | `Starting chapter or series URL` | The first URL the bot opens. It can be a chapter page, reader page, or a series page if chapter links are detectable. |
        | `Maximum chapters` | Hard stop for how many chapter comment sections the bot processes. |
        | `Reader routing` | `Auto detect` handles common layouts. Continuous mode finds lower chapter sections on one URL. Next-chapter mode uses chapter links/buttons. Paginated mode reads internal pages before commenting once. Mixed mode separates page and chapter controls. |
        | `Selector profile` | Supplies separate CSS selectors for Next Chapter, Next Page, page counter, chapter list/dropdown, comment editor, Submit, Thanks, and confirmation. |
        | `Click Say thanks and mark chapter as read` | Clicks the chapter `Say thanks` button after a successful comment and records the canonical chapter URL in `read_markers.jsonl`. Later runs skip locally marked chapters when possible. |
        | `Scroll speed` | Preset for page scrolling, visible typing cadence, chapter cooldown, and submit verification wait. |
        | `Min/Max scroll px` | Manual pixel jump range for each scroll step. Higher values move faster. |
        | `Min/Max pause sec` | Manual pause range between scroll steps. Lower values move faster. |
        | `Fallback type min/max sec` | Manual keyboard fallback speed if instant editor fill cannot be used. Lower values type faster. |
        | `Cooldown min/max sec` | Pause between completed chapters. Ultra fast keeps this near zero; Super fast removes it. |
        | `Submit verify wait sec` | How long the bot waits for the posted comment to become visible before treating the chapter as failed. |
        | `Reader mode` | `Auto` detects image-heavy chapters, `Novel text` scrapes page text, and `Manga / Manhwa / Manhua` forces image OCR. |
        | `Chapter content container` | The page area that holds the chapter. For most comic readers, use the recommended preset and leave this alone. |
        | `OCR image selector` | Images inside the chapter container. The recommended preset covers normal and lazy-loaded manga, manhwa, and manhua pages. |
        | `OCR confidence filter` | How strict OCR should be. `0.45` is balanced, higher is cleaner but may miss faint text. |
        | `Local research file` | Optional JSON file used to enrich generated comments with staging-safe notes. |
        | `Research endpoint` | Optional local/private HTTP service called as `?q=<chapter title and keywords>`. |
        """
    )
    st.markdown("#### Manual Chapter Selection")
    st.markdown(
        """
        Use `Open Browser / Pick Chapter` when the pasted URL is a series page or when you want to choose a specific chapter yourself.
        The bot pauses until you click `Start Comments`, and it starts from the page currently open in Chrome.
        """
    )
    st.markdown("#### Research Endpoint")
    st.markdown(
        """
        You do not need a research endpoint. Leave it blank unless you run your own local service.

        If configured, the bot calls:

        ```text
        http://127.0.0.1:8000/research?q=Chapter Title keyword1 keyword2
        ```

        It may return plain text, or JSON:

        ```json
        {
          "summary": "A short context note for this chapter."
        }
        ```

        JSON fields `text` and `context` are also accepted.
        """
    )
    st.markdown("#### AI Assistant")
    st.markdown(
        """
        Open `Assistant` for the full Panelyra Copilot, or use the compact `AI` control from another page.

        - `Ask` explains pages, controls, settings, and workflows from the local Panelyra knowledge base.
        - `Diagnose` inspects the selected workspace's run record, log events, browser lock, and outputs.
        - `Configure` extracts a URL and settings, then offers to fill the matching setup form without starting it.
        - `Support` drafts a user-facing reply and can create a tracked local support ticket.
        - `Auto` chooses the mode from the request.

        Every answer can show its evidence. Controlled action buttons navigate, fill forms, prepare retries,
        inspect the active Chrome page, or create a ticket. The copilot does not silently start, stop, submit,
        delete, publish, or post.

        Browser inspection is optional and user-confirmed. It reads a visible DOM summary from Chrome on port
        `9222` and can save a screenshot under `outputs/assistant`.

        The local copilot works without an API key. Model enhancement and streamed responses require
        `OPENAI_API_KEY`:

        ```bash
        cd "/path/to/Panelyra"
        cp .env.example .env
        ```

        Then edit `.env` so it contains:

        ```text
        OPENAI_API_KEY=sk-your-real-key-here
        ```

        Restart Streamlit after changing `.env`.

        Web search remains off for site help and diagnostics. It is considered only when `Ask` mode explicitly
        requests current external research and the web-search setting is enabled.

        Support tickets and assistant ratings are available in `Settings`.
        """
    )

render_floating_chat_widget(history_records, status_label)
persist_workspace_drafts()

should_auto_refresh = any(
    is_process_running(runtime.get("process"))
    for runtime in st.session_state.bot_runtimes.values()
)
if should_auto_refresh and route_page in {"Engage Live", "Capture Live", "Studio Live"}:
    time.sleep(int(st.session_state.auto_refresh_seconds))
    st.rerun()
