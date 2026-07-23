#!/usr/bin/env python3
"""
Sequential Selenium QA engine for owned content pages.

Configure selectors below for your staging or QA target before running.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import getpass
import json
import logging
import random
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional, TextIO
from urllib.error import URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import urlopen

from selenium import webdriver
from selenium.common.exceptions import (
    JavascriptException,
    NoAlertPresentException,
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

from chapter_content_extractor import extract_chapter_content
from automation_runtime import (
    RunStore,
    acquire_browser_lock,
    normalize_web_url,
    release_browser_lock,
    reporter_from_environment,
)
from history_utils import append_jsonl_record, read_jsonl_records
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
NEXT_LINK_SELECTOR = DEFAULT_NEXT_CHAPTER_SELECTOR
CHAPTER_ROUTING_MODE = "auto"  # auto, next-link, or infinite-scroll
NEXT_PAGE_SELECTOR = DEFAULT_NEXT_PAGE_SELECTOR
PAGE_COUNTER_SELECTOR = DEFAULT_PAGE_COUNTER_SELECTOR
CHAPTER_LIST_SELECTOR = DEFAULT_CHAPTER_LIST_SELECTOR
CONTINUE_READING_SELECTOR = (
    "a[href*='chapter' i], button, a"
)
FIRST_CHAPTER_SELECTOR = (
    "a[href*='/content/'], a[href*='chapter'], a[href*='episode'], "
    ".chapter a, .chapter-list a, .episodes a, .episode-list a, "
    ".wp-manga-chapter a, .listing-chapters_wrap a, "
    "[class*='chapter' i] a, [class*='episode' i] a"
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
CONTENT_MODE = "auto"  # auto, novel, or image-based comic
COMIC_CONTENT_MODE_ALIASES = {"comic", "manga", "manhwa", "manhua"}
MANHWA_IMAGE_SELECTOR = (
    "img.wp-manga-chapter-img, img.chapter-img, img[data-src], "
    "img[data-lazy-src], img[data-original], img[src], img"
)
OCR_CONFIDENCE = 0.45

# Optional. Leave blank if the app does not show a predictable confirmation node.
CONFIRMATION_SELECTOR = ".comment-success, .notice-success, .toast-success, [data-testid='comment-success']"

# Timing and pacing settings.
WAIT_SECONDS = 15
SUBMIT_BUTTON_WAIT_SECONDS = 2
POST_SUBMIT_WAIT_SECONDS = 4.0
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
# Keep emoji usage intentional and ChromeDriver-safe. Each option is only selected
# when the active comment style has a matching story context.
OPTIONAL_EMOJIS = [" ✨", " ⚡", " ⚔", " ❤", " ☠"]
EMOJI_STYLE_MAP = {
    "joke": [" ✨"],
    "comedy_timing": [" ✨"],
    "mystery": [" ⚡"],
    "suspicion": [" ⚡"],
    "theory": [" ⚡"],
    "foreshadowing": [" ⚡"],
    "battle_tactics": [" ⚔"],
    "power_scaling": [" ⚔"],
    "emotional": [" ❤"],
    "relationship": [" ❤"],
    "betrayal_watch": [" ☠"],
    "villain_watch": [" ☠"],
}
REFLECTION_EVERY_N_COMMENTS = 5
COMMENT_SIMILARITY_LIMIT = 0.72
COMMENT_TOKEN_OVERLAP_LIMIT = 0.68
COMMENT_HISTORY_COMPARISON_LIMIT = 500
PRIMARY_KEYWORD_REUSE_LIMIT = 2
SECONDARY_KEYWORD_REUSE_LIMIT = 3
RECENT_PHRASE_WINDOW = 80
REPEATED_PHRASE_LIMIT = 1
GRAMMAR_IMPERFECTION_RATE = 0.14
DASH_TRANSLATION = str.maketrans({
    "-": " ",
    "\u2010": " ",
    "\u2011": " ",
    "\u2012": " ",
    "\u2013": " ",
    "\u2014": " ",
    "\u2212": " ",
})
NOISY_KEYWORDS = {
    "ad",
    "ads",
    "advert",
    "adverts",
    "already",
    "advertisement",
    "advertisements",
    "advertising",
    "albex",
    "banner",
    "banners",
    "bedsidecanoe",
    "calsed",
    "chapter",
    "chapters",
    "comment",
    "commenting",
    "comments",
    "compe",
    "cookie",
    "cookies",
    "diei",
    "discord",
    "embed",
    "first",
    "firs",
    "gtrash",
    "hasjfnjknsvjnjkdsfvnjknf",
    "help",
    "hopefllly",
    "huhi",
    "ifelt",
    "ishould",
    "jkasndjknasjknjksnjkvnjknjscnjks",
    "join",
    "kami",
    "leave",
    "less",
    "lristensei",
    "mangatod",
    "notes",
    "olherreaders",
    "page",
    "pages",
    "patreon",
    "paypal",
    "popup",
    "popups",
    "promo",
    "promos",
    "promoted",
    "promotion",
    "promotions",
    "purplepoet",
    "read",
    "readireread",
    "reader",
    "readers",
    "reading",
    "reread",
    "rimzthbone",
    "repost",
    "right",
    "scan",
    "scans",
    "scanlation",
    "scanlations",
    "sconlations",
    "send",
    "server",
    "site",
    "skyroy",
    "stabi",
    "strash",
    "strasge",
    "support",
    "sponsor",
    "sponsored",
    "sponsors",
    "sponsorship",
    "talk",
    "thank",
    "thanked",
    "thanks",
    "tpash",
    "trash",
    "trashscanlations",
    "translator",
    "unable",
    "upload",
    "vyncuy",
    "website",
    "zerccies",
}
COMMENT_BANNED_WORDS = {
    "ad",
    "ads",
    "advert",
    "adverts",
    "advertisement",
    "advertisements",
    "advertising",
    "banner",
    "banners",
    "cookie",
    "cookies",
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
}
NEUTRAL_COMMENT_ANCHORS = [
    "scene direction",
    "setup",
    "pacing",
    "tension",
    "final beat",
    "mood shift",
    "reveal",
    "panel flow",
    "character choice",
    "quiet detail",
    "cliffhanger",
    "timing",
    "buildup",
    "small reaction",
    "main turn",
    "chapter flow",
]
REFLECTION_TEMPLATES = [
    lambda keyword: f"Kind of reminds me of Socrates saying the unexamined life is not worth living; {keyword} feels like the thing this chapter wants us to examine.",
    lambda keyword: f"Aristotle wrote that character is revealed through action, and {keyword} really fits that idea here.",
    lambda keyword: f"Marcus Aurelius would probably ask whether {keyword} is something they can control or something they have to endure.",
    lambda keyword: f"Sun Tzu would call {keyword} the kind of detail that matters before the fight even starts.",
    lambda keyword: f"Nietzsche's line about becoming who you are came to mind here; {keyword} feels like part of that pressure.",
    lambda keyword: f"Plato would probably ask if {keyword} is the truth of the scene or just the shadow on the wall.",
    lambda keyword: f"Machiavelli would probably watch {keyword} and ask who benefits from that move.",
    lambda keyword: f"Confucius would probably care less about the noise and more about what {keyword} says about duty.",
    lambda keyword: f"Shakespeare's idea that appearance can hide intent fits {keyword} really well here.",
    lambda keyword: f"Einstein's warning about imagination being powerful came to mind because {keyword} opens up so many possibilities.",
    lambda keyword: f"Lincoln's line about character being tested by power fits the way {keyword} is being handled.",
    lambda keyword: f"Bruce Lee's idea of adapting like water weirdly fits {keyword}; the scene keeps changing shape around it.",
    lambda keyword: f"This chapter makes me wonder: does {keyword} show who someone is, or what the situation forced them to become?",
    lambda keyword: f"Philosophy question for this chapter: if {keyword} changes the outcome, was it fate, choice, or just timing?",
    lambda keyword: f"This has that old question of power versus responsibility, especially with how {keyword} is handled.",
    lambda keyword: f"The chapter quietly asks whether {keyword} is courage, pride, or survival.",
]
REFLECTION_BY_CONTEXT = {
    "choice": [lambda keyword: f"The question here is whether {keyword} is a real choice or the only option left."],
    "duty": [lambda keyword: f"The tension around {keyword} makes this feel like a duty-versus-desire question."],
    "identity": [lambda keyword: f"The way {keyword} lands makes identity feel like the real conflict underneath the scene."],
    "power": [lambda keyword: f"The chapter makes {keyword} feel less like strength and more like responsibility."],
    "control": [lambda keyword: f"The interesting question is whether {keyword} is something anyone here can actually control."],
    "truth": [lambda keyword: f"The chapter turns {keyword} into a question of what people are willing to admit."],
    "survival": [lambda keyword: f"With {keyword} on the line, survival and principle stop being easy to separate."],
    "strategy": [lambda keyword: f"The strategy around {keyword} makes the scene feel like a choice between patience and action."],
    "sacrifice": [lambda keyword: f"The cost behind {keyword} is what gives this chapter its philosophical weight."],
    "betrayal": [lambda keyword: f"The way {keyword} is handled raises the harder question: what does loyalty cost here?"],
    "loyalty": [lambda keyword: f"The chapter makes {keyword} feel like a test of loyalty rather than a simple plot beat."],
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
    "definitely",
    "detail",
    "details",
    "did",
    "does",
    "down",
    "each",
    "even",
    "feel",
    "feels",
    "felt",
    "from",
    "going",
    "gave",
    "give",
    "given",
    "giving",
    "had",
    "has",
    "have",
    "her",
    "here",
    "him",
    "his",
    "how",
    "into",
    "just",
    "keep",
    "keeps",
    "let",
    "lets",
    "like",
    "made",
    "make",
    "makes",
    "more",
    "not",
    "now",
    "out",
    "over",
    "person",
    "already",
    "comment",
    "comments",
    "reply",
    "see",
    "seeing",
    "seen",
    "send",
    "thanks",
    "thanked",
    "really",
    "she",
    "should",
    "some",
    "someone",
    "still",
    "that",
    "the",
    "their",
    "them",
    "then",
    "there",
    "they",
    "thing",
    "things",
    "this",
    "through",
    "time",
    "was",
    "were",
    "very",
    "what",
    "when",
    "where",
    "which",
    "while",
    "who",
    "why",
    "will",
    "with",
    "without",
    "would",
    "you",
    "advertisement",
    "asura",
    "credits",
    "cleaner",
    "discord",
    "donate",
    "follow",
    "patreon",
    "proofreader",
    "redrawer",
    "repost",
    "reupload",
    "scan",
    "scans",
    "scanlation",
    "scanlations",
    "staff",
    "support",
    "translator",
    "translation",
    "typesetter",
    "uploader",
    "visit",
    *NOISY_KEYWORDS,
}


@dataclass(frozen=True)
class DriverSession:
    driver: WebDriver
    attached_to_existing_chrome: bool
    browser_lock: TextIO


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
        choices=("auto", "next-link", "infinite-scroll", "paginated", "mixed"),
        default=CHAPTER_ROUTING_MODE,
        help="Reader routing: auto, next chapter links, continuous chapters, paginated pages, or mixed pagination plus next chapter.",
    )
    parser.add_argument("--next-link-selector", default=NEXT_LINK_SELECTOR)
    parser.add_argument("--next-page-selector", default=NEXT_PAGE_SELECTOR)
    parser.add_argument("--page-counter-selector", default=PAGE_COUNTER_SELECTOR)
    parser.add_argument("--chapter-list-selector", default=CHAPTER_LIST_SELECTOR)
    parser.add_argument("--comment-editor-selector", default=COMMENT_TEXTAREA_SELECTOR)
    parser.add_argument("--submit-button-selector", default=SUBMIT_BUTTON_SELECTOR)
    parser.add_argument("--thanks-button-selector", default=THANK_BUTTON_SELECTOR)
    parser.add_argument("--confirmation-selector", default=CONFIRMATION_SELECTOR)
    parser.add_argument(
        "--content-mode",
        choices=("auto", "novel", "comic", "manga", "manhwa", "manhua"),
        default=CONTENT_MODE,
        help="How chapter content is read before comment generation. Comic, manga, manhwa, and manhua use the same image reader.",
    )
    parser.add_argument(
        "--content-container-selector",
        default=CONTENT_SELECTOR,
        help="CSS selector for the chapter content container used by text extraction and OCR image discovery.",
    )
    parser.add_argument(
        "--ocr-image-selector",
        default=MANHWA_IMAGE_SELECTOR,
        help="CSS selector for manhwa/manga image pages inside the chapter container.",
    )
    parser.add_argument(
        "--ocr-confidence",
        type=float,
        default=OCR_CONFIDENCE,
        help="Minimum EasyOCR confidence to keep a detected text fragment.",
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
    parser.add_argument(
        "--reflection-every-n-comments",
        type=int,
        default=REFLECTION_EVERY_N_COMMENTS,
        help="Add a famous-person/philosophical reflection every N generated comments.",
    )
    parser.add_argument(
        "--first-comment-override",
        default="",
        help="Optional reviewed text to use for the first chapter only.",
    )
    parser.add_argument("--type-min-delay", type=float, default=TYPE_MIN_DELAY, help="Minimum seconds between typed characters.")
    parser.add_argument("--type-max-delay", type=float, default=TYPE_MAX_DELAY, help="Maximum seconds between typed characters.")
    parser.add_argument("--visible-type-chars", type=int, default=0, help="Characters to visibly type before fast editor completion.")
    parser.add_argument(
        "--reuse-loaded-images",
        choices=("true", "false"),
        default="false",
        help="Use already-loaded chapter image URLs instead of doing the extra pre-comment image scan.",
    )
    parser.add_argument("--cooldown-min-seconds", type=float, default=COOLDOWN_MIN_SECONDS, help="Minimum seconds between chapters.")
    parser.add_argument("--cooldown-max-seconds", type=float, default=COOLDOWN_MAX_SECONDS, help="Maximum seconds between chapters.")
    parser.add_argument("--post-submit-wait-seconds", type=float, default=POST_SUBMIT_WAIT_SECONDS, help="Seconds to wait/check after clicking submit.")
    parser.add_argument("--headless", action="store_true", help="Run Chrome headlessly for local automated tests.")
    parser.add_argument("--chrome-profile-dir", type=Path, help="Optional isolated Chrome profile directory.")
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
    chrome_root = Path(f"/Users/{username}/Library/Application Support/Google/Chrome")
    preferred = chrome_root / "PanelyraProfile"
    legacy = chrome_root / "AutomationProfile"
    if legacy.exists() and not preferred.exists():
        return str(legacy)
    return str(preferred)


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


def bring_chrome_to_front() -> None:
    if sys.platform != "darwin":
        return
    try:
        subprocess.run(
            ["osascript", "-e", 'tell application "Google Chrome" to activate'],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logging.debug("Could not activate Chrome window: %s", exc)


def build_webdriver_manager_service() -> Optional[Service]:
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
        stderr = version_check.stderr.strip()
        logging.warning(
            "ChromeDriver self-check exited with status %s. Falling back to Selenium Manager. stderr=%s",
            version_check.returncode,
            stderr or "none",
        )
        return None

    logging.info("ChromeDriver self-check passed: %s", version_check.stdout.strip())
    return Service(driver_path)


def start_chrome_driver(options: Options) -> WebDriver:
    try:
        logging.info("Starting Chrome through Selenium Manager.")
        return webdriver.Chrome(options=options)
    except WebDriverException as selenium_manager_error:
        logging.warning("Selenium Manager Chrome startup failed: %s", selenium_manager_error)

    service = build_webdriver_manager_service()
    if service is None:
        raise WebDriverException("Chrome startup failed and no validated ChromeDriver fallback is available.")

    logging.info("Starting Chrome through validated webdriver-manager fallback.")
    return webdriver.Chrome(service=service, options=options)


def build_driver(*, headless: bool = False, chrome_profile_dir: Optional[Path] = None) -> DriverSession:
    browser_lock = acquire_browser_lock("Panelyra Engage or Capture")
    profile_path = str(chrome_profile_dir) if chrome_profile_dir else chrome_profile_path()
    logging.info("Using Chrome automation profile: %s", profile_path)

    try:
        options = Options()
        attached_to_existing_chrome = (
            not headless
            and chrome_profile_dir is None
            and existing_chrome_debug_session() is not None
        )

        if attached_to_existing_chrome:
            logging.info("Attaching to existing Chrome on %s:%s.", CHROME_DEBUG_HOST, CHROME_DEBUG_PORT)
            options.debugger_address = f"{CHROME_DEBUG_HOST}:{CHROME_DEBUG_PORT}"
        else:
            options.add_argument(f"--user-data-dir={profile_path}")
            options.add_argument("--profile-directory=Default")
            options.add_argument("--start-maximized")
            options.add_argument("--no-first-run")
            options.add_argument("--no-default-browser-check")
            options.add_argument("--disable-popup-blocking")
            options.add_argument("--disable-dev-shm-usage")
            if headless:
                options.add_argument("--headless=new")
                options.add_argument("--window-size=1440,1800")
            else:
                options.add_argument(f"--remote-debugging-port={CHROME_DEBUG_PORT}")
                options.add_argument(f"--remote-debugging-address={CHROME_DEBUG_HOST}")
            if KEEP_BROWSER_OPEN_ON_EXIT:
                options.add_experimental_option("detach", True)

        driver = start_chrome_driver(options)
        driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT_SECONDS)
        return DriverSession(
            driver=driver,
            attached_to_existing_chrome=attached_to_existing_chrome,
            browser_lock=browser_lock,
        )
    except BaseException:
        release_browser_lock(browser_lock)
        raise


def close_driver_session(driver_session: DriverSession) -> None:
    try:
        if driver_session.attached_to_existing_chrome:
            logging.info("Closing WebDriver session; existing Chrome debug process will remain open.")
        else:
            logging.info("Closing WebDriver and Chrome process.")
        driver_session.driver.quit()
    except WebDriverException as exc:
        logging.warning("Failed to close WebDriver cleanly: %s", exc)
    finally:
        release_browser_lock(driver_session.browser_lock)


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
                dismiss_page_interruptions(driver)
                bring_chrome_to_front()
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


def _dismiss_interruption_in_context(driver: WebDriver, allow_global_close: bool = False) -> str:
    script = """
        const allowGlobal = Boolean(arguments[0]);
        const safeAll = (root, selector) => {
            try { return Array.from(root.querySelectorAll(selector)); }
            catch (_) { return []; }
        };
        const visible = node => {
            const rect = node.getBoundingClientRect(), style = getComputedStyle(node);
            return rect.width > 0 && rect.height > 0
                && style.display !== "none" && style.visibility !== "hidden"
                && Number(style.opacity || 1) > 0.02
                && !node.disabled && node.getAttribute("aria-disabled") !== "true";
        };
        const label = node => [
            node.innerText || "", node.textContent || "", node.getAttribute("aria-label") || "",
            node.getAttribute("title") || "", node.getAttribute("data-dismiss") || "",
            node.id || "", String(node.className || "")
        ].join(" ").replace(/\\s+/g, " ").trim().toLowerCase();
        const rootSelector = [
            "[role='dialog']", "[aria-modal='true']", "dialog[open]",
            ".modal", "[class*='modal' i]", "[id*='modal' i]",
            ".popup", "[class*='popup' i]", "[id*='popup' i]",
            "[class*='overlay' i]", "[id*='overlay' i]",
            "[class*='interstitial' i]", "[class*='lightbox' i]",
            "[class*='consent' i]", "[class*='cookie' i]"
        ].join(",");
        const roots = safeAll(document, rootSelector).filter(visible);
        const fixedCandidates = safeAll(
            document,
            "[style*='position:fixed' i], [style*='position: fixed' i], "
            + "[class*='fixed' i], [class*='backdrop' i], [class*='interstitial' i]"
        );
        for (const node of fixedCandidates) {
            if (!visible(node)) continue;
            const style = getComputedStyle(node), rect = node.getBoundingClientRect();
            const z = Number.parseInt(style.zIndex || "0", 10) || 0;
            const large = rect.width * rect.height > innerWidth * innerHeight * 0.12;
            if ((style.position === "fixed" || style.position === "sticky") && z >= 20 && large) {
                roots.push(node);
            }
        }
        const closeSelector = [
            "button", "[role='button']", "a", "[data-dismiss]", "[data-close]",
            "[aria-label*='close' i]", "[title*='close' i]",
            "[class*='close' i]", "[id*='close' i]"
        ].join(",");
        const candidates = [];
        for (const root of Array.from(new Set(roots))) {
            for (const node of safeAll(root, closeSelector)) candidates.push({node, root});
        }
        if (allowGlobal) {
            for (const node of safeAll(document, closeSelector)) candidates.push({node, root: document.body});
        }
        const blocked = /next\\s*(page|chapter|episode)|send|submit|post\\s*comment|reply|login|sign\\s*in|download/;
        const closeWords = /(^|\\s)(close|dismiss|skip\\s*ad|no\\s*thanks|not\\s*now|got\\s*it)(\\s|$)/;
        const exactIcon = /^(x|×|✕|✖|❌)$/;
        const consentWords = /^(accept|accept\\s*all|agree|allow|reject|reject\\s*all)$/;
        const ranked = Array.from(new Set(candidates.map(item => item.node)))
            .filter(visible)
            .map(node => {
                const text = label(node);
                if (blocked.test(text)) return {node, text, score: -1000};
                const icon = (node.innerText || node.textContent || "").replace(/\\s+/g, "").trim();
                let score = 0;
                if (exactIcon.test(icon)) score += 160;
                if (closeWords.test(text)) score += 120;
                if (/aria-label.*close|title.*close|close[-_ ]?(button|btn|icon)?/.test(text)) score += 90;
                if (consentWords.test((node.innerText || node.textContent || "").trim().toLowerCase())) score += 55;
                if (node.matches("[data-dismiss],[data-close]")) score += 80;
                return {node, text, score};
            })
            .filter(item => item.score > 0)
            .sort((a, b) => b.score - a.score);
        if (!ranked.length) return "";
        const target = ranked[0];
        target.node.scrollIntoView({block: "center", inline: "center"});
        try { target.node.click(); }
        catch (_) {
            target.node.dispatchEvent(new MouseEvent("click", {bubbles: true, cancelable: true, view: window}));
        }
        return target.text || target.node.tagName.toLowerCase();
    """
    try:
        return str(driver.execute_script(script, allow_global_close) or "")
    except (JavascriptException, WebDriverException):
        return ""


def dismiss_page_interruptions(driver: WebDriver, attempts: int = 4) -> int:
    """Dismiss visible modal interruptions without touching reader workflow controls."""
    dismissed = 0
    driver.switch_to.default_content()
    try:
        driver.switch_to.alert.dismiss()
        dismissed += 1
        logging.info("Dismissed a browser alert.")
    except (NoAlertPresentException, WebDriverException):
        pass

    try:
        driver.execute_script(
            """
            document.dispatchEvent(new KeyboardEvent("keydown", {key:"Escape", code:"Escape", keyCode:27, bubbles:true}));
            document.dispatchEvent(new KeyboardEvent("keyup", {key:"Escape", code:"Escape", keyCode:27, bubbles:true}));
            """
        )
    except (JavascriptException, WebDriverException):
        pass

    for _ in range(max(1, attempts)):
        driver.switch_to.default_content()
        label = _dismiss_interruption_in_context(driver)
        if label:
            dismissed += 1
            logging.info("Dismissed page interruption: %s", label[:120])
            time.sleep(0.12)
            continue

        clicked_in_frame = False
        try:
            frames = driver.find_elements(By.TAG_NAME, "iframe")[:12]
        except WebDriverException:
            frames = []
        for frame in frames:
            driver.switch_to.default_content()
            try:
                driver.switch_to.frame(frame)
                label = _dismiss_interruption_in_context(driver, allow_global_close=True)
            except WebDriverException:
                label = ""
            finally:
                driver.switch_to.default_content()
            if label:
                dismissed += 1
                clicked_in_frame = True
                logging.info("Dismissed iframe interruption: %s", label[:120])
                time.sleep(0.12)
                break
        if not clicked_in_frame:
            break
    driver.switch_to.default_content()
    return dismissed


def css_locator(css_selector: str, label: str) -> Locator:
    return Locator(By.CSS_SELECTOR, css_selector, label)


def wait_for_clickable(driver: WebDriver, css_selector: str, label: str) -> WebElement:
    logging.info("Waiting for %s using selector: %s", label, css_selector)
    return wait_for_locator_clickable(driver, css_locator(css_selector, label), WAIT_SECONDS)


def chapter_link_score(current_url: str, href: str, label: str, element: WebElement) -> int:
    current_parts = [part for part in urlsplit(current_url).path.split("/") if part]
    href_parts = [part for part in urlsplit(href).path.split("/") if part]
    haystack_parts = [href, label]
    try:
        haystack_parts.extend(
            [
                element.get_attribute("class") or "",
                element.get_attribute("id") or "",
                element.get_attribute("aria-label") or "",
                element.get_attribute("title") or "",
            ]
        )
    except WebDriverException:
        pass
    haystack = " ".join(haystack_parts).lower()

    score = 0
    if href_parts[:2] == current_parts[:2] and len(href_parts) > len(current_parts):
        score += 120
    if "/content/" in href and len(href_parts) >= 3:
        score += 90
    if re.search(r"\b(chapter|episode|ch\.?|ep\.?)\b", haystack):
        score += 70
    if re.search(r"[0-9a-f]{8}-[0-9a-f-]{12,}", href.lower()):
        score += 45
    if re.search(r"\b\d{1,5}\b", label):
        score += 25
    if any(term in haystack for term in ("comment", "login", "bookmark", "genre", "privacy", "discord")):
        score -= 60
    return score


def first_matching_link_url(driver: WebDriver, css_selector: str, current_url: str) -> Optional[str]:
    driver.switch_to.default_content()
    try:
        links = driver.find_elements(By.CSS_SELECTOR, css_selector)
    except WebDriverException:
        return None

    current_key = canonical_chapter_key(current_url)
    candidates: list[tuple[int, str, str]] = []
    for link in links:
        try:
            href = link.get_attribute("href")
            label = normalize_space(link.text)
            if not href or not link.is_displayed():
                continue
            if canonical_chapter_key(href) == current_key:
                continue
            score = chapter_link_score(current_url, href, label, link)
            if score > 0:
                candidates.append((score, href, label))
        except WebDriverException:
            continue

    if not candidates:
        return None

    candidates.sort(key=lambda candidate: candidate[0], reverse=True)
    score, href, label = candidates[0]
    logging.info("First chapter candidate: %s (%s, score=%s)", href, label or "no label", score)
    return href


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


def looks_like_series_landing_url(url: str) -> bool:
    parsed = urlsplit(url)
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) == 2 and parts[0].lower() == "content":
        return True
    if len(parts) == 2 and parts[0].lower() in {"manga", "manhwa", "series", "comic"}:
        return True
    return False


def maybe_open_resume_or_first_chapter(driver: WebDriver, current_url: str) -> str:
    series_landing = looks_like_series_landing_url(current_url)
    if series_landing:
        logging.info("Start URL looks like a series landing page; opening a chapter before posting.")
    elif page_has_comment_input(driver):
        return current_url

    continue_url = continue_reading_url_or_click(driver)
    if continue_url and continue_url != current_url:
        logging.info("No comment input on current page; opening continue-reading chapter: %s", continue_url)
        if load_url_with_retries(driver, continue_url, "continue-reading chapter"):
            return continue_url
        return current_url

    first_chapter_url = first_matching_link_url(driver, FIRST_CHAPTER_SELECTOR, current_url)
    if not first_chapter_url or first_chapter_url == current_url:
        if series_landing and page_has_comment_input(driver):
            logging.warning("No chapter link was found from the series page; falling back to current page comment input.")
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
    dismiss_page_interruptions(driver)
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
    dismiss_page_interruptions(driver)
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
        driver.execute_script("arguments[0].setAttribute('data-panelyra-submitted-editor', 'true');", element)
    except (JavascriptException, WebDriverException):
        logging.debug("Could not mark submitted editor; continuing.")


def clear_submitted_editor_marks(driver: WebDriver) -> None:
    try:
        cleared = driver.execute_script(
            """
            const nodes = Array.from(document.querySelectorAll('[data-panelyra-submitted-editor]'));
            for (const node of nodes) node.removeAttribute('data-panelyra-submitted-editor');
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


def current_comment_editor_y(driver: WebDriver) -> Optional[int]:
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
            return node.getAttribute("data-panelyra-submitted-editor") !== "true" && visible(node);
        }).map((node) => {
            const rect = node.getBoundingClientRect();
            const pageY = rect.top + window.scrollY;
            return {pageY, distance: Math.abs(pageY - viewportCenter)};
        });
        candidates.sort((a, b) => a.distance - b.distance || a.pageY - b.pageY);
        return candidates.length ? Math.round(candidates[0].pageY) : 0;
    """
    try:
        value = int(driver.execute_script(script, COMMENT_TEXTAREA_SELECTOR) or 0)
    except (JavascriptException, TypeError, ValueError):
        return None
    return value or None


def visible_chapter_image_urls(driver: WebDriver, img_selector: str, min_y: int, max_y: int) -> list[str]:
    driver.switch_to.default_content()
    script = """
        const selector = arguments[0];
        const minY = arguments[1];
        const maxY = arguments[2];
        const srcsetUrl = (value) => {
            if (!value) return "";
            const candidates = value.split(",").map((raw) => {
                const pieces = raw.trim().split(/\\s+/);
                const url = pieces[0] || "";
                const descriptor = pieces[1] || "";
                let score = 1;
                if (descriptor.endsWith("w")) score = Number.parseFloat(descriptor) || 1;
                if (descriptor.endsWith("x")) score = (Number.parseFloat(descriptor) || 1) * 1000;
                return {url, score};
            }).filter((item) => item.url);
            candidates.sort((a, b) => b.score - a.score);
            return candidates.length ? candidates[0].url : "";
        };
        const pickUrl = (img) => {
            const attrs = [
                img.currentSrc || "",
                img.getAttribute("data-src") || "",
                img.getAttribute("data-lazy-src") || "",
                img.getAttribute("data-original") || "",
                img.getAttribute("data-cfsrc") || "",
                img.getAttribute("data-large_image") || "",
                img.src || "",
                srcsetUrl(img.getAttribute("data-srcset") || ""),
                srcsetUrl(img.getAttribute("srcset") || "")
            ];
            return attrs.find((value) => value && !value.startsWith("data:")) || "";
        };
        const visible = (node) => {
            const rect = node.getBoundingClientRect();
            const style = window.getComputedStyle(node);
            return rect.width >= 180
                && rect.height >= 180
                && style.visibility !== "hidden"
                && style.display !== "none";
        };
        return Array.from(document.querySelectorAll(selector)).filter((img) => {
            if (!visible(img)) return false;
            const rect = img.getBoundingClientRect();
            const top = rect.top + window.scrollY;
            const bottom = top + rect.height;
            return bottom >= minY && top <= maxY;
        }).map(pickUrl).filter(Boolean);
    """
    try:
        raw_urls = driver.execute_script(script, img_selector, int(min_y), int(max_y))
    except JavascriptException as exc:
        logging.warning("Could not collect visible chapter image URLs: %s", exc)
        return []
    urls: list[str] = []
    for raw_url in raw_urls or []:
        url = str(raw_url).strip()
        if url and url not in urls:
            urls.append(url)
    return urls


def loaded_chapter_image_urls(driver: WebDriver, img_selector: str, min_y: int, max_y: int) -> list[str]:
    driver.switch_to.default_content()
    script = """
        const selector = arguments[0];
        const minY = arguments[1];
        const maxY = arguments[2];
        const srcsetUrl = (value) => {
            if (!value) return "";
            const candidates = value.split(",").map((raw) => {
                const pieces = raw.trim().split(/\\s+/);
                const url = pieces[0] || "";
                const descriptor = pieces[1] || "";
                let score = 1;
                if (descriptor.endsWith("w")) score = Number.parseFloat(descriptor) || 1;
                if (descriptor.endsWith("x")) score = (Number.parseFloat(descriptor) || 1) * 1000;
                return {url, score};
            }).filter((item) => item.url);
            candidates.sort((a, b) => b.score - a.score);
            return candidates.length ? candidates[0].url : "";
        };
        const pickUrl = (img) => {
            const attrs = [
                img.currentSrc || "",
                img.getAttribute("data-src") || "",
                img.getAttribute("data-lazy-src") || "",
                img.getAttribute("data-original") || "",
                img.getAttribute("data-cfsrc") || "",
                img.getAttribute("data-large_image") || "",
                img.src || "",
                srcsetUrl(img.getAttribute("data-srcset") || ""),
                srcsetUrl(img.getAttribute("srcset") || "")
            ];
            return attrs.find((value) => value && !value.startsWith("data:")) || "";
        };
        return Array.from(document.querySelectorAll(selector)).filter((img) => {
            const rect = img.getBoundingClientRect();
            const top = rect.top + window.scrollY;
            const height = rect.height || img.naturalHeight || img.height || 0;
            const bottom = top + height;
            return bottom >= minY && top <= maxY;
        }).map(pickUrl).filter(Boolean);
    """
    try:
        raw_urls = driver.execute_script(script, img_selector, int(min_y), int(max_y))
    except JavascriptException as exc:
        logging.warning("Could not collect loaded chapter image URLs: %s", exc)
        return []

    urls: list[str] = []
    for raw_url in raw_urls or []:
        url = str(raw_url).strip()
        if url and url not in urls:
            urls.append(url)
    return urls


def collect_chapter_image_urls_by_scrolling(
    driver: WebDriver,
    *,
    img_selector: str,
    min_y: int,
    max_y: int,
    min_pause_seconds: float,
    max_pause_seconds: float,
    skip_waits: bool = False,
) -> list[str]:
    driver.switch_to.default_content()
    min_y = max(0, int(min_y))
    max_y = max(min_y, int(max_y))
    if max_y <= min_y:
        return []

    logging.info("Scanning chapter images between y=%s and y=%s before generating comment context.", min_y, max_y)
    driver.execute_script("window.scrollTo({top: arguments[0], left: 0, behavior: 'auto'});", min_y)
    if not skip_waits:
        time.sleep(0.25)

    urls: list[str] = []
    stagnant_steps = 0
    for attempt in range(1, 260):
        for url in visible_chapter_image_urls(driver, img_selector, min_y, max_y):
            if url not in urls:
                urls.append(url)

        y_before = current_scroll_y(driver)
        if y_before + viewport_height(driver) >= max_y - 20:
            break

        step = max(420, int(viewport_height(driver) * 0.72))
        driver.execute_script("window.scrollBy({top: arguments[0], left: 0, behavior: 'auto'});", step)
        if not skip_waits:
            time.sleep(max(random.uniform(min_pause_seconds, max_pause_seconds), 0.08))
        elif max_pause_seconds > 0:
            time.sleep(random.uniform(min_pause_seconds, max_pause_seconds))
        y_after = current_scroll_y(driver)
        if y_after <= y_before + 5:
            stagnant_steps += 1
        else:
            stagnant_steps = 0
        if stagnant_steps >= 8:
            logging.info("Image scan stopped after stagnant scrolling near y=%s.", y_after)
            break
        if attempt % 20 == 0:
            logging.info("Image scan progress: %s unique image URL(s) collected by y=%s.", len(urls), y_after)

    target_y = max(0, max_y - int(viewport_height(driver) * 0.45))
    driver.execute_script("window.scrollTo({top: arguments[0], left: 0, behavior: 'auto'});", target_y)
    if not skip_waits:
        time.sleep(0.2)
    logging.info("Collected %s unique chapter image URL(s) for OCR.", len(urls))
    return urls


def scroll_to_comment_section(
    driver: WebDriver,
    *,
    min_y: int,
    min_step_px: int,
    max_step_px: int,
    min_pause_seconds: float,
    max_pause_seconds: float,
    label: str,
    skip_waits: bool = False,
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
                if (node.getAttribute("data-panelyra-submitted-editor") === "true") return false;
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
            if not skip_waits:
                time.sleep(0.15)
            logging.info("Found %s comment section after %s scan step(s).", label, attempt)
            return True
        if found == "trigger":
            logging.info("Opened comment trigger while searching for %s section.", label)
            if not skip_waits:
                time.sleep(0.45)
            continue

        step = min(random.randint(min_step_px, max_step_px), max(900, int(viewport_height(driver) * 1.5)))
        driver.execute_script("window.scrollBy({top: arguments[0], left: 0, behavior: 'auto'});", step)
        if not skip_waits:
            time.sleep(max(random.uniform(min_pause_seconds, max_pause_seconds), 0.06))
        elif max_pause_seconds > 0:
            time.sleep(random.uniform(min_pause_seconds, max_pause_seconds))
        if attempt % 10 == 0 and not skip_waits:
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
    skip_waits: bool = False,
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
        skip_waits=skip_waits,
    )


def scroll_to_first_infinite_comment_section(
    driver: WebDriver,
    *,
    min_step_px: int,
    max_step_px: int,
    min_pause_seconds: float,
    max_pause_seconds: float,
    skip_waits: bool = False,
) -> bool:
    return scroll_to_comment_section(
        driver,
        min_y=current_scroll_y(driver),
        min_step_px=min_step_px,
        max_step_px=max_step_px,
        min_pause_seconds=min_pause_seconds,
        max_pause_seconds=max_pause_seconds,
        label="first infinite-scroll",
        skip_waits=skip_waits,
    )


def scroll_to_next_chapter_comment_section(
    driver: WebDriver,
    *,
    last_marker_y: int,
    min_step_px: int,
    max_step_px: int,
    min_pause_seconds: float,
    max_pause_seconds: float,
    skip_waits: bool = False,
) -> bool:
    driver.switch_to.default_content()
    min_y = last_marker_y + 120
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
            if not skip_waits:
                time.sleep(0.25)
            return scroll_to_comment_section(
                driver,
                min_y=thanks_y_int + 120,
                min_step_px=min_step_px,
                max_step_px=max_step_px,
                min_pause_seconds=min_pause_seconds,
                max_pause_seconds=max_pause_seconds,
                label="next chapter comment",
                skip_waits=skip_waits,
            )

        step = min(random.randint(min_step_px, max_step_px), max(1000, int(viewport_height(driver) * 1.75)))
        driver.execute_script("window.scrollBy({top: arguments[0], left: 0, behavior: 'auto'});", step)
        if not skip_waits:
            time.sleep(max(random.uniform(min_pause_seconds, max_pause_seconds), 0.06))
        elif max_pause_seconds > 0:
            time.sleep(random.uniform(min_pause_seconds, max_pause_seconds))
        if attempt % 12 == 0 and not skip_waits:
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


NON_STORY_CONTEXT_PATTERNS = [
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
        r"\bcomments?\b|\balready\s+thanked\b|\bfirst\s+new\b|\blog\s*in\b|\bsign\s*in\b",
        r"\bclick\s+(here|the)\b|\btap\s+(here|the)\b|\btype\s+your\b",
        r"\bad(s)?\b|\badvert(s|isement|isements|ising)?\b|\bpop-?ups?\b|\bpromoted?\b|\bpromotions?\b|\bsponsor(ed|s|ship)?\b",
    )
]


def clean_story_context_text(text: str) -> str:
    lines = [normalize_space(line) for line in str(text).splitlines()]
    cleaned = [
        line
        for line in lines
        if line and not any(pattern.search(line) for pattern in NON_STORY_CONTEXT_PATTERNS)
    ]
    removed = sum(1 for line in lines if line) - len(cleaned)
    if removed:
        logging.info("Story context filter removed %s non-story line(s).", removed)
    return normalize_space("\n".join(cleaned))


def contains_banned_comment_word(text: str) -> bool:
    return any(normalize_keyword(word) in COMMENT_BANNED_WORDS for word in re.findall(r"[A-Za-z][A-Za-z'-]*", text))


def remove_comment_cliches(text: str) -> str:
    """Remove generic filler that does not add chapter-specific meaning."""
    rewritten = re.sub(
        r"\bthe\s+[A-Za-z][A-Za-z'-]*\s+(?:angle|part)\s+is\s+what\s+makes\s+it\s+stick\s+for\s+me[.!?]?",
        "",
        text,
        flags=re.IGNORECASE,
    )
    rewritten = re.sub(r"\bangle\b", "part", rewritten, flags=re.IGNORECASE)
    return normalize_space(rewritten)


def sanitize_comment_subject_text(text: str, fallback: str = "this chapter") -> str:
    clean = normalize_space(text)
    if not clean:
        return fallback
    if contains_banned_comment_word(clean):
        logging.info("Removed ad-related text from generated comment context.")
        return fallback
    return clean


def title_hint_for_comment(title: str, max_length: int = 80) -> str:
    clean = sanitize_comment_subject_text(title, fallback="this chapter")
    if not clean:
        clean = "this chapter"
    return clean if len(clean) <= max_length else clean[: max_length - 3] + "..."


def count_chapter_images(driver: WebDriver, container_selector: str, img_selector: str) -> int:
    driver.switch_to.default_content()
    try:
        container = driver.find_element(By.CSS_SELECTOR, container_selector)
        return len(container.find_elements(By.CSS_SELECTOR, img_selector))
    except WebDriverException as exc:
        logging.info("Could not count chapter images with selector %s: %s", img_selector, exc)
        return 0


def extract_dom_content_text(driver: WebDriver, container_selector: str) -> str:
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
        text = driver.execute_script(script, container_selector)
    except JavascriptException:
        logging.warning("Could not extract content using selector %s; falling back to body text.", container_selector)
        try:
            text = driver.find_element(By.TAG_NAME, "body").text
        except WebDriverException:
            text = ""

    return clean_story_context_text(str(text))


def normalize_content_mode(content_mode: str) -> str:
    mode = content_mode.strip().lower()
    return "manhwa" if mode in COMIC_CONTENT_MODE_ALIASES else mode


def extract_content_text(
    driver: WebDriver,
    *,
    content_mode: str,
    container_selector: str,
    img_selector: str,
    ocr_confidence: float,
    image_urls: Optional[list[str]] = None,
    min_y: Optional[int] = None,
    max_y: Optional[int] = None,
) -> str:
    mode = normalize_content_mode(content_mode)
    container_selector = container_selector.strip() or CONTENT_SELECTOR
    img_selector = img_selector.strip() or MANHWA_IMAGE_SELECTOR

    def image_context_text(reason: str = "") -> str:
        urls = list(image_urls or [])
        if not urls:
            try:
                container = driver.find_element(By.CSS_SELECTOR, container_selector)
                images = container.find_elements(By.CSS_SELECTOR, img_selector)
                urls = [
                    str(
                        image.get_attribute("currentSrc")
                        or image.get_attribute("data-src")
                        or image.get_attribute("data-lazy-src")
                        or image.get_attribute("data-original")
                        or image.get_attribute("src")
                        or ""
                    ).strip()
                    for image in images
                ]
            except WebDriverException:
                urls = []
        urls = [url for url in urls if url]
        title = ""
        try:
            title = normalize_space(str(driver.title))
        except WebDriverException:
            title = ""
        current_url = ""
        try:
            current_url = str(driver.current_url)
        except WebDriverException:
            current_url = ""
        page_match = re.search(r"[?&]page=(\d+)", current_url)
        page_label = f"page {page_match.group(1)}" if page_match else "current reader position"
        pieces = [
            title or "Image-based chapter",
            f"Reader context: {page_label}.",
            f"Visible chapter image count: {len(urls)}.",
        ]
        if reason:
            pieces.append(f"OCR fallback reason: {reason}.")
        pieces.append(
            "This is an image-heavy chapter, so generate a short, natural comment about pacing, panels, character choices, tension, or a theory without pretending to quote exact dialogue."
        )
        return normalize_space(" ".join(pieces))

    def novel_text() -> str:
        try:
            return normalize_space(extract_chapter_content(driver, container_selector, {"mode": "novel"}))
        except Exception as exc:
            logging.warning("BeautifulSoup chapter extraction failed; using DOM fallback: %s", exc)
            return extract_dom_content_text(driver, container_selector)

    def manhwa_text() -> str:
        return normalize_space(
            extract_chapter_content(
                driver,
                container_selector,
                {
                    "mode": "manhwa",
                    "img_selector": img_selector,
                    "confidence": ocr_confidence,
                    "image_urls": image_urls or [],
                    "min_y": min_y,
                    "max_y": max_y,
                },
            )
        )

    if mode == "novel":
        logging.info("Content reader mode: novel text.")
        return novel_text()

    if mode == "manhwa":
        logging.info("Content reader mode: manhwa OCR. Image selector: %s", img_selector)
        try:
            return manhwa_text()
        except Exception as exc:
            logging.warning("Forced OCR unavailable; using fast image-context fallback: %s", exc)
            return image_context_text(str(exc))

    if image_urls:
        logging.info("Content reader auto mode using fast image context from %s pre-collected chapter image URL(s).", len(image_urls))
        return image_context_text()

    image_count = count_chapter_images(driver, container_selector, img_selector)
    if image_count >= 2:
        logging.info("Content reader auto mode detected %s chapter image page(s); using fast image-context extraction.", image_count)
        return image_context_text()

    logging.info("Content reader auto mode using novel text extraction.")
    text = novel_text()
    if text or image_count == 0:
        return text

    logging.info("Novel extraction returned no text; using fast image-context fallback for %s image(s).", image_count)
    return image_context_text()


def extract_title(driver: WebDriver, text: str) -> str:
    try:
        heading = driver.find_elements(By.CSS_SELECTOR, "h1, .chapter-title, .entry-title, .post-title")
        for element in heading:
            value = normalize_space(element.text)
            subject = sanitize_comment_subject_text(value, fallback="")
            if subject:
                return subject
    except WebDriverException:
        pass

    title = sanitize_comment_subject_text(normalize_space(driver.title), fallback="")
    if title:
        return title

    return sanitize_comment_subject_text(text[:80], fallback="this chapter") if text else "this chapter"


def normalize_keyword(value: str) -> str:
    word = re.sub(r"[^a-z0-9']+", "", value.lower()).strip("'")
    if word.endswith("'s"):
        word = word[:-2]
    return word


def looks_like_ocr_garbage(word: str) -> bool:
    if len(word) > 18:
        return True
    if re.search(r"(.)\1{3,}", word):
        return True
    if len(word) >= 10:
        vowels = sum(1 for char in word if char in "aeiou")
        if vowels / max(1, len(word)) < 0.22:
            return True
    if re.search(r"[bcdfghjklmnpqrstvwxyz]{5,}", word):
        return True
    if any(noise in word for noise in ("trash", "scanlation", "discord", "patreon", "paypal", "readire", "advert", "sponsor", "promo")):
        return True
    return False


def keyword_is_usable_candidate(value: str) -> bool:
    word = normalize_keyword(value)
    if len(word) <= 3:
        return False
    if word in STOPWORDS or word in NOISY_KEYWORDS:
        return False
    if looks_like_ocr_garbage(word):
        return False
    return True


def keyword_is_comment_anchor(value: str) -> bool:
    return keyword_is_usable_candidate(value)


def extract_keywords(text: str, limit: int = 8) -> list[str]:
    raw_words = re.findall(r"[A-Za-z][A-Za-z'-]{3,}", text)
    words = [
        normalize_keyword(word)
        for word in raw_words
        if keyword_is_comment_anchor(word)
    ]
    counts = Counter(word for word in words if word)
    return [word for word, _ in counts.most_common(limit)]


def build_chapter_context(
    driver: WebDriver,
    *,
    content_mode: str,
    container_selector: str,
    img_selector: str,
    ocr_confidence: float,
    image_urls: Optional[list[str]] = None,
    min_y: Optional[int] = None,
    max_y: Optional[int] = None,
) -> ChapterContext:
    text = extract_content_text(
        driver,
        content_mode=content_mode,
        container_selector=container_selector,
        img_selector=img_selector,
        ocr_confidence=ocr_confidence,
        image_urls=image_urls,
        min_y=min_y,
        max_y=max_y,
    )
    title = extract_title(driver, text)
    keywords = extract_keywords(text)
    excerpt = trim_comment(text, max_length=360) if text else ""

    logging.info("Chapter title/context: %s", title)
    logging.info("Extracted chapter context length: %s character(s).", len(text))
    logging.info("Extracted keywords: %s", ", ".join(keywords[:6]) if keywords else "none")
    return ChapterContext(title=title, keywords=keywords, excerpt=excerpt, text_length=len(text))


def tokenize_for_match(value: str) -> set[str]:
    return {
        word
        for word in re.findall(r"[A-Za-z][A-Za-z'-]{2,}", value.lower())
        if keyword_is_usable_candidate(word)
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


def historical_keyword_counts(history: list[dict[str, object]]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for record in history[-COMMENT_HISTORY_COMPARISON_LIMIT:]:
        keyword = normalize_keyword(str(record.get("keyword", "")))
        if keyword and keyword_is_comment_anchor(keyword):
            counts[keyword] += 1
    return counts


def historical_comment_term_counts(history: list[dict[str, object]], used_comments: set[str]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for comment in [*previous_comment_texts(history), *used_comments]:
        for token in comment_token_set(comment):
            if token not in NOISY_KEYWORDS:
                counts[token] += 1
    return counts


def choose_least_used_keyword(candidates: list[str], counts: Counter[str], reuse_limit: int) -> Optional[str]:
    fresh = [
        normalize_keyword(candidate)
        for candidate in candidates
        if keyword_is_comment_anchor(candidate) and counts.get(normalize_keyword(candidate), 0) < reuse_limit
    ]
    fresh = list(dict.fromkeys(fresh))
    if not fresh:
        return None
    lowest_count = min(counts.get(candidate, 0) for candidate in fresh)
    least_used = [candidate for candidate in fresh if counts.get(candidate, 0) == lowest_count]
    return random.choice(least_used)


def neutral_anchor_terms(anchor: str) -> set[str]:
    return {
        normalize_keyword(token)
        for token in anchor.split()
        if keyword_is_usable_candidate(token)
    }


def neutral_anchor_use_count(anchor: str, counts: Counter[str]) -> int:
    terms = neutral_anchor_terms(anchor)
    if not terms:
        return counts.get(normalize_keyword(anchor), 0)
    return max(counts.get(term, 0) for term in terms)


def choose_neutral_anchor(counts: Counter[str], exclude: set[str] | None = None) -> str:
    exclude = exclude or set()
    ranked = [
        anchor
        for anchor in NEUTRAL_COMMENT_ANCHORS
        if normalize_keyword(anchor) not in exclude
    ]
    if not ranked:
        return "this moment"
    lowest_count = min(neutral_anchor_use_count(anchor, counts) for anchor in ranked)
    least_used = [anchor for anchor in ranked if neutral_anchor_use_count(anchor, counts) == lowest_count]
    return random.choice(least_used)


def keyword_for_history(keyword: str) -> str:
    normalized = normalize_keyword(keyword)
    neutral_anchor_keys = {normalize_keyword(anchor) for anchor in NEUTRAL_COMMENT_ANCHORS}
    if normalized in neutral_anchor_keys:
        return ""
    return normalized if keyword_is_comment_anchor(normalized) else ""


def context_story_terms(context: ChapterContext) -> list[str]:
    """Return terms that came from the current chapter, never UI fallback anchors."""
    title_terms = [
        normalize_keyword(term)
        for term in re.findall(r"[A-Za-z][A-Za-z'-]{3,}", context.title)
        if normalize_keyword(term) not in {"chapter", "episode", "volume", "vol"}
    ]
    candidates = [*context.keywords, *title_terms]
    return list(
        dict.fromkeys(
            normalize_keyword(term)
            for term in candidates
            if keyword_is_comment_anchor(term)
        )
    )


def pick_keyword(
    context: ChapterContext,
    history: list[dict[str, object]],
    used_comments: set[str],
    fallback: str = "this moment",
) -> str:
    keyword_counts = historical_keyword_counts(history)
    story_terms = context_story_terms(context)
    keyword = choose_least_used_keyword(story_terms, keyword_counts, PRIMARY_KEYWORD_REUSE_LIMIT)
    if keyword:
        return keyword

    # Reuse a current-chapter term before inventing a generic theme. This keeps
    # every generated comment anchored to what was actually extracted.
    if story_terms:
        lowest = min(keyword_counts.get(term, 0) for term in story_terms)
        return random.choice([term for term in story_terms if keyword_counts.get(term, 0) == lowest])

    term_counts = historical_comment_term_counts(history, used_comments)
    neutral = choose_neutral_anchor(term_counts)
    logging.info("No fresh story keyword available; using neutral anchor: %s", neutral)
    return neutral or fallback


def pick_distinct_keyword(
    context: ChapterContext,
    first_keyword: str,
    history: list[dict[str, object]],
    used_comments: set[str],
    fallback: str = "the setup",
) -> str:
    keyword_counts = historical_keyword_counts(history)
    first_normalized = normalize_keyword(first_keyword)
    candidates = [
        keyword
        for keyword in context_story_terms(context)
        if normalize_keyword(keyword) != first_normalized
    ]
    keyword = choose_least_used_keyword(candidates, keyword_counts, SECONDARY_KEYWORD_REUSE_LIMIT)
    if keyword:
        return keyword

    if candidates:
        lowest = min(keyword_counts.get(term, 0) for term in candidates)
        return random.choice([term for term in candidates if keyword_counts.get(term, 0) == lowest])

    term_counts = historical_comment_term_counts(history, used_comments)
    neutral = choose_neutral_anchor(term_counts, exclude={first_normalized})
    return neutral if normalize_keyword(neutral) != first_normalized else fallback


def contextual_emoji(style: str, context: ChapterContext) -> str:
    """Use a modest emoji only when the selected response mode calls for it."""
    choices = EMOJI_STYLE_MAP.get(style, [])
    if not choices or not context_story_terms(context):
        return ""
    return random.choice(choices)


def generated_comment_count(history: list[dict[str, object]]) -> int:
    return sum(1 for record in history if str(record.get("comment", "")).strip())


def should_add_scheduled_reflection(history: list[dict[str, object]], reflection_every_n_comments: int) -> bool:
    if reflection_every_n_comments <= 0:
        return False
    next_comment_number = generated_comment_count(history) + 1
    return next_comment_number % reflection_every_n_comments == 0


def maybe_add_scheduled_reflection(
    comment: str,
    *,
    keyword: str,
    context: ChapterContext,
    history: list[dict[str, object]],
    reflection_every_n_comments: int,
) -> tuple[str, bool]:
    if not should_add_scheduled_reflection(history, reflection_every_n_comments):
        return comment, False

    context_terms = tokenize_for_match(" ".join([context.title, context.excerpt, *context.keywords]))
    matched_topics = [topic for topic in REFLECTION_BY_CONTEXT if topic in context_terms]
    if not matched_topics:
        logging.info("Reflection cadence reached, but the chapter has no matching philosophical context; skipping it.")
        return comment, False

    reflection = trim_comment(random.choice(REFLECTION_BY_CONTEXT[random.choice(matched_topics)])(keyword), max_length=190)
    joined = f"{comment.rstrip('.!?')}. {reflection}"
    return trim_comment(joined, max_length=300), True


def trim_comment(text: str, max_length: int = 240) -> str:
    text = normalize_space(text)
    if len(text) <= max_length:
        return text
    trimmed = text[: max_length - 3].rsplit(" ", 1)[0]
    return f"{trimmed}..."


def remove_comment_dashes(text: str) -> str:
    return normalize_space(text.translate(DASH_TRANSLATION))


def simplify_comment_apostrophes(text: str) -> str:
    replacements = {
        "can't": "cant",
        "Can't": "Cant",
        "won't": "wont",
        "Won't": "Wont",
        "don't": "dont",
        "Don't": "Dont",
        "doesn't": "doesnt",
        "Doesn't": "Doesnt",
        "isn't": "isnt",
        "Isn't": "Isnt",
        "wasn't": "wasnt",
        "Wasn't": "Wasnt",
        "aren't": "arent",
        "Aren't": "Arent",
        "couldn't": "couldnt",
        "Couldn't": "Couldnt",
        "wouldn't": "wouldnt",
        "Wouldn't": "Wouldnt",
        "shouldn't": "shouldnt",
        "Shouldn't": "Shouldnt",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return text


def maybe_add_casual_grammar_imperfection(text: str) -> str:
    if random.random() >= GRAMMAR_IMPERFECTION_RATE:
        return text

    replacements = [
        (r"\bdo not\b", "dont"),
        (r"\bDo not\b", "Dont"),
        (r"\bdoes not\b", "dont"),
        (r"\bDoes not\b", "Dont"),
        (r"\bI am\b", "I"),
        (r"\bit is\b", "it"),
        (r"\bIt is\b", "It"),
        (r"\bThis chapter\b", "This chapter it"),
    ]
    random.shuffle(replacements)
    for pattern, replacement in replacements:
        updated, count = re.subn(pattern, replacement, text, count=1)
        if count:
            return normalize_space(updated)
    return text


def finalize_comment_text(text: str, *, allow_grammar_imperfection: bool = True, max_length: int = 300) -> str:
    finalized = remove_comment_cliches(remove_comment_dashes(text))
    finalized = simplify_comment_apostrophes(finalized)
    if contains_banned_comment_word(finalized):
        logging.info("Generated comment contained ad-related page noise; using a neutral story-safe fallback.")
        finalized = "This chapter kept the pacing and tension clear enough that I want to see how the next part pays it off."
    if allow_grammar_imperfection:
        finalized = maybe_add_casual_grammar_imperfection(finalized)
    finalized = remove_comment_cliches(remove_comment_dashes(finalized))
    return trim_comment(finalized, max_length=max_length)


def comment_signature(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


def comment_token_set(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z][a-z'-]{2,}", text.lower())
        if token not in STOPWORDS and len(token) > 3
    }


def previous_comment_texts(history: list[dict[str, object]]) -> list[str]:
    comments = [
        str(record.get("comment", "")).strip()
        for record in history[-COMMENT_HISTORY_COMPARISON_LIMIT:]
        if str(record.get("comment", "")).strip()
    ]
    return comments


def overused_history_terms(
    history: list[dict[str, object]],
    limit: int = SECONDARY_KEYWORD_REUSE_LIMIT,
) -> set[str]:
    return {
        keyword
        for keyword, count in historical_keyword_counts(history).items()
        if count >= limit and keyword_is_comment_anchor(keyword)
    }


def candidate_uses_overused_terms(candidate: str, history: list[dict[str, object]]) -> bool:
    return bool(comment_token_set(candidate) & overused_history_terms(history))


def comments_are_too_similar(candidate: str, previous: str) -> bool:
    candidate_signature = comment_signature(candidate)
    previous_signature = comment_signature(previous)
    if not candidate_signature or not previous_signature:
        return False
    if candidate_signature == previous_signature:
        return True

    ratio = SequenceMatcher(None, candidate_signature, previous_signature).ratio()
    if ratio >= COMMENT_SIMILARITY_LIMIT:
        return True

    candidate_tokens = comment_token_set(candidate)
    previous_tokens = comment_token_set(previous)
    if len(candidate_tokens) < 4 or len(previous_tokens) < 4:
        return False

    overlap = len(candidate_tokens & previous_tokens) / min(len(candidate_tokens), len(previous_tokens))
    return overlap >= COMMENT_TOKEN_OVERLAP_LIMIT


def repeated_template_phrase(candidate: str, history: list[dict[str, object]]) -> bool:
    """Reject the same template skeleton even when its story keyword changed."""
    candidate_tokens = comment_token_set(candidate)
    for previous in previous_comment_texts(history)[-RECENT_PHRASE_WINDOW:]:
        shared = candidate_tokens & comment_token_set(previous)
        if len(shared) >= 7:
            return True
    return False


def comment_is_unique(candidate: str, used_comments: set[str], history: list[dict[str, object]]) -> bool:
    if contains_banned_comment_word(candidate):
        return False
    if candidate_uses_overused_terms(candidate, history):
        return False
    if repeated_template_phrase(candidate, history):
        return False
    comparison_pool = [*used_comments, *previous_comment_texts(history)]
    return not any(comments_are_too_similar(candidate, previous) for previous in comparison_pool)


def add_contextual_variation(
    comment: str,
    *,
    context: ChapterContext,
    keyword: str,
    second_keyword: str,
    research_context: Optional[ResearchContext],
) -> str:
    title_hint = title_hint_for_comment(context.title, max_length=70)
    base = comment.rstrip(".!?")
    variations = [
        lambda: comment,
        lambda: f"{base} because the {keyword} and {second_keyword} pieces feel connected.",
        lambda: f"{base}; {second_keyword} makes that part feel less random.",
        lambda: f"{base}. The {second_keyword} part is what makes it stick for me.",
        lambda: f"{title_hint} makes that {keyword} detail feel more deliberate than usual.",
        lambda: f"What stood out to me was {keyword}, but {second_keyword} might be the real hint.",
        lambda: f"{base}. I would not be surprised if {second_keyword} is the part that matters later.",
    ]
    if research_context and research_context.matched_terms:
        matched = random.choice(research_context.matched_terms)
        variations.append(lambda: f"{base}. The extra context around {matched} makes it feel intentional.")
    return trim_comment(random.choice(variations)(), max_length=260)


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


def reader_url_has_page(url: str) -> bool:
    parsed = urlsplit(url)
    return any(key.lower() == "page" for key, _ in parse_qsl(parsed.query, keep_blank_values=True))


def canonical_read_marker_key(url: str, marker_y: Optional[int] = None) -> str:
    """Keep numbered reader pages distinct while ignoring transient UI state."""
    parsed = urlsplit(url)
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in {"scroll", "comment"}
    ]
    key = urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), urlencode(query), ""))
    if marker_y is not None and not reader_url_has_page(url):
        key = f"{key}#y={max(0, int(marker_y))}"
    return key


def series_url_from_chapter_url(url: str) -> Optional[str]:
    parsed = urlsplit(url)
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 3 and parts[0] == "content":
        return urlunsplit((parsed.scheme, parsed.netloc, f"/content/{parts[1]}", "", ""))
    if len(parts) == 2 and parts[0] == "content":
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))
    chapter_segment = re.compile(
        r"(?:^|[-_])(chapter|chap|ch|episode|ep)(?:[-_:]?\d|$)|^(chapter|chap|ch|episode|ep)[-_:]?",
        re.IGNORECASE,
    )
    for index in range(len(parts) - 1, 0, -1):
        if chapter_segment.search(parts[index]):
            parent_path = "/" + "/".join(parts[:index])
            return urlunsplit((parsed.scheme, parsed.netloc, parent_path, "", ""))
    return None


def load_read_marker_keys(marker_file: Path) -> set[str]:
    keys: set[str] = set()
    for record in read_jsonl_records(marker_file):
        url = str(record.get("url", "")).strip()
        if url:
            marker_y_value = record.get("marker_y")
            try:
                marker_y = int(marker_y_value) if marker_y_value is not None else None
            except (TypeError, ValueError):
                marker_y = None
            keys.add(canonical_read_marker_key(url, marker_y))
            continue

        stored_key = str(record.get("chapter_key", "")).strip()
        if stored_key:
            keys.add(stored_key)
    return keys


def append_read_marker(marker_file: Path, *, url: str, title: str, thanked: bool, marker_y: Optional[int] = None) -> str:
    chapter_key = canonical_read_marker_key(url, marker_y)
    append_jsonl_record(
        marker_file,
        {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "chapter_key": chapter_key,
            "url": url,
            "title": title,
            "thanked": thanked,
            "marker_y": marker_y,
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
        "content_fingerprint": hashlib.sha256(
            f"{context.title}\n{context.excerpt}".encode("utf-8", errors="replace")
        ).hexdigest()[:20],
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


def save_failure_artifacts(driver: WebDriver, history_file: Path, chapter_index: int) -> None:
    artifact_dir = history_file.parent / "failure_artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    prefix = artifact_dir / f"chapter-{chapter_index + 1}-{int(time.time())}"
    try:
        driver.save_screenshot(str(prefix.with_suffix(".png")))
        prefix.with_suffix(".html").write_text(driver.page_source, encoding="utf-8", errors="replace")
        prefix.with_suffix(".json").write_text(
            json.dumps({"url": driver.current_url, "title": driver.title}, indent=2),
            encoding="utf-8",
        )
        logging.info("Saved failure evidence: %s", prefix)
    except (OSError, WebDriverException) as exc:
        logging.warning("Could not save failure evidence: %s", exc)


def build_contextual_comment(
    context: ChapterContext,
    chapter_index: int,
    used_comments: set[str],
    history: list[dict[str, object]],
    research_context: Optional[ResearchContext],
    reflection_every_n_comments: int = REFLECTION_EVERY_N_COMMENTS,
) -> GeneratedComment:
    keyword = pick_keyword(context, history, used_comments)
    second_keyword = pick_distinct_keyword(context, keyword, history, used_comments)
    title_hint = title_hint_for_comment(context.title, max_length=80)
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
            lambda: f"The strategy behind {keyword} is what makes this chapter interesting.",
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
        "symbolism": [
            lambda: f"{keyword} feels symbolic here, like the chapter is saying more than it says out loud.",
            lambda: f"I like how {keyword} works as more than just a detail; it carries the theme a bit.",
        ],
        "stakes": [
            lambda: f"{keyword} made the stakes feel higher without the chapter needing to overstate it.",
            lambda: f"The stakes around {keyword} feel like they are about to get serious fast.",
        ],
        "motivation": [
            lambda: f"{keyword} makes me question what everyone actually wants in this scene.",
            lambda: f"The motivation behind {keyword} is what I want the next chapter to explain.",
        ],
        "betrayal_watch": [
            lambda: f"I am on betrayal watch now because {keyword} feels way too convenient.",
            lambda: f"{keyword} has that uncomfortable setup where someone might switch sides later.",
        ],
        "comedy_timing": [
            lambda: f"The timing around {keyword} gave the chapter a nice break without killing the tension.",
            lambda: f"I like when a chapter lets {keyword} lighten the mood for just a second.",
        ],
        "visual_cue": [
            lambda: f"The visual cue around {keyword} says a lot before anyone has to explain it.",
            lambda: f"{keyword} stood out visually in a way that makes the scene feel more deliberate.",
        ],
        "theme_check": [
            lambda: f"This chapter keeps circling the same theme, and {keyword} is the clearest sign of it.",
            lambda: f"{keyword} makes the theme feel more direct without turning the chapter into a lecture.",
        ],
        "parallel": [
            lambda: f"{keyword} feels like it is paralleling an earlier moment, and that makes it more interesting.",
            lambda: f"I like the possible parallel between {keyword} and {second_keyword}.",
        ],
        "hidden_agenda": [
            lambda: f"Someone has a hidden agenda here, and {keyword} feels like the clue.",
            lambda: f"{keyword} makes me think there is another plan happening under the surface.",
        ],
        "battle_tactics": [
            lambda: f"The tactical side of {keyword} is what makes this more than a simple clash.",
            lambda: f"If {keyword} was planned, then the fight is already more strategic than it looks.",
        ],
        "training_arc": [
            lambda: f"{keyword} feels like the kind of lesson that only pays off after a rough training arc.",
            lambda: f"This made me wonder if {keyword} is setting up growth more than an immediate win.",
        ],
        "crew_dynamics": [
            lambda: f"The crew dynamic around {keyword} is getting more interesting than I expected.",
            lambda: f"{keyword} shows a lot about how this group actually works together.",
        ],
        "moral_conflict": [
            lambda: f"{keyword} makes the moral side of this chapter feel less simple.",
            lambda: f"I like when the chapter makes {keyword} feel right and wrong at the same time.",
        ],
        "identity": [
            lambda: f"{keyword} feels tied to identity here, like someone is being pushed to define themselves.",
            lambda: f"The way {keyword} reflects who they are becoming is what stood out to me.",
        ],
        "reputation": [
            lambda: f"{keyword} feels like it could change how people see this character.",
            lambda: f"What {keyword} does to the character's reputation might matter more than the immediate conflict.",
        ],
        "turning_point": [
            lambda: f"{keyword} feels like the turning point the chapter was building toward.",
            lambda: f"This chapter may end up being remembered for how {keyword} changes the direction.",
        ],
        "aftershock": [
            lambda: f"I feel like the aftershock from {keyword} is going to matter more than the moment itself.",
            lambda: f"{keyword} feels like one of those choices that keeps causing problems later.",
        ],
        "calm_before_storm": [
            lambda: f"{keyword} has calm-before-the-storm energy all over it.",
            lambda: f"The chapter feels quiet around {keyword}, which somehow makes it more suspicious.",
        ],
        "final_panel": [
            lambda: f"If the final panel is pointing at {keyword}, then the next chapter has a lot to answer.",
            lambda: f"The ending focus on {keyword} is exactly the kind of hook that keeps me reading.",
        ],
        "micro_reaction": [
            lambda: f"That tiny reaction around {keyword} said more than a full explanation would have.",
            lambda: f"I like how a small reaction to {keyword} changed the whole feeling of the scene.",
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

    styles_by_recent_use = [
        style
        for style, _ in sorted(
            ((style, style_counts(history).get(style, 0)) for style in COMMENT_STYLES),
            key=lambda item: item[1],
        )
        if style in templates
    ]

    for attempt in range(160):
        style = preferred_style if attempt < 24 else random.choice(styles_by_recent_use[: max(6, len(styles_by_recent_use) // 2)])
        comment = trim_comment(random.choice(templates[style])())
        comment = add_contextual_variation(
            comment,
            context=context,
            keyword=keyword,
            second_keyword=second_keyword,
            research_context=research_context,
        )
        comment, reflection_used = maybe_add_scheduled_reflection(
            comment,
            keyword=keyword,
            context=context,
            history=history,
            reflection_every_n_comments=reflection_every_n_comments,
        )
        emoji_used = False
        if not reflection_used and random.random() < 0.48:
            emoji = contextual_emoji(style, context)
            if emoji:
                comment = f"{comment.rstrip('.!?')}{emoji}"
                emoji_used = True
        comment = finalize_comment_text(comment)
        source_terms = set(context_story_terms(context))
        if source_terms and not (comment_token_set(comment) & source_terms):
            logging.info("Rejected generated comment without a current-chapter term.")
            continue
        if comment_is_unique(comment, used_comments, history):
            used_comments.add(comment)
            return GeneratedComment(
                text=comment,
                style=style,
                keyword=keyword_for_history(keyword),
                emoji_used=emoji_used,
                research_source=research_source,
                reflection_used=reflection_used,
            )

    fallback = finalize_comment_text(
        f"This chapter keeps making {keyword} feel important, especially next to {second_keyword}.",
        max_length=260,
    )
    used_comments.add(fallback)
    return GeneratedComment(
        text=fallback,
        style=preferred_style,
        keyword=keyword_for_history(keyword),
        emoji_used=False,
        research_source=research_source,
        reflection_used=False,
    )


def generate_comment(chapter_index: int, used_comments: set[str]) -> str:
    raw_comment = generate_unique_comment(
        iteration=chapter_index,
        used_comments=used_comments,
        opening_phrases=OPENING_REMARKS,
        subject_phrases=SUBJECT_PHRASES,
        ending_phrases=ENDING_PHRASES,
        joiners=JOINERS,
        punctuation=END_PUNCTUATION,
    )
    final_comment = finalize_comment_text(raw_comment)
    used_comments.add(final_comment)
    return final_comment


def sanitize_for_chromedriver(text: str) -> str:
    sanitized = "".join(char for char in text if ord(char) <= 0xFFFF)
    if sanitized != text:
        logging.info("Removed unsupported non-BMP character(s) before typing.")
    return sanitized


def comment_verification_snippet(text: str, max_length: int = 72) -> str:
    words = re.findall(r"\S+", normalize_space(text))
    snippet = ""
    for word in words:
        candidate = f"{snippet} {word}".strip()
        if len(candidate) > max_length:
            break
        snippet = candidate
    return snippet or normalize_space(text)[:max_length]


def normalize_comment_match_text(text: str) -> str:
    value = text.translate(
        str.maketrans(
            {
                "\u2018": "'",
                "\u2019": "'",
                "\u201a": "'",
                "\u201b": "'",
                "\u2032": "'",
                "\u201c": '"',
                "\u201d": '"',
                "\u201e": '"',
                "\u201f": '"',
                "\u2033": '"',
                "\u2026": "...",
            }
        )
    )
    value = remove_comment_dashes(value)
    return normalize_space(value).lower()


def editor_current_text(driver: WebDriver, element: WebElement) -> str:
    script = """
        const node = arguments[0];
        if (!node) return "";
        return (node.value || node.innerText || node.textContent || "").replace(/\\s+/g, " ").trim();
    """
    try:
        return normalize_space(str(driver.execute_script(script, element) or ""))
    except WebDriverException:
        return ""


def insert_editor_text_with_js(driver: WebDriver, element: WebElement, text: str) -> str:
    script = """
        const node = arguments[0];
        const text = arguments[1];
        const fire = (target, type, data = "") => {
            try {
                target.dispatchEvent(new InputEvent(type, {
                    bubbles: true,
                    cancelable: true,
                    inputType: type === "input" ? "insertText" : undefined,
                    data
                }));
            } catch (_) {
                target.dispatchEvent(new Event(type, {bubbles: true, cancelable: true}));
            }
        };
        const setNativeValue = (target, value) => {
            const proto = Object.getPrototypeOf(target);
            const descriptor = Object.getOwnPropertyDescriptor(proto, "value");
            if (descriptor && descriptor.set) descriptor.set.call(target, value);
            else target.value = value;
        };

        node.scrollIntoView({block: "center", inline: "nearest"});
        node.focus();

        if ("value" in node && /^(textarea|input)$/i.test(node.tagName)) {
            setNativeValue(node, "");
            fire(node, "input");
            setNativeValue(node, text);
            fire(node, "input", text);
            fire(node, "change");
            return (node.value || "").replace(/\\s+/g, " ").trim();
        }

        const selection = window.getSelection();
        const range = document.createRange();
        range.selectNodeContents(node);
        selection.removeAllRanges();
        selection.addRange(range);
        document.execCommand("delete", false, null);
        document.execCommand("insertText", false, text);
        fire(node, "input", text);
        fire(node, "change");
        return (node.innerText || node.textContent || "").replace(/\\s+/g, " ").trim();
    """
    inserted_text = driver.execute_script(script, element, text)
    return normalize_space(str(inserted_text or ""))


def editor_contains_comment_text(driver: WebDriver, element: WebElement, text: str) -> bool:
    snippet = normalize_comment_match_text(comment_verification_snippet(text, max_length=56))
    if not snippet:
        return False
    current_text = normalize_comment_match_text(editor_current_text(driver, element))
    return snippet in current_text


def clear_and_type(
    element: WebElement,
    text: str,
    min_delay: float = TYPE_MIN_DELAY,
    max_delay: float = TYPE_MAX_DELAY,
    visible_type_chars: int = 0,
) -> None:
    text = sanitize_for_chromedriver(text)
    driver = element.parent
    logging.info("Filling generated QA input (%s characters).", len(text))

    visible_prefix = text[: max(0, min(int(visible_type_chars), len(text)))]
    if visible_prefix:
        logging.info(
            "Typing comment visibly (%s characters at %.3f-%.3fs per key).",
            len(visible_prefix),
            min_delay,
            max_delay,
        )
        clear_existing_text(element)
        slow_type(element, visible_prefix, min_delay, max_delay)
        if visible_prefix == text and editor_contains_comment_text(driver, element, text):
            logging.info("Full comment was typed visibly into the editor.")
            return

    try:
        inserted_text = insert_editor_text_with_js(driver, element, text)
        if editor_contains_comment_text(driver, element, text):
            logging.info("Comment editor filled with JavaScript input events (%s visible characters).", len(inserted_text))
            return
        logging.info("JavaScript editor fill did not stick; falling back to keyboard typing.")
    except WebDriverException as exc:
        logging.info("JavaScript editor fill failed; falling back to keyboard typing: %s", exc)

    clear_existing_text(element)
    slow_type(element, text, min_delay, max_delay)
    if not editor_contains_comment_text(driver, element, text):
        current_preview = editor_current_text(driver, element)[:120]
        raise TimeoutException(
            "Generated comment was not present in the editor after typing. "
            f"Editor preview: {current_preview!r}"
        )


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


def wait_for_confirmation(driver: WebDriver, wait_seconds: float = POST_SUBMIT_WAIT_SECONDS) -> None:
    if wait_seconds <= 0:
        logging.info("Confirmation selector wait skipped.")
        return

    if not CONFIRMATION_SELECTOR.strip():
        logging.info("No confirmation selector configured; waiting %.2f seconds after submit.", wait_seconds)
        time.sleep(wait_seconds)
        return

    try:
        WebDriverWait(driver, wait_seconds).until(
            EC.visibility_of_element_located((By.CSS_SELECTOR, CONFIRMATION_SELECTOR))
        )
        logging.info("Submission confirmation detected.")
    except TimeoutException:
        logging.warning("Confirmation selector was not detected within %.2f seconds.", wait_seconds)


def comment_text_visible_outside_editor(driver: WebDriver, editor: Optional[WebElement], comment_text: str) -> bool:
    snippet = normalize_comment_match_text(comment_verification_snippet(comment_text))
    if not snippet:
        return False

    script = """
        const editor = arguments[0];
        const snippet = arguments[1];
        const normalize = (value) => (value || "")
            .replace(/[\\u2018\\u2019\\u201a\\u201b\\u2032]/g, "'")
            .replace(/[\\u201c\\u201d\\u201e\\u201f\\u2033]/g, '"')
            .replace(/\\u2026/g, "...")
            .replace(/[\\u002d\\u2010\\u2011\\u2012\\u2013\\u2014\\u2212]/g, " ")
            .replace(/\\s+/g, " ")
            .trim()
            .toLowerCase();
        const visible = (node) => {
            const rect = node.getBoundingClientRect();
            const style = window.getComputedStyle(node);
            return rect.width > 0
                && rect.height > 0
                && style.visibility !== "hidden"
                && style.display !== "none";
        };
        const insideEditable = (node) => {
            if (!node) return false;
            if (editor && (node === editor || editor.contains(node) || node.contains(editor))) return true;
            return Boolean(node.closest("textarea, input, [contenteditable='true'], [role='textbox']"));
        };
        const selector = [
            "[class*='comment' i]",
            "[data-testid*='comment' i]",
            "article",
            "li",
            "p",
            "blockquote",
            "span",
            "div"
        ].join(",");
        for (const node of Array.from(document.querySelectorAll(selector))) {
            if (insideEditable(node) || !visible(node)) continue;
            const text = normalize(node.innerText || node.textContent || "");
            if (text && text.includes(snippet)) return true;
        }
        return false;
    """
    try:
        return bool(driver.execute_script(script, editor, snippet))
    except WebDriverException:
        try:
            return bool(driver.execute_script(script, None, snippet))
        except WebDriverException:
            return False


def visible_submission_error(driver: WebDriver) -> str:
    script = """
        const visible = (node) => {
            const rect = node.getBoundingClientRect();
            const style = window.getComputedStyle(node);
            return rect.width > 0
                && rect.height > 0
                && style.visibility !== "hidden"
                && style.display !== "none";
        };
        const selector = [
            "[role='alert']",
            "[aria-live]",
            ".error",
            ".notice",
            ".toast",
            "[class*='error' i]",
            "[class*='toast' i]",
            "[class*='notice' i]",
            "[class*='warning' i]"
        ].join(",");
        for (const node of Array.from(document.querySelectorAll(selector))) {
            if (!visible(node)) continue;
            const text = (node.innerText || node.textContent || "").replace(/\\s+/g, " ").trim();
            if (/error|failed|try again|rate|limit|wait|cooldown|too fast|spam|login|required/i.test(text)) {
                return text.slice(0, 240);
            }
        }
        return "";
    """
    try:
        return str(driver.execute_script(script) or "").strip()
    except WebDriverException:
        return ""


def visible_submission_success(driver: WebDriver) -> str:
    script = """
        const visible = (node) => {
            const rect = node.getBoundingClientRect();
            const style = window.getComputedStyle(node);
            return rect.width > 0
                && rect.height > 0
                && style.visibility !== "hidden"
                && style.display !== "none";
        };
        const selector = [
            "[role='status']",
            "[role='alert']",
            "[aria-live]",
            ".success",
            ".toast",
            ".notice",
            "[class*='success' i]",
            "[class*='toast' i]",
            "[class*='notice' i]"
        ].join(",");
        for (const node of Array.from(document.querySelectorAll(selector))) {
            if (!visible(node)) continue;
            const text = (node.innerText || node.textContent || "").replace(/\\s+/g, " ").trim();
            if (/comment.{0,30}(posted|submitted|published|success)|successfully.{0,30}(posted|submitted|published)|thanks for (commenting|your comment)/i.test(text)) {
                return text.slice(0, 240);
            }
        }
        return "";
    """
    try:
        return str(driver.execute_script(script) or "").strip()
    except WebDriverException:
        return ""


def verify_comment_submitted(
    driver: WebDriver,
    editor: WebElement,
    comment_text: str,
    wait_seconds: float = POST_SUBMIT_WAIT_SECONDS,
) -> str:
    snippet = comment_verification_snippet(comment_text)
    if not snippet:
        raise TimeoutException("Generated comment was blank; refusing to mark the chapter submitted.")

    logging.info("Verifying posted comment appears outside the editor.")
    try:
        WebDriverWait(driver, max(0.5, wait_seconds), poll_frequency=0.2).until(
            lambda active_driver: comment_text_visible_outside_editor(active_driver, editor, comment_text)
        )
    except TimeoutException as exc:
        editor_preview = editor_current_text(driver, editor)[:160]
        submission_error = visible_submission_error(driver)
        if submission_error:
            raise TimeoutException(
                "Comment submission returned a visible error after clicking Send. "
                f"Error: {submission_error!r}. Editor preview: {editor_preview!r}"
            ) from exc

        submission_success = visible_submission_success(driver)
        if submission_success:
            logging.info("Submission success signal detected: %s", submission_success)
            return "verified"

        if not normalize_comment_match_text(editor_preview):
            logging.warning(
                "SUBMISSION_UNVERIFIED: editor cleared after Send, but no posted comment or success signal appeared within %.2f seconds.",
                wait_seconds,
            )
            return "unverified"
        raise TimeoutException(
            "Comment submit was not verified on the page after clicking Send. "
            f"Expected snippet: {snippet!r}. Editor preview: {editor_preview!r}. "
            "No success or error signal was visible."
        ) from exc
    logging.info("Posted comment verified on the page.")
    return "verified"


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
                const type = (button.getAttribute("type") || "").toLowerCase();
                const hasActionLabel = /\\b(send|post|submit|comment|reply)\\b/.test(text);
                const isSubmitType = type === "submit";
                if (!hasActionLabel && !isSubmitType) continue;
                const rect = button.getBoundingClientRect();
                let score = 100;
                if (isSubmitType) score += 80;
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
            return node.getAttribute("data-panelyra-submitted-editor") !== "true" && visible(node);
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


def click_submit_button(driver: WebDriver, button: WebElement, pre_click_wait_seconds: float = 0.05) -> None:
    try:
        driver.execute_script("arguments[0].scrollIntoView({block: 'center', inline: 'nearest'});", button)
        if pre_click_wait_seconds > 0:
            time.sleep(pre_click_wait_seconds)
        button.click()
        logging.info("Submit button clicked.")
    except WebDriverException as exc:
        logging.warning("Normal submit click failed, trying JavaScript click: %s", exc)
        driver.execute_script("arguments[0].click();", button)
        logging.info("Submit button clicked with JavaScript fallback.")


def click_say_thanks(
    driver: WebDriver,
    near_y: Optional[int] = None,
    post_click_wait_seconds: float = 0.35,
) -> Optional[int]:
    dismiss_page_interruptions(driver)
    driver.switch_to.default_content()
    script = """
        const selector = arguments[0];
        const nearY = arguments[1];
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
            if (nearY) return Math.abs(ay - nearY) - Math.abs(by - nearY);
            return ay - by;
        });
        const markerY = Math.round(candidates[0].getBoundingClientRect().top + window.scrollY);
        candidates[0].scrollIntoView({block: "center", inline: "nearest"});
        candidates[0].click();
        return markerY;
    """
    try:
        marker_y = driver.execute_script(script, THANK_BUTTON_SELECTOR, int(near_y or 0))
    except (JavascriptException, WebDriverException) as exc:
        logging.warning("Could not click Say thanks marker: %s", exc)
        return None
    try:
        marker_y_int = int(marker_y)
    except (TypeError, ValueError):
        marker_y_int = 0
    if marker_y_int:
        logging.info("Clicked Say thanks read marker at y=%s.", marker_y_int)
        if post_click_wait_seconds > 0:
            time.sleep(post_click_wait_seconds)
        return marker_y_int
    else:
        logging.info("Say thanks read marker was not found or was not clickable.")
    return None


def submit_comment(
    driver: WebDriver,
    comment_text: str,
    *,
    type_min_delay: float = TYPE_MIN_DELAY,
    type_max_delay: float = TYPE_MAX_DELAY,
    visible_type_chars: int = 0,
    post_submit_wait_seconds: float = POST_SUBMIT_WAIT_SECONDS,
    confirmation_wait_seconds: float = 0.5,
    pre_submit_click_wait_seconds: float = 0.05,
) -> tuple[int, str]:
    dismiss_page_interruptions(driver)
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

    last_error: Optional[WebDriverException] = None
    for attempt in range(1, 3):
        submit_clicked = False
        try:
            driver.switch_to.default_content()
            comment_area = find_visible_unsubmitted_comment_editor(driver)
            if comment_area is None:
                comment_area = find_in_page_or_iframe(driver, comment_locators, WAIT_SECONDS)
            logging.info("Comment textarea found in %s.", comment_area.context)
            submission_y = element_page_y(driver, comment_area.element)
            logging.info("Active comment editor page position: y=%s.", submission_y)
            clear_and_type(
                comment_area.element,
                comment_text,
                type_min_delay,
                type_max_delay,
                visible_type_chars,
            )

            nearby_submit = find_nearby_submit_button(driver, comment_area.element)
            if nearby_submit is not None:
                logging.info("Submit button found near the active editor; clicking immediately after typing.")
                click_submit_button(driver, nearby_submit, pre_submit_click_wait_seconds)
            else:
                logging.info("No nearby submit button found; using short fallback locator search.")
                submit_button = find_in_page_or_iframe(driver, submit_locators, 1)
                logging.info("Submit button found in %s.", submit_button.context)
                click_submit_button(driver, submit_button.element, pre_submit_click_wait_seconds)
            submit_clicked = True

            verification_status = verify_comment_submitted(
                driver,
                comment_area.element,
                comment_text,
                post_submit_wait_seconds,
            )
            mark_submitted_editor(driver, comment_area.element)
            wait_for_confirmation(driver, confirmation_wait_seconds)
            return submission_y, verification_status
        except (TimeoutException, WebDriverException, JavascriptException) as exc:
            last_error = exc
            if submit_clicked or attempt >= 2:
                if not submit_clicked:
                    log_comment_diagnostics(driver)
                raise
            logging.warning(
                "COMMENT_CONTROL_RETRY: comment controls changed before Send; reacquiring editor and button (attempt %s/2): %s",
                attempt + 1,
                exc,
            )
            driver.switch_to.default_content()
            time.sleep(0.25)

    if last_error is not None:
        raise last_error
    raise TimeoutException("Comment submission failed before locating an editor.")


def active_reader_selectors() -> ReaderSelectors:
    return ReaderSelectors(
        next_chapter=NEXT_LINK_SELECTOR,
        next_page=NEXT_PAGE_SELECTOR,
        page_counter=PAGE_COUNTER_SELECTOR,
        comment_editor=COMMENT_TEXTAREA_SELECTOR,
        chapter_list=CHAPTER_LIST_SELECTOR,
    )


def normalized_reader_layout(chapter_routing: str) -> str:
    return {
        "next-link": "next-chapter",
        "infinite-scroll": "continuous",
    }.get(chapter_routing, chapter_routing)


def detect_reader_layout(driver: WebDriver, chapter_routing: str) -> str:
    snapshot = reader_snapshot(driver, active_reader_selectors())
    layout = classify_reader_layout(snapshot, normalized_reader_layout(chapter_routing))
    logging.info(
        "Reader layout: %s (page %s/%s, visible editors %s, next-page %s, next-chapter %s).",
        layout,
        snapshot.page_current or "?",
        snapshot.page_total or "?",
        snapshot.visible_comment_editors,
        snapshot.has_next_page,
        snapshot.has_next_chapter,
    )
    return layout


def prepare_paginated_chapter(
    driver: WebDriver,
    *,
    img_selector: str,
    content_mode: str,
    min_step_px: int,
    max_step_px: int,
    min_pause_seconds: float,
    max_pause_seconds: float,
    reuse_loaded_images: bool,
) -> tuple[Optional[int], list[str]]:
    """Read every page in a chapter and stop on the page containing its comment form."""
    selectors = active_reader_selectors()
    image_urls: list[str] = []
    seen_urls: set[str] = set()
    seen_states: set[tuple[str, int, str]] = set()

    for page_attempt in range(1, 201):
        slow_reading_scroll(
            driver,
            min_step_px=min_step_px,
            max_step_px=max_step_px,
            min_pause_seconds=min_pause_seconds,
            max_pause_seconds=max_pause_seconds,
        )
        snapshot = reader_snapshot(driver, selectors)
        state_key = (snapshot.url, snapshot.page_current, snapshot.image_signature)
        if state_key in seen_states:
            logging.warning("Paginated reader repeated page state; stopping page traversal.")
            break
        seen_states.add(state_key)

        if content_mode in {"auto", "manhwa"}:
            current_page_urls = loaded_chapter_image_urls(
                driver,
                img_selector=img_selector,
                min_y=0,
                max_y=document_height(driver),
            )
            if not current_page_urls and not reuse_loaded_images:
                current_page_urls = collect_chapter_image_urls_by_scrolling(
                    driver,
                    img_selector=img_selector,
                    min_y=0,
                    max_y=document_height(driver),
                    min_pause_seconds=min_pause_seconds,
                    max_pause_seconds=max_pause_seconds,
                    skip_waits=False,
                )
            for url in current_page_urls:
                if url not in seen_urls:
                    seen_urls.add(url)
                    image_urls.append(url)

        editor_y = current_comment_editor_y(driver)
        logging.info(
            "Paginated reader progress: page %s/%s, %s unique chapter image(s), comment form %s.",
            snapshot.page_current or page_attempt,
            snapshot.page_total or "?",
            len(image_urls),
            "found" if editor_y is not None else "not found",
        )
        at_last_page = bool(
            snapshot.page_total
            and snapshot.page_current >= snapshot.page_total
        )
        if editor_y is not None and (at_last_page or not snapshot.has_next_page):
            return editor_y, image_urls
        if at_last_page:
            break
        if advance_reader_page(driver, selectors) is None:
            break

    return current_comment_editor_y(driver), image_urls


def click_reader_next_control(driver: WebDriver) -> Optional[str]:
    driver.switch_to.default_content()
    after = advance_reader_page(driver, active_reader_selectors(), timeout_seconds=15)
    if after is None:
        logging.info("Reader next-page control was not available or did not change reader state.")
        return None
    logging.info(
        "Reader next-page control advanced to page %s/%s: %s",
        after.page_current or "?",
        after.page_total or "?",
        after.url,
    )
    return after.url


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
    dismiss_page_interruptions(driver)
    driver.switch_to.default_content()
    next_url = activate_next_chapter(driver, active_reader_selectors(), timeout_seconds=15)
    if next_url:
        logging.info("Next chapter control resolved: %s", next_url)
        return next_url
    logging.info("No distinct next chapter control was found.")
    list_url = next_chapter_from_visible_list(
        driver,
        active_reader_selectors(),
        driver.current_url,
        getattr(driver, "title", ""),
    )
    if list_url:
        logging.info("Next chapter resolved from the visible chapter list: %s", list_url)
    return list_url


def resolve_next_reader_position(driver: WebDriver) -> Optional[str]:
    """Prefer the reader pager, then fall back to normal Next-link discovery."""
    next_url = click_reader_next_control(driver)
    if next_url:
        return next_url
    return resolve_next_url(driver)


def resolve_next_chapter_position(driver: WebDriver, reader_layout: str) -> Optional[str]:
    """Resolve a chapter transition without mistaking an internal page for a chapter."""
    next_url = resolve_next_url(driver)
    if next_url or reader_layout not in {"paginated", "mixed"}:
        return next_url

    selectors = active_reader_selectors()
    for _ in range(200):
        snapshot = reader_snapshot(driver, selectors)
        if snapshot.page_total and snapshot.page_current >= snapshot.page_total:
            break
        if advance_reader_page(driver, selectors) is None:
            break
    return resolve_next_url(driver)


def chapter_cooldown(min_seconds: float = COOLDOWN_MIN_SECONDS, max_seconds: float = COOLDOWN_MAX_SECONDS) -> None:
    min_seconds = max(0.0, float(min_seconds))
    max_seconds = max(min_seconds, float(max_seconds))
    if max_seconds <= 0:
        logging.info("Chapter cooldown skipped.")
        return
    wait_seconds = random.uniform(min_seconds, max_seconds)
    logging.info("Chapter cooldown: sleeping for %.2f seconds.", wait_seconds)
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


def recover_after_chapter_failure(
    driver: WebDriver,
    chapter_routing: str,
    scroll_min_px: int,
    scroll_max_px: int,
    scroll_min_delay: float,
    scroll_max_delay: float,
    reuse_loaded_images: bool,
) -> Optional[str]:
    """Move past a failed chapter without selecting the same editor again."""
    failed_key = canonical_read_marker_key(driver.current_url)
    try:
        if chapter_routing in {"paginated", "mixed"}:
            next_url = resolve_next_chapter_position(driver, normalized_reader_layout(chapter_routing))
        elif reader_url_has_page(driver.current_url):
            next_url = resolve_next_reader_position(driver)
        elif chapter_routing == "infinite-scroll":
            failed_editor_y = current_comment_editor_y(driver) or current_scroll_y(driver)
            if scroll_to_next_infinite_comment_section(
                driver,
                last_submission_y=failed_editor_y,
                min_step_px=scroll_min_px,
                max_step_px=scroll_max_px,
                min_pause_seconds=scroll_min_delay,
                max_pause_seconds=scroll_max_delay,
                skip_waits=reuse_loaded_images,
            ):
                next_url = driver.current_url
            else:
                next_url = resolve_next_reader_position(driver)
        else:
            next_url = resolve_next_url(driver)
    except (TimeoutException, WebDriverException, JavascriptException) as exc:
        logging.exception("Failure recovery could not advance to another chapter: %s", exc)
        return None

    if not next_url:
        return None
    next_key = canonical_read_marker_key(next_url)
    if next_key == failed_key and not find_visible_unsubmitted_comment_editor(driver):
        return None
    return next_url


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
    first_comment_override: str = "",
    already_on_start_url: bool = False,
    run_reporter: Optional[RunStore] = None,
) -> int:
    content_mode = normalize_content_mode(content_mode)
    used_comments: set[str] = set()
    current_url: Optional[str] = start_url
    completed = 0
    next_context_min_y = 0
    history = load_comment_history(history_file)
    used_comments.update(previous_comment_texts(history))
    read_markers = load_read_marker_keys(read_marker_file)
    logging.info("Loaded %s historical comment record(s) from %s.", len(history), history_file)
    logging.info("Loaded %s read marker(s) from %s.", len(read_markers), read_marker_file)
    logging.info("Chapter routing mode: %s.", chapter_routing)
    logging.info("Content reader mode: %s.", content_mode)
    logging.info("Content container selector: %s.", content_container_selector)
    if content_mode in {"auto", "manhwa"}:
        logging.info("OCR image selector: %s.", ocr_image_selector)
        logging.info("OCR confidence threshold: %.2f.", ocr_confidence)
    logging.info("Comment generator active styles: %s.", len(COMMENT_STYLES))
    logging.info("Famous-person/philosophical reflection cadence: every %s comment(s).", reflection_every_n_comments)
    logging.info("Thanks/read marker mode: %s.", "enabled" if use_thanks_marker else "disabled")
    logging.info(
        "Pacing: visible-start chars %s, reuse loaded images %s, fallback type %.3f-%.3fs, cooldown %.2f-%.2fs, submit verification %.2fs.",
        visible_type_chars,
        "enabled" if reuse_loaded_images else "disabled",
        type_min_delay,
        type_max_delay,
        cooldown_min_seconds,
        cooldown_max_seconds,
        post_submit_wait_seconds,
    )

    processed_slots = 0
    while processed_slots < max_chapters:
        chapter_index = processed_slots
        effective_layout = normalized_reader_layout(chapter_routing)
        if not current_url:
            logging.info("Stopping because there is no next URL.")
            break

        logging.info("Processing chapter %s/%s: %s", chapter_index + 1, max_chapters, current_url)
        if run_reporter:
            run_reporter.checkpoint(
                "scanning",
                chapter_index=chapter_index + 1,
                url=current_url,
                message=f"Scanning chapter {chapter_index + 1}/{max_chapters}",
            )
        chapter_context: Optional[ChapterContext] = None
        generated_comment: Optional[GeneratedComment] = None
        try:
            driver.switch_to.default_content()
            if chapter_index > 0 and chapter_routing == "infinite-scroll":
                logging.info("Continuing on the already-open infinite-scroll reader page.")
            elif chapter_index == 0 and already_on_start_url:
                next_context_min_y = 0
                current_url = driver.current_url
                logging.info("Using the already-open page after manual setup: %s", current_url)
                wait_for_page_ready(driver)
                if not wait_for_cloudflare_to_clear(driver):
                    logging.error("Manual setup page is still blocked by a challenge; stopping run.")
                    break
            elif not load_url_with_retries(driver, current_url, f"chapter {chapter_index + 1}"):
                break
            else:
                next_context_min_y = 0
                if not reuse_loaded_images:
                    time.sleep(random.uniform(0.25, 0.6))

            if chapter_index == 0:
                current_url = maybe_open_resume_or_first_chapter(driver, current_url)
                clear_submitted_editor_marks(driver)

            effective_layout = detect_reader_layout(driver, chapter_routing)
            current_chapter_key = (
                canonical_chapter_url(driver.current_url)
                if effective_layout in {"paginated", "mixed"}
                else canonical_read_marker_key(driver.current_url)
            )
            if effective_layout != "continuous" or reader_url_has_page(driver.current_url):
                already_submitted = any(
                    record.get("status") == "submitted"
                    and (
                        canonical_chapter_url(str(record.get("url", "")))
                        if effective_layout in {"paginated", "mixed"}
                        else canonical_read_marker_key(str(record.get("url", "")))
                    )
                    == current_chapter_key
                    for record in history
                )
                if already_submitted:
                    logging.warning("IDEMPOTENCY_SKIP: a verified comment already exists for %s.", current_chapter_key)
                    previous_url = driver.current_url
                    if effective_layout == "continuous" and reader_url_has_page(driver.current_url):
                        next_url = resolve_next_reader_position(driver)
                    else:
                        next_url = resolve_next_chapter_position(driver, effective_layout)
                    if not next_url:
                        next_url = resolve_series_continue_url(driver, driver.current_url)
                    same_target = (
                        canonical_read_marker_key(next_url) == canonical_read_marker_key(previous_url)
                        if effective_layout == "continuous" and reader_url_has_page(previous_url)
                        else canonical_chapter_url(next_url) == canonical_chapter_url(previous_url)
                    ) if next_url else False
                    if not next_url or same_target:
                        logging.error("RUN_STOP_REASON: verified chapter was skipped but no distinct next chapter was found.")
                        break
                    current_url = next_url
                    next_context_min_y = 0
                    continue
            marker_skip_supported = (
                effective_layout != "continuous" or reader_url_has_page(driver.current_url)
            )
            normalized_read_markers = (
                {canonical_chapter_url(marker) for marker in read_markers}
                if effective_layout in {"paginated", "mixed"}
                else read_markers
            )
            if (
                use_thanks_marker
                and marker_skip_supported
                and current_chapter_key in normalized_read_markers
            ):
                logging.info("Current chapter is already marked as read/thanked locally: %s", current_chapter_key)
                previous_url = driver.current_url
                if effective_layout == "continuous" and reader_url_has_page(driver.current_url):
                    next_url = resolve_next_reader_position(driver)
                else:
                    next_url = resolve_next_chapter_position(driver, effective_layout)
                if not next_url:
                    next_url = resolve_series_continue_url(driver, driver.current_url)
                same_target = (
                    canonical_read_marker_key(next_url) == canonical_read_marker_key(previous_url)
                    if effective_layout == "continuous" and reader_url_has_page(previous_url)
                    else canonical_chapter_url(next_url) == canonical_chapter_url(previous_url)
                ) if next_url else False
                if not next_url or same_target:
                    logging.info("No next chapter URL found after read-marker skip; stopping.")
                    break
                logging.info("Read-marker skip advanced to the next reader page: %s", next_url)
                current_url = next_url
                next_context_min_y = 0
                continue

            comment_editor_y: Optional[int] = None
            paginated_image_urls: list[str] = []
            if effective_layout in {"paginated", "mixed"}:
                comment_editor_y, paginated_image_urls = prepare_paginated_chapter(
                    driver,
                    img_selector=ocr_image_selector,
                    content_mode=content_mode,
                    min_step_px=scroll_min_px,
                    max_step_px=scroll_max_px,
                    min_pause_seconds=scroll_min_delay,
                    max_pause_seconds=scroll_max_delay,
                    reuse_loaded_images=reuse_loaded_images,
                )
            elif chapter_routing == "infinite-scroll" and chapter_index == 0:
                if not scroll_to_first_infinite_comment_section(
                    driver,
                    min_step_px=scroll_min_px,
                    max_step_px=scroll_max_px,
                    min_pause_seconds=scroll_min_delay,
                    max_pause_seconds=scroll_max_delay,
                    skip_waits=reuse_loaded_images,
                ):
                    logging.info("No first comment section found on the infinite-scroll page; stopping.")
                    log_comment_diagnostics(driver)
                    break
                comment_editor_y = current_comment_editor_y(driver)
            elif chapter_routing == "infinite-scroll" and chapter_index > 0:
                logging.info("Already positioned at the next infinite-scroll comment section.")
                comment_editor_y = current_comment_editor_y(driver)
            else:
                slow_reading_scroll(
                    driver,
                    min_step_px=scroll_min_px,
                    max_step_px=scroll_max_px,
                    min_pause_seconds=scroll_min_delay,
                    max_pause_seconds=scroll_max_delay,
                )
                comment_editor_y = current_comment_editor_y(driver)

            context_max_y = comment_editor_y or document_height(driver)
            chapter_image_urls: list[str] = list(paginated_image_urls)
            if content_mode in {"auto", "manhwa"}:
                if reuse_loaded_images and not chapter_image_urls:
                    chapter_image_urls = loaded_chapter_image_urls(
                        driver,
                        img_selector=ocr_image_selector,
                        min_y=next_context_min_y,
                        max_y=context_max_y,
                    )
                    if chapter_image_urls:
                        logging.info(
                            "Super fast mode: reusing %s already-loaded chapter image URL(s); skipping the extra pre-comment scan.",
                            len(chapter_image_urls),
                        )
                    else:
                        logging.info("Super fast mode: no loaded chapter images were available; doing one fallback scan.")
                if not chapter_image_urls:
                    chapter_image_urls = collect_chapter_image_urls_by_scrolling(
                        driver,
                        img_selector=ocr_image_selector,
                        min_y=next_context_min_y,
                        max_y=context_max_y,
                        min_pause_seconds=scroll_min_delay,
                        max_pause_seconds=scroll_max_delay,
                        skip_waits=reuse_loaded_images,
                    )
            try:
                chapter_context = build_chapter_context(
                    driver,
                    content_mode=content_mode,
                    container_selector=content_container_selector,
                    img_selector=ocr_image_selector,
                    ocr_confidence=ocr_confidence,
                    image_urls=chapter_image_urls,
                    min_y=next_context_min_y,
                    max_y=context_max_y,
                )
            except Exception as exc:
                logging.exception("Chapter context extraction failed; falling back to generic comment generation: %s", exc)
                fallback_title = extract_title(driver, "")
                chapter_context = ChapterContext(title=fallback_title, keywords=[], excerpt="", text_length=0)
            logging.info(
                "Chapter scan completed: %s/%s (context characters: %s).",
                chapter_index + 1,
                max_chapters,
                chapter_context.text_length,
            )
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
                    reflection_every_n_comments,
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
            if chapter_index == 0 and first_comment_override.strip():
                override_text = " ".join(first_comment_override.split()).strip()
                generated_comment = GeneratedComment(
                    text=override_text,
                    style="reviewed_override",
                    keyword=generated_comment.keyword,
                    emoji_used=any(char in override_text for char in "🙂😅👀🔥"),
                    research_source=generated_comment.research_source,
                )
            logging.info("Generated contextual QA comment [%s]: %s", generated_comment.style, generated_comment.text)
            if run_reporter:
                run_reporter.checkpoint(
                    "generated",
                    chapter_index=chapter_index + 1,
                    url=current_url,
                    comment=generated_comment.text,
                    message="Comment generated and ready to type",
                )
            if generated_comment.reflection_used:
                logging.info("Scheduled famous-person/philosophical reflection added to this comment.")
            submission_y, submission_verification = submit_comment(
                driver,
                generated_comment.text,
                type_min_delay=type_min_delay,
                type_max_delay=type_max_delay,
                visible_type_chars=visible_type_chars,
                post_submit_wait_seconds=post_submit_wait_seconds,
                confirmation_wait_seconds=0.0 if reuse_loaded_images else min(0.5, post_submit_wait_seconds),
                pre_submit_click_wait_seconds=0.0 if reuse_loaded_images else 0.05,
            )
            thanked = False
            thanks_marker_y = submission_y
            if use_thanks_marker:
                clicked_thanks_y = click_say_thanks(
                    driver,
                    near_y=submission_y,
                    post_click_wait_seconds=0.0 if reuse_loaded_images else 0.35,
                )
                thanked = clicked_thanks_y is not None
                if clicked_thanks_y is not None:
                    thanks_marker_y = clicked_thanks_y
                chapter_key = append_read_marker(
                    read_marker_file,
                    url=(
                        canonical_chapter_url(driver.current_url)
                        if effective_layout in {"paginated", "mixed"}
                        else driver.current_url
                    ),
                    title=chapter_context.title,
                    thanked=thanked,
                    marker_y=thanks_marker_y if effective_layout == "continuous" else None,
                )
                read_markers.add(chapter_key)
                logging.info("Recorded local read marker for chapter: %s", chapter_key)
            history_status = "submitted" if submission_verification == "verified" else "unverified"
            history_url = (
                canonical_chapter_url(driver.current_url)
                if effective_layout in {"paginated", "mixed"}
                else current_url
            )
            append_comment_history(
                history_file,
                chapter_index=chapter_index,
                url=history_url,
                context=chapter_context,
                generated=generated_comment,
                status=history_status,
            )
            history.append(
                {
                    "style": generated_comment.style,
                    "keyword": generated_comment.keyword,
                    "comment": generated_comment.text,
                    "emoji_used": generated_comment.emoji_used,
                    "reflection_used": generated_comment.reflection_used,
                    "research_source": generated_comment.research_source,
                    "status": history_status,
                }
            )
            completed += 1
            processed_slots += 1
            if run_reporter:
                run_reporter.update(
                    stage="verified" if submission_verification == "verified" else "unverified",
                    progress_current=completed,
                    progress_total=max_chapters,
                    message=f"Processed chapter {chapter_index + 1}",
                )
            if submission_verification == "verified":
                logging.info("Chapter submission completed: %s/%s.", completed, max_chapters)
            else:
                logging.warning("Chapter submission unverified: %s/%s.", completed, max_chapters)
            next_context_min_y = max(submission_y, thanks_marker_y) + 1

            if chapter_index == max_chapters - 1:
                logging.info("Reached configured max chapter count.")
                break

            if effective_layout in {"paginated", "mixed"}:
                next_url = resolve_next_chapter_position(driver, effective_layout)
                if not next_url:
                    next_url = resolve_series_continue_url(driver, driver.current_url)
                if not next_url or canonical_chapter_url(next_url) == canonical_chapter_url(driver.current_url):
                    logging.error("RUN_STOP_REASON: paginated chapter finished but no distinct next chapter was found.")
                    break
                chapter_cooldown(cooldown_min_seconds, cooldown_max_seconds)
                current_url = next_url
                next_context_min_y = 0
                continue

            if use_thanks_marker and chapter_routing == "infinite-scroll":
                logging.info("Read marker mode completed this chapter; scrolling down to the next chapter block.")
                if not scroll_to_next_chapter_comment_section(
                    driver,
                    last_marker_y=thanks_marker_y,
                    min_step_px=scroll_min_px,
                    max_step_px=scroll_max_px,
                    min_pause_seconds=scroll_min_delay,
                    max_pause_seconds=scroll_max_delay,
                    skip_waits=reuse_loaded_images,
                ):
                    logging.info("No lower chapter block found; trying the reader next-page control.")
                    previous_reader_key = canonical_read_marker_key(driver.current_url)
                    next_url = resolve_next_reader_position(driver)
                    if not next_url or canonical_read_marker_key(next_url) == previous_reader_key:
                        logging.error("RUN_STOP_REASON: no next reader page was found after the completed chapter.")
                        log_comment_diagnostics(driver)
                        break
                    logging.info("Advanced to the next reader page after submission: %s", next_url)
                    current_url = next_url
                    next_context_min_y = 0
                    chapter_cooldown(cooldown_min_seconds, cooldown_max_seconds)
                    continue
                current_url = driver.current_url
                chapter_cooldown(cooldown_min_seconds, cooldown_max_seconds)
                continue

            if chapter_routing == "infinite-scroll":
                if not scroll_to_next_infinite_comment_section(
                    driver,
                    last_submission_y=submission_y,
                    min_step_px=scroll_min_px,
                    max_step_px=scroll_max_px,
                    min_pause_seconds=scroll_min_delay,
                    max_pause_seconds=scroll_max_delay,
                    skip_waits=reuse_loaded_images,
                ):
                    logging.info("No lower comment section found; trying the reader next-page control.")
                    previous_reader_key = canonical_read_marker_key(driver.current_url)
                    next_url = resolve_next_reader_position(driver)
                    if not next_url or canonical_read_marker_key(next_url) == previous_reader_key:
                        logging.error("RUN_STOP_REASON: no next reader page was found after the completed chapter.")
                        log_comment_diagnostics(driver)
                        break
                    logging.info("Advanced to the next reader page after submission: %s", next_url)
                    current_url = next_url
                    next_context_min_y = 0
                    chapter_cooldown(cooldown_min_seconds, cooldown_max_seconds)
                    continue
                current_url = driver.current_url
                chapter_cooldown(cooldown_min_seconds, cooldown_max_seconds)
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
                    skip_waits=reuse_loaded_images,
                ):
                    current_url = driver.current_url
                    chapter_routing = "infinite-scroll"
                    chapter_cooldown(cooldown_min_seconds, cooldown_max_seconds)
                    continue

            if not next_url or next_url == current_url:
                logging.error("RUN_STOP_REASON: no new next chapter URL or comment section was available.")
                break

            chapter_cooldown(cooldown_min_seconds, cooldown_max_seconds)
            current_url = next_url
            next_context_min_y = 0
        except (TimeoutException, WebDriverException, JavascriptException) as exc:
            logging.exception("Chapter %s failed gracefully; attempting to continue: %s", chapter_index + 1, exc)
            save_failure_artifacts(driver, history_file, chapter_index)
            if run_reporter:
                run_reporter.update(stage="recovering", message=f"Chapter {chapter_index + 1} failed: {exc}")
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
            processed_slots += 1
            failed_key = canonical_read_marker_key(driver.current_url)
            next_url = recover_after_chapter_failure(
                driver,
                chapter_routing,
                scroll_min_px,
                scroll_max_px,
                scroll_min_delay,
                scroll_max_delay,
                reuse_loaded_images,
            )
            recovered_same_page_editor = bool(next_url) and find_visible_unsubmitted_comment_editor(driver) is not None
            if not next_url or (
                canonical_read_marker_key(next_url) == failed_key and not recovered_same_page_editor
            ):
                logging.error(
                    "RUN_STOP_REASON: chapter %s failed and recovery could not reach a distinct next chapter.",
                    chapter_index + 1,
                )
                break
            logging.warning("Chapter %s was skipped after failure; continuing at %s", chapter_index + 1, next_url)
            current_url = next_url
            next_context_min_y = 0
            continue

    return completed


def main() -> int:
    global NEXT_LINK_SELECTOR
    global NEXT_PAGE_SELECTOR
    global PAGE_COUNTER_SELECTOR
    global CHAPTER_LIST_SELECTOR
    global COMMENT_TEXTAREA_SELECTOR
    global SUBMIT_BUTTON_SELECTOR
    global THANK_BUTTON_SELECTOR
    global CONFIRMATION_SELECTOR

    args = parse_args()
    args.start_url = normalize_web_url(args.start_url)
    configure_logging(args.log_file)
    args.content_mode = normalize_content_mode(args.content_mode)

    NEXT_LINK_SELECTOR = args.next_link_selector.strip() or NEXT_LINK_SELECTOR
    NEXT_PAGE_SELECTOR = args.next_page_selector.strip() or NEXT_PAGE_SELECTOR
    PAGE_COUNTER_SELECTOR = args.page_counter_selector.strip() or PAGE_COUNTER_SELECTOR
    CHAPTER_LIST_SELECTOR = args.chapter_list_selector.strip() or CHAPTER_LIST_SELECTOR
    COMMENT_TEXTAREA_SELECTOR = args.comment_editor_selector.strip() or COMMENT_TEXTAREA_SELECTOR
    SUBMIT_BUTTON_SELECTOR = args.submit_button_selector.strip() or SUBMIT_BUTTON_SELECTOR
    THANK_BUTTON_SELECTOR = args.thanks_button_selector.strip() or THANK_BUTTON_SELECTOR
    CONFIRMATION_SELECTOR = args.confirmation_selector.strip()

    if args.max_chapters < 1:
        logging.error("--max-chapters must be at least 1.")
        return 2
    if args.scroll_min_px < 1 or args.scroll_max_px < args.scroll_min_px:
        logging.error("Scroll pixel settings are invalid. Ensure 1 <= min <= max.")
        return 2
    if args.scroll_min_delay < 0 or args.scroll_max_delay < args.scroll_min_delay:
        logging.error("Scroll delay settings are invalid. Ensure 0 <= min <= max.")
        return 2
    if args.type_min_delay < 0 or args.type_max_delay < args.type_min_delay:
        logging.error("Typing delay settings are invalid. Ensure 0 <= min <= max.")
        return 2
    if args.visible_type_chars < 0:
        logging.error("--visible-type-chars must be zero or greater.")
        return 2
    if args.cooldown_min_seconds < 0 or args.cooldown_max_seconds < args.cooldown_min_seconds:
        logging.error("Cooldown settings are invalid. Ensure 0 <= min <= max.")
        return 2
    if args.post_submit_wait_seconds < 0.5:
        logging.error("--post-submit-wait-seconds must be at least 0.5 so submit verification can run.")
        return 2
    if args.reflection_every_n_comments < 2:
        logging.error("--reflection-every-n-comments must be at least 2.")
        return 2
    if not args.content_container_selector.strip():
        logging.error("--content-container-selector cannot be blank.")
        return 2
    if args.content_mode in {"auto", "manhwa"} and not args.ocr_image_selector.strip():
        logging.error("--ocr-image-selector cannot be blank when OCR may be used.")
        return 2
    if not 0 <= args.ocr_confidence <= 1:
        logging.error("--ocr-confidence must be between 0 and 1.")
        return 2

    driver_session: Optional[DriverSession] = None
    run_reporter = reporter_from_environment()
    try:
        if run_reporter:
            run_reporter.update(status="running", stage="opening-browser", progress_total=args.max_chapters)
        driver_session = build_driver(headless=args.headless, chrome_profile_dir=args.chrome_profile_dir)
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
            args.reflection_every_n_comments,
            args.content_mode,
            args.content_container_selector,
            args.ocr_image_selector,
            args.ocr_confidence,
            args.type_min_delay,
            args.type_max_delay,
            args.visible_type_chars,
            args.reuse_loaded_images == "true",
            args.cooldown_min_seconds,
            args.cooldown_max_seconds,
            args.post_submit_wait_seconds,
            first_comment_override=args.first_comment_override,
            already_on_start_url=args.start_signal_file is not None,
            run_reporter=run_reporter,
        )
        logging.info("Run complete. Chapters processed: %s.", completed)
        if completed:
            logging.info("RUN_OUTCOME: completed with %s processed chapter(s).", completed)
        else:
            logging.warning("RUN_OUTCOME: no chapters were processed.")
        if run_reporter:
            run_reporter.update(
                status="completed",
                stage="completed",
                progress_current=completed,
                progress_total=args.max_chapters,
            )
        return 0
    except Exception as exc:
        logging.exception("Automation failed: %s", exc)
        if run_reporter:
            run_reporter.update(status="failed", stage="failed", stop_reason=str(exc))
        return 1
    finally:
        if driver_session is not None:
            close_driver_session(driver_session)


if __name__ == "__main__":
    sys.exit(main())
