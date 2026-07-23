#!/usr/bin/env python3
"""
Local Selenium workflow for testing note/editor image upload plus word-art text.

Configure selectors from the dashboard for the target staging note editor.
"""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import random
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional, TextIO
from urllib.error import URLError
from urllib.request import urlopen

from selenium import webdriver
from selenium.common.exceptions import JavascriptException, TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

from automation_runtime import (
    acquire_browser_lock,
    normalize_web_url,
    release_browser_lock,
    reporter_from_environment,
)
from qa_utils import Locator, slow_type, wait_for_clickable as wait_for_locator_clickable


CHROME_DEBUG_PORT = 9222
CHROME_DEBUG_HOST = "127.0.0.1"
WAIT_SECONDS = 20
PAGE_LOAD_TIMEOUT_SECONDS = 60
KEEP_BROWSER_OPEN_ON_EXIT = True

DEFAULT_EDITOR_SELECTOR = "textarea, [contenteditable='true'], [role='textbox'], .ProseMirror, [data-slate-editor='true']"
DEFAULT_UPLOAD_INPUT_SELECTOR = "input[type='file'][accept*='image'], input[type='file']"
DEFAULT_UPLOAD_TRIGGER_SELECTOR = (
    "button[aria-label*='upload'], button[title*='upload'], "
    "button[data-testid*='upload'], button[class*='upload'], "
    "label[for*='upload'], [role='button'][aria-label*='upload']"
)
DEFAULT_SUBMIT_SELECTOR = (
    "button[type='submit'], input[type='submit'], "
    "button[aria-label*='save'], button[title*='save'], "
    "button[aria-label*='publish'], button[title*='publish'], "
    "button[data-testid*='save'], button[data-testid*='publish']"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Note image upload and word-art Selenium QA runner.")
    parser.add_argument("--note-url", required=True, help="Target note/editor URL.")
    parser.add_argument("--image-path", type=Path, required=True, help="Local image path to upload.")
    parser.add_argument("--word-art-text", required=True, help="Main text to turn into simple word art.")
    parser.add_argument("--caption", default="", help="Optional caption below the word art.")
    parser.add_argument("--style-mode", choices=("banner", "box", "stack"), default="banner")
    parser.add_argument("--text-color", default="#F4F4F5")
    parser.add_argument("--panel-color", default="#101114")
    parser.add_argument("--text-align", choices=("left", "center", "right"), default="center")
    parser.add_argument("--panel-placement", choices=("top", "bottom"), default="bottom")
    parser.add_argument("--upload-input-selector", default=DEFAULT_UPLOAD_INPUT_SELECTOR)
    parser.add_argument("--upload-trigger-selector", default=DEFAULT_UPLOAD_TRIGGER_SELECTOR)
    parser.add_argument("--editor-selector", default=DEFAULT_EDITOR_SELECTOR)
    parser.add_argument("--submit-selector", default=DEFAULT_SUBMIT_SELECTOR)
    parser.add_argument("--skip-submit", action="store_true", help="Fill the note but do not click save/publish.")
    parser.add_argument("--start-signal-file", type=Path, help="Pause until this file exists.")
    parser.add_argument("--log-file", type=Path)
    return parser.parse_args()


def configure_logging(log_file: Optional[Path]) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, mode="a", encoding="utf-8"))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
        force=True,
    )


def normalize_selectors(args: argparse.Namespace) -> None:
    args.upload_input_selector = args.upload_input_selector.strip() or DEFAULT_UPLOAD_INPUT_SELECTOR
    args.upload_trigger_selector = args.upload_trigger_selector.strip() or DEFAULT_UPLOAD_TRIGGER_SELECTOR
    args.editor_selector = args.editor_selector.strip() or DEFAULT_EDITOR_SELECTOR
    args.submit_selector = args.submit_selector.strip() or DEFAULT_SUBMIT_SELECTOR


def chrome_profile_path() -> str:
    username = getpass.getuser()
    chrome_root = Path(f"/Users/{username}/Library/Application Support/Google/Chrome")
    preferred = chrome_root / "PanelyraProfile"
    legacy = chrome_root / "AutomationProfile"
    if legacy.exists() and not preferred.exists():
        return str(legacy)
    return str(preferred)


def chrome_debugger_url() -> str:
    return f"http://{CHROME_DEBUG_HOST}:{CHROME_DEBUG_PORT}/json/version"


def existing_chrome_debug_session() -> bool:
    try:
        with urlopen(chrome_debugger_url(), timeout=2) as response:
            payload = response.read().decode("utf-8")
        data = json.loads(payload)
    except (OSError, URLError, TimeoutError, json.JSONDecodeError):
        return False
    browser = str(data.get("Browser", ""))
    return "chrome" in browser.lower() or "chromium" in browser.lower()


def build_chromedriver_service() -> Optional[Service]:
    try:
        driver_path = ChromeDriverManager().install()
    except Exception as exc:
        logging.warning("webdriver-manager could not resolve ChromeDriver: %s", exc)
        return None

    try:
        version_check = subprocess.run(
            [driver_path, "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logging.warning("ChromeDriver self-check failed before startup: %s", exc)
        return None

    if version_check.returncode != 0:
        logging.warning(
            "ChromeDriver self-check exited with status %s. Falling back to Selenium Manager. stderr=%s",
            version_check.returncode,
            version_check.stderr.strip() or "none",
        )
        return None

    logging.info("ChromeDriver self-check passed: %s", version_check.stdout.strip())
    return Service(driver_path)


def build_driver() -> tuple[WebDriver, TextIO]:
    browser_lock = acquire_browser_lock("Panelyra Studio")
    try:
        options = Options()
        if existing_chrome_debug_session():
            logging.info("Attaching to existing Chrome on %s:%s.", CHROME_DEBUG_HOST, CHROME_DEBUG_PORT)
            options.debugger_address = f"{CHROME_DEBUG_HOST}:{CHROME_DEBUG_PORT}"
        else:
            profile_path = chrome_profile_path()
            logging.info("Starting Chrome automation profile: %s", profile_path)
            options.add_argument(f"--user-data-dir={profile_path}")
            options.add_argument("--profile-directory=Default")
            options.add_argument("--start-maximized")
            options.add_argument(f"--remote-debugging-port={CHROME_DEBUG_PORT}")
            options.add_argument(f"--remote-debugging-address={CHROME_DEBUG_HOST}")
            options.add_experimental_option("detach", KEEP_BROWSER_OPEN_ON_EXIT)

        service = build_chromedriver_service()
        if service is None:
            logging.info("Starting Chrome through Selenium Manager fallback.")
            driver = webdriver.Chrome(options=options)
        else:
            driver = webdriver.Chrome(service=service, options=options)
        driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT_SECONDS)
        return driver, browser_lock
    except BaseException:
        release_browser_lock(browser_lock)
        raise


def wait_for_page_ready(driver: WebDriver, timeout_seconds: int = WAIT_SECONDS) -> None:
    try:
        WebDriverWait(driver, timeout_seconds).until(
            lambda current_driver: current_driver.execute_script("return document.readyState") == "complete"
        )
    except TimeoutException:
        logging.warning("Page did not reach readyState=complete within %s seconds.", timeout_seconds)


def wait_for_manual_start(driver: WebDriver, note_url: str, signal_file: Optional[Path]) -> None:
    if signal_file is None:
        driver.get(note_url)
        wait_for_page_ready(driver)
        return

    driver.get(note_url)
    wait_for_page_ready(driver)
    logging.info("Opened note/editor URL: %s", note_url)
    logging.info("Set up the note page manually if needed, then click Start Word Art in the dashboard.")
    while not signal_file.exists():
        time.sleep(1)
    logging.info("Start signal received. Continuing from current browser page: %s", driver.current_url)


def ascii_word_art(text: str, style_mode: str, caption: str = "") -> str:
    clean = " ".join(text.strip().split()) or "WORD ART"
    if style_mode == "stack":
        body = "\n".join(f"{index + 1:02d}. {word.upper()}" for index, word in enumerate(clean.split()))
    elif style_mode == "box":
        width = min(max(len(clean) + 4, 18), 72)
        line = "+" + "-" * width + "+"
        centered = clean.upper().center(width)
        body = f"{line}\n|{centered}|\n{line}"
    else:
        border = "=" * min(max(len(clean) + 8, 24), 80)
        body = f"{border}\n   {clean.upper()}\n{border}"
    if caption.strip():
        body = f"{body}\n\n{caption.strip()}"
    return body


def render_word_art_image(
    image_path: Path,
    text: str,
    style_mode: str,
    *,
    text_color: str = "#F4F4F5",
    panel_color: str = "#101114",
    text_align: str = "center",
    panel_placement: str = "bottom",
) -> Path:
    """Create a visual preview/upload while retaining the text editor version."""
    from PIL import Image, ImageDraw, ImageEnhance, ImageFont

    image = Image.open(image_path).convert("RGB")
    image.thumbnail((1600, 1200))
    overlay_height = max(180, image.height // 4)
    canvas = Image.new("RGB", (image.width, image.height + overlay_height), panel_color)
    image_y = overlay_height if panel_placement == "top" else 0
    panel_y = 0 if panel_placement == "top" else image.height
    canvas.paste(ImageEnhance.Contrast(image).enhance(1.05), (0, image_y))
    draw = ImageDraw.Draw(canvas)
    font_size = max(28, min(72, image.width // max(8, len(text) // 2 or 1)))
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial Bold.ttf", font_size)
    except OSError:
        font = ImageFont.load_default()
    label = " ".join(text.strip().split()).upper()
    box = draw.textbbox((0, 0), label, font=font)
    text_width = box[2] - box[0]
    if text_align == "left":
        x = 32
    elif text_align == "right":
        x = max(32, image.width - text_width - 32)
    else:
        x = max(24, (image.width - text_width) // 2)
    y = panel_y + max(24, (overlay_height - (box[3] - box[1])) // 2)
    if style_mode == "box":
        draw.rounded_rectangle(
            (18, panel_y + 18, image.width - 18, panel_y + overlay_height - 18),
            radius=18,
            outline=text_color,
            width=3,
        )
    draw.text((x, y), label, font=font, fill=text_color)
    target = image_path.with_name(f"{image_path.stem}-word-art.png")
    canvas.save(target, "PNG", optimize=True)
    return target


def find_first_present(driver: WebDriver, selector: str, label: str) -> WebElement:
    logging.info("Waiting for %s using selector: %s", label, selector)
    return WebDriverWait(driver, WAIT_SECONDS).until(EC.presence_of_element_located((By.CSS_SELECTOR, selector)))


def find_clickable(driver: WebDriver, selector: str, label: str, wait_seconds: int = WAIT_SECONDS) -> WebElement:
    logging.info("Waiting for %s using selector: %s", label, selector)
    return wait_for_locator_clickable(driver, Locator(By.CSS_SELECTOR, selector, label), wait_seconds)


def upload_image(driver: WebDriver, image_path: Path, input_selector: str, trigger_selector: str) -> None:
    if not image_path.exists():
        raise FileNotFoundError(f"Image path does not exist: {image_path}")

    try:
        file_input = find_first_present(driver, input_selector, "image upload input")
    except TimeoutException:
        logging.info("No file input found immediately; trying upload trigger.")
        trigger = find_clickable(driver, trigger_selector, "upload trigger", wait_seconds=8)
        driver.execute_script("arguments[0].scrollIntoView({block: 'center', inline: 'nearest'});", trigger)
        time.sleep(0.2)
        trigger.click()
        file_input = find_first_present(driver, input_selector, "image upload input after trigger")

    driver.execute_script("arguments[0].style.display = 'block'; arguments[0].style.visibility = 'visible';", file_input)
    file_input.send_keys(str(image_path.resolve()))
    logging.info("Uploaded image file: %s", image_path)
    WebDriverWait(driver, 12).until(
        lambda active: bool(active.execute_script("return arguments[0].files && arguments[0].files.length > 0;", file_input))
    )
    logging.info("WORD_ART_UPLOAD_VERIFIED")


def fill_word_art(driver: WebDriver, editor_selector: str, text: str) -> None:
    editor = find_clickable(driver, editor_selector, "note editor")
    driver.execute_script("arguments[0].scrollIntoView({block: 'center', inline: 'nearest'});", editor)
    time.sleep(0.2)
    inserted = driver.execute_script(
        """
        const editor = arguments[0], text = arguments[1];
        editor.focus();
        if (editor.tagName === "TEXTAREA" || editor.tagName === "INPUT") {
            editor.value = text;
        } else {
            editor.textContent = text;
        }
        editor.dispatchEvent(new InputEvent("input", {bubbles: true, inputType: "insertText", data: text}));
        editor.dispatchEvent(new Event("change", {bubbles: true}));
        return (editor.value || editor.innerText || editor.textContent || "").trim();
        """,
        editor,
        text,
    )
    if text.strip() not in str(inserted):
        slow_type(editor, text, min_delay=0.02, max_delay=0.08)
    logging.info("Inserted word-art text into note editor.")
    logging.info("WORD_ART_EDITOR_VERIFIED")


def click_submit(driver: WebDriver, submit_selector: str) -> None:
    submit = find_clickable(driver, submit_selector, "save/publish button", wait_seconds=10)
    before_url = driver.current_url
    driver.execute_script("arguments[0].scrollIntoView({block: 'center', inline: 'nearest'});", submit)
    time.sleep(0.2)
    try:
        submit.click()
    except WebDriverException:
        driver.execute_script("arguments[0].click();", submit)
    logging.info("Clicked save/publish button.")
    WebDriverWait(driver, 12).until(
        lambda active: bool(
            active.current_url != before_url
            or submit.get_attribute("disabled")
            or submit.get_attribute("aria-disabled") == "true"
            or
            active.execute_script(
                """
                return Array.from(document.querySelectorAll(
                    "[role='status'],[role='alert'],[aria-live],.toast,.success,[class*='success']"
                )).some(node => /saved|published|success/i.test(node.innerText || node.textContent || ""));
                """
            )
        )
    )
    logging.info("WORD_ART_SAVE_VERIFIED")


def main() -> int:
    args = parse_args()
    args.note_url = normalize_web_url(args.note_url)
    normalize_selectors(args)
    configure_logging(args.log_file)
    if not args.image_path.exists():
        logging.error("Image file does not exist: %s", args.image_path)
        return 2

    driver: Optional[WebDriver] = None
    browser_lock: Optional[TextIO] = None
    reporter = reporter_from_environment()
    try:
        if reporter:
            reporter.update(status="running", stage="opening-browser")
        driver, browser_lock = build_driver()
        wait_for_manual_start(driver, args.note_url, args.start_signal_file)
        word_art = ascii_word_art(args.word_art_text, args.style_mode, args.caption)
        rendered_image = render_word_art_image(
            args.image_path,
            args.word_art_text,
            args.style_mode,
            text_color=args.text_color,
            panel_color=args.panel_color,
            text_align=args.text_align,
            panel_placement=args.panel_placement,
        )
        if reporter:
            reporter.checkpoint("uploading", image_path=str(rendered_image), message="Uploading visual word art")
        upload_image(driver, rendered_image, args.upload_input_selector, args.upload_trigger_selector)
        if reporter:
            reporter.checkpoint("editing", message="Filling note editor")
        fill_word_art(driver, args.editor_selector, word_art)
        if args.skip_submit:
            logging.info("Skip submit enabled; leaving note open for review.")
        else:
            click_submit(driver, args.submit_selector)
        if reporter:
            reporter.update(status="completed", stage="completed", progress_current=1, progress_total=1)
        logging.info("Word-art note automation complete.")
        return 0
    except Exception as exc:
        if reporter:
            reporter.update(status="failed", stage="failed", stop_reason=str(exc))
        logging.exception("Word-art note automation failed: %s", exc)
        return 1
    finally:
        if driver is not None:
            if KEEP_BROWSER_OPEN_ON_EXIT:
                logging.info("Leaving Chrome open after note automation.")
            else:
                try:
                    driver.quit()
                except WebDriverException:
                    pass
        release_browser_lock(browser_lock)


if __name__ == "__main__":
    sys.exit(main())
