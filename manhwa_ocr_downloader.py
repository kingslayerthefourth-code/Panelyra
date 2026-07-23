#!/usr/bin/env python3
"""Standalone OCR downloader for authorized manga, manhwa, and manhua pages.

The default output is an OCR text transcript plus image URL metadata. Raw image
downloads are optional and should only be used for content you own or are
authorized to archive.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import re
import shutil
import sys
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlencode, urlparse, urlsplit, urlunsplit

import requests
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
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent / "ocr_downloads"


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
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR, help="Directory for OCR transcript output.")
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
        "--save-images",
        action="store_true",
        help="Also save raw chapter images locally. Use only when you have rights to archive the content.",
    )
    parser.add_argument("--skip-cbz", action="store_true", help="Do not create a CBZ archive.")
    parser.add_argument("--skip-ocr", action="store_true", help="Do not create an OCR transcript.")
    return parser.parse_args()


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


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
    target = series_identity(series_title)
    return bool(target) and any(
        series_identity(candidate) == target
        for candidate in series_directory_candidates(directory)
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
) -> list[str]:
    height = max(bot.document_height(driver), 1)
    viewport = max(bot.viewport_height(driver), 600)
    step = max(350, int(viewport * 0.72))
    urls: list[str] = []
    seen: set[str] = set()
    y = max(0, int(segment_min_y))
    stagnant_rounds = 0
    for _ in range(800):
        if y > height:
            break
        driver.execute_script("window.scrollTo(0, arguments[0]);", y)
        time.sleep(max(0.04, min(max_delay, 0.25)))
        bot.dismiss_page_interruptions(driver, attempts=2)
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
    driver.execute_script("window.scrollTo(0, arguments[0]);", max(0, int(segment_min_y)))
    logging.info("Detected %s story-region image(s); site chrome and comment media were excluded.", len(urls))
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
) -> list[str]:
    selectors = ReaderSelectors(
        next_chapter=next_chapter_selector,
        next_page=next_page_selector,
        page_counter=page_counter_selector,
        comment_editor="textarea, [contenteditable='true'], [role='textbox']",
        chapter_list=chapter_list_selector,
    )
    first_snapshot = reader_snapshot(driver, selectors)
    detected_layout = classify_reader_layout(first_snapshot, reader_layout)
    logging.info(
        "Capture reader layout: %s (page %s/%s).",
        detected_layout,
        first_snapshot.page_current or "?",
        first_snapshot.page_total or "?",
    )
    if detected_layout not in {"paginated", "mixed"}:
        return collect_story_image_urls(
            driver,
            image_selector,
            min_delay,
            max_delay,
            segment_min_y=segment_min_y,
            segment_max_y=segment_max_y,
        )

    all_urls: list[str] = []
    seen: set[str] = set()
    seen_states: set[tuple[str, int, str]] = set()
    for page_attempt in range(1, 201):
        for url in collect_story_image_urls(driver, image_selector, min_delay, max_delay):
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


def is_image_payload(payload: bytes) -> bool:
    return image_extension_from_payload("", payload) != ".jpg" or payload.startswith(b"\xff\xd8\xff")


def _download_image(item: tuple[int, str], headers: dict[str, str], timeout_seconds: float) -> tuple[int, str, bytes]:
    index, url = item
    response = requests.get(url, timeout=timeout_seconds, headers=headers)
    response.raise_for_status()
    content_type = response.headers.get("Content-Type", "").lower()
    payload = response.content
    if len(payload) < 512 or (not content_type.startswith("image/") and not is_image_payload(payload)):
        raise ValueError(f"Invalid image response ({content_type or 'unknown'}, {len(payload)} bytes)")
    return index, url, payload


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


def save_images(driver: object, urls: list[str], output_dir: Path, timeout_seconds: float) -> int:
    output_dir.mkdir(parents=True, exist_ok=True)
    headers = request_headers(driver)
    saved = 0
    seen_hashes: set[str] = set()
    with ThreadPoolExecutor(max_workers=min(4, max(1, len(urls)))) as executor:
        futures = [
            executor.submit(_download_image, item, headers, timeout_seconds)
            for item in enumerate(urls, start=1)
        ]
    for fallback_index, (future, source_url) in enumerate(zip(futures, urls), start=1):
        url = source_url
        try:
            index, url, payload = future.result()
            digest = hashlib.sha256(payload).hexdigest()
            if digest in seen_hashes:
                logging.info("Skipped duplicate image: %s", url)
                continue
            seen_hashes.add(digest)
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


def create_chapter_archive(images_dir: Path, chapter_dir: Path) -> Path:
    archive = chapter_dir / "chapter-pages.cbz"
    image_files = sorted(
        path
        for path in images_dir.iterdir()
        if path.is_file() and path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".gif"}
    )
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as output:
        for image_path in image_files:
            output.write(image_path, arcname=image_path.name)
    logging.info("Chapter image archive written: %s (%s page files).", archive, len(image_files))
    return archive


def chapter_number_from_text(value: str) -> Optional[float]:
    matches = re.findall(r"(?:chapter|ch\.?)\s*(\d+(?:\.\d+)?)", value, re.IGNORECASE)
    if not matches:
        return None
    try:
        return float(matches[-1])
    except ValueError:
        return None


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
    return next_chapter_from_series(driver, chapter_url, chapter_title_text)


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
    )
    if not image_urls:
        raise RuntimeError(
            "No chapter page media was detected. Select a different reader layout or set the chapter image selector in Advanced configuration."
        )
    image_urls_file = chapter_dir / "image_urls.txt"
    image_urls_file.write_text("\n".join(image_urls) + ("\n" if image_urls else ""), encoding="utf-8")

    text = ""
    text_file: Optional[Path] = None
    if save_ocr:
        text = extract_chapter_content(
            driver,
            container_selector,
            {
                "mode": "manhwa",
                "img_selector": image_selector,
                "confidence": confidence,
                "timeout_seconds": image_fetch_timeout,
                "image_urls": image_urls,
            },
        )
        text_file = chapter_dir / "ocr.txt"
        text_file.write_text(text.rstrip() + "\n", encoding="utf-8")

    images_dir = chapter_dir / "images"
    archive_file: Optional[Path] = None
    saved = 0
    if save_raw_images or create_archive:
        saved = save_images(driver, image_urls, images_dir, image_fetch_timeout)
        logging.info("Saved %s/%s raw image(s) for chapter %s.", saved, len(image_urls), chapter_index)
        if saved and create_archive:
            archive_file = create_chapter_archive(images_dir, chapter_dir)
        if saved and not save_raw_images:
            shutil.rmtree(images_dir)

    metadata = {
        "index": chapter_index,
        "series_title": series_title,
        "series_slug": series_slug,
        "series_dir": str(output_dir),
        "chapter_folder": chapter_dir.name,
        "url": chapter_source_url,
        "title": title,
        "image_count": len(image_urls),
        "saved_image_count": saved,
        "images_dir": str(images_dir) if saved and save_raw_images else "",
        "archive_file": str(archive_file) if archive_file else "",
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
        text_file=str(text_file) if text_file else "",
        image_urls_file=str(image_urls_file),
        image_count=len(image_urls),
        character_count=len(text),
        status="ok",
        images_dir=str(images_dir) if saved and save_raw_images else "",
        archive_file=str(archive_file) if archive_file else "",
    )


def download_manhwa_ocr(
    *,
    start_url: str,
    max_chapters: int,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    container_selector: str = bot.CONTENT_SELECTOR,
    image_selector: str = bot.MANHWA_IMAGE_SELECTOR,
    confidence: float = bot.OCR_CONFIDENCE,
    scroll_min_delay: float = 0.08,
    scroll_max_delay: float = 0.18,
    image_fetch_timeout: float = 20.0,
    save_raw_images: bool = False,
    create_archive: bool = True,
    save_ocr: bool = True,
    reader_layout: str = "auto",
    next_chapter_selector: str = DEFAULT_NEXT_CHAPTER_SELECTOR,
    next_page_selector: str = DEFAULT_NEXT_PAGE_SELECTOR,
    page_counter_selector: str = DEFAULT_PAGE_COUNTER_SELECTOR,
    chapter_list_selector: str = DEFAULT_CHAPTER_LIST_SELECTOR,
) -> list[ChapterDownload]:
    start_url = normalize_web_url(start_url)
    if max_chapters < 1 or max_chapters > MAX_CHAPTERS:
        raise ValueError(f"max_chapters must be between 1 and {MAX_CHAPTERS}.")

    output_dir.mkdir(parents=True, exist_ok=True)
    driver_session: Optional[bot.DriverSession] = None
    results: list[ChapterDownload] = []
    current_url: Optional[str] = start_url
    reporter = reporter_from_environment()
    manifest: Optional[Path] = None
    manifest_records: list[dict[str, object]] = []
    completed_urls: set[str] = set()

    try:
        if reporter:
            reporter.update(status="running", stage="opening-browser", progress_total=max_chapters)
        driver_session = bot.build_driver()
        driver = driver_session.driver
        if not bot.load_url_with_retries(driver, start_url, "OCR start page"):
            raise TimeoutException(f"Could not load start URL: {start_url}")

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
            logging.info("OCR downloading chapter %s/%s: %s", chapter_index, max_chapters, driver.current_url)
            if reporter:
                reporter.checkpoint(
                    "scanning",
                    chapter_index=chapter_index,
                    url=driver.current_url,
                    message=f"Scanning chapter {chapter_index}/{max_chapters}",
                )
            try:
                processed_chapter_url = driver.current_url
                processed_chapter_title = chapter_title(driver, chapter_index)
                effective_layout = classify_reader_layout(
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
                        save_ocr=save_ocr,
                        reader_layout=reader_layout,
                        next_chapter_selector=next_chapter_selector,
                        next_page_selector=next_page_selector,
                        page_counter_selector=page_counter_selector,
                        chapter_list_selector=chapter_list_selector,
                        segment_min_y=segment_min_y,
                    )
                    results.append(result)
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
                        url=getattr(driver, "current_url", current_url or ""),
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

        if manifest is not None:
            for result in results:
                if result.status == "ok":
                    continue
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
        if reporter:
            reporter.update(status="completed", stage="completed", progress_current=len(results))
        return results
    except Exception as exc:
        if reporter:
            reporter.update(status="failed", stage="failed", stop_reason=str(exc))
        raise
    finally:
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
        )
    except Exception as exc:
        logging.exception("Comic OCR downloader failed: %s", exc)
        return 1

    logging.info("Finished. Chapters written: %s.", len([result for result in results if result.status == "ok"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
