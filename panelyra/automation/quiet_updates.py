"""Quiet, transactional chapter updates around Panelyra's existing downloader.

This module deliberately does not alter or call the Selenium downloader for quiet
updates. Browser fallback remains an explicit wrapper action.
"""

from __future__ import annotations

import fcntl
import hashlib
import io
import json
import logging
import os
import random
import re
import shutil
import subprocess
import tempfile
import time
import zipfile
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
import plistlib
from typing import Any, Callable, Iterable, Iterator, Sequence
from urllib.parse import unquote, urljoin, urlsplit

import requests
from bs4 import BeautifulSoup
from PIL import Image

from automation_runtime import atomic_write_json, browser_lock_owner
import bot
from manhwa_ocr_downloader import (
    DEFAULT_DOWNLOAD_CHROME_PROFILE,
    DEFAULT_OUTPUT_DIR,
    DOWNLOAD_BROWSER_LOCK,
    download_manhwa_ocr,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_HISTORY_FILE = PROJECT_ROOT / "data/updates/downloaded_chapters.json"
DEFAULT_STATUS_FILE = PROJECT_ROOT / "data/updates/update_tracker_status.json"
DEFAULT_FAILURE_FILE = PROJECT_ROOT / "data/updates/failed_updates.json"
DEFAULT_SOURCE_FILE = PROJECT_ROOT / "data/updates/series_sources.json"
DEFAULT_LOCK_FILE = Path(tempfile.gettempdir()) / "panelyra-library-updater.lock"
LAUNCH_AGENT_LABEL = "com.panelyra.library-updater"
DEFAULT_INTERVAL_SECONDS = 10_800
MIN_INTERVAL_SECONDS = 300
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"}
PLACEHOLDER_MARKERS = ("placeholder", "loading", "lazy", "spacer", "blank", "logo", "avatar")
CHALLENGE_MARKERS = ("cf-chl-", "cloudflare ray id", "just a moment...", "checking your browser")
LOGIN_MARKERS = ("wp-login.php", "sign in to continue", "login to continue", "age verification")
AUTH_FAILURE_CODES = {"login_required", "authentication_required", "age_verification"}
KNOWN_PROVIDER_HOSTS = {
    "asurascans.com",
    "asurascans.my",
    "en-thunderscans.com",
    "kaynscans.com",
    "novatoon.com",
}


class UpdateError(RuntimeError):
    def __init__(self, code: str, message: str, recovery: str = "quiet") -> None:
        super().__init__(message)
        self.code = code
        self.recovery = recovery


@dataclass(frozen=True)
class QuietUpdateResult:
    url: str
    series: str
    chapter: float | None
    status: str
    image_count: int = 0
    error_code: str = ""
    error: str = ""
    recovery: str = ""
    source_fingerprint: str = ""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_url(value: str) -> str:
    from panelyra.automation.series_updates import canonical_history_url

    return canonical_history_url(value)


def load_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def normalized_history(path: Path = DEFAULT_HISTORY_FILE) -> set[str]:
    from panelyra.automation.series_updates import load_download_history

    return load_download_history(path)


def record_success(url: str, path: Path = DEFAULT_HISTORY_FILE) -> None:
    from panelyra.automation.series_updates import record_downloaded_chapter

    record_downloaded_chapter(url, path)


def chapter_number(value: str) -> float | None:
    from panelyra.automation.series_updates import chapter_number as parse

    return parse(value)


def series_slug_from_url(value: str) -> str:
    parsed = urlsplit(canonical_url(value))
    parts = [unquote(part).lower() for part in parsed.path.split("/") if part]
    part = parts[-1] if parts else ""
    part = re.sub(r"(?:^|[-_])(chapter|chap|ch|episode|ep)[-_]?\d+(?:\.\d+)?(?:[-_].*)?$", "", part)
    if not part.strip("-_") and len(parts) > 1:
        part = parts[-2]
    return re.sub(r"[^a-z0-9]+", "-", part).strip("-") or "manhwa"


def safe_name(value: str, fallback: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return cleaned or fallback


def chapter_folder(number: float | None, url: str) -> str:
    if number is None:
        return safe_name(urlsplit(url).path.rstrip("/").split("/")[-1], "chapter")
    return str(int(number)).zfill(2) if number.is_integer() else str(number).rstrip("0").rstrip(".")


def _source_urls_from_payload(payload: Any) -> set[str]:
    values: set[str] = set()
    records = payload if isinstance(payload, list) else [payload]
    for record in records:
        if not isinstance(record, dict):
            continue
        for key in ("series_url", "source_url", "url"):
            raw = str(record.get(key, "")).strip()
            if raw.startswith(("http://", "https://")):
                values.add(canonical_url(raw))
    return values


def discover_library_sources(
    library_root: Path = DEFAULT_OUTPUT_DIR,
    source_file: Path = DEFAULT_SOURCE_FILE,
) -> dict[str, dict[str, Any]]:
    registry = load_json(source_file, {})
    discovered: dict[str, dict[str, Any]] = {}
    if not library_root.is_dir():
        return discovered
    for series_dir in sorted(path for path in library_root.iterdir() if path.is_dir()):
        local_numbers: set[float] = set()
        chapter_urls: set[str] = set()
        landing_urls: set[str] = set()
        for chapter_dir in series_dir.iterdir():
            if chapter_dir.is_dir():
                number = chapter_number(chapter_dir.name)
                if number is None:
                    try:
                        number = float(chapter_dir.name)
                    except ValueError:
                        number = None
                if number is not None:
                    local_numbers.add(number)
                metadata = load_json(chapter_dir / "metadata.json", {})
                urls = _source_urls_from_payload(metadata)
                chapter_urls.update(urls)
                for url in urls:
                    candidate = derive_series_url(url)
                    if candidate:
                        landing_urls.add(candidate)
        manifest = load_json(series_dir / "manifest.json", [])
        manifest_urls = _source_urls_from_payload(manifest)
        chapter_urls.update(manifest_urls)
        for url in manifest_urls:
            candidate = derive_series_url(url)
            if candidate:
                landing_urls.add(candidate)
        registered = registry.get(series_dir.name, {}) if isinstance(registry, dict) else {}
        if isinstance(registered, str):
            landing_urls.add(canonical_url(registered))
        elif isinstance(registered, dict):
            for value in registered.get("series_urls", []):
                if isinstance(value, str) and value.startswith(("http://", "https://")):
                    landing_urls.add(canonical_url(value))
        discovered[series_dir.name] = {
            "series_dir": str(series_dir),
            "series_urls": sorted(landing_urls),
            "chapter_urls": sorted(chapter_urls),
            "local_numbers": sorted(local_numbers),
            "highest_local": max(local_numbers) if local_numbers else None,
        }
    return discovered


def persist_source_registry(discovered: dict[str, dict[str, Any]], source_file: Path = DEFAULT_SOURCE_FILE) -> None:
    existing = load_json(source_file, {})
    registry = dict(existing) if isinstance(existing, dict) else {}
    changed = False
    for series, details in discovered.items():
        urls = sorted({canonical_url(str(url)) for url in details.get("series_urls", [])})
        if not urls:
            continue
        previous = registry.get(series, {})
        previous_urls = previous.get("series_urls", []) if isinstance(previous, dict) else [previous]
        combined = sorted({canonical_url(str(url)) for url in previous_urls if str(url).startswith(("http://", "https://"))} | set(urls))
        record = {"series_urls": combined, "updated_at": utc_now()}
        if not isinstance(previous, dict) or previous.get("series_urls") != combined:
            registry[series] = record
            changed = True
    if changed:
        atomic_write_json(source_file, registry)


def derive_series_url(chapter_url: str) -> str | None:
    parsed = urlsplit(canonical_url(chapter_url))
    parts = [part for part in parsed.path.split("/") if part]
    if not parts:
        return None
    final = parts[-1]
    stripped = re.sub(
        r"(?:^|[-_])(chapter|chap|ch|episode|ep)[-_]?\d+(?:\.\d+)?(?:[-_].*)?$",
        "",
        final,
        flags=re.IGNORECASE,
    ).strip("-_")
    if stripped and stripped != final:
        parts[-1] = stripped
        return canonical_url(f"{parsed.scheme}://{parsed.netloc}/{'/'.join(parts)}")
    return canonical_url(f"{parsed.scheme}://{parsed.netloc}/{'/'.join(parts[:-1])}") if len(parts) > 1 else None


def _retry_after_seconds(response: requests.Response) -> float | None:
    raw = response.headers.get("Retry-After", "").strip()
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        try:
            return max(0.0, parsedate_to_datetime(raw).timestamp() - time.time())
        except (TypeError, ValueError, OverflowError):
            return None


def request_with_retry(
    session: requests.Session,
    url: str,
    *,
    timeout: float = 30.0,
    attempts: int = 3,
    headers: dict[str, str] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
) -> requests.Response:
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            response = session.get(url, timeout=timeout, headers=headers or {}, allow_redirects=True)
            if response.status_code == 429:
                delay = _retry_after_seconds(response)
                if attempt + 1 < attempts:
                    sleeper(min(delay if delay is not None else 2**attempt + random.random(), 60.0))
                    continue
                raise UpdateError("rate_limited", "The source is rate-limiting update checks.", "quiet")
            if response.status_code == 403:
                raise UpdateError("access_blocked", "The source blocked quiet access and needs browser review.", "browser")
            if response.status_code >= 500 and attempt + 1 < attempts:
                sleeper(min(2**attempt + random.random(), 30.0))
                continue
            response.raise_for_status()
            return response
        except UpdateError:
            raise
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_error = exc
            if attempt + 1 < attempts:
                sleeper(min(2**attempt + random.random(), 30.0))
                continue
        except requests.RequestException as exc:
            raise UpdateError("http_error", str(exc), "quiet") from exc
    code = "timeout" if isinstance(last_error, requests.Timeout) else "network_error"
    raise UpdateError(code, str(last_error or "Network request failed."), "quiet")


def page_failure(html: str, final_url: str, requested_url: str) -> None:
    lowered = html.lower()
    if any(marker in lowered for marker in CHALLENGE_MARKERS):
        raise UpdateError("cloudflare", "A browser challenge prevented quiet access.", "browser")
    if any(marker in lowered for marker in LOGIN_MARKERS):
        raise UpdateError("login_required", "The chapter requires login or age verification.", "browser")
    requested_host = urlsplit(requested_url).netloc.lower().removeprefix("www.")
    final_host = urlsplit(final_url).netloc.lower().removeprefix("www.")
    if requested_host and final_host and requested_host != final_host:
        raise UpdateError("redirect_mismatch", f"The source redirected to {final_host}.", "browser")


def extract_chapter_links(series_url: str, html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    candidates: dict[tuple[str, float], str] = {}
    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href", "")).strip()
        label = anchor.get_text(" ", strip=True)
        absolute = canonical_url(urljoin(series_url, href))
        number = chapter_number(f"{label} {absolute}")
        if number is None:
            continue
        slug = series_slug_from_url(absolute)
        candidates[(slug, number)] = absolute
    return [candidates[key] for key in sorted(candidates, key=lambda item: item[1])]


def _srcset_url(value: str) -> str:
    choices = [piece.strip().split()[0] for piece in value.split(",") if piece.strip()]
    return choices[-1] if choices else ""


def extract_image_urls(chapter_url: str, html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    from panelyra.automation.reader_sources import mangareader_pages
    try:
        explicit_pages = mangareader_pages(soup, chapter_url)
    except ValueError as exc:
        raise UpdateError("invalid_reader_data", str(exc), "browser") from exc
    if explicit_pages is not None:
        return explicit_pages
    candidates: list[tuple[str, str]] = []
    attributes = ("data-src", "data-lazy-src", "data-original", "src", "srcset")
    for image in soup.find_all("img"):
        for attribute in attributes:
            raw = str(image.get(attribute, "")).strip()
            if not raw:
                continue
            candidate = _srcset_url(raw) if attribute == "srcset" else raw
            absolute = urljoin(chapter_url, candidate)
            lowered = absolute.lower()
            if not absolute.startswith(("http://", "https://")) or any(marker in lowered for marker in PLACEHOLDER_MARKERS):
                continue
            candidates.append((absolute, str(image.get("alt", ""))))
            break
    host = urlsplit(chapter_url).netloc.lower().removeprefix("www.")
    if host == "novatoon.com":
        focused = [
            (url, alt)
            for url, alt in candidates
            if "/wp-content/uploads/manga/" in url.lower()
            or bool(re.search(r"\bchapter\b.*\bpage\s*\d+\b", alt, re.IGNORECASE))
        ]
        if focused:
            candidates = focused
    ordered: list[str] = []
    seen: set[str] = set()
    for absolute, _ in candidates:
        if absolute not in seen:
            seen.add(absolute)
            ordered.append(absolute)
    script_pattern = re.compile(r"https?:\\?/\\?/[^\"'\s<>]+?\.(?:jpe?g|png|webp|gif|avif)(?:\?[^\"'\s<>]*)?", re.I)
    for match in script_pattern.findall(html):
        candidate = match.replace("\\/", "/").replace("&amp;", "&")
        if host == "novatoon.com" and "/wp-content/uploads/manga/" not in candidate.lower():
            continue
        if candidate not in seen and not any(marker in candidate.lower() for marker in PLACEHOLDER_MARKERS):
            seen.add(candidate)
            ordered.append(candidate)
    if not ordered:
        raise UpdateError("no_images_found", "No chapter images were found in the quiet page response.", "browser")
    return ordered


def _extension(url: str, content_type: str, image_format: str) -> str:
    path_suffix = Path(urlsplit(url).path).suffix.lower()
    if path_suffix in IMAGE_SUFFIXES:
        return ".jpg" if path_suffix == ".jpeg" else path_suffix
    mapping = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp", "GIF": ".gif", "AVIF": ".avif"}
    return mapping.get(image_format.upper(), ".jpg" if "jpeg" in content_type else ".png")


def download_images(
    session: requests.Session,
    chapter_url: str,
    image_urls: Sequence[str],
    images_dir: Path,
    *,
    timeout: float = 45.0,
    sleeper: Callable[[float], None] = time.sleep,
) -> list[Path]:
    images_dir.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    for index, image_url in enumerate(image_urls, 1):
        response = request_with_retry(
            session,
            image_url,
            timeout=timeout,
            headers={"Referer": chapter_url, "Accept": "image/avif,image/webp,image/*,*/*;q=0.8"},
            sleeper=sleeper,
        )
        content_type = response.headers.get("Content-Type", "").lower()
        if "text/html" in content_type:
            raise UpdateError("expired_image_url", f"Image {index} returned HTML instead of image data.", "browser")
        content = response.content
        if len(content) < 128:
            raise UpdateError("partial_download", f"Image {index} was unexpectedly small.", "quiet")
        try:
            with Image.open(io.BytesIO(content)) as image:
                from panelyra.services.library_store import decode_image
                decode_image(content)
                image_format = image.format or ""
        except Exception as exc:
            raise UpdateError("corrupt_image", f"Image {index} is not readable.", "quiet") from exc
        suffix = _extension(image_url, content_type, image_format)
        target = images_dir / f"{index:03d}{suffix}"
        target.write_bytes(content)
        saved.append(target)
    if len(saved) != len(image_urls):
        raise UpdateError("partial_download", f"Saved {len(saved)}/{len(image_urls)} images.", "quiet")
    return saved


def build_cbz(chapter_dir: Path, images: Sequence[Path]) -> Path:
    archive = chapter_dir / "chapter-pages.cbz"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as bundle:
        for image in images:
            bundle.write(image, arcname=image.name)
    with zipfile.ZipFile(archive) as bundle:
        if bundle.testzip() is not None or len(bundle.namelist()) != len(images):
            raise UpdateError("invalid_archive", "The generated CBZ failed validation.", "quiet")
    return archive


def _portable(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def _write_manifest(series_dir: Path, record: dict[str, Any]) -> None:
    manifest_path = series_dir / "manifest.json"
    payload = load_json(manifest_path, [])
    records = [dict(item) for item in payload if isinstance(item, dict)] if isinstance(payload, list) else []
    wanted = canonical_url(str(record["url"]))
    records = [item for item in records if canonical_url(str(item.get("url", wanted))) != wanted]
    records.append(record)
    records.sort(key=lambda item: chapter_number(f"{item.get('title', '')} {item.get('url', '')}") or float("inf"))
    atomic_write_json(manifest_path, records)


def quiet_download_chapter(
    url: str,
    *,
    series_slug: str,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    session: requests.Session | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    rendered_html: str = "",
    rendered_url: str = "",
) -> QuietUpdateResult:
    chapter_url = canonical_url(url)
    number = chapter_number(chapter_url)
    if number is None:
        raise UpdateError("uncertain_chapter", "The chapter number could not be determined safely.", "browser")
    client = session or requests.Session()
    client.headers.update({"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124 Safari/537.36"})
    if rendered_html:
        html = rendered_html
        final_url = rendered_url or chapter_url
    else:
        response = request_with_retry(client, chapter_url, sleeper=sleeper)
        html = response.text
        final_url = response.url
    page_failure(html, final_url, chapter_url)
    image_urls = extract_image_urls(final_url, html)
    series_dir = output_dir / safe_name(series_slug, "manhwa")
    folder = chapter_folder(number, chapter_url)
    destination = series_dir / folder
    if destination.is_dir() and (destination / "metadata.json").is_file() and (destination / "chapter-pages.cbz").is_file():
        raise UpdateError("already_exists", "A completed local chapter already exists; it was not overwritten.", "none")
    output_dir.mkdir(parents=True, exist_ok=True)
    series_dir.mkdir(parents=True, exist_ok=True)
    staged_root = Path(tempfile.mkdtemp(prefix=f".{series_dir.name}-{folder}-", dir=output_dir))
    staged = staged_root / folder
    try:
        staged.mkdir()
        images = download_images(client, chapter_url, image_urls, staged / "images", sleeper=sleeper)
        (staged / "image_urls.txt").write_text("\n".join(image_urls) + "\n", encoding="utf-8")
        archive = build_cbz(staged, images)
        title = BeautifulSoup(html, "html.parser").title
        title_text = title.get_text(" ", strip=True) if title else f"Chapter {number:g}"
        final_images = destination / "images"
        metadata = {
            "index": 1,
            "series_title": series_dir.name.replace("-", " ").title(),
            "series_slug": series_dir.name,
            "series_dir": _portable(series_dir),
            "chapter_folder": folder,
            "url": chapter_url,
            "title": title_text,
            "image_count": len(images),
            "saved_image_count": len(images),
            "images_dir": _portable(final_images),
            "archive_file": _portable(destination / archive.name),
            "character_count": 0,
            "saved_at_epoch": int(time.time()),
            "reader_layout": "quiet-http",
            "quiet_update": True,
        }
        atomic_write_json(staged / "metadata.json", metadata)
        if destination.exists():
            raise UpdateError("existing_incomplete", "A local chapter folder already exists and needs review.", "browser")
        os.replace(staged, destination)
        record = {
            "index": 1,
            "url": chapter_url,
            "title": title_text,
            "text_file": "",
            "image_urls_file": _portable(destination / "image_urls.txt"),
            "image_count": len(images),
            "character_count": 0,
            "status": "ok",
            "error": "",
            "images_dir": _portable(final_images),
            "archive_file": _portable(destination / "chapter-pages.cbz"),
        }
        try:
            _write_manifest(series_dir, record)
        except Exception as exc:
            quarantine = series_dir / f".{folder}.failed-{int(time.time())}"
            if destination.exists():
                os.replace(destination, quarantine)
            raise UpdateError(
                "manifest_write_failed",
                f"Chapter files were preserved at {quarantine.name}, but finalization failed.",
                "quiet",
            ) from exc
        return QuietUpdateResult(chapter_url, series_dir.name, number, "ok", len(images))
    finally:
        shutil.rmtree(staged_root, ignore_errors=True)


def headless_download_chapter(
    url: str,
    *,
    series_slug: str,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
) -> QuietUpdateResult:
    """Render a protected chapter invisibly, then reuse the transactional saver."""
    driver_session = bot.build_driver(
        headless=True,
        chrome_profile_dir=DEFAULT_DOWNLOAD_CHROME_PROFILE,
        browser_lock_path=DOWNLOAD_BROWSER_LOCK,
    )
    try:
        driver = driver_session.driver
        if not bot.load_url_with_retries(driver, canonical_url(url), "quiet updater headless review"):
            raise UpdateError("headless_load_failed", "Headless Chrome could not load the chapter.", "browser")
        stable = 0
        previous_height = 0
        for _ in range(30):
            height = int(driver.execute_script("return Math.max(document.body.scrollHeight, document.documentElement.scrollHeight)") or 0)
            driver.execute_script("window.scrollTo(0, arguments[0])", height)
            time.sleep(0.2)
            stable = stable + 1 if height == previous_height else 0
            previous_height = height
            if stable >= 3:
                break
        session = requests.Session()
        session.headers.update({"User-Agent": driver.execute_script("return navigator.userAgent")})
        for cookie in driver.get_cookies():
            session.cookies.set(cookie["name"], cookie["value"], domain=cookie.get("domain"), path=cookie.get("path", "/"))
        return quiet_download_chapter(
            url,
            series_slug=series_slug,
            output_dir=output_dir,
            session=session,
            rendered_html=driver.page_source,
            rendered_url=driver.current_url,
        )
    finally:
        bot.close_driver_session(driver_session)


def _failure_key(url: str) -> str:
    return hashlib.sha256(canonical_url(url).encode("utf-8")).hexdigest()[:20]


def record_failure(
    result: QuietUpdateResult,
    failure_file: Path = DEFAULT_FAILURE_FILE,
) -> None:
    payload = load_json(failure_file, {})
    failures = payload if isinstance(payload, dict) else {}
    key = _failure_key(result.url)
    previous = failures.get(key, {}) if isinstance(failures.get(key), dict) else {}
    attempts = int(previous.get("attempts", 0)) + 1
    permanent = result.error_code in {"uncertain_series", "uncertain_chapter", "redirect_mismatch", "login_required"} or attempts >= 3
    failures[key] = {
        "url": canonical_url(result.url),
        "series": result.series,
        "chapter": result.chapter,
        "attempts": attempts,
        "reason_code": result.error_code,
        "message": result.error,
        "recommended_recovery": result.recovery or "quiet",
        "source_fingerprint": result.source_fingerprint,
        "first_failed_at": previous.get("first_failed_at", utc_now()),
        "last_failed_at": utc_now(),
        "suppressed": permanent,
    }
    atomic_write_json(failure_file, failures)


def clear_failure(url: str, failure_file: Path = DEFAULT_FAILURE_FILE) -> None:
    payload = load_json(failure_file, {})
    if isinstance(payload, dict) and payload.pop(_failure_key(url), None) is not None:
        atomic_write_json(failure_file, payload)


@contextmanager
def updater_lock(lock_file: Path = DEFAULT_LOCK_FILE) -> Iterator[None]:
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_file.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise UpdateError("already_running", "Another Panelyra library update is already running.", "none") from exc
    try:
        handle.seek(0)
        handle.truncate()
        json.dump({"pid": os.getpid(), "started_at": utc_now()}, handle)
        handle.flush()
        os.fsync(handle.fileno())
        yield
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _suppressed_urls(failure_file: Path) -> set[str]:
    payload = load_json(failure_file, {})
    if not isinstance(payload, dict):
        return set()
    return {
        canonical_url(str(item.get("url", "")))
        for item in payload.values()
        if isinstance(item, dict) and item.get("suppressed") and item.get("url")
    }


def unsuppress_changed_source(series: str, fingerprint: str, failure_file: Path) -> set[str]:
    """Release permanent failures when their series landing page has changed."""
    payload = load_json(failure_file, {})
    if not isinstance(payload, dict):
        return set()
    released: set[str] = set()
    changed = False
    for item in payload.values():
        if not isinstance(item, dict) or item.get("series") != series or not item.get("suppressed"):
            continue
        previous = str(item.get("source_fingerprint", ""))
        if previous and previous != fingerprint:
            item["suppressed"] = False
            item["source_changed_at"] = utc_now()
            released.add(canonical_url(str(item.get("url", ""))))
            changed = True
    if changed:
        atomic_write_json(failure_file, payload)
    return released


def run_quiet_library_once(
    *,
    library_root: Path = DEFAULT_OUTPUT_DIR,
    history_file: Path = DEFAULT_HISTORY_FILE,
    status_file: Path = DEFAULT_STATUS_FILE,
    failure_file: Path = DEFAULT_FAILURE_FILE,
    source_file: Path = DEFAULT_SOURCE_FILE,
    session: requests.Session | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    host_delay: tuple[float, float] = (2.0, 3.0),
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
) -> list[QuietUpdateResult]:
    started = utc_now()
    results: list[QuietUpdateResult] = []
    with updater_lock():
        def publish(current_series: str = "", current_url: str = "", finished: bool = False) -> None:
            atomic_write_json(status_file, {
                "mode": "quiet", "state": "finished" if finished else "running",
                "pid": os.getpid(), "started_at": started, "updated_at": utc_now(),
                "current_series": current_series, "current_url": current_url,
                "successful": sum(item.status == "ok" for item in results),
                "failed": sum(item.status == "failed" for item in results),
                "results": [asdict(item) for item in results],
            })
        publish()
        discovered = discover_library_sources(library_root, source_file)
        persist_source_registry(discovered, source_file)
        history = normalized_history(history_file)
        suppressed = _suppressed_urls(failure_file)
        client = session or requests.Session()
        for series_slug, source in discovered.items():
            publish(series_slug)
            highest = source.get("highest_local")
            if highest is None or not source.get("series_urls"):
                if highest is not None:
                    result = QuietUpdateResult("", series_slug, None, "failed", error_code="uncertain_series", error="No reliable series landing URL is recorded.", recovery="browser")
                    results.append(result)
                continue
            for landing_url in source["series_urls"]:
                publish(series_slug, str(landing_url))
                if canonical_url(str(landing_url)) in suppressed:
                    logging.info("Skipping permanently blocked source: %s", landing_url)
                    results.append(QuietUpdateResult(str(landing_url), series_slug, None, "skipped", error_code="source_suppressed", error="Source needs attention; automatic retries are paused.", recovery="none"))
                    continue
                try:
                    response = request_with_retry(client, str(landing_url), sleeper=sleeper)
                    page_failure(response.text, response.url, str(landing_url))
                    available = extract_chapter_links(str(landing_url), response.text)
                    source_fingerprint = hashlib.sha256(response.text.encode("utf-8", errors="replace")).hexdigest()
                    suppressed.difference_update(unsuppress_changed_source(series_slug, source_fingerprint, failure_file))
                except UpdateError as exc:
                    result = QuietUpdateResult(str(landing_url), series_slug, None, "failed", error_code=exc.code, error=str(exc), recovery=exc.recovery)
                    results.append(result)
                    record_failure(result, failure_file)
                    continue
                expected_slug = safe_name(series_slug, "manhwa")
                candidates = []
                for url in available:
                    number = chapter_number(url)
                    remote_slug = series_slug_from_url(url)
                    if number is not None and number > float(highest) and (expected_slug in remote_slug or remote_slug in expected_slug):
                        candidates.append(url)
                for chapter_url in sorted(set(candidates), key=lambda value: chapter_number(value) or float("inf")):
                    normalized = canonical_url(chapter_url)
                    if normalized in history or normalized in suppressed:
                        continue
                    publish(series_slug, chapter_url)
                    try:
                        result = quiet_download_chapter(chapter_url, series_slug=series_slug, output_dir=library_root, session=client, sleeper=sleeper)
                        record_success(chapter_url, history_file)
                        history.add(normalized)
                        clear_failure(chapter_url, failure_file)
                    except UpdateError as exc:
                        result = QuietUpdateResult(chapter_url, series_slug, chapter_number(chapter_url), "failed", error_code=exc.code, error=str(exc), recovery=exc.recovery, source_fingerprint=source_fingerprint)
                        record_failure(result, failure_file)
                    except Exception as exc:
                        logging.exception("Quiet chapter update failed: %s", chapter_url)
                        result = QuietUpdateResult(chapter_url, series_slug, chapter_number(chapter_url), "failed", error_code="unexpected_error", error=str(exc), recovery="quiet", source_fingerprint=source_fingerprint)
                        record_failure(result, failure_file)
                    results.append(result)
                    publish(series_slug, chapter_url)
                    if host_delay[1] > 0:
                        sleeper(random.uniform(*host_delay))
        finished = utc_now()
        status = {
            "mode": "quiet",
            "state": "finished",
            "pid": os.getpid(),
            "started_at": started,
            "finished_at": finished,
            "next_check_epoch": int(time.time()) + interval_seconds,
            "discovered_series": len(discovered),
            "series": discovered,
            "new_chapters": sum(item.chapter is not None for item in results),
            "successful": sum(item.status == "ok" for item in results),
            "failed": sum(item.status == "failed" for item in results),
            "skipped": sum(item.status == "skipped" for item in results),
            "results": [asdict(item) for item in results],
        }
        atomic_write_json(status_file, status)
    return results


def retry_failed_updates(
    *,
    mode: str,
    failure_file: Path = DEFAULT_FAILURE_FILE,
    history_file: Path = DEFAULT_HISTORY_FILE,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
) -> list[QuietUpdateResult]:
    payload = load_json(failure_file, {})
    failures = [item for item in payload.values() if isinstance(item, dict) and item.get("url")] if isinstance(payload, dict) else []
    results: list[QuietUpdateResult] = []
    for item in failures:
        url = str(item["url"])
        series = str(item.get("series") or series_slug_from_url(url))
        if str(item.get("reason_code", "")) in AUTH_FAILURE_CODES:
            results.append(
                QuietUpdateResult(
                    url,
                    series,
                    chapter_number(url),
                    "skipped",
                    error_code=str(item.get("reason_code", "login_required")),
                    error="Skipped because this source requires login or verification.",
                    recovery="none",
                )
            )
            continue
        try:
            if mode == "quiet":
                result = quiet_download_chapter(url, series_slug=series, output_dir=output_dir)
            else:
                owner = browser_lock_owner(DOWNLOAD_BROWSER_LOCK)
                if owner:
                    raise UpdateError("browser_busy", "Capture's browser is already owned by another run.", mode)
                if mode == "headless-browser":
                    result = headless_download_chapter(url, series_slug=series, output_dir=output_dir)
                else:
                    downloaded = download_manhwa_ocr(start_url=url, max_chapters=1, output_dir=output_dir, reader_layout="auto", save_raw_images=True, create_archive=True, save_ocr=False)
                    if not downloaded or any(getattr(entry, "status", "") != "ok" for entry in downloaded):
                        raise UpdateError("browser_download_failed", "The browser downloader did not finish successfully.", "browser")
                    result = QuietUpdateResult(url, series, chapter_number(url), "ok", sum(int(getattr(entry, "image_count", 0)) for entry in downloaded))
            record_success(url, history_file)
            clear_failure(url, failure_file)
        except UpdateError as exc:
            result = QuietUpdateResult(url, series, chapter_number(url), "failed", error_code=exc.code, error=str(exc), recovery=exc.recovery)
            record_failure(result, failure_file)
        results.append(result)
    return results


def run_quiet_tracker(*, interval_seconds: int = DEFAULT_INTERVAL_SECONDS, **kwargs: Any) -> None:
    if interval_seconds < MIN_INTERVAL_SECONDS:
        raise ValueError(f"interval_seconds must be at least {MIN_INTERVAL_SECONDS}")
    while True:
        try:
            run_quiet_library_once(interval_seconds=interval_seconds, **kwargs)
        except UpdateError as exc:
            logging.warning("Library update skipped: %s", exc)
        time.sleep(interval_seconds)


def launch_agent_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"


def install_launch_agent(interval_seconds: int = DEFAULT_INTERVAL_SECONDS) -> Path:
    if interval_seconds < MIN_INTERVAL_SECONDS:
        raise ValueError(f"interval_seconds must be at least {MIN_INTERVAL_SECONDS}")
    python = PROJECT_ROOT / ".venv" / "bin" / "python"
    if not python.is_file():
        raise FileNotFoundError(f"Panelyra Python was not found at {python}")
    logs = Path(tempfile.gettempdir()) / "panelyra_logs"
    logs.mkdir(parents=True, exist_ok=True)
    target = launch_agent_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "Label": LAUNCH_AGENT_LABEL,
        "ProgramArguments": [
            str(python),
            str(PROJECT_ROOT / "series_update_tracker.py"),
            "--library",
            "--quiet",
            "--once",
        ],
        "WorkingDirectory": str(PROJECT_ROOT),
        "StartInterval": interval_seconds,
        "RunAtLoad": False,
        "StandardOutPath": str(logs / "library-updater.log"),
        "StandardErrorPath": str(logs / "library-updater-error.log"),
        "ProcessType": "Background",
    }
    temporary = target.with_suffix(".plist.tmp")
    temporary.write_bytes(plistlib.dumps(payload, sort_keys=True))
    os.replace(temporary, target)
    subprocess.run(["launchctl", "unload", str(target)], check=False, capture_output=True)
    subprocess.run(["launchctl", "load", str(target)], check=True, capture_output=True)
    return target


def uninstall_launch_agent() -> bool:
    target = launch_agent_path()
    if not target.exists():
        return False
    subprocess.run(["launchctl", "unload", str(target)], check=False, capture_output=True)
    target.unlink()
    return True
