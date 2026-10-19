"""Standalone series update and backward traversal wrappers for Capture."""

from __future__ import annotations

import argparse
import json
import logging
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from automation_runtime import atomic_write_json, normalize_web_url
from manhwa_ocr_downloader import DEFAULT_OUTPUT_DIR, download_manhwa_ocr


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_HISTORY_FILE = PROJECT_ROOT / "data/updates/downloaded_chapters.json"
DEFAULT_INTERVAL_SECONDS = 10_800
MADARA_PREV_SELECTORS = "a.btn.prev_page, a.prev_page"


DownloadFunction = Callable[..., Sequence[object]]


@dataclass(frozen=True)
class WrapperDownloadResult:
    url: str
    status: str
    recorded: bool
    error: str = ""


def canonical_history_url(url: str) -> str:
    normalized = normalize_web_url(url)
    parsed = urlsplit(normalized)
    if parsed.hostname == "comic.naver.com":
        from urllib.parse import parse_qs, urlencode
        query = parse_qs(parsed.query)
        identity = {key: query[key][0] for key in ("titleId", "no") if key in query}
        return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/"), urlencode(identity), ""))
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/"), "", ""))


def load_download_history(history_file: Path = DEFAULT_HISTORY_FILE) -> set[str]:
    if not history_file.is_file():
        return set()
    try:
        payload = json.loads(history_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logging.warning("Could not read %s; treating history as empty.", history_file)
        return set()
    if isinstance(payload, list):
        values = payload
    elif isinstance(payload, dict):
        values = payload.get("downloaded") or payload.get("chapters") or payload.get("urls") or []
    else:
        values = []
    return {
        canonical_history_url(str(value))
        for value in values
        if isinstance(value, str) and value.strip()
    }


def save_download_history(urls: Iterable[str], history_file: Path = DEFAULT_HISTORY_FILE) -> None:
    atomic_write_json(history_file, sorted({canonical_history_url(url) for url in urls}))


def record_downloaded_chapter(url: str, history_file: Path = DEFAULT_HISTORY_FILE) -> None:
    history = load_download_history(history_file)
    history.add(canonical_history_url(url))
    save_download_history(history, history_file)


def chapter_number_from_url_or_text(value: str) -> tuple[int, float | str]:
    match = re.search(r"(?:chapter|chap|ch|episode|ep)[-/_.\s]*(\d+(?:\.\d+)?)", value, re.IGNORECASE)
    if match:
        return (0, float(match.group(1)))
    return (1, value.lower())


def chapter_number(value: str) -> float | None:
    parsed = chapter_number_from_url_or_text(value)
    if parsed[0] == 0 and isinstance(parsed[1], float):
        return parsed[1]
    return None


def chapter_series_slug(value: str) -> str:
    parsed = urlsplit(normalize_web_url(value))
    candidate = parsed.path.rstrip("/").split("/")[-1]
    candidate = re.sub(r"(?:^|[-_])(chapter|chap|ch|episode|ep)[-_]?\d+(?:\.\d+)?$", "", candidate, flags=re.IGNORECASE)
    return candidate.strip("-_").lower()


def series_url_from_chapter_url(url: str) -> str | None:
    parsed = urlsplit(normalize_web_url(url))
    parts = [part for part in parsed.path.split("/") if part]
    for index in range(len(parts) - 1, -1, -1):
        if chapter_number(parts[index]) is not None:
            parent_path = "/" + "/".join(parts[:index])
            return canonical_history_url(urlunsplit((parsed.scheme, parsed.netloc, parent_path or "/", "", "")))
    return None


def fetch_html(url: str, *, timeout: float = 30.0, session: requests.Session | None = None) -> str:
    client = session or requests.Session()
    response = client.get(
        normalize_web_url(url),
        timeout=timeout,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
            )
        },
    )
    response.raise_for_status()
    return response.text


def extract_chapter_links(series_url: str, html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    links: dict[str, str] = {}
    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href", "")).strip()
        if "chapter-" not in href.lower():
            continue
        absolute = canonical_history_url(urljoin(series_url, href))
        label = anchor.get_text(" ", strip=True) or absolute
        links[absolute] = label
    return sorted(links, key=lambda url: chapter_number_from_url_or_text(f"{links[url]} {url}"))


def latest_chapter_links_only(available_urls: Sequence[str], local_urls: Sequence[str]) -> list[str]:
    local_numbers = [number for number in (chapter_number(url) for url in local_urls) if number is not None]
    if not local_numbers:
        return list(available_urls)
    highest_local = max(local_numbers)
    local_slugs = {slug for slug in (chapter_series_slug(url) for url in local_urls) if slug}
    latest = [
        url for url in available_urls
        if (
            chapter_number(url) is not None
            and chapter_number(url) > highest_local
            and (not local_slugs or chapter_series_slug(url) in local_slugs)
        )
    ]
    return sorted(latest, key=lambda url: chapter_number_from_url_or_text(url))


def library_series_sources(
    library_root: Path = DEFAULT_OUTPUT_DIR,
) -> dict[str, dict[str, object]]:
    sources: dict[str, dict[str, object]] = {}
    if not library_root.is_dir():
        return sources
    for series_dir in sorted(path for path in library_root.iterdir() if path.is_dir()):
        local_urls: list[str] = []
        series_urls: set[str] = set()
        for metadata_path in series_dir.glob("*/metadata.json"):
            try:
                payload = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            url = str(payload.get("url", "")).strip()
            if not url:
                continue
            local_urls.append(canonical_history_url(url))
            derived = series_url_from_chapter_url(url)
            if derived:
                series_urls.add(derived)
            elif chapter_number(url) is None:
                series_urls.add(canonical_history_url(url))
        if series_urls:
            sources[series_dir.name] = {
                "series_urls": sorted(series_urls),
                "local_urls": sorted({canonical_history_url(url) for url in local_urls}),
            }
    return sources


def find_madara_previous_url(current_url: str, html: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    anchor = soup.select_one(MADARA_PREV_SELECTORS)
    if not anchor:
        return None
    href = str(anchor.get("href", "")).strip()
    if not href or href == "#":
        return None
    previous = canonical_history_url(urljoin(current_url, href))
    return previous if previous != canonical_history_url(current_url) else None


def successful_download(results: Sequence[object]) -> bool:
    if not results:
        return False
    for result in results:
        status = getattr(result, "status", "")
        if status != "ok":
            return False
    return True


def call_existing_downloader(
    url: str,
    *,
    download_func: DownloadFunction = download_manhwa_ocr,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    save_ocr: bool = False,
) -> bool:
    results = download_func(
        start_url=url,
        max_chapters=1,
        output_dir=output_dir,
        reader_layout="auto",
        save_raw_images=True,
        create_archive=True,
        save_ocr=save_ocr,
    )
    return successful_download(results)


def download_missing_series_chapters_once(
    series_urls: Sequence[str],
    *,
    history_file: Path = DEFAULT_HISTORY_FILE,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    save_ocr: bool = False,
    download_func: DownloadFunction = download_manhwa_ocr,
    session: requests.Session | None = None,
) -> list[WrapperDownloadResult]:
    history = load_download_history(history_file)
    outcomes: list[WrapperDownloadResult] = []
    for series_url in series_urls:
        html = fetch_html(series_url, session=session)
        missing = [url for url in extract_chapter_links(series_url, html) if canonical_history_url(url) not in history]
        for chapter_url in missing:
            try:
                if call_existing_downloader(chapter_url, download_func=download_func, output_dir=output_dir, save_ocr=save_ocr):
                    record_downloaded_chapter(chapter_url, history_file)
                    history.add(canonical_history_url(chapter_url))
                    outcomes.append(WrapperDownloadResult(chapter_url, "ok", True))
                else:
                    outcomes.append(WrapperDownloadResult(chapter_url, "failed", False, "Downloader returned no successful result."))
            except Exception as exc:
                logging.exception("Series update download failed for %s", chapter_url)
                outcomes.append(WrapperDownloadResult(chapter_url, "failed", False, str(exc)))
    return outcomes


def download_library_latest_chapters_once(
    *,
    library_root: Path = DEFAULT_OUTPUT_DIR,
    history_file: Path = DEFAULT_HISTORY_FILE,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    save_ocr: bool = False,
    download_func: DownloadFunction = download_manhwa_ocr,
    session: requests.Session | None = None,
) -> list[WrapperDownloadResult]:
    history = load_download_history(history_file)
    outcomes: list[WrapperDownloadResult] = []
    for source in library_series_sources(library_root).values():
        local_urls = [str(url) for url in source["local_urls"]]
        for series_url in source["series_urls"]:
            html = fetch_html(str(series_url), session=session)
            available = extract_chapter_links(str(series_url), html)
            latest_missing = [
                url for url in latest_chapter_links_only(available, local_urls)
                if canonical_history_url(url) not in history
            ]
            for chapter_url in latest_missing:
                try:
                    if call_existing_downloader(chapter_url, download_func=download_func, output_dir=output_dir, save_ocr=save_ocr):
                        record_downloaded_chapter(chapter_url, history_file)
                        history.add(canonical_history_url(chapter_url))
                        local_urls.append(canonical_history_url(chapter_url))
                        outcomes.append(WrapperDownloadResult(chapter_url, "ok", True))
                    else:
                        outcomes.append(WrapperDownloadResult(chapter_url, "failed", False, "Downloader returned no successful result."))
                except Exception as exc:
                    logging.exception("Library latest download failed for %s", chapter_url)
                    outcomes.append(WrapperDownloadResult(chapter_url, "failed", False, str(exc)))
    return outcomes


def run_library_latest_tracker(
    *,
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
    library_root: Path = DEFAULT_OUTPUT_DIR,
    history_file: Path = DEFAULT_HISTORY_FILE,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    save_ocr: bool = False,
) -> None:
    while True:
        download_library_latest_chapters_once(
            library_root=library_root,
            history_file=history_file,
            output_dir=output_dir,
            save_ocr=save_ocr,
        )
        time.sleep(interval_seconds)


def run_series_update_tracker(
    series_urls: Sequence[str],
    *,
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
    history_file: Path = DEFAULT_HISTORY_FILE,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    save_ocr: bool = False,
) -> None:
    while True:
        download_missing_series_chapters_once(
            series_urls,
            history_file=history_file,
            output_dir=output_dir,
            save_ocr=save_ocr,
        )
        time.sleep(interval_seconds)


def download_backward_from_madara_prev(
    start_url: str,
    count: int,
    *,
    history_file: Path = DEFAULT_HISTORY_FILE,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    save_ocr: bool = False,
    download_func: DownloadFunction = download_manhwa_ocr,
    session: requests.Session | None = None,
    delay_seconds: tuple[float, float] = (2.0, 3.0),
) -> list[WrapperDownloadResult]:
    if count < 1:
        raise ValueError("count must be at least 1")
    current_url = canonical_history_url(start_url)
    outcomes: list[WrapperDownloadResult] = []
    for index in range(count):
        html = fetch_html(current_url, session=session)
        previous_url = find_madara_previous_url(current_url, html)
        try:
            if call_existing_downloader(current_url, download_func=download_func, output_dir=output_dir, save_ocr=save_ocr):
                record_downloaded_chapter(current_url, history_file)
                outcomes.append(WrapperDownloadResult(current_url, "ok", True))
            else:
                outcomes.append(WrapperDownloadResult(current_url, "failed", False, "Downloader returned no successful result."))
        except Exception as exc:
            logging.exception("Backward download failed for %s", current_url)
            outcomes.append(WrapperDownloadResult(current_url, "failed", False, str(exc)))
        if not previous_url or index == count - 1:
            break
        time.sleep(random.uniform(*delay_seconds))
        current_url = previous_url
    return outcomes


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Track and download Madara manhwa updates without changing the core downloader.")
    root.add_argument("--history-file", type=Path, default=DEFAULT_HISTORY_FILE)
    root.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    root.add_argument("--save-ocr", action="store_true", help="Also generate OCR transcripts.")
    root.add_argument("--series-url", action="append", help="Series URL for the default watch mode.")
    root.add_argument("--interval-seconds", type=int, default=DEFAULT_INTERVAL_SECONDS)
    root.add_argument("--once", action="store_true", help="Run one poll instead of looping forever.")
    root.add_argument("--library", action="store_true", help="Discover chapters for saved series without downloading. Results appear in Panelyra.")
    root.add_argument("--library-root", type=Path, default=DEFAULT_OUTPUT_DIR)
    mode = root.add_mutually_exclusive_group()
    mode.add_argument("--quiet", action="store_true", help="Download through HTTP without opening Chrome.")
    mode.add_argument("--headless-browser", action="store_true", help="Retry queued failures in invisible Chrome.")
    mode.add_argument("--browser", action="store_true", help="Use the preserved visible-browser downloader explicitly.")
    root.add_argument("--retry-failed", action="store_true", help="Retry chapters recorded in failed_updates.json.")
    root.add_argument("--install-schedule", action="store_true", help="Install the three-hour discovery-only macOS background job.")
    root.add_argument("--uninstall-schedule", action="store_true", help="Remove the macOS background job.")
    subparsers = root.add_subparsers(dest="command")

    tracker = subparsers.add_parser("watch", help="Poll one or more series pages forever.")
    tracker.add_argument("--series-url", action="append", required=True)
    tracker.add_argument("--interval-seconds", type=int, default=DEFAULT_INTERVAL_SECONDS)
    tracker.add_argument("--once", action="store_true", help="Run one poll instead of looping forever.")

    backward = subparsers.add_parser("backward", help="Download backward through Madara previous-chapter links.")
    backward.add_argument("--start-url", required=True)
    backward.add_argument("--count", type=int, required=True)
    return root


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = parser().parse_args(argv)
    if args.install_schedule or args.uninstall_schedule:
        from panelyra.automation.quiet_updates import install_launch_agent, uninstall_launch_agent

        if args.install_schedule:
            path = install_launch_agent(args.interval_seconds)
            logging.info("Installed discovery-only library updater: %s", path)
        else:
            logging.info("Removed quiet library updater: %s", uninstall_launch_agent())
        return 0
    if args.library:
        from panelyra.services.reading_updates import scheduled
        scheduled(args.library_root.parent)
        return 0
    if args.retry_failed:
        from panelyra.automation.quiet_updates import retry_failed_updates

        selected_mode = "browser" if args.browser else "headless-browser" if args.headless_browser else "quiet"
        outcomes = retry_failed_updates(
            mode=selected_mode,
            history_file=args.history_file,
            output_dir=args.output_dir,
        )
    elif args.command == "backward":
        outcomes = download_backward_from_madara_prev(
            args.start_url,
            args.count,
            history_file=args.history_file,
            output_dir=args.output_dir,
            save_ocr=args.save_ocr,
        )
    else:
        series_urls = getattr(args, "series_url", None)
        if not series_urls:
            raise SystemExit("Use watch --series-url <url> or backward --start-url <url> --count <n>.")
        if getattr(args, "once", False):
            outcomes = download_missing_series_chapters_once(
                series_urls,
                history_file=args.history_file,
                output_dir=args.output_dir,
                save_ocr=args.save_ocr,
            )
        else:
            run_series_update_tracker(
                series_urls,
                interval_seconds=args.interval_seconds,
                history_file=args.history_file,
                output_dir=args.output_dir,
                save_ocr=args.save_ocr,
            )
            outcomes = []
    for outcome in outcomes:
        logging.info("%s %s", outcome.status.upper(), outcome.url)
    return 0 if all(outcome.status == "ok" for outcome in outcomes) else 1
