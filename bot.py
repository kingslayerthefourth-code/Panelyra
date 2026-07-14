#!/usr/bin/env python3
"""
Sequential Selenium QA engine for owned content pages.

Configure selectors below for your staging or QA target before running.
"""

from __future__ import annotations

import argparse
import re
import getpass
import json
import logging
import random
import sys
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.error import URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import urlopen

from selenium import webdriver
from selenium.common.exceptions import (
    JavascriptException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

from history_utils import append_jsonl_record, read_jsonl_records
from qa_utils import (
    LocatedElement,
    Locator,
    clear_existing_text,
    find_in_page_or_iframe,
    generate_unique_comment,
    gradual_scroll,
    slow_type,
    wait_for_clickable as wait_for_locator_clickable,
)


# =========================
# Target Selector Settings
# =========================
# Update these for your owned staging target.
NEXT_LINK_SELECTOR = (
    "a.next-chapter, a.next, button.next-chapter, button.next, "
    "a[rel='next'], a[aria-label*='next' i], button[aria-label*='next' i], "
    "a[title*='next' i], button[title*='next' i], "
    "a[class*='next' i], button[class*='next' i]"
)
CHAPTER_ROUTING_MODE = "auto"  # auto, next-link, or infinite-scroll
CONTINUE_READING_SELECTOR = (
    "a[href*='chapter' i], button, a"
)
FIRST_CHAPTER_SELECTOR = (
    "a[href*='chapter'], "
    ".chapter a, .chapter-list a, .episodes a, .episode-list a, "
    ".wp-manga-chapter a, .listing-chapters_wrap a"
)
COMMENT_TEXTAREA_SELECTOR = (
    "textarea[name='comment'], textarea#comment, textarea.comment, textarea, "
    "[contenteditable='true'], [role='textbox'], "
    "[placeholder*='comment' i], [aria-label*='comment' i], "
    "[data-testid*='comment' i]"
)
SUBMIT_BUTTON_SELECTOR = (
    "button[type='submit'], input[type='submit'], button.submit-comment, "
    "button[name='submit'], #submit, .comment-submit, .submit-comment, "
    "button[aria-label*='send' i], button[title*='send' i], button[class*='send' i], "
    "button[aria-label*='post' i], button[title*='post' i], button[class*='post' i]"
)
THANK_BUTTON_SELECTOR = "button, a"
CONTENT_SELECTOR = "article, main, .chapter-content, .entry-content, .post-content, .content"

# Optional. Leave blank if the app does not show a predictable confirmation node.
CONFIRMATION_SELECTOR = ".comment-success, .notice-success, .toast-success, [data-testid='comment-success']"

# Timing and pacing settings.
WAIT_SECONDS = 15
SUBMIT_BUTTON_WAIT_SECONDS = 2
POST_SUBMIT_WAIT_SECONDS = 0.5
PAGE_LOAD_TIMEOUT_SECONDS = 60
CHAPTER_RETRY_ATTEMPTS = 3
CHAPTER_RETRY_DELAY_SECONDS = 1
CHROME_DEBUG_PORT = 9222
CHROME_DEBUG_HOST = "127.0.0.1"
READ_SCROLL_MIN_PX = 650
READ_SCROLL_MAX_PX = 1200
READ_SCROLL_MIN_DELAY = 0.12
READ_SCROLL_MAX_DELAY = 0.35
TYPE_MIN_DELAY = 0.05
TYPE_MAX_DELAY = 0.22
COOLDOWN_MIN_SECONDS = 5
COOLDOWN_MAX_SECONDS = 10
KEEP_BROWSER_OPEN_ON_EXIT = False
COMMENT_HISTORY_PATH = Path(__file__).resolve().parent / "comment_history.jsonl"
READ_MARKERS_PATH = Path(__file__).resolve().parent / "read_markers.jsonl"
RESEARCH_CONTEXT_PATH = Path(__file__).resolve().parent / "research_context.json"


OPENING_REMARKS = [
    "Wow",
    "Honestly",
    "Just caught up with this",
    "This chapter really landed",
    "I was not expecting that",
    "Had to reread this section",
    "The update was worth the wait",
    "Okay",
    "Not gonna lie",
    "I am really into this arc",
]

SUBJECT_PHRASES = [
    "the pacing here feels excellent",
    "the art style is incredibly clean",
    "this storyline is getting wild",
    "the character moment works so well",
    "the panel flow is easy to follow",
    "the tension keeps building in a good way",
    "the dialogue feels natural",
    "the reveal was handled really well",
    "the mood shift is strong",
    "this scene has a lot of energy",
]

ENDING_PHRASES = [
    "can't wait for the next drop",
    "this chapter was a strong one",
    "that caught me completely off guard",
    "I need to see where this goes next",
    "this one is definitely memorable",
    "the ending really sticks",
    "the payoff here was worth it",
    "the next chapter should be interesting",
    "this was a great read",
    "I am curious how the next part opens",
]

JOINERS = ["and", "plus", "because", "especially since", "with how"]
END_PUNCTUATION = ["!", ".", "!!", "..."]
OPTIONAL_EMOJIS = ["", "", "", "", " :)"]
RARE_REFLECTION_PROBABILITY = 0.08
RARE_REFLECTION_COOLDOWN = 9
RARE_REFLECTION_TEMPLATES = [
    lambda keyword: f"Kind of reminds me of Socrates saying the unexamined life is not worth living; {keyword} feels like the thing this chapter wants us to examine.",
    lambda keyword: f"Aristotle wrote that character is revealed through action, and {keyword} really fits that idea here.",
    lambda keyword: f"Marcus Aurelius would probably ask whether {keyword} is something they can control or something they have to endure.",
    lambda keyword: f"Sun Tzu would call {keyword} the kind of detail that matters before the fight even starts.",
    lambda keyword: f"Nietzsche's line about becoming who you are came to mind here; {keyword} feels like part of that pressure.",
    lambda keyword: f"Plato would probably ask if {keyword} is the truth of the scene or just the shadow on the wall.",
    lambda keyword: f"This chapter makes me wonder: does {keyword} show who someone is, or what the situation forced them to become?",
    lambda keyword: f"Philosophy question for this chapter: if {keyword} changes the outcome, was it fate, choice, or just timing?",
    lambda keyword: f"This has that old question of power versus responsibility, especially with how {keyword} is handled.",
    lambda keyword: f"The chapter quietly asks whether {keyword} is courage, pride, or survival.",
]
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
]

STOPWORDS = {
    "about",
    "after",
    "again",
    "against",
    "also",
    "and",
    "are",
    "because",
    "been",
    "before",
    "being",
    "between",
    "chapter",
    "could",
    "did",
    "does",
    "down",
    "each",
    "even",
    "from",
    "had",
    "has",
    "have",
    "her",
    "here",
    "him",
    "his",
    "into",
    "just",
    "like",
    "more",
    "not",
    "now",
    "out",
    "over",
    "really",
    "she",
    "should",
    "that",
    "the",
    "their",
    "them",
    "then",
    "there",
    "they",
    "this",
    "through",
    "was",
    "were",
    "what",
    "when",
    "where",
    "which",
    "while",
    "who",
    "why",
    "will",
    "with",
    "would",
    "you",
}


@dataclass(frozen=True)
class DriverSession:
    driver: WebDriver
    attached_to_existing_chrome: bool


@dataclass(frozen=True)
class ChapterContext:
    title: str
    keywords: list[str]
    excerpt: str
    text_length: int


@dataclass(frozen=True)
class ResearchContext:
    source: str
    text: str
    matched_terms: list[str]


@dataclass(frozen=True)
class GeneratedComment:
    text: str
    style: str
    keyword: str
    emoji_used: bool
    research_source: str
    reflection_used: bool = False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sequential content-page form-submission QA runner.")
    parser.add_argument("--start-url", required=True, help="First chapter/content URL.")
    parser.add_argument("--max-chapters", required=True, type=int, help="Maximum number of chapters to process.")
    parser.add_argument("--log-file", type=Path, help="Optional log file path for dashboard streaming.")
    parser.add_argument(
        "--start-signal-file",
        type=Path,
        help="Optional file path. If set, the bot opens the start URL and waits until this file exists before posting.",
    )
    parser.add_argument("--history-file", type=Path, default=COMMENT_HISTORY_PATH, help="JSONL comment history path.")
    parser.add_argument("--read-marker-file", type=Path, default=READ_MARKERS_PATH, help="JSONL chapter read marker path.")
    parser.add_argument(
        "--use-thanks-marker",
        action="store_true",
        help="Click the chapter Say thanks button and use it with local read markers to avoid repeating chapters.",
    )
    parser.add_argument("--research-file", type=Path, default=RESEARCH_CONTEXT_PATH, help="Optional local JSON research context.")
    parser.add_argument(
        "--research-endpoint",
        default="",
        help="Optional HTTP endpoint. Bot calls endpoint?q=<title and keywords> and expects text or JSON.",
    )
    parser.add_argument(
        "--chapter-routing",
        choices=("auto", "next-link", "infinite-scroll"),
        default=CHAPTER_ROUTING_MODE,
        help="How the bot moves between chapters. Use infinite-scroll for long reader pages with comment blocks after each chapter.",
    )
    parser.add_argument("--scroll-min-px", type=int, default=READ_SCROLL_MIN_PX, help="Minimum pixels per scroll step.")
    parser.add_argument("--scroll-max-px", type=int, default=READ_SCROLL_MAX_PX, help="Maximum pixels per scroll step.")
    parser.add_argument(
        "--scroll-min-delay",
        type=float,
        default=READ_SCROLL_MIN_DELAY,
        help="Minimum seconds between scroll steps.",
    )
    parser.add_argument(
        "--scroll-max-delay",
        type=float,
        default=READ_SCROLL_MAX_DELAY,
        help="Maximum seconds between scroll steps.",
    )
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


def chrome_profile_path() -> str:
    username = getpass.getuser()
    return f"/Users/{username}/Library/Application Support/Google/Chrome/AutomationProfile"


def chrome_debugger_url() -> str:
    return f"http://{CHROME_DEBUG_HOST}:{CHROME_DEBUG_PORT}/json/version"


def existing_chrome_debug_session() -> Optional[dict[str, object]]:
    try:
        with urlopen(chrome_debugger_url(), timeout=2) as response:
            payload = response.read().decode("utf-8")
    except (OSError, URLError, TimeoutError):
        return None

    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        logging.warning("Chrome debug endpoint on port %s returned invalid JSON.", CHROME_DEBUG_PORT)
        return None

    browser = str(data.get("Browser", "unknown"))
    if "chrome" not in browser.lower() and "chromium" not in browser.lower():
        logging.warning("Port %s is open but does not look like Chrome DevTools: %s", CHROME_DEBUG_PORT, browser)
        return None

    logging.info("Detected existing Chrome debug session on port %s: %s", CHROME_DEBUG_PORT, browser)
    return data


def build_driver() -> DriverSession:
    profile_path = chrome_profile_path()
    logging.info("Using Chrome automation profile: %s", profile_path)

    options = Options()
    attached_to_existing_chrome = existing_chrome_debug_session() is not None

    if attached_to_existing_chrome:
        logging.info("Attaching to existing Chrome on %s:%s.", CHROME_DEBUG_HOST, CHROME_DEBUG_PORT)
        options.debugger_address = f"{CHROME_DEBUG_HOST}:{CHROME_DEBUG_PORT}"
    else:
        options.add_argument(f"--user-data-dir={profile_path}")
        options.add_argument("--profile-directory=Default")
        options.add_argument("--start-maximized")
        options.add_argument(f"--remote-debugging-port={CHROME_DEBUG_PORT}")
        options.add_argument(f"--remote-debugging-address={CHROME_DEBUG_HOST}")
        if KEEP_BROWSER_OPEN_ON_EXIT:
            options.add_experimental_option("detach", True)

    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)
    driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT_SECONDS)
    return DriverSession(driver=driver, attached_to_existing_chrome=attached_to_existing_chrome)


def wait_for_page_ready(driver: WebDriver, timeout_seconds: int = WAIT_SECONDS) -> None:
    try:
        WebDriverWait(driver, timeout_seconds).until(
            lambda current_driver: current_driver.execute_script("return document.readyState") == "complete"
        )
    except TimeoutException:
        logging.warning("Document did not reach readyState=complete within %s seconds.", timeout_seconds)


def page_looks_like_cloudflare_challenge(driver: WebDriver) -> bool:
    try:
        title = driver.title.lower()
        body_text = driver.find_element(By.TAG_NAME, "body").text.lower()
    except WebDriverException:
        return False

    challenge_markers = (
        "just a moment",
        "checking your browser",
        "verify you are human",
        "cloudflare",
        "cf-browser-verification",
    )
    return any(marker in title or marker in body_text for marker in challenge_markers)


def wait_for_cloudflare_to_clear(driver: WebDriver, timeout_seconds: int = PAGE_LOAD_TIMEOUT_SECONDS) -> bool:
    if not page_looks_like_cloudflare_challenge(driver):
        return True

    logging.warning("Possible Cloudflare or anti-bot challenge detected; waiting up to %s seconds.", timeout_seconds)
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        time.sleep(3)
        if not page_looks_like_cloudflare_challenge(driver):
            logging.info("Challenge page cleared.")
            return True

    logging.warning("Challenge page did not clear before timeout.")
    return False


def load_url_with_retries(driver: WebDriver, url: str, label: str) -> bool:
    for attempt in range(1, CHAPTER_RETRY_ATTEMPTS + 1):
        try:
            logging.info("Loading %s, attempt %s/%s: %s", label, attempt, CHAPTER_RETRY_ATTEMPTS, url)
            driver.switch_to.default_content()
            driver.get(url)
            wait_for_page_ready(driver)
            if wait_for_cloudflare_to_clear(driver):
                return True
        except TimeoutException:
            logging.warning("Timed out loading %s on attempt %s/%s.", label, attempt, CHAPTER_RETRY_ATTEMPTS)
        except WebDriverException as exc:
            logging.warning("WebDriver error loading %s on attempt %s/%s: %s", label, attempt, CHAPTER_RETRY_ATTEMPTS, exc)

        if attempt < CHAPTER_RETRY_ATTEMPTS:
            logging.info("Retrying %s after %s seconds.", label, CHAPTER_RETRY_DELAY_SECONDS)
            time.sleep(CHAPTER_RETRY_DELAY_SECONDS)

    logging.error("Unable to load %s after %s attempts.", label, CHAPTER_RETRY_ATTEMPTS)
    return False


def css_locator(css_selector: str, label: str) -> Locator:
    return Locator(By.CSS_SELECTOR, css_selector, label)


def wait_for_clickable(driver: WebDriver, css_selector: str, label: str) -> WebElement:
    logging.info("Waiting for %s using selector: %s", label, css_selector)
    return wait_for_locator_clickable(driver, css_locator(css_selector, label), WAIT_SECONDS)


def first_matching_link_url(driver: WebDriver, css_selector: str) -> Optional[str]:
    driver.switch_to.default_content()
    try:
        links = driver.find_elements(By.CSS_SELECTOR, css_selector)
    except WebDriverException:
        return None

    candidates: list[tuple[str, str]] = []
    for link in links:
        try:
            href = link.get_attribute("href")
            label = normalize_space(link.text)
            if href and link.is_displayed():
                candidates.append((href, label))
        except WebDriverException:
            continue

    if not candidates:
        return None

    logging.info("First chapter candidate: %s (%s)", candidates[0][0], candidates[0][1] or "no label")
    return candidates[0][0]


def continue_reading_url_or_click(driver: WebDriver) -> Optional[str]:
    driver.switch_to.default_content()
    try:
        elements = driver.find_elements(By.CSS_SELECTOR, CONTINUE_READING_SELECTOR)
    except WebDriverException:
        return None

    for element in elements:
        try:
            if not element.is_displayed():
                continue
            label = normalize_space(element.text or element.get_attribute("aria-label") or element.get_attribute("title") or "")
            href = element.get_attribute("href")
            haystack = f"{label} {href or ''}".lower()
            if "continue" not in haystack or "chapter" not in haystack:
                continue

            if href:
                logging.info("Continue-reading candidate: %s (%s)", href, label or "no label")
                return href

            logging.info("Clicking continue-reading control: %s", label or element.tag_name)
            driver.execute_script("arguments[0].scrollIntoView({block: 'center', inline: 'nearest'});", element)
            time.sleep(0.05)
            element.click()
            time.sleep(random.uniform(0.4, 0.8))
            return driver.current_url
        except WebDriverException:
            continue

    return None


def page_has_comment_input(driver: WebDriver) -> bool:
    driver.switch_to.default_content()
    try:
        if driver.find_elements(By.CSS_SELECTOR, COMMENT_TEXTAREA_SELECTOR):
            return True
    except WebDriverException:
        return False

    for frame in driver.find_elements(By.TAG_NAME, "iframe"):
        driver.switch_to.default_content()
        try:
            driver.switch_to.frame(frame)
            if driver.find_elements(By.CSS_SELECTOR, COMMENT_TEXTAREA_SELECTOR):
                driver.switch_to.default_content()
                return True
        except WebDriverException:
            continue
    driver.switch_to.default_content()
    return False


def maybe_open_resume_or_first_chapter(driver: WebDriver, current_url: str) -> str:
    if page_has_comment_input(driver):
        return current_url

    continue_url = continue_reading_url_or_click(driver)
    if continue_url and continue_url != current_url:
        logging.info("No comment input on current page; opening continue-reading chapter: %s", continue_url)
        if load_url_with_retries(driver, continue_url, "continue-reading chapter"):
            return continue_url
        return current_url

    first_chapter_url = first_matching_link_url(driver, FIRST_CHAPTER_SELECTOR)
    if not first_chapter_url or first_chapter_url == current_url:
        return current_url

    logging.info("No continue-reading link found; opening first detected chapter page: %s", first_chapter_url)
    if load_url_with_retries(driver, first_chapter_url, "chapter from series page"):
        return first_chapter_url
    return current_url


def slow_reading_scroll(
    driver: WebDriver,
    *,
    min_step_px: int,
    max_step_px: int,
    min_pause_seconds: float,
    max_pause_seconds: float,
) -> None:
    logging.info(
        "Starting page scroll with %s-%spx steps and %.3f-%.3fs pauses.",
        min_step_px,
        max_step_px,
        min_pause_seconds,
        max_pause_seconds,
    )
    gradual_scroll(
        driver,
        min_step_px=min_step_px,
        max_step_px=max_step_px,
        min_pause_seconds=min_pause_seconds,
        max_pause_seconds=max_pause_seconds,
    )
    logging.info("Finished gradual page scroll.")


def current_scroll_y(driver: WebDriver) -> int:
    try:
        return int(driver.execute_script("return window.scrollY || document.documentElement.scrollTop || 0;"))
    except (JavascriptException, TypeError, ValueError):
        return 0


def viewport_height(driver: WebDriver) -> int:
    try:
        return int(driver.execute_script("return window.innerHeight || document.documentElement.clientHeight || 800;"))
    except (JavascriptException, TypeError, ValueError):
        return 800


def element_page_y(driver: WebDriver, element: WebElement) -> int:
    try:
        return int(
            driver.execute_script(
                "const rect = arguments[0].getBoundingClientRect(); return rect.top + window.scrollY;",
                element,
            )
        )
    except (JavascriptException, TypeError, ValueError, WebDriverException):
        return current_scroll_y(driver)


def mark_submitted_editor(driver: WebDriver, element: WebElement) -> None:
    try:
        driver.execute_script("arguments[0].setAttribute('data-asura-qa-submitted-editor', 'true');", element)
    except (JavascriptException, WebDriverException):
        logging.debug("Could not mark submitted editor; continuing.")


def clear_submitted_editor_marks(driver: WebDriver) -> None:
    try:
        cleared = driver.execute_script(
            """
            const nodes = Array.from(document.querySelectorAll('[data-asura-qa-submitted-editor]'));
            for (const node of nodes) node.removeAttribute('data-asura-qa-submitted-editor');
            return nodes.length;
            """
        )
        if cleared:
            logging.info("Cleared %s stale submitted-editor marker(s) from the page.", cleared)
    except (JavascriptException, WebDriverException):
        logging.debug("Could not clear submitted editor markers; continuing.")


def document_height(driver: WebDriver) -> int:
    try:
        return int(
            driver.execute_script("return Math.max(document.body.scrollHeight, document.documentElement.scrollHeight);")
        )
    except (JavascriptException, TypeError, ValueError):
        return 0


def visible_comment_input_count(driver: WebDriver) -> int:
    driver.switch_to.default_content()
    script = """
        const selector = arguments[0];
        return Array.from(document.querySelectorAll(selector)).filter((node) => {
            const rect = node.getBoundingClientRect();
            const style = window.getComputedStyle(node);
            return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
        }).length;
    """
    try:
        return int(driver.execute_script(script, COMMENT_TEXTAREA_SELECTOR))
    except (JavascriptException, TypeError, ValueError):
        return 0


def scroll_to_comment_section(
    driver: WebDriver,
    *,
    min_y: int,
    min_step_px: int,
    max_step_px: int,
    min_pause_seconds: float,
    max_pause_seconds: float,
    label: str,
) -> bool:
    driver.switch_to.default_content()
    previous_height = document_height(driver)
    stagnant_scrolls = 0
    logging.info("Scrolling until %s comment section is visible below y=%s.", label, min_y)

    for attempt in range(1, 220):
        found = driver.execute_script(
            """
            const selector = arguments[0];
            const minY = arguments[1];
            const visible = (node) => {
                const rect = node.getBoundingClientRect();
                const style = window.getComputedStyle(node);
                return rect.width > 0
                    && rect.height > 0
                    && style.visibility !== "hidden"
                    && style.display !== "none"
                    && !node.disabled
                    && node.getAttribute("aria-disabled") !== "true";
            };
            const pageY = (node) => node.getBoundingClientRect().top + window.scrollY;
            const editorCandidates = Array.from(document.querySelectorAll(selector)).filter((node) => {
                if (node.getAttribute("data-asura-qa-submitted-editor") === "true") return false;
                return pageY(node) >= minY && visible(node);
            });
            if (editorCandidates.length) {
                editorCandidates.sort((a, b) => pageY(a) - pageY(b));
                editorCandidates[0].scrollIntoView({block: "center", inline: "nearest"});
                return "editor";
            }

            const triggerCandidates = Array.from(document.querySelectorAll("button, a")).filter((node) => {
                if (!visible(node)) return false;
                if (pageY(node) < minY) return false;
                const haystack = [
                    node.innerText || "",
                    node.getAttribute("aria-label") || "",
                    node.getAttribute("title") || "",
                    node.id || "",
                    node.className || ""
                ].join(" ").toLowerCase();
                if (/hide\\s+comments|dismiss|cancel/.test(haystack)) return false;
                return /leave\\s+a\\s+comment|add\\s+comment|write\\s+a\\s+comment|comment\\s+on\\s+the\\s+image|reply/.test(haystack);
            });
            if (triggerCandidates.length) {
                triggerCandidates.sort((a, b) => pageY(a) - pageY(b));
                triggerCandidates[0].scrollIntoView({block: "center", inline: "nearest"});
                triggerCandidates[0].click();
                return "trigger";
            }

            return "";
            """,
            COMMENT_TEXTAREA_SELECTOR,
            min_y,
        )
        if found == "editor":
            time.sleep(0.15)
            logging.info("Found %s comment section after %s scan step(s).", label, attempt)
            return True
        if found == "trigger":
            logging.info("Opened comment trigger while searching for %s section.", label)
            time.sleep(0.45)
            continue

        step = min(random.randint(min_step_px, max_step_px), max(900, int(viewport_height(driver) * 1.5)))
        driver.execute_script("window.scrollBy({top: arguments[0], left: 0, behavior: 'auto'});", step)
        time.sleep(max(random.uniform(min_pause_seconds, max_pause_seconds), 0.06))
        if attempt % 10 == 0:
            time.sleep(0.35)

        new_height = document_height(driver)
        if new_height <= previous_height and current_scroll_y(driver) + 20 >= max(0, new_height - 1):
            stagnant_scrolls += 1
        else:
            stagnant_scrolls = 0
        previous_height = max(previous_height, new_height)

        if stagnant_scrolls >= 18:
            logging.info("Reached page bottom while searching for %s comment section.", label)
            return False

    logging.info("Gave up searching for %s comment section.", label)
    return False


def scroll_to_next_infinite_comment_section(
    driver: WebDriver,
    *,
    last_submission_y: int,
    min_step_px: int,
    max_step_px: int,
    min_pause_seconds: float,
    max_pause_seconds: float,
) -> bool:
    next_section_min_y = last_submission_y + max(900, int(viewport_height(driver) * 0.75))
    return scroll_to_comment_section(
        driver,
        min_y=next_section_min_y,
        min_step_px=min_step_px,
        max_step_px=max_step_px,
        min_pause_seconds=min_pause_seconds,
        max_pause_seconds=max_pause_seconds,
        label="next infinite-scroll",
    )


def scroll_to_first_infinite_comment_section(
    driver: WebDriver,
    *,
    min_step_px: int,
    max_step_px: int,
    min_pause_seconds: float,
    max_pause_seconds: float,
) -> bool:
    return scroll_to_comment_section(
        driver,
        min_y=current_scroll_y(driver),
        min_step_px=min_step_px,
        max_step_px=max_step_px,
        min_pause_seconds=min_pause_seconds,
        max_pause_seconds=max_pause_seconds,
        label="first infinite-scroll",
    )


def scroll_to_next_chapter_comment_section(
    driver: WebDriver,
    *,
    last_marker_y: int,
    min_step_px: int,
    max_step_px: int,
    min_pause_seconds: float,
    max_pause_seconds: float,
) -> bool:
    driver.switch_to.default_content()
    min_y = last_marker_y + max(1200, int(viewport_height(driver) * 1.25))
    previous_height = document_height(driver)
    stagnant_scrolls = 0
    logging.info("Continuous reader mode: searching for next chapter Say thanks block below y=%s.", min_y)

    for attempt in range(1, 260):
        thanks_y = driver.execute_script(
            """
            const minY = arguments[0];
            const visible = (node) => {
                const rect = node.getBoundingClientRect();
                const style = window.getComputedStyle(node);
                return rect.width > 0
                    && rect.height > 0
                    && style.visibility !== "hidden"
                    && style.display !== "none"
                    && !node.disabled
                    && node.getAttribute("aria-disabled") !== "true";
            };
            const pageY = (node) => node.getBoundingClientRect().top + window.scrollY;
            const candidates = Array.from(document.querySelectorAll("button, a")).filter((node) => {
                if (!visible(node) || pageY(node) < minY) return false;
                const haystack = [
                    node.innerText || "",
                    node.getAttribute("aria-label") || "",
                    node.getAttribute("title") || "",
                    node.id || "",
                    node.className || ""
                ].join(" ").toLowerCase();
                return /say\\s+thanks/.test(haystack);
            });
            if (!candidates.length) return 0;
            candidates.sort((a, b) => pageY(a) - pageY(b));
            candidates[0].scrollIntoView({block: "center", inline: "nearest"});
            return Math.round(pageY(candidates[0]));
            """,
            min_y,
        )
        try:
            thanks_y_int = int(thanks_y or 0)
        except (TypeError, ValueError):
            thanks_y_int = 0

        if thanks_y_int:
            logging.info("Continuous reader mode: found next chapter Say thanks block at y=%s.", thanks_y_int)
            time.sleep(0.25)
            return scroll_to_comment_section(
                driver,
                min_y=thanks_y_int + 120,
                min_step_px=min_step_px,
                max_step_px=max_step_px,
                min_pause_seconds=min_pause_seconds,
                max_pause_seconds=max_pause_seconds,
                label="next chapter comment",
            )

        step = min(random.randint(min_step_px, max_step_px), max(1000, int(viewport_height(driver) * 1.75)))
        driver.execute_script("window.scrollBy({top: arguments[0], left: 0, behavior: 'auto'});", step)
        time.sleep(max(random.uniform(min_pause_seconds, max_pause_seconds), 0.06))
        if attempt % 12 == 0:
            time.sleep(0.35)

        new_height = document_height(driver)
        if new_height <= previous_height and current_scroll_y(driver) + 20 >= max(0, new_height - 1):
            stagnant_scrolls += 1
        else:
            stagnant_scrolls = 0
        previous_height = max(previous_height, new_height)

        if stagnant_scrolls >= 18:
            logging.info("Continuous reader mode: reached page bottom before another chapter Say thanks block.")
            return False

    logging.info("Continuous reader mode: gave up searching for another chapter block.")
    return False


def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def extract_content_text(driver: WebDriver) -> str:
    driver.switch_to.default_content()
    script = """
        const selector = arguments[0];
        const nodes = Array.from(document.querySelectorAll(selector));
        const source = nodes.length ? nodes : [document.body];
        return source
            .map((node) => node.innerText || node.textContent || "")
            .join("\\n")
            .replace(/\\n{3,}/g, "\\n\\n");
    """
    try:
        text = driver.execute_script(script, CONTENT_SELECTOR)
    except JavascriptException:
        logging.warning("Could not extract content using selector %s; falling back to body text.", CONTENT_SELECTOR)
        try:
            text = driver.find_element(By.TAG_NAME, "body").text
        except WebDriverException:
            text = ""

    return normalize_space(str(text))


def extract_title(driver: WebDriver, text: str) -> str:
    try:
        heading = driver.find_elements(By.CSS_SELECTOR, "h1, .chapter-title, .entry-title, .post-title")
        for element in heading:
            value = normalize_space(element.text)
            if value:
                return value
    except WebDriverException:
        pass

    title = normalize_space(driver.title)
    if title:
        return title

    return text[:80] if text else "this chapter"


def extract_keywords(text: str, limit: int = 8) -> list[str]:
    words = re.findall(r"[A-Za-z][A-Za-z'-]{3,}", text.lower())
    words = [word.strip("'") for word in words if word not in STOPWORDS and len(word) > 3]
    counts = Counter(words)
    return [word for word, _ in counts.most_common(limit)]


def build_chapter_context(driver: WebDriver) -> ChapterContext:
    text = extract_content_text(driver)
    title = extract_title(driver, text)
    keywords = extract_keywords(text)
    excerpt = trim_comment(text, max_length=360) if text else ""

    logging.info("Chapter title/context: %s", title)
    logging.info("Extracted keywords: %s", ", ".join(keywords[:6]) if keywords else "none")
    return ChapterContext(title=title, keywords=keywords, excerpt=excerpt, text_length=len(text))


def tokenize_for_match(value: str) -> set[str]:
    return {
        word
        for word in re.findall(r"[A-Za-z][A-Za-z'-]{2,}", value.lower())
        if word not in STOPWORDS
    }


def load_local_research_context(research_file: Path, chapter_context: ChapterContext) -> Optional[ResearchContext]:
    if not research_file.exists():
        return None

    try:
        raw_value = json.loads(research_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logging.warning("Could not read research file %s: %s", research_file, exc)
        return None

    if isinstance(raw_value, dict):
        entries = raw_value.get("entries", [])
        if isinstance(entries, dict):
            entries = [{"title": key, "notes": value} for key, value in entries.items()]
    elif isinstance(raw_value, list):
        entries = raw_value
    else:
        entries = []

    title_terms = tokenize_for_match(chapter_context.title)
    keyword_terms = set(chapter_context.keywords)
    search_terms = title_terms | keyword_terms
    best_score = 0
    best_entry: Optional[dict[str, object]] = None

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        haystack = " ".join(str(entry.get(field, "")) for field in ("title", "chapter", "series", "tags", "notes", "summary"))
        entry_terms = tokenize_for_match(haystack)
        score = len(search_terms & entry_terms)
        if score > best_score:
            best_score = score
            best_entry = entry

    if not best_entry:
        return None

    notes = normalize_space(
        " ".join(
            str(best_entry.get(field, ""))
            for field in ("summary", "notes", "context", "theory_seed")
            if best_entry.get(field)
        )
    )
    if not notes:
        return None

    matched_terms = sorted(search_terms & tokenize_for_match(notes))
    logging.info("Loaded local research context from %s.", research_file)
    return ResearchContext(source=f"local:{research_file.name}", text=trim_comment(notes, 420), matched_terms=matched_terms)


def fetch_research_endpoint(endpoint: str, chapter_context: ChapterContext) -> Optional[ResearchContext]:
    if not endpoint.strip():
        return None

    query = " ".join([chapter_context.title, *chapter_context.keywords[:6]]).strip()
    separator = "&" if "?" in endpoint else "?"
    url = f"{endpoint}{separator}{urlencode({'q': query})}"
    try:
        with urlopen(url, timeout=8) as response:
            raw_text = response.read().decode("utf-8", errors="replace")
    except (OSError, URLError, TimeoutError) as exc:
        logging.warning("Research endpoint request failed: %s", exc)
        return None

    text = raw_text
    try:
        payload = json.loads(raw_text)
        if isinstance(payload, dict):
            text = str(payload.get("summary") or payload.get("text") or payload.get("context") or raw_text)
    except json.JSONDecodeError:
        pass

    text = normalize_space(text)
    if not text:
        return None

    logging.info("Loaded research context from endpoint.")
    return ResearchContext(source="endpoint", text=trim_comment(text, 420), matched_terms=list(tokenize_for_match(query))[:8])


def resolve_research_context(
    chapter_context: ChapterContext,
    research_file: Path,
    research_endpoint: str,
) -> Optional[ResearchContext]:
    local_context = load_local_research_context(research_file, chapter_context)
    if local_context:
        return local_context
    return fetch_research_endpoint(research_endpoint, chapter_context)


def pick_keyword(context: ChapterContext, fallback: str = "this moment") -> str:
    if context.keywords:
        return random.choice(context.keywords[: min(5, len(context.keywords))])
    return fallback


def maybe_emoji() -> str:
    return random.choice(OPTIONAL_EMOJIS)


def recent_reflection_used(history: list[dict[str, object]]) -> bool:
    recent = history[-RARE_REFLECTION_COOLDOWN:]
    return any(bool(record.get("reflection_used")) for record in recent)


def maybe_add_rare_reflection(
    comment: str,
    *,
    keyword: str,
    history: list[dict[str, object]],
) -> tuple[str, bool]:
    if recent_reflection_used(history):
        return comment, False
    if random.random() > RARE_REFLECTION_PROBABILITY:
        return comment, False

    reflection = trim_comment(random.choice(RARE_REFLECTION_TEMPLATES)(keyword), max_length=180)
    joined = f"{comment.rstrip('.!?')}. {reflection}"
    return trim_comment(joined, max_length=260), True


def trim_comment(text: str, max_length: int = 240) -> str:
    text = normalize_space(text)
    if len(text) <= max_length:
        return text
    trimmed = text[: max_length - 3].rsplit(" ", 1)[0]
    return f"{trimmed}..."


def load_comment_history(history_file: Path) -> list[dict[str, object]]:
    return read_jsonl_records(history_file)


def canonical_chapter_key(url: str) -> str:
    parsed = urlsplit(url)
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in {"page", "scroll", "comment"}
    ]
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), urlencode(query), ""))


def series_url_from_chapter_url(url: str) -> Optional[str]:
    parsed = urlsplit(url)
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 3 and parts[0] == "content":
        return urlunsplit((parsed.scheme, parsed.netloc, f"/content/{parts[1]}", "", ""))
    if len(parts) == 2 and parts[0] == "content":
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))
    return None


def load_read_marker_keys(marker_file: Path) -> set[str]:
    return {
        str(record.get("chapter_key", "")).strip()
        for record in read_jsonl_records(marker_file)
        if str(record.get("chapter_key", "")).strip()
    }


def append_read_marker(marker_file: Path, *, url: str, title: str, thanked: bool) -> str:
    chapter_key = canonical_chapter_key(url)
    append_jsonl_record(
        marker_file,
        {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "chapter_key": chapter_key,
            "url": url,
            "title": title,
            "thanked": thanked,
        },
    )
    return chapter_key


def style_counts(history: list[dict[str, object]]) -> Counter[str]:
    return Counter(str(record.get("style", "unknown")) for record in history if record.get("style"))


def choose_style(history: list[dict[str, object]]) -> str:
    counts = style_counts(history)
    lowest_count = min(counts.get(style, 0) for style in COMMENT_STYLES)
    underused = [style for style in COMMENT_STYLES if counts.get(style, 0) == lowest_count]
    return random.choice(underused)


def validate_comment_templates(templates: dict[str, list[object]]) -> None:
    missing = sorted(set(COMMENT_STYLES) - set(templates))
    unused = sorted(set(templates) - set(COMMENT_STYLES))
    if missing:
        raise RuntimeError(f"Missing comment templates for styles: {', '.join(missing)}")
    if unused:
        logging.warning("Comment templates exist for unlisted style(s): %s", ", ".join(unused))


def append_comment_history(
    history_file: Path,
    *,
    chapter_index: int,
    url: str,
    context: ChapterContext,
    generated: GeneratedComment,
    status: str,
    error: str = "",
) -> None:
    record = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "chapter_index": chapter_index + 1,
        "url": url,
        "title": context.title,
        "keywords": context.keywords[:8],
        "text_length": context.text_length,
        "comment": generated.text,
        "style": generated.style,
        "keyword": generated.keyword,
        "emoji_used": generated.emoji_used,
        "reflection_used": generated.reflection_used,
        "research_source": generated.research_source,
        "status": status,
        "error": error,
    }
    append_jsonl_record(history_file, record)


def build_contextual_comment(
    context: ChapterContext,
    chapter_index: int,
    used_comments: set[str],
    history: list[dict[str, object]],
    research_context: Optional[ResearchContext],
) -> GeneratedComment:
    keyword = pick_keyword(context)
    second_keyword = pick_keyword(context, fallback="the setup")
    title_hint = context.title if len(context.title) <= 80 else context.title[:77] + "..."
    preferred_style = choose_style(history)
    research_hint = ""
    research_source = ""
    if research_context and research_context.text:
        research_hint = research_context.text
        research_source = research_context.source

    templates = {
        "question": [
            lambda: f"Is {keyword} going to matter later, or was that just a clever fakeout?",
            lambda: f"Did anyone else notice how much attention this chapter gave to {keyword}?",
            lambda: f"After that setup, do you think {keyword} is meant to connect back to the bigger picture?",
        ],
        "theory": [
            lambda: f"I have a theory that {keyword} is connected to {second_keyword}, and now I need the next chapter.",
            lambda: f"My current theory is that {keyword} is not as random as it looks.",
            lambda: f"If the extra context is pointing the right way, {keyword} might be doing more work than it seems.",
        ],
        "reaction": [
            lambda: f"The way this chapter handled {keyword} made the whole scene feel more tense.",
            lambda: f"{title_hint} gave me more questions than answers, especially around {keyword}.",
            lambda: f"This chapter made {keyword} stand out more than I expected.",
        ],
        "joke": [
            lambda: f"Not me overthinking {keyword} like it is a final exam question.",
            lambda: f"I laughed a little at how quickly {keyword} went from small detail to suspicious detail.",
        ],
        "prediction": [
            lambda: f"If {keyword} turns out to be foreshadowing, I am officially calling it now.",
            lambda: f"I feel like {keyword} is going to come back at the worst possible moment.",
        ],
        "detail": [
            lambda: f"I like how this chapter makes {keyword} feel important without spelling everything out.",
            lambda: f"The pacing worked well here; {keyword} kept pulling my attention back in.",
            lambda: f"The detail around {keyword} works because the chapter does not over-explain it.",
        ],
        "foreshadowing": [
            lambda: f"That {keyword} detail feels like it is going to hit differently a few chapters from now.",
            lambda: f"This has the energy of a chapter that quietly planted a clue with {keyword}.",
        ],
        "character_focus": [
            lambda: f"The character writing around {keyword} is doing more than it first looks like.",
            lambda: f"I like how {keyword} changes the way the character moment lands.",
        ],
        "pacing": [
            lambda: f"The pacing here is quick, but {keyword} still gets enough room to stand out.",
            lambda: f"This chapter moves fast without making {keyword} feel rushed.",
        ],
        "cliffhanger": [
            lambda: f"Ending on that note after {keyword} is honestly unfair in the best way.",
            lambda: f"That ending made {keyword} feel like a problem waiting to explode.",
        ],
        "worldbuilding": [
            lambda: f"The way {keyword} fits into the larger setting is what caught my attention.",
            lambda: f"I like when a chapter makes the world feel bigger through a detail like {keyword}.",
        ],
        "mystery": [
            lambda: f"The mystery around {keyword} is the part I keep coming back to.",
            lambda: f"This chapter made {keyword} feel like a question mark with consequences.",
        ],
        "emotional": [
            lambda: f"The emotional beat around {keyword} landed better than I expected.",
            lambda: f"There is something about {keyword} here that makes the scene feel heavier.",
        ],
        "comparison": [
            lambda: f"This chapter feels different from the last one, especially with how it uses {keyword}.",
            lambda: f"Compared to the earlier setup, {keyword} feels way more important now.",
        ],
        "callback": [
            lambda: f"{keyword} feels like a callback I should remember, even if I cannot place it yet.",
            lambda: f"This made me wonder if {keyword} is connecting back to something earlier.",
        ],
        "suspicion": [
            lambda: f"I do not trust how casually {keyword} showed up here.",
            lambda: f"{keyword} seems too specific to be harmless.",
        ],
        "appreciation": [
            lambda: f"I really like how cleanly this chapter built around {keyword}.",
            lambda: f"The chapter does a good job making {keyword} feel worth paying attention to.",
        ],
        "tension": [
            lambda: f"The tension around {keyword} kept creeping up in a really effective way.",
            lambda: f"Every time {keyword} came back into focus, the scene felt tighter.",
        ],
        "confusion": [
            lambda: f"I am confused in a good way about {keyword}, and I want answers.",
            lambda: f"I still do not fully know what to make of {keyword}, but that is kind of the hook.",
        ],
        "reread": [
            lambda: f"This feels like one of those chapters where rereading for {keyword} will pay off.",
            lambda: f"I might need to reread this because {keyword} feels easy to miss.",
        ],
        "favorite_moment": [
            lambda: f"My favorite part was how {keyword} shifted the tone of the scene.",
            lambda: f"The {keyword} moment is probably what I will remember most from this chapter.",
        ],
        "next_chapter_hook": [
            lambda: f"Now I need the next chapter just to see what happens with {keyword}.",
            lambda: f"This chapter did its job because now I am waiting on the {keyword} payoff.",
        ],
        "plot_twist": [
            lambda: f"If {keyword} was meant to be the twist, it actually worked on me.",
            lambda: f"The shift around {keyword} made the chapter feel way less predictable.",
        ],
        "panel_art": [
            lambda: f"The way the scene frames {keyword} makes the moment feel sharper.",
            lambda: f"Even without overexplaining it, the visual focus on {keyword} says a lot.",
        ],
        "dialogue": [
            lambda: f"The dialogue around {keyword} feels simple at first, but it carries a lot.",
            lambda: f"I like how the lines here make {keyword} feel more loaded than usual.",
        ],
        "power_scaling": [
            lambda: f"If {keyword} is a power clue, the balance of this arc could change fast.",
            lambda: f"This made me wonder whether {keyword} is hinting at a bigger gap in strength.",
        ],
        "villain_watch": [
            lambda: f"I am keeping an eye on {keyword} because it feels like villain setup.",
            lambda: f"{keyword} has the kind of energy that makes me suspicious of everyone involved.",
        ],
        "hero_moment": [
            lambda: f"The moment with {keyword} gave the lead more weight than I expected.",
            lambda: f"This chapter made {keyword} feel like a real turning point for the main character.",
        ],
        "side_character": [
            lambda: f"I like when a side detail like {keyword} gets enough space to matter.",
            lambda: f"{keyword} made the supporting cast feel more connected to the chapter.",
        ],
        "relationship": [
            lambda: f"The dynamic around {keyword} feels like it could get complicated soon.",
            lambda: f"I am curious how {keyword} changes the relationship side of the story.",
        ],
        "strategy": [
            lambda: f"The strategy angle around {keyword} is what makes this chapter interesting.",
            lambda: f"If {keyword} was intentional, someone is planning a few moves ahead.",
        ],
        "lore_question": [
            lambda: f"Does {keyword} connect to the lore, or am I reading too much into it?",
            lambda: f"This chapter makes me want a lore explanation for {keyword}.",
        ],
        "mood_shift": [
            lambda: f"The mood changed fast once {keyword} came into focus.",
            lambda: f"{keyword} gave the chapter a different tone without making it feel forced.",
        ],
        "slow_burn": [
            lambda: f"This feels like slow-burn setup, especially with how {keyword} keeps showing up.",
            lambda: f"I like that the chapter is taking its time with {keyword}.",
        ],
        "payoff": [
            lambda: f"If {keyword} pays off later, this chapter is going to age really well.",
            lambda: f"The setup around {keyword} already feels like it is waiting for a payoff.",
        ],
        "setup": [
            lambda: f"This chapter feels like setup, but {keyword} keeps it from feeling empty.",
            lambda: f"The groundwork around {keyword} is subtle, but it feels deliberate.",
        ],
        "reader_poll": [
            lambda: f"Am I the only one thinking {keyword} is the key detail here?",
            lambda: f"Curious what everyone else thinks about {keyword} after this chapter.",
        ],
        "wild_guess": [
            lambda: f"Wild guess, but {keyword} might be way more important than it looks.",
            lambda: f"I am probably reaching, but {keyword} feels like a hidden clue.",
        ],
        "respect": [
            lambda: f"I respect how the chapter handles {keyword} without making it too obvious.",
            lambda: f"Credit where it is due, the use of {keyword} here is pretty clean.",
        ],
        "quiet_detail": [
            lambda: f"The quiet detail with {keyword} is exactly the kind of thing I like noticing.",
            lambda: f"{keyword} is small, but it gives the chapter more texture.",
        ],
    }
    if research_hint:
        templates["question"].append(lambda: f"Knowing the broader setup, is {keyword} the detail we should be watching now?")
        templates["theory"].append(lambda: f"The outside context makes me think {keyword} is tied to the main conflict somehow.")
        templates["detail"].append(lambda: f"The extra context makes {keyword} feel less random and more like deliberate setup.")
        templates["worldbuilding"].append(lambda: f"The outside context makes {keyword} feel more connected to the setting than it first seemed.")
        templates["foreshadowing"].append(lambda: f"With the extra context in mind, {keyword} feels like deliberate foreshadowing.")
        templates["lore_question"].append(lambda: f"The outside context makes me wonder if {keyword} is tied to the lore directly.")
        templates["strategy"].append(lambda: f"With that extra context, {keyword} feels less accidental and more like a planned move.")
        templates["payoff"].append(lambda: f"The extra context makes the possible payoff around {keyword} feel stronger.")

    validate_comment_templates(templates)
    if preferred_style not in templates:
        logging.warning("Preferred comment style %s has no template; choosing a validated fallback.", preferred_style)
        preferred_style = choose_style([record for record in history if record.get("style") in templates])

    for _ in range(100):
        style = preferred_style
        comment = trim_comment(random.choice(templates[style])())
        comment, reflection_used = maybe_add_rare_reflection(comment, keyword=keyword, history=history)
        emoji_used = False
        if not reflection_used and random.random() < 0.35:
            emoji = maybe_emoji()
            if emoji:
                comment = f"{comment.rstrip('.!?')}{emoji}"
                emoji_used = True
        if comment not in used_comments:
            used_comments.add(comment)
            return GeneratedComment(
                text=comment,
                style=style,
                keyword=keyword,
                emoji_used=emoji_used,
                research_source=research_source,
                reflection_used=reflection_used,
            )

    fallback = f"{trim_comment(random.choice(templates[preferred_style])())} qa-{chapter_index + 1}"
    used_comments.add(fallback)
    return GeneratedComment(
        text=fallback,
        style=preferred_style,
        keyword=keyword,
        emoji_used=False,
        research_source=research_source,
        reflection_used=False,
    )


def generate_comment(chapter_index: int, used_comments: set[str]) -> str:
    return generate_unique_comment(
        iteration=chapter_index,
        used_comments=used_comments,
        opening_phrases=OPENING_REMARKS,
        subject_phrases=SUBJECT_PHRASES,
        ending_phrases=ENDING_PHRASES,
        joiners=JOINERS,
        punctuation=END_PUNCTUATION,
    )


def sanitize_for_chromedriver(text: str) -> str:
    sanitized = "".join(char for char in text if ord(char) <= 0xFFFF)
    if sanitized != text:
        logging.info("Removed unsupported non-BMP character(s) before typing.")
    return sanitized


def clear_and_type(element: WebElement, text: str) -> None:
    text = sanitize_for_chromedriver(text)
    logging.info("Typing generated QA input (%s characters).", len(text))
    clear_existing_text(element)
    slow_type(element, text, TYPE_MIN_DELAY, TYPE_MAX_DELAY)


def log_comment_diagnostics(driver: WebDriver) -> None:
    driver.switch_to.default_content()
    script = """
        const nodes = Array.from(document.querySelectorAll('textarea, input, button, [contenteditable="true"], [role="textbox"], a'));
        return nodes.slice(0, 120).map((node) => ({
            tag: node.tagName.toLowerCase(),
            type: node.getAttribute('type') || '',
            id: node.id || '',
            cls: node.className || '',
            name: node.getAttribute('name') || '',
            placeholder: node.getAttribute('placeholder') || '',
            aria: node.getAttribute('aria-label') || '',
            text: (node.innerText || node.value || node.textContent || '').trim().slice(0, 80),
            href: node.getAttribute('href') || ''
        })).filter((item) => {
            const haystack = Object.values(item).join(' ').toLowerCase();
            return /comment|reply|post|submit|login|chapter/.test(haystack);
        }).slice(0, 30);
    """
    try:
        candidates = driver.execute_script(script)
    except JavascriptException:
        logging.warning("Could not collect comment diagnostics.")
        return

    logging.warning("Comment form was not found. Candidate page controls:")
    for index, item in enumerate(candidates or [], start=1):
        logging.warning("Candidate %s: %s", index, json.dumps(item, ensure_ascii=False))


def wait_for_confirmation(driver: WebDriver) -> None:
    if not CONFIRMATION_SELECTOR.strip():
        logging.info("No confirmation selector configured; waiting %s seconds after submit.", POST_SUBMIT_WAIT_SECONDS)
        time.sleep(POST_SUBMIT_WAIT_SECONDS)
        return

    try:
        WebDriverWait(driver, POST_SUBMIT_WAIT_SECONDS).until(
            EC.visibility_of_element_located((By.CSS_SELECTOR, CONFIRMATION_SELECTOR))
        )
        logging.info("Submission confirmation detected.")
    except TimeoutException:
        logging.warning("Confirmation selector was not detected within %s seconds.", POST_SUBMIT_WAIT_SECONDS)


def find_nearby_submit_button(driver: WebDriver, editor: WebElement) -> Optional[WebElement]:
    script = """
        const editor = arguments[0];
        const visible = (node) => {
            if (!node) return false;
            const rect = node.getBoundingClientRect();
            const style = window.getComputedStyle(node);
            return rect.width > 0
                && rect.height > 0
                && style.visibility !== "hidden"
                && style.display !== "none"
                && !node.disabled
                && node.getAttribute("aria-disabled") !== "true";
        };
        const haystack = (node) => [
            node.innerText || "",
            node.value || "",
            node.getAttribute("aria-label") || "",
            node.getAttribute("title") || "",
            node.getAttribute("type") || "",
            node.id || "",
            node.className || ""
        ].join(" ").toLowerCase();
        const roots = [];
        const form = editor.closest("form");
        if (form) roots.push(form);
        let node = editor.parentElement;
        for (let depth = 0; node && depth < 8; depth += 1, node = node.parentElement) {
            roots.push(node);
        }
        roots.push(document);

        const editorRect = editor.getBoundingClientRect();
        const seen = new Set();
        const candidates = [];
        for (const root of roots) {
            for (const button of root.querySelectorAll("button, input[type='submit'], input[type='button']")) {
                if (seen.has(button) || !visible(button)) continue;
                seen.add(button);
                const text = haystack(button);
                const rect = button.getBoundingClientRect();
                let score = 0;
                if (/send|post|submit|comment|reply/.test(text)) score += 100;
                if ((button.getAttribute("type") || "").toLowerCase() === "submit") score += 80;
                if (rect.top >= editorRect.top - 120) score += 20;
                if (Math.abs(rect.top - editorRect.bottom) < 240) score += 20;
                if (rect.left >= editorRect.left - 80) score += 10;
                candidates.push({button, score, top: rect.top, left: rect.left});
            }
            const strong = candidates.filter((candidate) => candidate.score >= 80);
            if (strong.length) {
                strong.sort((a, b) => b.score - a.score || a.top - b.top || b.left - a.left);
                return strong[0].button;
            }
        }
        candidates.sort((a, b) => b.score - a.score || a.top - b.top || b.left - a.left);
        return candidates.length && candidates[0].score >= 40 ? candidates[0].button : null;
    """
    try:
        button = driver.execute_script(script, editor)
    except JavascriptException:
        return None
    return button if isinstance(button, WebElement) else None


def find_visible_unsubmitted_comment_editor(driver: WebDriver) -> Optional[LocatedElement]:
    driver.switch_to.default_content()
    script = """
        const selector = arguments[0];
        const viewportCenter = window.scrollY + (window.innerHeight / 2);
        const visible = (node) => {
            const rect = node.getBoundingClientRect();
            const style = window.getComputedStyle(node);
            return rect.width > 0
                && rect.height > 0
                && style.visibility !== "hidden"
                && style.display !== "none"
                && !node.disabled
                && node.getAttribute("aria-disabled") !== "true";
        };
        const candidates = Array.from(document.querySelectorAll(selector)).filter((node) => {
            return node.getAttribute("data-asura-qa-submitted-editor") !== "true" && visible(node);
        }).map((node) => {
            const rect = node.getBoundingClientRect();
            const pageY = rect.top + window.scrollY;
            return {node, distance: Math.abs(pageY - viewportCenter), pageY};
        });
        candidates.sort((a, b) => a.distance - b.distance || a.pageY - b.pageY);
        return candidates.length ? candidates[0].node : null;
    """
    try:
        element = driver.execute_script(script, COMMENT_TEXTAREA_SELECTOR)
    except JavascriptException:
        return None
    if not isinstance(element, WebElement):
        return None
    logging.info("Using visible unsubmitted editor nearest the viewport.")
    return LocatedElement(
        element=element,
        locator=css_locator(COMMENT_TEXTAREA_SELECTOR, "visible unsubmitted comment editor"),
        context="top document",
    )


def click_submit_button(driver: WebDriver, button: WebElement) -> None:
    try:
        driver.execute_script("arguments[0].scrollIntoView({block: 'center', inline: 'nearest'});", button)
        time.sleep(0.05)
        button.click()
        logging.info("Submit button clicked.")
    except WebDriverException as exc:
        logging.warning("Normal submit click failed, trying JavaScript click: %s", exc)
        driver.execute_script("arguments[0].click();", button)
        logging.info("Submit button clicked with JavaScript fallback.")


def click_say_thanks(driver: WebDriver) -> Optional[int]:
    driver.switch_to.default_content()
    script = """
        const selector = arguments[0];
        const visible = (node) => {
            const rect = node.getBoundingClientRect();
            const style = window.getComputedStyle(node);
            return rect.width > 0
                && rect.height > 0
                && style.visibility !== "hidden"
                && style.display !== "none"
                && !node.disabled
                && node.getAttribute("aria-disabled") !== "true";
        };
        const candidates = Array.from(document.querySelectorAll(selector)).filter((node) => {
            if (!visible(node)) return false;
            const haystack = [
                node.innerText || "",
                node.getAttribute("aria-label") || "",
                node.getAttribute("title") || "",
                node.id || "",
                node.className || ""
            ].join(" ").toLowerCase();
            return /say\\s+thanks|thanks/.test(haystack) && !/already\\s+thanked/.test(haystack);
        });
        if (!candidates.length) return null;
        candidates.sort((a, b) => {
            const ay = a.getBoundingClientRect().top + window.scrollY;
            const by = b.getBoundingClientRect().top + window.scrollY;
            return ay - by;
        });
        const markerY = Math.round(candidates[0].getBoundingClientRect().top + window.scrollY);
        candidates[0].scrollIntoView({block: "center", inline: "nearest"});
        candidates[0].click();
        return markerY;
    """
    try:
        marker_y = driver.execute_script(script, THANK_BUTTON_SELECTOR)
    except (JavascriptException, WebDriverException) as exc:
        logging.warning("Could not click Say thanks marker: %s", exc)
        return None
    try:
        marker_y_int = int(marker_y)
    except (TypeError, ValueError):
        marker_y_int = 0
    if marker_y_int:
        logging.info("Clicked Say thanks read marker at y=%s.", marker_y_int)
        time.sleep(0.35)
        return marker_y_int
    else:
        logging.info("Say thanks read marker was not found or was not clickable.")
    return None


def submit_comment(driver: WebDriver, comment_text: str) -> int:
    comment_locators = [
        css_locator(COMMENT_TEXTAREA_SELECTOR, "comment textarea"),
        Locator(By.XPATH, "//textarea[contains(translate(@placeholder, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'comment')]", "comment textarea by placeholder"),
        Locator(By.XPATH, "//*[@contenteditable='true' or @role='textbox']", "editable comment field"),
    ]
    submit_locators = [
        css_locator(SUBMIT_BUTTON_SELECTOR, "submit button"),
        Locator(By.XPATH, "//button[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'send')]", "send button text"),
        Locator(By.XPATH, "//button[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'post')]", "post button text"),
        Locator(By.XPATH, "//button[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'submit')]", "submit button text"),
        Locator(By.XPATH, "//button[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'comment')]", "comment button text"),
        Locator(By.XPATH, "//button[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'reply')]", "reply button text"),
        Locator(By.XPATH, "//input[@type='submit' or @type='button']", "input submit/button"),
    ]

    comment_area = find_visible_unsubmitted_comment_editor(driver)
    if comment_area is None:
        try:
            comment_area = find_in_page_or_iframe(driver, comment_locators, WAIT_SECONDS)
        except TimeoutException:
            log_comment_diagnostics(driver)
            raise
    logging.info("Comment textarea found in %s.", comment_area.context)
    submission_y = element_page_y(driver, comment_area.element)
    logging.info("Active comment editor page position: y=%s.", submission_y)
    clear_and_type(comment_area.element, comment_text)

    nearby_submit = find_nearby_submit_button(driver, comment_area.element)
    if nearby_submit is not None:
        logging.info("Submit button found near the active editor; clicking immediately after typing.")
        click_submit_button(driver, nearby_submit)
    else:
        logging.info("No nearby submit button found; using short fallback locator search.")
        submit_button = find_in_page_or_iframe(driver, submit_locators, 1)
        logging.info("Submit button found in %s.", submit_button.context)
        click_submit_button(driver, submit_button.element)
    mark_submitted_editor(driver, comment_area.element)
    wait_for_confirmation(driver)
    return submission_y


def click_reader_next_control(driver: WebDriver) -> Optional[str]:
    driver.switch_to.default_content()
    before_url = driver.current_url
    before_key = canonical_chapter_key(before_url)
    script = """
        const visible = (node) => {
            const rect = node.getBoundingClientRect();
            const style = window.getComputedStyle(node);
            return rect.width > 0
                && rect.height > 0
                && style.visibility !== "hidden"
                && style.display !== "none"
                && !node.disabled
                && node.getAttribute("aria-disabled") !== "true";
        };
        const pageCounters = Array.from(document.querySelectorAll("button[role='combobox'], [role='combobox']")).filter((node) => {
            return visible(node) && /^\\s*\\d+\\s*\\/\\s*\\d+\\s*$/.test((node.innerText || node.textContent || "").trim());
        }).map((node) => {
            const rect = node.getBoundingClientRect();
            return {node, rect, y: rect.top + window.scrollY};
        });
        if (!pageCounters.length) return false;
        pageCounters.sort((a, b) => b.y - a.y);
        const counter = pageCounters[0];
        const buttons = Array.from(document.querySelectorAll("button")).filter((button) => {
            if (!visible(button)) return false;
            const rect = button.getBoundingClientRect();
            return Math.abs(rect.top - counter.rect.top) < 60 && rect.left > counter.rect.left + 10;
        }).map((button) => ({button, rect: button.getBoundingClientRect()}));
        if (!buttons.length) return false;
        buttons.sort((a, b) => a.rect.left - b.rect.left);
        const nextButton = buttons[0].button;
        nextButton.scrollIntoView({block: "center", inline: "nearest"});
        nextButton.click();
        return true;
    """
    try:
        clicked = bool(driver.execute_script(script))
    except (JavascriptException, WebDriverException) as exc:
        logging.warning("Reader next control fallback failed: %s", exc)
        return None

    if not clicked:
        logging.info("Reader next control fallback did not find a usable page/chapter button.")
        return None

    logging.info("Clicked reader next control fallback.")
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        time.sleep(0.5)
        try:
            after_url = driver.current_url
        except WebDriverException:
            return None
        if after_url != before_url:
            logging.info("Reader next control changed URL: %s", after_url)
            wait_for_page_ready(driver, timeout_seconds=5)
            return after_url
        if canonical_chapter_key(after_url) != before_key:
            logging.info("Reader next control changed chapter: %s", after_url)
            wait_for_page_ready(driver, timeout_seconds=5)
            return after_url

    logging.info("Reader next control did not change the URL within the navigation timeout.")
    return None


def resolve_series_continue_url(driver: WebDriver, current_url: str) -> Optional[str]:
    series_url = series_url_from_chapter_url(current_url)
    if not series_url:
        logging.info("Could not derive series URL from current chapter URL: %s", current_url)
        return None

    current_key = canonical_chapter_key(current_url)
    logging.info("Trying series Continue fallback: %s", series_url)
    if not load_url_with_retries(driver, series_url, "series continue fallback"):
        return None

    script = """
        const currentKey = arguments[0];
        const links = Array.from(document.querySelectorAll('a[href*="/content/"]')).map((link) => {
            const rect = link.getBoundingClientRect();
            const text = (link.innerText || link.textContent || '').trim();
            return {
                href: link.href,
                text,
                visible: rect.width > 0 && rect.height > 0 && getComputedStyle(link).display !== 'none' && getComputedStyle(link).visibility !== 'hidden'
            };
        }).filter((item) => item.href && item.visible);
        const preferred = links.find((item) => /continue/i.test(item.text));
        return preferred ? preferred.href : "";
    """
    try:
        continue_url = str(driver.execute_script(script, current_key) or "")
    except JavascriptException:
        continue_url = ""

    if not continue_url:
        logging.info("Series Continue fallback did not find a Continue chapter link.")
        return None
    if canonical_chapter_key(continue_url) == current_key:
        logging.info("Series Continue fallback points to the same chapter: %s", continue_url)
        return None

    logging.info("Series Continue fallback resolved next chapter URL: %s", continue_url)
    return continue_url


def resolve_next_url(driver: WebDriver) -> Optional[str]:
    driver.switch_to.default_content()
    try:
        next_element = wait_for_clickable(driver, NEXT_LINK_SELECTOR, "next chapter link")
    except TimeoutException:
        logging.info("No standard next chapter link found; trying reader next control fallback.")
        return click_reader_next_control(driver)

    href = next_element.get_attribute("href")
    if href:
        logging.info("Next chapter URL resolved from href: %s", href)
        return href

    logging.info("Next control has no href; clicking it directly.")
    before_url = driver.current_url
    next_element.click()
    time.sleep(random.uniform(0.8, 1.6))
    if driver.current_url != before_url:
        return driver.current_url
    return click_reader_next_control(driver)


def chapter_cooldown() -> None:
    wait_seconds = random.randint(COOLDOWN_MIN_SECONDS, COOLDOWN_MAX_SECONDS)
    logging.info("Chapter cooldown: sleeping for %s seconds.", wait_seconds)
    time.sleep(wait_seconds)


def wait_for_dashboard_start(driver: WebDriver, start_url: str, signal_file: Optional[Path]) -> None:
    if signal_file is None:
        return

    if not load_url_with_retries(driver, start_url, "manual-login start page"):
        raise TimeoutException(f"Could not load start URL for manual login: {start_url}")
    logging.info("Opened start URL for manual setup: %s", start_url)
    logging.info("Log in, choose the exact chapter/page you want in Chrome, then press Start Comments in the dashboard.")
    logging.info("Waiting for dashboard start signal: %s", signal_file)

    while not signal_file.exists():
        time.sleep(1)

    logging.info("Dashboard start signal received. Beginning comment workflow from current browser page: %s", driver.current_url)


def process_chapters(
    driver: WebDriver,
    start_url: str,
    max_chapters: int,
    history_file: Path,
    read_marker_file: Path,
    use_thanks_marker: bool,
    research_file: Path,
    research_endpoint: str,
    chapter_routing: str,
    scroll_min_px: int,
    scroll_max_px: int,
    scroll_min_delay: float,
    scroll_max_delay: float,
    already_on_start_url: bool = False,
) -> int:
    used_comments: set[str] = set()
    current_url: Optional[str] = start_url
    completed = 0
    history = load_comment_history(history_file)
    read_markers = load_read_marker_keys(read_marker_file)
    logging.info("Loaded %s historical comment record(s) from %s.", len(history), history_file)
    logging.info("Loaded %s read marker(s) from %s.", len(read_markers), read_marker_file)
    logging.info("Chapter routing mode: %s.", chapter_routing)
    logging.info("Comment generator active styles: %s.", len(COMMENT_STYLES))
    logging.info("Thanks/read marker mode: %s.", "enabled" if use_thanks_marker else "disabled")

    for chapter_index in range(max_chapters):
        if not current_url:
            logging.info("Stopping because there is no next URL.")
            break

        logging.info("Processing chapter %s/%s: %s", chapter_index + 1, max_chapters, current_url)
        chapter_context: Optional[ChapterContext] = None
        generated_comment: Optional[GeneratedComment] = None
        try:
            driver.switch_to.default_content()
            if chapter_index > 0 and chapter_routing == "infinite-scroll":
                logging.info("Continuing on the already-open infinite-scroll reader page.")
            elif chapter_index == 0 and already_on_start_url:
                current_url = driver.current_url
                logging.info("Using the already-open page after manual setup: %s", current_url)
                wait_for_page_ready(driver)
                if not wait_for_cloudflare_to_clear(driver):
                    logging.error("Manual setup page is still blocked by a challenge; stopping run.")
                    break
            elif not load_url_with_retries(driver, current_url, f"chapter {chapter_index + 1}"):
                break
            else:
                time.sleep(random.uniform(0.25, 0.6))

            if chapter_index == 0:
                current_url = maybe_open_resume_or_first_chapter(driver, current_url)
                clear_submitted_editor_marks(driver)

            current_chapter_key = canonical_chapter_key(driver.current_url)
            if (
                use_thanks_marker
                and current_chapter_key in read_markers
                and not (chapter_routing == "infinite-scroll" and chapter_index > 0)
            ):
                logging.info("Current chapter is already marked as read/thanked locally: %s", current_chapter_key)
                next_url = resolve_next_url(driver)
                if not next_url:
                    next_url = resolve_series_continue_url(driver, driver.current_url)
                if not next_url or canonical_chapter_key(next_url) == current_chapter_key:
                    logging.info("No next chapter URL found after read-marker skip; stopping.")
                    break
                current_url = next_url
                continue

            if chapter_routing == "infinite-scroll" and chapter_index == 0:
                if not scroll_to_first_infinite_comment_section(
                    driver,
                    min_step_px=scroll_min_px,
                    max_step_px=scroll_max_px,
                    min_pause_seconds=scroll_min_delay,
                    max_pause_seconds=scroll_max_delay,
                ):
                    logging.info("No first comment section found on the infinite-scroll page; stopping.")
                    log_comment_diagnostics(driver)
                    break
            elif chapter_routing == "infinite-scroll" and chapter_index > 0:
                logging.info("Already positioned at the next infinite-scroll comment section.")
            else:
                slow_reading_scroll(
                    driver,
                    min_step_px=scroll_min_px,
                    max_step_px=scroll_max_px,
                    min_pause_seconds=scroll_min_delay,
                    max_pause_seconds=scroll_max_delay,
                )
            chapter_context = build_chapter_context(driver)
            research_context = resolve_research_context(chapter_context, research_file, research_endpoint)
            if research_context:
                logging.info("Research enrichment source: %s", research_context.source)
            else:
                logging.info("No research enrichment found for this chapter; using chapter text only.")

            if chapter_context.text_length:
                generated_comment = build_contextual_comment(
                    chapter_context,
                    chapter_index,
                    used_comments,
                    history,
                    research_context,
                )
            else:
                logging.warning("No chapter text extracted; falling back to generic QA comment.")
                fallback_text = generate_comment(chapter_index, used_comments)
                generated_comment = GeneratedComment(
                    text=fallback_text,
                    style="fallback",
                    keyword="",
                    emoji_used=any(char in fallback_text for char in "🙂😅👀🔥"),
                    research_source=research_context.source if research_context else "",
                )
            logging.info("Generated contextual QA comment [%s]: %s", generated_comment.style, generated_comment.text)
            if generated_comment.reflection_used:
                logging.info("Rare historical/philosophical reflection added to this comment.")
            submission_y = submit_comment(driver, generated_comment.text)
            thanked = False
            thanks_marker_y = submission_y
            if use_thanks_marker:
                clicked_thanks_y = click_say_thanks(driver)
                thanked = clicked_thanks_y is not None
                if clicked_thanks_y is not None:
                    thanks_marker_y = clicked_thanks_y
                chapter_key = append_read_marker(
                    read_marker_file,
                    url=driver.current_url,
                    title=chapter_context.title,
                    thanked=thanked,
                )
                read_markers.add(chapter_key)
                logging.info("Recorded local read marker for chapter: %s", chapter_key)
            append_comment_history(
                history_file,
                chapter_index=chapter_index,
                url=current_url,
                context=chapter_context,
                generated=generated_comment,
                status="submitted",
            )
            history.append(
                {
                    "style": generated_comment.style,
                    "keyword": generated_comment.keyword,
                    "emoji_used": generated_comment.emoji_used,
                    "reflection_used": generated_comment.reflection_used,
                    "research_source": generated_comment.research_source,
                    "status": "submitted",
                }
            )
            completed += 1

            if chapter_index == max_chapters - 1:
                logging.info("Reached configured max chapter count.")
                break

            if use_thanks_marker and chapter_routing == "infinite-scroll":
                logging.info("Read marker mode completed this chapter; scrolling down to the next chapter block.")
                if not scroll_to_next_chapter_comment_section(
                    driver,
                    last_marker_y=thanks_marker_y,
                    min_step_px=scroll_min_px,
                    max_step_px=scroll_max_px,
                    min_pause_seconds=scroll_min_delay,
                    max_pause_seconds=scroll_max_delay,
                ):
                    logging.info("No lower chapter block found after read marker; stopping.")
                    log_comment_diagnostics(driver)
                    break
                current_url = driver.current_url
                chapter_cooldown()
                continue

            if chapter_routing == "infinite-scroll":
                if not scroll_to_next_infinite_comment_section(
                    driver,
                    last_submission_y=submission_y,
                    min_step_px=scroll_min_px,
                    max_step_px=scroll_max_px,
                    min_pause_seconds=scroll_min_delay,
                    max_pause_seconds=scroll_max_delay,
                ):
                    logging.info("No lower comment section found on the infinite-scroll page; stopping.")
                    log_comment_diagnostics(driver)
                    break
                current_url = driver.current_url
                chapter_cooldown()
                continue

            next_url = resolve_next_url(driver)
            if (not next_url or next_url == current_url) and chapter_routing == "auto":
                logging.info("No next URL found; falling back to infinite-scroll routing on the same page.")
                if scroll_to_next_infinite_comment_section(
                    driver,
                    last_submission_y=submission_y,
                    min_step_px=scroll_min_px,
                    max_step_px=scroll_max_px,
                    min_pause_seconds=scroll_min_delay,
                    max_pause_seconds=scroll_max_delay,
                ):
                    current_url = driver.current_url
                    chapter_routing = "infinite-scroll"
                    chapter_cooldown()
                    continue

            if not next_url or next_url == current_url:
                logging.info("No new next URL available; stopping.")
                break

            chapter_cooldown()
            current_url = next_url
        except (TimeoutException, WebDriverException, JavascriptException) as exc:
            logging.exception("Chapter %s failed gracefully; stopping run without crashing: %s", chapter_index + 1, exc)
            if chapter_context is not None and generated_comment is not None:
                append_comment_history(
                    history_file,
                    chapter_index=chapter_index,
                    url=current_url,
                    context=chapter_context,
                    generated=generated_comment,
                    status="failed",
                    error=str(exc),
                )
            break

    return completed


def main() -> int:
    args = parse_args()
    configure_logging(args.log_file)

    if args.max_chapters < 1:
        logging.error("--max-chapters must be at least 1.")
        return 2
    if args.scroll_min_px < 1 or args.scroll_max_px < args.scroll_min_px:
        logging.error("Scroll pixel settings are invalid. Ensure 1 <= min <= max.")
        return 2
    if args.scroll_min_delay < 0 or args.scroll_max_delay < args.scroll_min_delay:
        logging.error("Scroll delay settings are invalid. Ensure 0 <= min <= max.")
        return 2

    driver_session: Optional[DriverSession] = None
    try:
        driver_session = build_driver()
        driver = driver_session.driver
        wait_for_dashboard_start(driver, args.start_url, args.start_signal_file)
        completed = process_chapters(
            driver,
            args.start_url,
            args.max_chapters,
            args.history_file,
            args.read_marker_file,
            args.use_thanks_marker,
            args.research_file,
            args.research_endpoint,
            args.chapter_routing,
            args.scroll_min_px,
            args.scroll_max_px,
            args.scroll_min_delay,
            args.scroll_max_delay,
            already_on_start_url=args.start_signal_file is not None,
        )
        logging.info("Run complete. Chapters processed: %s.", completed)
        return 0
    except Exception as exc:
        logging.exception("Automation failed: %s", exc)
        return 1
    finally:
        if driver_session is not None:
            try:
                if driver_session.attached_to_existing_chrome:
                    logging.info("Closing WebDriver session; existing Chrome debug process will remain open.")
                else:
                    logging.info("Closing WebDriver and Chrome process.")
                driver_session.driver.quit()
            except WebDriverException as exc:
                logging.warning("Failed to close WebDriver cleanly: %s", exc)


if __name__ == "__main__":
    sys.exit(main())
