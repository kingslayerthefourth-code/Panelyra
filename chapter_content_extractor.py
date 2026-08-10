"""Utilities for extracting chapter context from novel text or manhwa images."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from selenium.webdriver.common.by import By


_EASYOCR_READER: Any | None = None
EASYOCR_CACHE_DIR = Path(__file__).resolve().parent / ".easyocr"

NON_NARRATIVE_SELECTORS = ("script", "style", "iframe", "ins", "a", "button")
CONTEXT_LIMIT = 3_000
CONTEXT_HEAD = 1_200
CONTEXT_TAIL = 1_200
CONTEXT_DIVIDER = "\n\n--- [context truncated] ---\n\n"
DEFAULT_OCR_CONFIDENCE = 0.45
DEFAULT_IMAGE_TIMEOUT_SECONDS = 20.0
IMAGE_URL_ATTRIBUTES = (
    "currentSrc",
    "data-src",
    "data-lazy-src",
    "data-original",
    "data-cfsrc",
    "data-large_image",
    "src",
    "data-srcset",
    "srcset",
)
NON_STORY_LINE_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bdiscord\b",
        r"\bscanlation(s)?\b",
        r"\bscans?\b",
        r"\basura\s*scans?\b",
        r"\bjoin\s+(our|the)\b",
        r"\bsupport\s+(us|the\s+team)\b",
        r"\bpatreon\b|\bko-?fi\b|\bdonate\b",
        r"\btranslator\b|\btranslation\b|\btypesetter\b|\bproofreader\b|\bcleaner\b|\bredrawer\b|\buploader\b",
        r"\braws?\b|\braw\s+provider\b",
        r"\bcredit(s)?\b|\bstaff\b",
        r"\bfollow\s+(us|our)\b|\bvisit\s+(us|our)\b",
        r"\breupload\b|\bdo\s+not\s+repost\b|\ball\s+rights\s+reserved\b",
        r"\bhttps?://|\bwww\.",
        r"\bchapter\s+comments?\b|\bleave\s+a\s+comment\b|\bsay\s+thanks\b",
        r"\bad(s)?\b|\badvert(s|isement|isements|ising)?\b|\bpop-?ups?\b|\bpromoted?\b|\bpromotions?\b|\bsponsor(ed|s|ship)?\b",
    )
]


def _get_easyocr_reader() -> Any:
    global _EASYOCR_READER
    if _EASYOCR_READER is None:
        try:
            import easyocr
        except ImportError as exc:
            raise RuntimeError(
                "Manhwa OCR mode requires easyocr. Install project dependencies with "
                "`python -m pip install -r requirements.txt`."
            ) from exc

        _EASYOCR_READER = easyocr.Reader(
            ["en"],
            model_storage_directory=str(EASYOCR_CACHE_DIR / "model"),
            user_network_directory=str(EASYOCR_CACHE_DIR / "user_network"),
            download_enabled=False,
        )
    return _EASYOCR_READER


def _normalize_text_parts(parts: list[str]) -> list[str]:
    normalized: list[str] = []
    for part in parts:
        clean = " ".join(str(part).split())
        if clean:
            normalized.append(clean)
    return normalized


def _is_non_story_text(value: str) -> bool:
    clean = " ".join(value.split()).strip()
    if not clean:
        return True
    lower = clean.lower()
    if any(pattern.search(clean) for pattern in NON_STORY_LINE_PATTERNS):
        return True
    words = re.findall(r"[a-z][a-z0-9'-]{2,}", lower)
    if not words:
        return len(clean) < 4
    promo_words = {
        "discord",
        "scanlation",
        "scanlations",
        "scans",
        "translator",
        "typesetter",
        "proofreader",
        "cleaner",
        "redrawer",
        "uploader",
        "patreon",
        "donate",
        "ad",
        "ads",
        "advert",
        "adverts",
        "advertisement",
        "advertisements",
        "advertising",
        "popup",
        "popups",
        "promo",
        "promos",
        "promoted",
        "promotion",
        "promotions",
        "sponsor",
        "sponsored",
        "sponsors",
        "sponsorship",
        "support",
        "credit",
        "credits",
        "staff",
    }
    promo_hits = sum(1 for word in words if word in promo_words)
    return promo_hits >= max(1, len(words) // 3)


def _filter_story_text_parts(parts: list[str]) -> list[str]:
    normalized = _normalize_text_parts(parts)
    filtered = [part for part in normalized if not _is_non_story_text(part)]
    removed = len(normalized) - len(filtered)
    if removed:
        logging.info("Story filter removed %s non-story OCR/text fragment(s).", removed)
    return filtered


def _clip_context(text: str) -> str:
    if len(text) <= CONTEXT_LIMIT:
        return text
    return f"{text[:CONTEXT_HEAD].rstrip()}{CONTEXT_DIVIDER}{text[-CONTEXT_TAIL:].lstrip()}"


def _coerce_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _srcset_url(value: str) -> str:
    candidates: list[tuple[float, str]] = []
    for raw_part in value.split(","):
        part = raw_part.strip()
        if not part:
            continue
        pieces = part.split()
        url = pieces[0].strip()
        score = 1.0
        if len(pieces) > 1:
            descriptor = pieces[1].strip().lower()
            try:
                if descriptor.endswith("w"):
                    score = float(descriptor[:-1])
                elif descriptor.endswith("x"):
                    score = float(descriptor[:-1]) * 1000
            except ValueError:
                score = 1.0
        candidates.append((score, url))
    if not candidates:
        return ""
    return max(candidates, key=lambda item: item[0])[1]


def _image_source_url(image: Any, base_url: str) -> str:
    for attribute in IMAGE_URL_ATTRIBUTES:
        raw_value = (image.get_attribute(attribute) or "").strip()
        if not raw_value:
            continue
        candidate = _srcset_url(raw_value) if "srcset" in attribute.lower() else raw_value
        if not candidate or candidate.startswith("data:"):
            continue
        return urljoin(base_url, candidate)
    return ""


def _element_page_y(driver: Any, element: Any) -> float:
    try:
        return float(
            driver.execute_script(
                "const rect = arguments[0].getBoundingClientRect(); return rect.top + window.scrollY;",
                element,
            )
        )
    except Exception:
        return 0.0


def _inside_vertical_range(driver: Any, element: Any, min_y: float | None, max_y: float | None) -> bool:
    if min_y is None and max_y is None:
        return True
    page_y = _element_page_y(driver, element)
    if min_y is not None and page_y < min_y:
        return False
    if max_y is not None and page_y > max_y:
        return False
    return True


def _find_container(driver: Any, container_selector: str) -> Any:
    try:
        return driver.find_element(By.CSS_SELECTOR, container_selector)
    except Exception as exc:
        logging.warning(
            "Chapter container selector was not found; falling back to document body. Selector: %s. Error: %s",
            container_selector,
            exc,
        )
        return driver.find_element(By.TAG_NAME, "body")


def _driver_cookie_header(driver: Any) -> str:
    try:
        cookies = driver.get_cookies()
    except Exception:
        return ""
    pairs = [
        f"{cookie.get('name')}={cookie.get('value')}"
        for cookie in cookies
        if cookie.get("name") and cookie.get("value")
    ]
    return "; ".join(pairs)


def _fetch_image_bytes(driver: Any, image_url: str, timeout_seconds: float) -> bytes | None:
    import requests

    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": getattr(driver, "current_url", ""),
    }
    cookie_header = _driver_cookie_header(driver)
    if cookie_header:
        headers["Cookie"] = cookie_header

    try:
        response = requests.get(image_url, timeout=timeout_seconds, headers=headers)
        response.raise_for_status()
    except requests.RequestException as exc:
        logging.warning("Skipping OCR image fetch failure for %s: %s", image_url, exc)
        return None

    return response.content


def _extract_novel_content(driver: Any, container_selector: str) -> str:
    try:
        from bs4 import BeautifulSoup
    except ImportError as exc:
        raise RuntimeError(
            "Novel mode requires beautifulsoup4. Install project dependencies with "
            "`python -m pip install -r requirements.txt`."
        ) from exc

    container = _find_container(driver, container_selector)
    soup = BeautifulSoup(container.get_attribute("outerHTML") or "", "html.parser")

    for node in soup.select(", ".join(NON_NARRATIVE_SELECTORS)):
        node.decompose()

    parts = _filter_story_text_parts(list(soup.stripped_strings))
    return _clip_context("\n".join(parts))


def _extract_manhwa_content(
    driver: Any,
    container_selector: str,
    img_selector: str,
    *,
    min_confidence: float = DEFAULT_OCR_CONFIDENCE,
    timeout_seconds: float = DEFAULT_IMAGE_TIMEOUT_SECONDS,
    min_y: float | None = None,
    max_y: float | None = None,
    image_urls: list[str] | None = None,
    image_files: list[str] | None = None,
) -> str:
    reader = _get_easyocr_reader()
    images: list[Any] = []
    if image_urls is None:
        container = _find_container(driver, container_selector)
        images = container.find_elements(By.CSS_SELECTOR, img_selector)
    extracted: list[str] = []
    base_url = getattr(driver, "current_url", "")
    processed = 0
    targets: list[tuple[str, Any | None]] = []

    if image_files is not None:
        for raw_path in image_files:
            image_path = Path(raw_path)
            if image_path.is_file():
                targets.append((str(image_path), None))
    elif image_urls is not None:
        seen_urls: set[str] = set()
        for raw_url in image_urls:
            image_url = urljoin(base_url, str(raw_url).strip())
            if image_url and image_url not in seen_urls:
                targets.append((image_url, None))
                seen_urls.add(image_url)
    else:
        for image in images:
            if not _inside_vertical_range(driver, image, min_y, max_y):
                continue

            image_url = _image_source_url(image, base_url)
            if not image_url:
                continue
            targets.append((image_url, image))

    for image_url, image in targets:
        local_path = Path(image_url)
        if image_files is not None and local_path.is_file():
            try:
                image_bytes = local_path.read_bytes()
            except OSError as exc:
                logging.warning("Skipping local OCR image read failure for %s: %s", local_path, exc)
                image_bytes = None
        else:
            image_bytes = _fetch_image_bytes(driver, image_url, timeout_seconds)
        if image_bytes is None and image is not None:
            try:
                image_bytes = image.screenshot_as_png
                logging.info("Using element screenshot fallback for OCR image: %s", image_url)
            except Exception as exc:
                logging.warning("Skipping OCR image screenshot failure for %s: %s", image_url, exc)
                image_bytes = None
        if image_bytes is None:
            continue

        processed += 1

        for result in reader.readtext(image_bytes):
            if len(result) < 3:
                continue
            _, text, confidence = result
            if confidence > min_confidence:
                extracted.extend(_filter_story_text_parts([text]))

    logging.info(
        "OCR scanned %s/%s matching chapter image(s) and kept %s text fragment(s).",
        processed,
        len(targets),
        len(extracted),
    )
    return _clip_context("\n".join(extracted))


def extract_chapter_content(driver: Any, container_selector: str, settings: dict[str, Any]) -> str:
    """Extract chapter text from a Selenium page using novel or manhwa settings.

    Supported settings:
    - {"mode": "novel"}
    - {"mode": "manhwa", "img_selector": "img.wp-manga-chapter-img"}
    """
    mode = str(settings.get("mode", "")).strip().lower()

    if mode == "novel":
        return _extract_novel_content(driver, container_selector)

    if mode == "manhwa":
        img_selector = str(settings.get("img_selector", "")).strip()
        if not img_selector:
            raise ValueError("Manhwa mode requires settings['img_selector'].")
        return _extract_manhwa_content(
            driver,
            container_selector,
            img_selector,
            min_confidence=_coerce_float(settings.get("confidence"), DEFAULT_OCR_CONFIDENCE),
            timeout_seconds=_coerce_float(settings.get("timeout_seconds"), DEFAULT_IMAGE_TIMEOUT_SECONDS),
            min_y=_coerce_float(settings["min_y"], 0.0) if settings.get("min_y") is not None else None,
            max_y=_coerce_float(settings["max_y"], 0.0) if settings.get("max_y") is not None else None,
            image_urls=[str(url) for url in settings.get("image_urls", [])] if settings.get("image_urls") else None,
            image_files=[str(path) for path in settings.get("image_files", [])] if settings.get("image_files") else None,
        )

    raise ValueError("settings['mode'] must be either 'novel' or 'manhwa'.")
