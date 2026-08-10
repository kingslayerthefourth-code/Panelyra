#!/usr/bin/env python3
"""Standalone OCR downloader for authorized manga, manhwa, and manhua pages.

The default output is an OCR text transcript plus image URL metadata. Raw image
downloads are optional and should only be used for content you own or are
authorized to archive.
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import re
import shutil
import sys
import tempfile
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlencode, urlparse, urlsplit, urlunsplit

import requests
from requests.adapters import HTTPAdapter
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.remote.webelement import WebElement

import bot
from automation_runtime import atomic_write_json, normalize_web_url, reporter_from_environment
from chapter_content_extractor import extract_chapter_content
from reader_routing import (
    DEFAULT_CHAPTER_LIST_SELECTOR,
    DEFAULT_NEXT_CHAPTER_SELECTOR,
    DEFAULT_NEXT_PAGE_SELECTOR,
    DEFAULT_PAGE_COUNTER_SELECTOR,
    ReaderSelectors,
    activate_next_chapter,
    advance_reader_page,
    canonical_chapter_url,
    classify_reader_layout,
    next_chapter_from_visible_list,
    reader_snapshot,
)


MAX_CHAPTERS = 20
CAPTURE_PERFORMANCE_PROFILES = {
    "reliable": {"stable_rounds": 6, "max_rounds": 30, "download_workers": 4},
    "balanced": {"stable_rounds": 3, "max_rounds": 18, "download_workers": 6},
    "rapid": {"stable_rounds": 2, "max_rounds": 12, "download_workers": 8},
}
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent / "manhwa"
PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_DOWNLOAD_CHROME_PROFILE = (
    Path.home()
    / "Library"
    / "Application Support"
    / "Google"
    / "Chrome"
    / "PanelyraDownloadProfile"
)
DOWNLOAD_DEBUG_PORT = 9223
DOWNLOAD_BROWSER_LOCK = Path(tempfile.gettempdir()) / "panelyra-capture-browser.lock"
LOST_DRIVER_MESSAGES = (
    "no such window",
    "target window already closed",
    "web view not found",
    "invalid session id",
    "disconnected",
    "chrome not reachable",
)


@dataclass(frozen=True)
class ChapterDownload:
    index: int
    url: str
    title: str
    text_file: str
    image_urls_file: str
    image_count: int
    character_count: int
    status: str
    error: str = ""
    images_dir: str = ""
    archive_file: str = ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Archive and OCR up to 20 authorized manga, manhwa, or manhua chapters."
    )
    parser.add_argument("--start-url", required=True, help="First chapter URL, or a series page with a detectable first chapter.")
    parser.add_argument("--max-chapters", type=int, default=1, help=f"Number of chapters to OCR. Hard capped at {MAX_CHAPTERS}.")
    parser.add_argument(
        "--start-signal-file",
        type=Path,
        help="Open the reader and wait for this dashboard signal before capturing.",
    )
    parser.add_argument(
        "--chrome-profile-dir",
        type=Path,
        default=DEFAULT_DOWNLOAD_CHROME_PROFILE,
        help="Persistent Chrome profile used only by Capture.",
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR, help="Directory for captured chapters and OCR output.")
    parser.add_argument("--content-container-selector", default=bot.CONTENT_SELECTOR, help="Container selector for chapter images.")
    parser.add_argument("--ocr-image-selector", default=bot.MANHWA_IMAGE_SELECTOR, help="Image selector for chapter pages.")
    parser.add_argument(
        "--reader-layout",
        choices=("auto", "continuous", "next-chapter", "paginated", "mixed"),
        default="auto",
        help="Reader layout. Auto detects continuous, paginated, next-chapter, and mixed readers.",
    )
    parser.add_argument("--next-chapter-selector", default=DEFAULT_NEXT_CHAPTER_SELECTOR)
    parser.add_argument("--next-page-selector", default=DEFAULT_NEXT_PAGE_SELECTOR)
    parser.add_argument("--page-counter-selector", default=DEFAULT_PAGE_COUNTER_SELECTOR)
    parser.add_argument("--chapter-list-selector", default=DEFAULT_CHAPTER_LIST_SELECTOR)
    parser.add_argument("--ocr-confidence", type=float, default=bot.OCR_CONFIDENCE, help="EasyOCR confidence threshold, 0 to 1.")
    parser.add_argument("--scroll-min-delay", type=float, default=0.08, help="Minimum delay while collecting image URLs.")
    parser.add_argument("--scroll-max-delay", type=float, default=0.18, help="Maximum delay while collecting image URLs.")
    parser.add_argument("--image-fetch-timeout", type=float, default=20.0, help="Seconds to wait for each OCR image fetch.")
    parser.add_argument(
        "--performance-profile",
        choices=tuple(CAPTURE_PERFORMANCE_PROFILES),
        default="balanced",
        help="Panel stabilization and download concurrency profile.",
    )
    parser.add_argument(
        "--save-images",
        action="store_true",
        default=True,
        help="Save raw chapter images locally (enabled by default).",
    )
    parser.add_argument("--no-save-images", action="store_false", dest="save_images", help="Keep only the CBZ archive and OCR transcript.")
    parser.add_argument("--skip-cbz", action="store_true", help="Do not create a CBZ archive.")
    parser.add_argument("--skip-ocr", action="store_true", help="Do not create an OCR transcript.")
    return parser.parse_args()


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def driver_session_lost(exc: BaseException) -> bool:
    message = str(exc).lower()
    return isinstance(exc, WebDriverException) and any(
        fragment in message for fragment in LOST_DRIVER_MESSAGES
    )


def safe_driver_url(driver: object, fallback: str = "") -> str:
    try:
        return str(getattr(driver, "current_url", "") or fallback)
    except WebDriverException:
        return fallback


def portable_output_path(path: Optional[Path]) -> str:
    """Store project-owned outputs relatively so the project can be moved."""
    if path is None:
        return ""
    resolved = path.resolve()
    try:
        return str(resolved.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(resolved)


def local_output_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def validate_args(args: argparse.Namespace) -> int:
    if args.max_chapters < 1:
        logging.error("--max-chapters must be at least 1.")
        return 2
    if args.max_chapters > MAX_CHAPTERS:
        logging.error("--max-chapters is capped at %s. Requested: %s.", MAX_CHAPTERS, args.max_chapters)
        return 2
    if not 0 <= args.ocr_confidence <= 1:
        logging.error("--ocr-confidence must be between 0 and 1.")
        return 2
    if not args.content_container_selector.strip():
        logging.error("--content-container-selector cannot be blank.")
        return 2
    if not args.ocr_image_selector.strip():
        logging.error("--ocr-image-selector cannot be blank.")
        return 2
    if args.reader_layout in {"paginated", "mixed"} and not args.next_page_selector.strip():
        logging.error("--next-page-selector cannot be blank for paginated or mixed readers.")
        return 2
    if args.scroll_min_delay < 0 or args.scroll_max_delay < args.scroll_min_delay:
        logging.error("Scroll delay settings are invalid. Ensure 0 <= min <= max.")
        return 2
    if args.image_fetch_timeout <= 0:
        logging.error("--image-fetch-timeout must be greater than zero.")
        return 2
    return 0


def slugify(value: str, fallback: str) -> str:
    normalized = value.strip().lower().replace("'", "").replace("’", "")
    clean = re.sub(r"[^a-zA-Z0-9._-]+", "-", normalized).strip("-._")
    return clean[:90] or fallback


def chapter_title(driver: object, index: int) -> str:
    try:
        title = bot.normalize_space(str(driver.title))
    except WebDriverException:
        title = ""
    return title or f"chapter-{index:02d}"


CHAPTER_TITLE_PATTERN = re.compile(
    r"\s*(?:[-–—|:]\s*)?(?:(?:vol(?:ume)?\.?\s*\d+(?:\.\d+)?\s*)?)"
    r"(?:chapter|chap(?:ter)?|ch\.?|episode|ep\.?)\s*[-:#]?\s*\d+(?:\.\d+)?\b.*$",
    re.IGNORECASE,
)


def clean_series_title(value: str) -> str:
    normalized = bot.normalize_space(value)
    cleaned = CHAPTER_TITLE_PATTERN.sub("", normalized).strip(" -–—|:")
    cleaned = re.sub(r"^(?:read|reading)\s+", "", cleaned, flags=re.IGNORECASE)
    return cleaned or normalized


def series_title_from_page(driver: object) -> str:
    try:
        candidates = driver.execute_script(
            """
            return [
                document.querySelector('meta[property="og:title"]')?.content || "",
                document.querySelector('meta[name="twitter:title"]')?.content || "",
                document.querySelector("h1")?.innerText || "",
                document.title || ""
            ].filter(Boolean);
            """
        ) or []
    except WebDriverException:
        candidates = []
    for candidate in candidates:
        raw = bot.normalize_space(str(candidate))
        cleaned = clean_series_title(raw)
        if cleaned and cleaned != raw:
            return cleaned
    for candidate in candidates:
        cleaned = clean_series_title(str(candidate))
        if cleaned:
            return cleaned
    return "manhwa"


def chapter_folder_name(
    title: str,
    chapter_index: int,
    *,
    segment_min_y: int = 0,
) -> str:
    number = None if segment_min_y > 0 else chapter_number_from_text(title)
    if number is None:
        chapter_label = f"{chapter_index:02d}"
    elif float(number).is_integer():
        chapter_label = f"{int(number):02d}"
    else:
        chapter_label = f"{number:g}"
    return chapter_label


def capture_result_key(url: str) -> str:
    return url if "#segment-y=" in url else bot.canonical_chapter_key(url)


def series_identity(value: str) -> str:
    normalized = clean_series_title(value).lower().replace("'", "").replace("’", "")
    return re.sub(r"[^a-z0-9]+", "", normalized)


SERIES_TITLE_STOP_WORDS = {
    "a",
    "an",
    "and",
    "at",
    "by",
    "for",
    "from",
    "i",
    "im",
    "in",
    "is",
    "my",
    "of",
    "on",
    "read",
    "since",
    "the",
    "this",
    "to",
    "with",
}
SERIES_ALIAS_MATCH_THRESHOLD = 0.60


def series_title_tokens(value: str) -> set[str]:
    normalized = clean_series_title(value).lower().replace("’", "'")
    tokens = re.findall(r"[a-z0-9]+", normalized)
    return {
        token
        for token in tokens
        if len(token) >= 3 and token not in SERIES_TITLE_STOP_WORDS
    }


def series_alias_match_score(left: str, right: str) -> float:
    """Return a conservative cross-site title similarity score."""
    if series_identity(left) == series_identity(right):
        return 1.0
    left_tokens = series_title_tokens(left)
    right_tokens = series_title_tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    shared = left_tokens & right_tokens
    if len(shared) < 4:
        return 0.0
    containment = len(shared) / min(len(left_tokens), len(right_tokens))
    if containment < 0.64:
        return 0.0
    union = left_tokens | right_tokens
    jaccard = len(shared) / len(union)
    return (0.75 * containment) + (0.25 * jaccard)


def read_json_payload(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def series_directory_candidates(directory: Path) -> list[str]:
    candidates = [directory.name]
    metadata_paths = [directory / "metadata.json"]
    metadata_paths.extend(sorted(directory.glob("*/metadata.json"))[:3])
    for metadata_path in metadata_paths:
        payload = read_json_payload(metadata_path)
        if not isinstance(payload, dict):
            continue
        for key in ("series_title", "title", "url"):
            value = str(payload.get(key, "")).strip()
            if value:
                candidates.append(value)
    manifest_payload = read_json_payload(directory / "manifest.json")
    if isinstance(manifest_payload, list):
        for item in manifest_payload[:3]:
            if not isinstance(item, dict):
                continue
            for key in ("series_title", "title", "url"):
                value = str(item.get(key, "")).strip()
                if value:
                    candidates.append(value)
    return candidates


def directory_matches_series(directory: Path, series_title: str) -> bool:
    return directory_series_match_score(directory, series_title) >= SERIES_ALIAS_MATCH_THRESHOLD


def directory_series_match_score(directory: Path, series_title: str) -> float:
    return max(
        (
            series_alias_match_score(candidate, series_title)
            for candidate in series_directory_candidates(directory)
        ),
        default=0.0,
    )


def rebase_path_values(payload: object, old_prefix: str, new_prefix: str) -> object:
    if isinstance(payload, str):
        return new_prefix + payload[len(old_prefix):] if payload.startswith(old_prefix) else payload
    if isinstance(payload, list):
        return [rebase_path_values(item, old_prefix, new_prefix) for item in payload]
    if isinstance(payload, dict):
        return {
            key: rebase_path_values(value, old_prefix, new_prefix)
            for key, value in payload.items()
        }
    return payload


def payload_contains_path_prefix(payload: object, prefix: str) -> bool:
    if isinstance(payload, str):
        return payload.startswith(prefix)
    if isinstance(payload, list):
        return any(payload_contains_path_prefix(item, prefix) for item in payload)
    if isinstance(payload, dict):
        return any(payload_contains_path_prefix(value, prefix) for value in payload.values())
    return False


def migrate_legacy_chapter_directory(
    output_root: Path,
    series_dir: Path,
    legacy_dir: Path,
    series_title: str,
    series_slug: str,
) -> None:
    metadata_path = legacy_dir / "metadata.json"
    metadata = read_json_payload(metadata_path)
    if not isinstance(metadata, dict):
        return
    try:
        chapter_index = max(1, int(metadata.get("index", 1)))
    except (TypeError, ValueError):
        chapter_index = 1
    chapter_hint = " ".join(
        str(metadata.get(key, ""))
        for key in ("title", "url")
    )
    destination = series_dir / chapter_folder_name(chapter_hint, chapter_index)
    if destination.exists():
        logging.warning(
            "Existing chapter folder already occupies %s; leaving legacy folder unchanged: %s",
            destination,
            legacy_dir,
        )
        return

    old_prefix = str(legacy_dir)
    series_dir.mkdir(parents=True, exist_ok=True)
    shutil.move(str(legacy_dir), str(destination))
    new_prefix = str(destination)

    moved_metadata_path = destination / "metadata.json"
    moved_metadata = rebase_path_values(metadata, old_prefix, new_prefix)
    if isinstance(moved_metadata, dict):
        moved_metadata.update(
            {
                "series_title": series_title,
                "series_slug": series_slug,
                "series_dir": str(series_dir),
                "chapter_folder": destination.name,
            }
        )
        atomic_write_json(moved_metadata_path, moved_metadata)

    root_manifest_path = output_root / "manifest.json"
    root_manifest = read_json_payload(root_manifest_path)
    moved_records: list[dict[str, object]] = []
    retained_records: list[dict[str, object]] = []
    if isinstance(root_manifest, list):
        for item in root_manifest:
            if not isinstance(item, dict):
                continue
            if payload_contains_path_prefix(item, old_prefix):
                rebased = rebase_path_values(item, old_prefix, new_prefix)
                if isinstance(rebased, dict):
                    moved_records.append(rebased)
            else:
                retained_records.append(item)

    series_manifest_path = series_dir / "manifest.json"
    series_manifest_payload = read_json_payload(series_manifest_path)
    series_records = (
        [dict(item) for item in series_manifest_payload if isinstance(item, dict)]
        if isinstance(series_manifest_payload, list)
        else []
    )
    for record in moved_records:
        record_key = capture_result_key(str(record.get("url", "")))
        series_records = [
            item
            for item in series_records
            if capture_result_key(str(item.get("url", ""))) != record_key
        ]
        series_records.append(record)
    if moved_records:
        atomic_write_json(series_manifest_path, series_records)
        atomic_write_json(root_manifest_path, retained_records)

    logging.info("Adopted existing chapter folder into series library: %s", destination)


def adopt_root_manifest_records(
    output_root: Path,
    series_dir: Path,
    series_title: str,
) -> None:
    root_manifest_path = output_root / "manifest.json"
    root_payload = read_json_payload(root_manifest_path)
    if not isinstance(root_payload, list):
        return

    target_identity = series_identity(series_title)
    adopted: list[dict[str, object]] = []
    retained: list[dict[str, object]] = []
    for item in root_payload:
        if not isinstance(item, dict):
            continue
        item_title = str(item.get("series_title") or item.get("title") or "")
        if series_identity(item_title) != target_identity:
            retained.append(item)
            continue
        try:
            chapter_index = max(1, int(item.get("index", 1)))
        except (TypeError, ValueError):
            chapter_index = 1
        destination = series_dir / chapter_folder_name(
            f"{item_title} {item.get('url', '')}",
            chapter_index,
        )
        old_prefix = ""
        for key in ("text_file", "image_urls_file", "archive_file", "images_dir"):
            value = str(item.get(key, "")).strip()
            if not value:
                continue
            candidate = Path(value)
            old_prefix = str(candidate.parent if key != "images_dir" else candidate.parent)
            break
        rebased = (
            rebase_path_values(item, old_prefix, str(destination))
            if old_prefix
            else item
        )
        if isinstance(rebased, dict):
            adopted.append(rebased)

    if not adopted:
        return
    series_manifest_path = series_dir / "manifest.json"
    series_payload = read_json_payload(series_manifest_path)
    series_records = (
        [dict(item) for item in series_payload if isinstance(item, dict)]
        if isinstance(series_payload, list)
        else []
    )
    existing_keys = {
        capture_result_key(str(item.get("url", "")))
        for item in series_records
        if item.get("url")
    }
    for item in adopted:
        item_key = capture_result_key(str(item.get("url", "")))
        if item_key not in existing_keys:
            series_records.append(item)
            existing_keys.add(item_key)
    atomic_write_json(series_manifest_path, series_records)
    atomic_write_json(root_manifest_path, retained)
    logging.info("Adopted %s legacy manifest record(s) into %s.", len(adopted), series_dir)


def resolve_series_output_dir(
    output_root: Path,
    series_title: str,
    series_slug: str,
) -> Path:
    canonical = output_root / series_slug
    series_dir: Optional[Path] = canonical if canonical.is_dir() else None

    child_directories = [
        path
        for path in output_root.iterdir()
        if path.is_dir() and not path.name.startswith(".")
    ]
    if series_dir is None:
        for child in child_directories:
            if (child / "metadata.json").is_file():
                continue
            if directory_matches_series(child, series_title):
                series_dir = child
                logging.info("Reusing existing series folder: %s", series_dir)
                break
    if series_dir is None:
        series_dir = canonical
    series_dir.mkdir(parents=True, exist_ok=True)

    for child in child_directories:
        if child == series_dir or not (child / "metadata.json").is_file():
            continue
        if directory_matches_series(child, series_title):
            migrate_legacy_chapter_directory(
                output_root,
                series_dir,
                child,
                series_title,
                series_slug,
            )
    adopt_root_manifest_records(output_root, series_dir, series_title)
    return series_dir


PROMOTIONAL_PATTERN = re.compile(
    r"discord|telegram|scanlation|credit|advert|promo|patreon|donat|logo|avatar|favicon|banner",
    re.IGNORECASE,
)


def is_promotional_asset_url(value: str) -> bool:
    """Inspect the asset path, not its host, so reader domains are never excluded."""
    parsed = urlparse(value)
    asset_hint = f"{parsed.path.rsplit('/', 2)[-1]} {parsed.query}"
    return bool(PROMOTIONAL_PATTERN.search(asset_hint))


def page_has_chapter_media(driver: object, image_selector: str) -> bool:
    script = """
        const selector = arguments[0];
        const safeAll = value => {
            try { return Array.from(document.querySelectorAll(value)); }
            catch (_) { return []; }
        };
        const visible = node => {
            const rect = node.getBoundingClientRect(), style = getComputedStyle(node);
            return rect.width > 0 && rect.height > 0
                && style.display !== "none" && style.visibility !== "hidden";
        };
        const specificRoots = safeAll(
            "#readerarea, [data-reader], [class*='reader-area' i], [class*='chapter-reader' i]"
        ).filter(visible);
        const fallbackRoots = safeAll(
            ".reading-content, .chapter-content, .entry-content"
        ).filter(visible);
        const readerRoots = specificRoots.length ? specificRoots : fallbackRoots;
        const inReader = node => !readerRoots.length || readerRoots.some(root => root.contains(node));
        const images = safeAll(selector).filter(node => {
            if (!inReader(node)) return false;
            if (!visible(node)) return false;
            const rect = node.getBoundingClientRect();
            const width = Number(node.naturalWidth || rect.width);
            const height = Number(node.naturalHeight || rect.height);
            return readerRoots.length
                ? width >= 180 && height >= 260
                : width >= 280 && height >= 400;
        });
        const canvases = safeAll("canvas").filter(node => {
            if (!inReader(node)) return false;
            if (!visible(node)) return false;
            const rect = node.getBoundingClientRect();
            return Math.max(node.width || 0, rect.width) >= 180
                && Math.max(node.height || 0, rect.height) >= 260;
        });
        const backgrounds = safeAll("[style*='background'], [class*='page' i], [class*='reader' i]")
            .filter(node => inReader(node) && visible(node) && /url\\(/i.test(getComputedStyle(node).backgroundImage || ""));
        return images.length > 0 || canvases.length > 0 || backgrounds.length > 0;
    """
    try:
        return bool(driver.execute_script(script, image_selector))
    except WebDriverException:
        return False


def wait_for_chapter_media(
    driver: object,
    image_selector: str,
    timeout_seconds: float = 30.0,
) -> bool:
    """Allow client-rendered readers and challenge pages time to reveal panels."""
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    attempt = 0
    while time.monotonic() < deadline:
        if page_has_chapter_media(driver, image_selector):
            logging.info("Reader media became available after %s readiness check(s).", attempt + 1)
            return True
        attempt += 1
        bot.dismiss_page_interruptions(driver, attempts=1)
        if attempt % 3 == 0:
            try:
                driver.execute_script(
                    "window.scrollTo(0, Math.max(0, document.documentElement.scrollHeight - innerHeight));"
                )
            except WebDriverException:
                pass
        time.sleep(0.5)
    return page_has_chapter_media(driver, image_selector)


def maybe_open_capture_chapter(driver: object, current_url: str, image_selector: str) -> str:
    """Stay on a reader page even when it has no comment form."""
    if page_has_chapter_media(driver, image_selector):
        logging.info("Capture start page already contains reader media; keeping URL: %s", current_url)
        return current_url

    continue_url = bot.continue_reading_url_or_click(driver)
    if continue_url and bot.canonical_chapter_key(continue_url) != bot.canonical_chapter_key(current_url):
        logging.info("Capture start page resolved a continue-reading URL: %s", continue_url)
        if bot.load_url_with_retries(driver, continue_url, "capture continue-reading chapter"):
            return driver.current_url

    first_chapter_url = bot.first_matching_link_url(driver, bot.FIRST_CHAPTER_SELECTOR, current_url)
    if first_chapter_url and bot.canonical_chapter_key(first_chapter_url) != bot.canonical_chapter_key(current_url):
        logging.info("Capture start page resolved a chapter URL: %s", first_chapter_url)
        if bot.load_url_with_retries(driver, first_chapter_url, "capture chapter from series page"):
            return driver.current_url

    logging.info("No reader media or chapter link was detected; keeping the supplied URL for diagnostics.")
    return current_url


def visible_story_image_urls(
    driver: object,
    image_selector: str,
    min_y: int = 0,
    max_y: Optional[int] = None,
) -> list[str]:
    """Return large reader images inside one chapter segment."""
    script = """
        const selector = arguments[0];
        const minY = Number(arguments[1] || 0);
        const requestedMaxY = Number(arguments[2] || 0);
        const safeAll = value => {
            try { return Array.from(document.querySelectorAll(value)); }
            catch (_) { return []; }
        };
        const visible = node => {
            const r = node.getBoundingClientRect(), s = getComputedStyle(node);
            return r.width > 0 && r.height > 0 && s.display !== "none" && s.visibility !== "hidden";
        };
        const nodes = Array.from(document.querySelectorAll("button,a,h1,h2,h3,section,div"));
        const boundaries = nodes.filter(node => {
            if (!visible(node)) return false;
            const text = (node.innerText || node.textContent || "").replace(/\\s+/g, " ").trim();
            const y = node.getBoundingClientRect().top + scrollY;
            return y > minY + 80 && /^(say thanks|chapter comments|comments)$/i.test(text);
        }).map(node => node.getBoundingClientRect().top + scrollY).sort((a, b) => a - b);
        const boundaryY = requestedMaxY > minY
            ? requestedMaxY
            : (boundaries.length ? boundaries[0] : document.documentElement.scrollHeight);
        const absolute = value => {
            const candidate = String(value || "").trim();
            if (!candidate) return "";
            if (/^(?:data:image\\/|blob:)/i.test(candidate)) return candidate;
            try { return new URL(candidate, document.baseURI).href; }
            catch (_) { return ""; }
        };
        const srcsetUrl = value => {
            const candidates = String(value || "").split(",").map(item => item.trim().split(/\\s+/)[0]).filter(Boolean);
            return candidates.length ? candidates[candidates.length - 1] : "";
        };
        const specificRoots = safeAll(
            "#readerarea, [data-reader], [class*='reader-area' i], [class*='chapter-reader' i]"
        ).filter(root => visible(root) && root.querySelector("img,canvas,[style*='background']"));
        const fallbackRoots = safeAll(
            ".reading-content, .chapter-content, .entry-content"
        ).filter(root => visible(root) && root.querySelector("img,canvas,[style*='background']"));
        const readerRoots = specificRoots.length ? specificRoots : fallbackRoots;
        const inReader = node => !readerRoots.length || readerRoots.some(root => root.contains(node));
        const results = [];
        for (const image of safeAll(selector)) {
            if (!inReader(image)) continue;
            if (!visible(image)) continue;
            const rect = image.getBoundingClientRect();
            const y = rect.top + scrollY;
            const naturalWidth = Number(image.naturalWidth || rect.width);
            const naturalHeight = Number(image.naturalHeight || rect.height);
            if (y < Math.max(120, minY) || y >= boundaryY) continue;
            if (Math.max(naturalWidth, rect.width) < 180 || Math.max(naturalHeight, rect.height) < 260) continue;
            const ancestorText = (image.closest("header,nav,aside,footer,[class*='comment'],[class*='promo'],[class*='advert']")?.innerText || "");
            if (/discord|telegram|scanlation|patreon|advert|promotion|donat/i.test(ancestorText)) continue;
            const source = image.closest("picture")?.querySelector("source[srcset]");
            const candidates = [
                image.getAttribute("data-src"), image.getAttribute("data-lazy-src"),
                image.getAttribute("data-original"), image.getAttribute("data-url"),
                image.currentSrc, srcsetUrl(image.getAttribute("srcset")),
                source ? srcsetUrl(source.getAttribute("srcset")) : "",
                image.getAttribute("src")
            ];
            const src = candidates.map(absolute).find(Boolean);
            if (src) results.push(src);
        }
        const backgroundNodes = safeAll("[style*='background'], [class*='page' i], [class*='reader' i]");
        for (const node of backgroundNodes) {
            if (!inReader(node)) continue;
            if (!visible(node)) continue;
            const rect = node.getBoundingClientRect();
            const y = rect.top + scrollY;
            if (y < Math.max(120, minY) || y >= boundaryY || rect.width < 180 || rect.height < 260) continue;
            const match = (getComputedStyle(node).backgroundImage || "").match(/url\\(["']?(.*?)["']?\\)/i);
            const src = absolute(match ? match[1] : "");
            if (src) results.push(src);
        }
        for (const [index, canvas] of safeAll("canvas").entries()) {
            if (!inReader(canvas)) continue;
            if (!visible(canvas)) continue;
            const rect = canvas.getBoundingClientRect();
            const y = rect.top + scrollY;
            if (y < Math.max(120, minY) || y >= boundaryY) continue;
            if (Math.max(canvas.width || 0, rect.width) < 180 || Math.max(canvas.height || 0, rect.height) < 260) continue;
            results.push(`panelyra-canvas://${index}`);
        }
        return Array.from(new Set(results));
    """
    try:
        values = driver.execute_script(script, image_selector, int(min_y), int(max_y or 0)) or []
    except WebDriverException as exc:
        logging.warning("Story-region image detection failed: %s", exc)
        return []
    return [
        str(value)
        for value in values
        if str(value).startswith(("http://", "https://", "blob:", "data:image/", "panelyra-canvas://"))
    ]


def collect_story_image_urls(
    driver: object,
    image_selector: str,
    min_delay: float,
    max_delay: float,
    *,
    segment_min_y: int = 0,
    segment_max_y: Optional[int] = None,
    stable_rounds_required: int = 3,
    stability_max_rounds: int = 18,
    expected_image_count: int = 0,
) -> list[str]:
    initial_urls = [
        url
        for url in visible_story_image_urls(
            driver,
            image_selector,
            min_y=segment_min_y,
            max_y=segment_max_y,
        )
        if not is_promotional_asset_url(url)
    ]
    if expected_image_count > 0 and len(initial_urls) >= expected_image_count:
        driver.execute_script("window.scrollTo(0, arguments[0]);", max(0, int(segment_min_y)))
        logging.info(
            "Reader already exposes all %s expected panel URL(s); skipping redundant scan scrolling.",
            expected_image_count,
        )
        return list(dict.fromkeys(initial_urls))

    height = max(bot.document_height(driver), 1)
    viewport = max(bot.viewport_height(driver), 600)
    step = max(350, int(viewport * 0.72))
    urls: list[str] = []
    seen: set[str] = set()
    y = max(0, int(segment_min_y))
    stagnant_rounds = 0
    for scroll_round in range(800):
        if y > height:
            break
        driver.execute_script("window.scrollTo(0, arguments[0]);", y)
        time.sleep(max(0.04, min(max_delay, 0.25)))
        if scroll_round % 12 == 0:
            bot.dismiss_page_interruptions(driver, attempts=1)
        for url in visible_story_image_urls(
            driver,
            image_selector,
            min_y=segment_min_y,
            max_y=segment_max_y,
        ):
            if url not in seen and not is_promotional_asset_url(url):
                seen.add(url)
                urls.append(url)
        new_height = bot.document_height(driver)
        if new_height <= height:
            stagnant_rounds += 1
        else:
            stagnant_rounds = 0
        height = max(height, new_height)
        if segment_max_y is not None and y >= segment_max_y:
            break
        if stagnant_rounds >= 8 and y >= max(0, height - viewport):
            break
        y += step
    urls = stabilize_story_image_urls(
        driver,
        image_selector,
        min_delay,
        max_delay,
        urls,
        segment_min_y=segment_min_y,
        segment_max_y=segment_max_y,
        stable_rounds_required=stable_rounds_required,
        max_rounds=stability_max_rounds,
    )
    final_dom_order = [
        url
        for url in visible_story_image_urls(
            driver,
            image_selector,
            min_y=segment_min_y,
            max_y=segment_max_y,
        )
        if not is_promotional_asset_url(url)
    ]
    if final_dom_order:
        ordered_seen = set(final_dom_order)
        urls = final_dom_order + [url for url in urls if url not in ordered_seen]
    driver.execute_script("window.scrollTo(0, arguments[0]);", max(0, int(segment_min_y)))
    logging.info("Detected %s story-region image(s); site chrome and comment media were excluded.", len(urls))
    return urls


def stabilize_story_image_urls(
    driver: object,
    image_selector: str,
    min_delay: float,
    max_delay: float,
    collected_urls: list[str],
    *,
    segment_min_y: int = 0,
    segment_max_y: Optional[int] = None,
    stable_rounds_required: int = 6,
    max_rounds: int = 30,
) -> list[str]:
    """Wait at the chapter end until lazy-loaded panel URLs stop changing."""
    urls = list(collected_urls)
    seen = set(urls)
    previous_signature: tuple[int, tuple[str, ...]] | None = None
    stable_rounds = 0

    for _ in range(max_rounds):
        viewport = max(bot.viewport_height(driver), 600)
        document_end = max(bot.document_height(driver), 1)
        chapter_end = (
            min(document_end, int(segment_max_y))
            if segment_max_y is not None
            else document_end
        )
        for target in (
            max(int(segment_min_y), chapter_end - max(200, viewport // 2)),
            max(int(segment_min_y), chapter_end - 1),
        ):
            driver.execute_script("window.scrollTo(0, arguments[0]);", target)
            time.sleep(max(0.08, min(max_delay, 0.35)))
        bot.dismiss_page_interruptions(driver, attempts=1)

        detected = [
            url
            for url in visible_story_image_urls(
                driver,
                image_selector,
                min_y=segment_min_y,
                max_y=segment_max_y,
            )
            if not is_promotional_asset_url(url)
        ]
        added = 0
        for url in detected:
            if url not in seen:
                seen.add(url)
                urls.append(url)
                added += 1

        signature = (max(bot.document_height(driver), 1), tuple(detected))
        stable_rounds = (
            stable_rounds + 1
            if added == 0 and signature == previous_signature
            else 0
        )
        previous_signature = signature
        if stable_rounds >= stable_rounds_required:
            logging.info(
                "Chapter end stabilized with %s panel URL(s) after %s unchanged checks.",
                len(urls),
                stable_rounds_required,
            )
            break
    else:
        logging.warning(
            "Chapter end did not fully stabilize after %s checks; using %s panel URL(s).",
            max_rounds,
            len(urls),
        )
    return urls


def chapter_boundary_after(driver: object, min_y: int = 0) -> Optional[int]:
    script = """
        const minY = Number(arguments[0] || 0);
        const visible = node => {
            const r = node.getBoundingClientRect(), s = getComputedStyle(node);
            return r.width > 0 && r.height > 0 && s.display !== "none" && s.visibility !== "hidden";
        };
        const candidates = Array.from(document.querySelectorAll("button,a,h1,h2,h3,section,div"))
            .filter(node => {
                if (!visible(node)) return false;
                const y = node.getBoundingClientRect().top + scrollY;
                const text = (node.innerText || node.textContent || "").replace(/\\s+/g, " ").trim();
                return y > minY + 80 && /^(say thanks|chapter comments|comments)$/i.test(text);
            })
            .map(node => Math.round(node.getBoundingClientRect().top + scrollY))
            .sort((a, b) => a - b);
        return candidates.length ? candidates[0] : 0;
    """
    try:
        value = int(driver.execute_script(script, int(min_y)) or 0)
    except (TypeError, ValueError, WebDriverException):
        return None
    return value or None


def scroll_to_next_chapter_boundary(
    driver: object,
    current_boundary_y: int,
    min_delay: float,
    max_delay: float,
) -> Optional[int]:
    viewport = max(bot.viewport_height(driver), 600)
    step = max(500, int(viewport * 0.82))
    y = current_boundary_y + max(300, int(viewport * 0.45))
    stagnant = 0
    previous_height = bot.document_height(driver)
    for _ in range(240):
        driver.execute_script("window.scrollTo(0, arguments[0]);", y)
        time.sleep(max(0.03, min(max_delay, 0.2)))
        next_boundary = chapter_boundary_after(driver, current_boundary_y + 120)
        if next_boundary is not None:
            logging.info("Continuous Capture found the next chapter boundary at y=%s.", next_boundary)
            return next_boundary
        new_height = bot.document_height(driver)
        if new_height <= previous_height and y >= max(0, new_height - viewport):
            stagnant += 1
        else:
            stagnant = 0
        if stagnant >= 12:
            break
        previous_height = max(previous_height, new_height)
        y = min(y + step, max(0, new_height - 1))
    return None


def reader_page_position(driver: object) -> tuple[int, int]:
    script = """
        const visible = node => {
            const r = node.getBoundingClientRect(), s = getComputedStyle(node);
            return r.width > 0 && r.height > 0 && s.display !== "none" && s.visibility !== "hidden";
        };
        for (const node of document.querySelectorAll("[role='combobox'],button,[aria-label*='page' i]")) {
            if (!visible(node)) continue;
            const match = (node.innerText || node.textContent || "").trim().match(/^(\\d+)\\s*\\/\\s*(\\d+)$/);
            if (match) return [Number(match[1]), Number(match[2])];
        }
        return [0, 0];
    """
    try:
        current, total = driver.execute_script(script) or [0, 0]
        return int(current), int(total)
    except (TypeError, ValueError, WebDriverException):
        return 0, 0


def url_with_reader_page(url: str, zero_based_page: int) -> str:
    parts = urlsplit(url)
    query = dict(parse_qs(parts.query, keep_blank_values=True))
    query["page"] = [str(zero_based_page)]
    flattened = [(key, value) for key, values in query.items() for value in values]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(flattened), parts.fragment))


def page_query_state(url: str) -> tuple[str, int] | None:
    for key, value in parse_qs(urlsplit(url).query, keep_blank_values=True).items():
        if key.lower() not in {"page", "p", "reader_page"} or not value:
            continue
        try:
            return key, int(value[-1])
        except ValueError:
            continue
    return None


def url_with_page_value(url: str, key: str, value: int) -> str:
    parts = urlsplit(url)
    query = dict(parse_qs(parts.query, keep_blank_values=True))
    query[key] = [str(value)]
    flattened = [(query_key, item) for query_key, values in query.items() for item in values]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(flattened), parts.fragment))


def collect_chapter_image_urls(
    driver: object,
    image_selector: str,
    min_delay: float,
    max_delay: float,
    *,
    reader_layout: str = "auto",
    next_chapter_selector: str = DEFAULT_NEXT_CHAPTER_SELECTOR,
    next_page_selector: str = DEFAULT_NEXT_PAGE_SELECTOR,
    page_counter_selector: str = DEFAULT_PAGE_COUNTER_SELECTOR,
    chapter_list_selector: str = DEFAULT_CHAPTER_LIST_SELECTOR,
    segment_min_y: int = 0,
    segment_max_y: Optional[int] = None,
    stable_rounds_required: int = 3,
    stability_max_rounds: int = 18,
) -> list[str]:
    selectors = ReaderSelectors(
        next_chapter=next_chapter_selector,
        next_page=next_page_selector,
        page_counter=page_counter_selector,
        comment_editor="textarea, [contenteditable='true'], [role='textbox']",
        chapter_list=chapter_list_selector,
    )
    first_snapshot = reader_snapshot(driver, selectors)
    detected_layout = capture_reader_layout(first_snapshot, reader_layout)
    logging.info(
        "Capture reader layout: %s (page %s/%s).",
        detected_layout,
        first_snapshot.page_current or "?",
        first_snapshot.page_total or "?",
    )
    if detected_layout not in {"paginated", "mixed"}:
        expected_image_count = (
            first_snapshot.page_total
            if first_snapshot.page_total > 1
            and first_snapshot.image_count >= first_snapshot.page_total
            else 0
        )
        return collect_story_image_urls(
            driver,
            image_selector,
            min_delay,
            max_delay,
            segment_min_y=segment_min_y,
            segment_max_y=segment_max_y,
            stable_rounds_required=stable_rounds_required,
            stability_max_rounds=stability_max_rounds,
            expected_image_count=expected_image_count,
        )

    all_urls: list[str] = []
    seen: set[str] = set()
    seen_states: set[tuple[str, int, str]] = set()
    for page_attempt in range(1, 201):
        for url in collect_story_image_urls(
            driver,
            image_selector,
            min_delay,
            max_delay,
            stable_rounds_required=stable_rounds_required,
            stability_max_rounds=stability_max_rounds,
        ):
            if url not in seen:
                seen.add(url)
                all_urls.append(url)
        snapshot = reader_snapshot(driver, selectors)
        state_key = (snapshot.url, snapshot.page_current, snapshot.image_signature)
        if state_key in seen_states:
            logging.warning("Capture pagination repeated a reader state; stopping page traversal.")
            break
        seen_states.add(state_key)
        logging.info(
            "Reader media progress: page %s/%s, %s unique image(s).",
            snapshot.page_current or page_attempt,
            snapshot.page_total or "?",
            len(all_urls),
        )
        if snapshot.page_total and snapshot.page_current >= min(snapshot.page_total, 200):
            break

        query_state = page_query_state(snapshot.url)
        if query_state and snapshot.page_current:
            query_key, current_value = query_state
            offset = current_value - snapshot.page_current
            target_display_page = snapshot.page_current + 1
            page_url = url_with_page_value(snapshot.url, query_key, target_display_page + offset)
            if page_url == snapshot.url or not bot.load_url_with_retries(
                driver,
                page_url,
                f"reader page {target_display_page}/{snapshot.page_total or '?'}",
            ):
                break
            continue

        if advance_reader_page(driver, selectors) is None:
            break
    return all_urls


def capture_reader_layout(snapshot: object, preferred: str) -> str:
    """Let a concrete multi-page counter override stale next-chapter settings."""
    detected = classify_reader_layout(snapshot, preferred)
    page_total = int(getattr(snapshot, "page_total", 0) or 0)
    image_count = int(getattr(snapshot, "image_count", 0) or 0)
    all_pages_already_present = page_total > 1 and image_count >= page_total
    if (
        page_total > 1
        and not all_pages_already_present
        and detected not in {"paginated", "mixed"}
    ):
        corrected = "mixed" if bool(getattr(snapshot, "has_next_chapter", False)) else "paginated"
        logging.info(
            "Reader page counter reports %s pages; overriding Capture layout %s -> %s.",
            page_total,
            detected,
            corrected,
        )
        return corrected
    if all_pages_already_present:
        logging.info(
            "Reader exposes all %s panels in the current DOM; keeping vertical chapter routing.",
            page_total,
        )
        return "next-chapter" if bool(getattr(snapshot, "has_next_chapter", False)) else "continuous"
    return detected


def request_headers(driver: object) -> dict[str, str]:
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": getattr(driver, "current_url", ""),
    }
    try:
        cookies = driver.get_cookies()
    except WebDriverException:
        cookies = []
    cookie_header = "; ".join(
        f"{cookie.get('name')}={cookie.get('value')}"
        for cookie in cookies
        if cookie.get("name") and cookie.get("value")
    )
    if cookie_header:
        headers["Cookie"] = cookie_header
    return headers


def extension_from_url(url: str) -> str:
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
        return suffix
    return ".jpg"


def image_extension_from_payload(url: str, payload: bytes) -> str:
    if payload.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if payload.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if payload.startswith(b"RIFF") and payload[8:12] == b"WEBP":
        return ".webp"
    return extension_from_url(url)


def normalize_panel_url_order(urls: list[str]) -> list[str]:
    """Use explicit sequential source filenames when lazy loading shuffled the DOM."""
    numbered: list[tuple[int, int, str]] = []
    for position, url in enumerate(urls):
        match = re.match(r"^(\d{1,5})[_-]", Path(urlparse(url).path).name)
        if not match:
            return urls
        numbered.append((int(match.group(1)), position, url))
    page_numbers = [number for number, _, _ in numbered]
    if len(set(page_numbers)) != len(urls) or set(page_numbers) != set(
        range(1, len(urls) + 1)
    ):
        return urls
    ordered = [url for _, _, url in sorted(numbered)]
    if ordered != urls:
        logging.info("Reordered %s chapter pages using sequential source filenames.", len(urls))
    return ordered


def maximum_quality_image_url(url: str) -> str:
    """Remove common CDN resize/transcode controls while preserving auth parameters."""
    if not url.startswith(("http://", "https://")):
        return url
    parts = urlsplit(url)
    query = parse_qs(parts.query, keep_blank_values=True)
    transform_keys = {
        "width",
        "height",
        "w",
        "h",
        "quality",
        "q",
        "resize",
        "fit",
        "format",
        "type",
    }
    filtered = {
        key: values
        for key, values in query.items()
        if key.lower() not in transform_keys
    }
    if filtered == query:
        return url
    flattened = [
        (key, value)
        for key, values in filtered.items()
        for value in values
    ]
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(flattened), parts.fragment)
    )


def missing_sequential_panel_numbers(urls: list[str]) -> list[int]:
    """Report gaps when every source URL exposes a one-based panel number."""
    numbers: list[int] = []
    for url in urls:
        match = re.match(r"^(\d{1,5})[_-]", Path(urlparse(url).path).name)
        if not match:
            return []
        numbers.append(int(match.group(1)))
    if not numbers or min(numbers) != 1:
        return []
    return sorted(set(range(1, max(numbers) + 1)) - set(numbers))


def is_image_payload(payload: bytes) -> bool:
    return image_extension_from_payload("", payload) != ".jpg" or payload.startswith(b"\xff\xd8\xff")


def _download_image(item: tuple[int, str], headers: dict[str, str], timeout_seconds: float) -> tuple[int, str, bytes]:
    index, url = item
    preferred_url = maximum_quality_image_url(url)
    candidates = [preferred_url] if preferred_url == url else [preferred_url, url]
    last_error: Exception | None = None
    for candidate in candidates:
        try:
            response = _download_session().get(candidate, timeout=timeout_seconds, headers=headers)
            response.raise_for_status()
            content_type = response.headers.get("Content-Type", "").lower()
            payload = response.content
            if len(payload) < 512 or (
                not content_type.startswith("image/") and not is_image_payload(payload)
            ):
                raise ValueError(
                    f"Invalid image response ({content_type or 'unknown'}, {len(payload)} bytes)"
                )
            if candidate != url:
                logging.info("Downloaded maximum-quality panel source: %s", candidate)
            return index, candidate, payload
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
    if last_error is not None:
        raise last_error
    raise ValueError(f"No image download candidate was available for {url}")


def save_rendered_image_fallback(driver: object, url: str, target: Path) -> bool:
    try:
        element = driver.execute_script(
            """
            const wanted = arguments[0];
            if (wanted.startsWith("panelyra-canvas://")) {
                const index = Number(wanted.split("://")[1]);
                return Array.from(document.querySelectorAll("canvas"))[index] || null;
            }
            const absolute = value => {
                const candidate = String(value || "").trim();
                if (!candidate) return "";
                if (/^(?:data:image\\/|blob:)/i.test(candidate)) return candidate;
                try { return new URL(candidate, document.baseURI).href; }
                catch (_) { return ""; }
            };
            const srcsetValues = value => String(value || "").split(",")
                .map(item => absolute(item.trim().split(/\\s+/)[0])).filter(Boolean);
            const image = Array.from(document.images).find(node => {
                const source = node.closest("picture")?.querySelector("source[srcset]");
                const candidates = [
                    node.currentSrc, node.src, node.getAttribute("data-src"),
                    node.getAttribute("data-lazy-src"), node.getAttribute("data-original"),
                    node.getAttribute("data-url"), ...srcsetValues(node.getAttribute("srcset")),
                    ...(source ? srcsetValues(source.getAttribute("srcset")) : [])
                ].map(absolute);
                return candidates.includes(wanted);
            });
            if (image) return image;
            return Array.from(document.querySelectorAll("[style*='background'], [class*='page' i], [class*='reader' i]"))
                .find(node => {
                    const match = (getComputedStyle(node).backgroundImage || "").match(/url\\(["']?(.*?)["']?\\)/i);
                    return absolute(match ? match[1] : "") === wanted;
                }) || null;
            """,
            url,
        )
        if not isinstance(element, WebElement):
            return False
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", element)
        element.screenshot(str(target.with_suffix(".png")))
        return target.with_suffix(".png").stat().st_size >= 512
    except (OSError, WebDriverException):
        return False


def fetch_image_through_browser(driver: object, url: str) -> bytes:
    if url.startswith("panelyra-canvas://"):
        return b""
    script = """
        const url = arguments[0], done = arguments[arguments.length - 1];
        fetch(url, {credentials: "include"})
            .then(response => {
                if (!response.ok) throw new Error(`HTTP ${response.status}`);
                return response.blob();
            })
            .then(blob => {
                const reader = new FileReader();
                reader.onload = () => done(String(reader.result || ""));
                reader.onerror = () => done("");
                reader.readAsDataURL(blob);
            })
            .catch(() => done(""));
    """
    try:
        value = str(driver.execute_async_script(script, url) or "")
        if "," not in value or not value.startswith("data:image/"):
            return b""
        return base64.b64decode(value.split(",", 1)[1], validate=False)
    except (ValueError, WebDriverException):
        return b""


_DOWNLOAD_SESSION_STATE = threading.local()


def _download_session() -> requests.Session:
    session = getattr(_DOWNLOAD_SESSION_STATE, "session", None)
    if session is None:
        session = requests.Session()
        adapter = HTTPAdapter(pool_connections=8, pool_maxsize=8, max_retries=1)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        _DOWNLOAD_SESSION_STATE.session = session
    return session


def save_images(
    driver: object,
    urls: list[str],
    output_dir: Path,
    timeout_seconds: float,
    download_workers: int = 6,
) -> int:
    output_dir.mkdir(parents=True, exist_ok=True)
    headers = request_headers(driver)
    saved = 0
    with ThreadPoolExecutor(max_workers=min(max(1, download_workers), max(1, len(urls)))) as executor:
        futures = [
            executor.submit(_download_image, item, headers, timeout_seconds)
            for item in enumerate(urls, start=1)
        ]
    for fallback_index, (future, source_url) in enumerate(zip(futures, urls), start=1):
        url = source_url
        try:
            index, url, payload = future.result()
            target = output_dir / f"{index:03d}{image_extension_from_payload(url, payload)}"
            target.write_bytes(payload)
        except (requests.RequestException, ValueError) as exc:
            browser_payload = fetch_image_through_browser(driver, url)
            if len(browser_payload) >= 512 and is_image_payload(browser_payload):
                target = output_dir / f"{fallback_index:03d}{image_extension_from_payload(url, browser_payload)}"
                target.write_bytes(browser_payload)
                logging.info("Saved protected reader image through browser fetch: %s", url)
                saved += 1
                continue
            fallback_target = output_dir / f"{fallback_index:03d}.png"
            if save_rendered_image_fallback(driver, url, fallback_target):
                logging.info("Saved protected reader image through rendered-element fallback: %s", url)
                saved += 1
            else:
                logging.warning("Image download rejected and rendered fallback failed: %s", exc)
            continue
        saved += 1
    return saved


def ordered_archive_images(images_dir: Path) -> list[Path]:
    allowed = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

    def page_key(path: Path) -> tuple[int, str]:
        match = re.search(r"\d+", path.stem)
        return (int(match.group()) if match else sys.maxsize, path.name.lower())

    return sorted(
        (
            path
            for path in images_dir.iterdir()
            if path.is_file() and path.suffix.lower() in allowed
        ),
        key=page_key,
    )


def create_chapter_archive(images_dir: Path, chapter_dir: Path) -> Path:
    archive = chapter_dir / "chapter-pages.cbz"
    image_files = ordered_archive_images(images_dir)
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as output:
        for image_path in image_files:
            output.write(image_path, arcname=image_path.name)
    logging.info("Chapter image archive written: %s (%s page files).", archive, len(image_files))
    return archive


def extract_saved_images_ocr(images_dir: Path, confidence: float) -> str:
    """OCR the exact archived panel bytes without another network request."""
    return extract_chapter_content(
        None,
        "body",
        {
            "mode": "manhwa",
            "img_selector": "img",
            "confidence": confidence,
            "image_files": [str(path) for path in ordered_archive_images(images_dir)],
        },
    )


def chapter_number_from_text(value: str) -> Optional[float]:
    matches = re.findall(r"(?:chapter|ch\.?)\s*(\d+(?:\.\d+)?)", value, re.IGNORECASE)
    if not matches:
        return None
    try:
        return float(matches[-1])
    except ValueError:
        return None


def next_numbered_chapter_url(url: str) -> Optional[str]:
    """Advance common chapter URL suffixes only when no site control is available."""
    parsed = urlsplit(url)
    match = re.search(
        r"(?<![A-Za-z0-9])(?P<token>chapter|chap|ch|episode|ep)(?P<separator>[-_ /]*)(?P<number>\d+)(?P<trailing>/?)$",
        parsed.path,
        re.IGNORECASE,
    )
    if not match:
        return None
    number_start, number_end = match.span("number")
    next_path = (
        parsed.path[:number_start]
        + str(int(match.group("number")) + 1)
        + parsed.path[number_end:]
    )
    return urlunsplit((parsed.scheme, parsed.netloc, next_path, parsed.query, parsed.fragment))


def next_chapter_from_series(driver: object, chapter_url: str, chapter_title_text: str) -> Optional[str]:
    series_url = bot.series_url_from_chapter_url(chapter_url)
    if not series_url:
        return None
    current_number = chapter_number_from_text(chapter_title_text)
    current_key = bot.canonical_chapter_key(chapter_url)
    if not bot.load_url_with_retries(driver, series_url, "series chapter list"):
        return None
    candidates = driver.execute_script(
        """
        return Array.from(document.querySelectorAll("a[href]")).map(link => ({
            href: link.href,
            label: (link.innerText || link.textContent || link.getAttribute("aria-label") || "").trim()
        }));
        """
    ) or []
    parsed: list[tuple[float, str, str]] = []
    fallback: list[str] = []
    for item in candidates:
        href = str(item.get("href", ""))
        label = str(item.get("label", ""))
        if not href or bot.canonical_chapter_key(href) == current_key:
            continue
        if not href.startswith(series_url.rstrip("/") + "/"):
            continue
        fallback.append(href)
        number = chapter_number_from_text(label)
        if number is not None:
            parsed.append((number, href, label))
    if current_number is not None:
        later = sorted(item for item in parsed if item[0] > current_number)
        if later:
            number, href, label = later[0]
            logging.info("Series chapter list resolved next chapter %s (%s): %s", number, label, href)
            return href
    if parsed:
        parsed.sort()
        number, href, label = parsed[0]
        logging.info("Series chapter fallback selected %s (%s): %s", number, label, href)
        return href
    if fallback:
        logging.info("Series chapter fallback selected first distinct chapter URL: %s", fallback[0])
        return fallback[0]
    return None


def resolve_next_chapter_url(
    driver: object,
    chapter_url: str,
    chapter_title_text: str,
    *,
    next_chapter_selector: str = DEFAULT_NEXT_CHAPTER_SELECTOR,
    next_page_selector: str = DEFAULT_NEXT_PAGE_SELECTOR,
    page_counter_selector: str = DEFAULT_PAGE_COUNTER_SELECTOR,
    chapter_list_selector: str = DEFAULT_CHAPTER_LIST_SELECTOR,
) -> Optional[str]:
    selectors = ReaderSelectors(
        next_chapter=next_chapter_selector,
        next_page=next_page_selector,
        page_counter=page_counter_selector,
        comment_editor="textarea, [contenteditable='true'], [role='textbox']",
        chapter_list=chapter_list_selector,
    )
    direct = activate_next_chapter(driver, selectors)
    if direct and canonical_chapter_url(str(direct)) != canonical_chapter_url(chapter_url):
        logging.info("Direct next-chapter control resolved: %s", direct)
        return str(direct)
    list_url = next_chapter_from_visible_list(driver, selectors, chapter_url, chapter_title_text)
    if list_url and canonical_chapter_url(list_url) != canonical_chapter_url(chapter_url):
        logging.info("Visible chapter list resolved next chapter: %s", list_url)
        return list_url
    logging.info("No direct next-chapter link found; checking the series chapter list.")
    series_url = next_chapter_from_series(driver, chapter_url, chapter_title_text)
    if series_url:
        return series_url
    fallback_url = next_numbered_chapter_url(chapter_url)
    if fallback_url and canonical_chapter_url(fallback_url) != canonical_chapter_url(chapter_url):
        logging.info("Using numbered chapter URL fallback: %s", fallback_url)
        return fallback_url
    return None


def ocr_current_chapter(
    driver: object,
    *,
    chapter_index: int,
    output_dir: Path,
    series_title: str,
    series_slug: str,
    container_selector: str,
    image_selector: str,
    confidence: float,
    scroll_min_delay: float,
    scroll_max_delay: float,
    image_fetch_timeout: float,
    save_raw_images: bool,
    create_archive: bool,
    save_ocr: bool,
    reader_layout: str,
    next_chapter_selector: str,
    next_page_selector: str,
    page_counter_selector: str,
    chapter_list_selector: str,
    segment_min_y: int = 0,
    segment_max_y: Optional[int] = None,
    reporter: object | None = None,
    stable_rounds_required: int = 3,
    stability_max_rounds: int = 18,
    download_workers: int = 6,
) -> ChapterDownload:
    chapter_source_url = bot.canonical_chapter_key(getattr(driver, "current_url", ""))
    if segment_min_y > 0:
        chapter_source_url = f"{chapter_source_url}#segment-y={segment_min_y}"
    title = chapter_title(driver, chapter_index)
    chapter_dir = output_dir / chapter_folder_name(
        f"{title} {chapter_source_url}",
        chapter_index,
        segment_min_y=segment_min_y,
    )
    chapter_dir.mkdir(parents=True, exist_ok=True)

    if not wait_for_chapter_media(driver, image_selector):
        logging.warning(
            "Reader media was still unavailable after the readiness window; "
            "running final detection before failing."
        )
    image_urls = collect_chapter_image_urls(
        driver,
        image_selector,
        scroll_min_delay,
        scroll_max_delay,
        reader_layout=reader_layout,
        next_chapter_selector=next_chapter_selector,
        next_page_selector=next_page_selector,
        page_counter_selector=page_counter_selector,
        chapter_list_selector=chapter_list_selector,
        segment_min_y=segment_min_y,
        segment_max_y=segment_max_y,
        stable_rounds_required=stable_rounds_required,
        stability_max_rounds=stability_max_rounds,
    )
    image_urls = normalize_panel_url_order(image_urls)
    if not image_urls:
        raise RuntimeError(
            "No chapter page media was detected. Select a different reader layout or set the chapter image selector in Advanced configuration."
        )
    missing_panels = missing_sequential_panel_numbers(image_urls)
    if missing_panels:
        missing_text = ", ".join(str(number) for number in missing_panels[:12])
        raise RuntimeError(
            "Chapter capture is incomplete; numbered panel URL(s) are missing: "
            f"{missing_text}. The chapter was not finalized."
        )
    image_urls_file = chapter_dir / "image_urls.txt"
    image_urls_file.write_text("\n".join(image_urls) + ("\n" if image_urls else ""), encoding="utf-8")

    # Save pages before OCR so a slow OCR run cannot hide an otherwise complete capture.
    images_dir = chapter_dir / "images"
    archive_file: Optional[Path] = None
    saved = 0
    if save_raw_images or create_archive:
        if reporter:
            reporter.update(
                stage="saving-images",
                message=f"Saving chapter {chapter_index} images before OCR",
            )
        saved = save_images(
            driver,
            image_urls,
            images_dir,
            image_fetch_timeout,
            download_workers=download_workers,
        )
        logging.info(
            "Saved %s/%s raw image(s) for chapter %s before OCR.",
            saved,
            len(image_urls),
            chapter_index,
        )
        if saved != len(image_urls):
            raise RuntimeError(
                f"Chapter image download is incomplete: saved {saved}/{len(image_urls)} panels. "
                "The chapter was not finalized."
            )
        if create_archive:
            archive_file = create_chapter_archive(images_dir, chapter_dir)

    text = ""
    text_file: Optional[Path] = None
    if save_ocr:
        if reporter:
            reporter.update(
                stage="ocr",
                message=f"Reading text from chapter {chapter_index}",
            )
        saved_image_files = ordered_archive_images(images_dir) if images_dir.is_dir() else []
        ocr_settings: dict[str, object] = {
            "mode": "manhwa",
            "img_selector": image_selector,
            "confidence": confidence,
            "timeout_seconds": image_fetch_timeout,
        }
        if saved_image_files:
            ocr_settings["image_files"] = [str(path) for path in saved_image_files]
        else:
            ocr_settings["image_urls"] = image_urls
        text = extract_chapter_content(driver, container_selector, ocr_settings)
        text_file = chapter_dir / "ocr.txt"
        text_file.write_text(text.rstrip() + "\n", encoding="utf-8")

    if saved and not save_raw_images:
        shutil.rmtree(images_dir)

    metadata = {
        "index": chapter_index,
        "series_title": series_title,
        "series_slug": series_slug,
        "series_dir": portable_output_path(output_dir),
        "chapter_folder": chapter_dir.name,
        "url": chapter_source_url,
        "title": title,
        "image_count": len(image_urls),
        "saved_image_count": saved,
        "images_dir": portable_output_path(images_dir) if saved and save_raw_images else "",
        "archive_file": portable_output_path(archive_file),
        "character_count": len(text),
        "saved_at_epoch": int(time.time()),
        "reader_layout": reader_layout,
        "segment_min_y": segment_min_y,
        "segment_max_y": segment_max_y or 0,
    }
    (chapter_dir / "metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")

    return ChapterDownload(
        index=chapter_index,
        url=str(metadata["url"]),
        title=title,
        text_file=portable_output_path(text_file),
        image_urls_file=portable_output_path(image_urls_file),
        image_count=len(image_urls),
        character_count=len(text),
        status="ok",
        images_dir=portable_output_path(images_dir) if saved and save_raw_images else "",
        archive_file=portable_output_path(archive_file),
    )


def open_capture_driver(
    chrome_profile_dir: Path,
    url: str,
    label: str,
) -> bot.DriverSession:
    session = bot.build_driver(
        chrome_profile_dir=chrome_profile_dir,
        remote_debugging_port=DOWNLOAD_DEBUG_PORT,
        browser_lock_path=DOWNLOAD_BROWSER_LOCK,
    )
    if bot.load_url_with_retries(session.driver, url, label):
        return session
    bot.close_driver_session(session)
    raise TimeoutException(f"Could not load URL: {url}")


def download_manhwa_ocr(
    *,
    start_url: str,
    max_chapters: int,
    start_signal_file: Optional[Path] = None,
    chrome_profile_dir: Path = DEFAULT_DOWNLOAD_CHROME_PROFILE,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    container_selector: str = bot.CONTENT_SELECTOR,
    image_selector: str = bot.MANHWA_IMAGE_SELECTOR,
    confidence: float = bot.OCR_CONFIDENCE,
    scroll_min_delay: float = 0.08,
    scroll_max_delay: float = 0.18,
    image_fetch_timeout: float = 20.0,
    save_raw_images: bool = True,
    create_archive: bool = True,
    save_ocr: bool = True,
    reader_layout: str = "auto",
    next_chapter_selector: str = DEFAULT_NEXT_CHAPTER_SELECTOR,
    next_page_selector: str = DEFAULT_NEXT_PAGE_SELECTOR,
    page_counter_selector: str = DEFAULT_PAGE_COUNTER_SELECTOR,
    chapter_list_selector: str = DEFAULT_CHAPTER_LIST_SELECTOR,
    performance_profile: str = "balanced",
) -> list[ChapterDownload]:
    start_url = normalize_web_url(start_url)
    if max_chapters < 1 or max_chapters > MAX_CHAPTERS:
        raise ValueError(f"max_chapters must be between 1 and {MAX_CHAPTERS}.")
    profile = CAPTURE_PERFORMANCE_PROFILES.get(performance_profile)
    if profile is None:
        raise ValueError(f"Unknown performance profile: {performance_profile}")
    logging.info(
        "Capture performance profile: %s (%s stable checks, %s download workers).",
        performance_profile,
        profile["stable_rounds"],
        profile["download_workers"],
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    driver_session: Optional[bot.DriverSession] = None
    results: list[ChapterDownload] = []
    current_url: Optional[str] = start_url
    reporter = reporter_from_environment()
    manifest: Optional[Path] = None
    manifest_records: list[dict[str, object]] = []
    completed_urls: set[str] = set()
    defer_ocr = bool(save_ocr and save_raw_images and max_chapters > 1)
    ocr_executor = ThreadPoolExecutor(max_workers=1) if defer_ocr else None
    pending_ocr: list[tuple[int, object, Path]] = []

    try:
        if reporter:
            reporter.update(status="running", stage="opening-browser", progress_total=max_chapters)
        driver_session = open_capture_driver(
            chrome_profile_dir,
            start_url,
            "OCR start page",
        )
        driver = driver_session.driver

        if start_signal_file is not None:
            start_signal_file.unlink(missing_ok=True)
            logging.info(
                "Browser review is active. Confirm authentication and the target reader, "
                "then select Begin Capture in Panelyra."
            )
            if reporter:
                reporter.update(
                    status="running",
                    stage="waiting-for-user",
                    message=(
                        "Browser review required. Confirm the target reader, "
                        "then select Begin Capture."
                    ),
                )
            while not start_signal_file.exists():
                try:
                    if not driver.window_handles:
                        raise WebDriverException("Capture Chrome has no open windows.")
                    driver.current_url
                except WebDriverException:
                    logging.warning(
                        "Capture Chrome was closed during inspection; reopening it "
                        "with the persistent download profile."
                    )
                    bot.close_driver_session(driver_session)
                    driver_session = open_capture_driver(
                        chrome_profile_dir,
                        start_url,
                        "inspection browser recovery",
                    )
                    driver = driver_session.driver
                    if reporter:
                        reporter.update(
                            status="running",
                            stage="waiting-for-user",
                            message=(
                                "The Capture browser was restored. Confirm the target reader, "
                                "then select Begin Capture."
                            ),
                        )
                time.sleep(0.5)
            try:
                inspected_url = str(driver.current_url or "").strip()
            except WebDriverException:
                inspected_url = ""
            if not inspected_url.startswith(("http://", "https://")):
                logging.warning(
                    "The Capture browser was unavailable when Begin Capture was selected; "
                    "reopening the configured start URL."
                )
                bot.close_driver_session(driver_session)
                driver_session = open_capture_driver(
                    chrome_profile_dir,
                    start_url,
                    "continue browser recovery",
                )
                driver = driver_session.driver
                inspected_url = str(driver.current_url)
            logging.info(
                "Capture start signal received. Continuing from current browser page: %s",
                inspected_url,
            )
            start_signal_file.unlink(missing_ok=True)
            start_url = inspected_url
            current_url = inspected_url

        bot.dismiss_page_interruptions(driver)
        current_url = maybe_open_capture_chapter(driver, driver.current_url, image_selector)
        current_url = driver.current_url
        series_title = series_title_from_page(driver)
        series_slug = slugify(series_title, "manhwa")
        series_output_dir = resolve_series_output_dir(output_dir, series_title, series_slug)
        manifest = series_output_dir / "manifest.json"
        if manifest.exists():
            try:
                loaded_manifest = json.loads(manifest.read_text(encoding="utf-8"))
                if isinstance(loaded_manifest, list):
                    manifest_records = [
                        dict(item) for item in loaded_manifest if isinstance(item, dict)
                    ]
                completed_urls = {
                    capture_result_key(str(item.get("url", "")))
                    for item in manifest_records
                    if item.get("status") == "ok" and item.get("url")
                }
                logging.info("Resume mode loaded %s completed chapter URL(s).", len(completed_urls))
            except (OSError, json.JSONDecodeError):
                logging.warning("Existing series manifest could not be read; starting a fresh manifest.")
        logging.info("Series output folder: %s", series_output_dir)
        segment_min_y = 0
        for chapter_index in range(1, max_chapters + 1):
            effective_layout = reader_layout if reader_layout != "auto" else "next-chapter"
            logging.info(
                "OCR downloading chapter %s/%s: %s",
                chapter_index,
                max_chapters,
                safe_driver_url(driver, current_url or start_url),
            )
            if reporter:
                reporter.checkpoint(
                    "scanning",
                    chapter_index=chapter_index,
                    url=safe_driver_url(driver, current_url or start_url),
                    message=f"Scanning chapter {chapter_index}/{max_chapters}",
                )
            processed_chapter_url = current_url or start_url
            processed_chapter_title = f"chapter-{chapter_index:02d}"
            try:
                processed_chapter_url = driver.current_url
                processed_chapter_title = chapter_title(driver, chapter_index)
                effective_layout = capture_reader_layout(
                    reader_snapshot(
                        driver,
                        ReaderSelectors(
                            next_chapter=next_chapter_selector,
                            next_page=next_page_selector,
                            page_counter=page_counter_selector,
                            comment_editor="textarea, [contenteditable='true'], [role='textbox']",
                            chapter_list=chapter_list_selector,
                        ),
                    ),
                    reader_layout,
                )
                if reader_layout == "auto" and next_numbered_chapter_url(processed_chapter_url):
                    if effective_layout == "continuous":
                        logging.info(
                            "Numbered chapter URL detected; using next-chapter routing instead of continuous-boundary scanning."
                        )
                        effective_layout = "next-chapter"
                if (
                    effective_layout != "continuous"
                    and bot.canonical_chapter_key(driver.current_url) in completed_urls
                ):
                    logging.info("Resume mode skipped completed chapter: %s", driver.current_url)
                else:
                    result = ocr_current_chapter(
                        driver,
                        chapter_index=chapter_index,
                        output_dir=series_output_dir,
                        series_title=series_title,
                        series_slug=series_slug,
                        container_selector=container_selector,
                        image_selector=image_selector,
                        confidence=confidence,
                        scroll_min_delay=scroll_min_delay,
                        scroll_max_delay=scroll_max_delay,
                        image_fetch_timeout=image_fetch_timeout,
                        save_raw_images=save_raw_images,
                        create_archive=create_archive,
                        save_ocr=save_ocr and not defer_ocr,
                        reader_layout=reader_layout,
                        next_chapter_selector=next_chapter_selector,
                        next_page_selector=next_page_selector,
                        page_counter_selector=page_counter_selector,
                        chapter_list_selector=chapter_list_selector,
                        segment_min_y=segment_min_y,
                        reporter=reporter,
                        stable_rounds_required=int(profile["stable_rounds"]),
                        stability_max_rounds=int(profile["max_rounds"]),
                        download_workers=int(profile["download_workers"]),
                    )
                    results.append(result)
                    if defer_ocr and ocr_executor is not None and result.images_dir:
                        images_path = local_output_path(result.images_dir)
                        pending_ocr.append(
                            (
                                len(results) - 1,
                                ocr_executor.submit(extract_saved_images_ocr, images_path, confidence),
                                images_path.parent,
                            )
                        )
                        logging.info("Queued chapter %s for deferred local OCR.", chapter_index)
                    result_record = asdict(result)
                    result_key = capture_result_key(result.url)
                    manifest_records = [
                        item
                        for item in manifest_records
                        if capture_result_key(str(item.get("url", ""))) != result_key
                    ]
                    manifest_records.append(result_record)
                    atomic_write_json(manifest, manifest_records)
                    if reporter:
                        reporter.update(progress_current=chapter_index, message=f"Completed chapter {chapter_index}")
            except Exception as exc:
                logging.exception("Chapter %s OCR download failed: %s", chapter_index, exc)
                results.append(
                    ChapterDownload(
                        index=chapter_index,
                        url=safe_driver_url(driver, processed_chapter_url),
                        title=f"chapter-{chapter_index:02d}",
                        text_file="",
                        image_urls_file="",
                        image_count=0,
                        character_count=0,
                        status="failed",
                        error=str(exc),
                    )
                )
                if reporter:
                    reporter.update(message=f"Chapter {chapter_index} failed: {exc}")
                if driver_session_lost(exc):
                    recovery_url = processed_chapter_url or current_url or start_url
                    logging.warning(
                        "Controlled Chrome window was lost; restarting it at %s.",
                        recovery_url,
                    )
                    bot.close_driver_session(driver_session)
                    driver_session = open_capture_driver(
                        chrome_profile_dir,
                        recovery_url,
                        f"OCR recovery after chapter {chapter_index}",
                    )
                    driver = driver_session.driver
                    bot.dismiss_page_interruptions(driver)
                    current_url = driver.current_url
                    segment_min_y = 0
                    logging.info("Browser recovery succeeded; continuing the Capture run.")
                    continue

            if chapter_index >= max_chapters:
                break

            if effective_layout == "continuous":
                current_boundary = chapter_boundary_after(driver, segment_min_y)
                if current_boundary is not None:
                    next_boundary = scroll_to_next_chapter_boundary(
                        driver,
                        current_boundary,
                        scroll_min_delay,
                        scroll_max_delay,
                    )
                    if next_boundary is not None:
                        segment_min_y = current_boundary + 1
                        current_url = driver.current_url
                        logging.info(
                            "Continuous Capture advancing within the same URL to chapter segment %s.",
                            chapter_index + 1,
                        )
                        continue

            next_url = resolve_next_chapter_url(
                driver,
                processed_chapter_url,
                processed_chapter_title,
                next_chapter_selector=next_chapter_selector,
                next_page_selector=next_page_selector,
                page_counter_selector=page_counter_selector,
                chapter_list_selector=chapter_list_selector,
            )
            if not next_url or bot.canonical_chapter_key(next_url) == bot.canonical_chapter_key(processed_chapter_url):
                logging.info("No distinct next chapter URL found; stopping OCR download.")
                break
            current_url = next_url
            segment_min_y = 0
            if driver.current_url != current_url and not bot.load_url_with_retries(
                driver, current_url, f"OCR chapter {chapter_index + 1}"
            ):
                break

        if pending_ocr:
            if driver_session is not None:
                bot.close_driver_session(driver_session)
                driver_session = None
            if reporter:
                reporter.update(stage="ocr", message=f"Finalizing OCR for {len(pending_ocr)} captured chapter(s)")
            for position, (result_index, future, chapter_dir) in enumerate(pending_ocr, start=1):
                try:
                    text = future.result()
                    text_file = chapter_dir / "ocr.txt"
                    text_file.write_text(text.rstrip() + "\n", encoding="utf-8")
                    metadata_file = chapter_dir / "metadata.json"
                    metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
                    metadata["character_count"] = len(text)
                    metadata["ocr_status"] = "completed"
                    metadata_file.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
                    results[result_index] = replace(
                        results[result_index],
                        text_file=portable_output_path(text_file),
                        character_count=len(text),
                    )
                    logging.info("Deferred OCR completed for chapter %s/%s.", position, len(pending_ocr))
                except Exception as exc:
                    logging.exception("Deferred OCR failed for %s: %s", chapter_dir.name, exc)
                    metadata_file = chapter_dir / "metadata.json"
                    try:
                        metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
                        metadata["ocr_status"] = "failed"
                        metadata["ocr_error"] = str(exc)
                        metadata_file.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
                    except (OSError, json.JSONDecodeError):
                        pass
            ocr_executor.shutdown(wait=True)
            ocr_executor = None

        if manifest is not None:
            for result in results:
                result_record = asdict(result)
                result_key = capture_result_key(result.url)
                manifest_records = [
                    item
                    for item in manifest_records
                    if capture_result_key(str(item.get("url", ""))) != result_key
                ]
                manifest_records.append(result_record)
            atomic_write_json(manifest, manifest_records)
            logging.info("OCR download manifest written: %s", manifest)
        failed_results = [result for result in results if result.status != "ok"]
        if reporter and failed_results:
            failure_reason = failed_results[-1].error or "One or more chapters failed."
            reporter.update(
                status="failed",
                stage="failed",
                progress_current=len([result for result in results if result.status == "ok"]),
                stop_reason=failure_reason,
                message=failure_reason,
            )
        elif reporter:
            reporter.update(status="completed", stage="completed", progress_current=len(results))
        return results
    except Exception as exc:
        if reporter:
            reporter.update(status="failed", stage="failed", stop_reason=str(exc))
        raise
    finally:
        if ocr_executor is not None:
            ocr_executor.shutdown(wait=False, cancel_futures=True)
        if driver_session is not None:
            bot.close_driver_session(driver_session)


def main() -> int:
    args = parse_args()
    configure_logging()
    validation_code = validate_args(args)
    if validation_code:
        return validation_code

    try:
        results = download_manhwa_ocr(
            start_url=args.start_url,
            max_chapters=args.max_chapters,
            start_signal_file=args.start_signal_file,
            chrome_profile_dir=args.chrome_profile_dir,
            output_dir=args.output_dir,
            container_selector=args.content_container_selector,
            image_selector=args.ocr_image_selector,
            confidence=args.ocr_confidence,
            scroll_min_delay=args.scroll_min_delay,
            scroll_max_delay=args.scroll_max_delay,
            image_fetch_timeout=args.image_fetch_timeout,
            save_raw_images=args.save_images,
            create_archive=not args.skip_cbz,
            save_ocr=not args.skip_ocr,
            reader_layout=args.reader_layout,
            next_chapter_selector=args.next_chapter_selector,
            next_page_selector=args.next_page_selector,
            page_counter_selector=args.page_counter_selector,
            chapter_list_selector=args.chapter_list_selector,
            performance_profile=args.performance_profile,
        )
    except Exception as exc:
        logging.exception("Comic OCR downloader failed: %s", exc)
        return 1

    written = len([result for result in results if result.status == "ok"])
    failed = len(results) - written
    logging.info("Finished. Chapters written: %s. Failed: %s.", written, failed)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
