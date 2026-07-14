#!/usr/bin/env python3
"""
Long-running authenticated comment-flow QA script for a site you own.

Workflow:
1. Start Chrome manually with remote debugging enabled and your dedicated QA profile.
2. Log in to your site in that Chrome window.
3. Run this script. It attaches to that existing browser session.

The script defaults to dry-run mode. Add --confirm-live-posting only when you
intend to submit real comments to your own test/staging/production target.
"""

from __future__ import annotations

import argparse
import logging
import random
import sys
import time
from pathlib import Path

from selenium import webdriver
from selenium.common.exceptions import (
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webdriver import WebDriver
from webdriver_manager.chrome import ChromeDriverManager

from qa_utils import (
    Locator,
    clear_existing_text,
    find_in_page_or_iframe,
    generate_unique_comment as build_unique_comment,
    gradual_scroll,
    load_lines,
    slow_type,
)


# Replace these with your chapter/article URLs.
chapter_urls = [
    "https://YOUR-SITE.example/chapter-1",
    "https://YOUR-SITE.example/chapter-2",
]

# Optional deterministic fixture comments. Use --comment-mode file to post these
# or values from --comments-file instead of generated QA text.
comments_pool = [
    "QA automated comment 001",
    "QA automated comment 002",
    "QA automated comment 003",
]


OPENING_REMARKS = [
    "Wow",
    "Honestly",
    "Just caught up with this",
    "I was not expecting this",
    "Okay",
    "This chapter really landed",
    "Had to reread this part",
    "The update was worth the wait",
    "Not gonna lie",
    "I am really into this arc",
]

SUBJECT_PRAISE = [
    "the art style is incredible",
    "the pacing here is perfect",
    "this storyline is getting wild",
    "the character work feels sharp",
    "the panel flow is really clean",
    "the tension keeps building nicely",
    "the dialogue feels natural",
    "the reveal was handled so well",
    "the mood shift works beautifully",
    "this scene has a lot of energy",
]

CONCLUDING_THOUGHTS = [
    "can't wait for the next drop",
    "highly recommend this chapter",
    "this caught me completely off guard",
    "the next update should be interesting",
    "I need to see where this goes next",
    "this one is going on my reread list",
    "that ending really sticks",
    "the payoff here was strong",
    "I'm curious how the next chapter opens",
    "this was a great read",
]

BRIDGE_PHRASES = [
    "and",
    "plus",
    "because",
    "while",
    "especially since",
    "with how",
]

PUNCTUATION_ENDINGS = ["!", ".", "!!", "...", " :)"]


COMMENT_BOX_LOCATORS = [
    Locator(By.CSS_SELECTOR, "textarea[name='comment']", "textarea[name='comment']"),
    Locator(By.CSS_SELECTOR, "textarea#comment", "textarea#comment"),
    Locator(By.CSS_SELECTOR, "textarea.comment", "textarea.comment"),
    Locator(By.CSS_SELECTOR, "textarea", "generic textarea"),
    Locator(By.CSS_SELECTOR, "[contenteditable='true']", "contenteditable"),
    Locator(By.XPATH, "//textarea[contains(@placeholder, 'comment') or contains(@aria-label, 'comment')]", "comment textarea by hint"),
    Locator(By.XPATH, "//*[@contenteditable='true' and (contains(@aria-label, 'comment') or contains(@data-testid, 'comment'))]", "comment editor by hint"),
]

SUBMIT_BUTTON_LOCATORS = [
    Locator(By.CSS_SELECTOR, "button[type='submit']", "button[type='submit']"),
    Locator(By.CSS_SELECTOR, "input[type='submit']", "input[type='submit']"),
    Locator(By.XPATH, "//button[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'post')]", "button text contains post"),
    Locator(By.XPATH, "//button[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'submit')]", "button text contains submit"),
    Locator(By.XPATH, "//button[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'comment')]", "button text contains comment"),
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Authenticated Selenium comment-flow QA runner.")
    parser.add_argument("--debugger-address", default="127.0.0.1:9222", help="Chrome remote debugging address.")
    parser.add_argument("--wait-seconds", type=int, default=15, help="Explicit wait timeout for interactable elements.")
    parser.add_argument("--min-cooldown", type=int, default=120, help="Minimum seconds between submitted comments.")
    parser.add_argument("--max-cooldown", type=int, default=300, help="Maximum seconds between submitted comments.")
    parser.add_argument("--max-comments", type=int, default=0, help="Stop after this many comments. 0 means no limit.")
    parser.add_argument("--start-index", type=int, default=0, help="Comment index offset for resuming a long run.")
    parser.add_argument("--urls-file", type=Path, help="Optional newline-delimited list of chapter URLs.")
    parser.add_argument("--comments-file", type=Path, help="Optional newline-delimited list of comments.")
    parser.add_argument(
        "--comment-mode",
        choices=("generated", "file"),
        default="generated",
        help="Use generated unique QA comments or comments from comments_pool/--comments-file.",
    )
    parser.add_argument("--confirm-live-posting", action="store_true", help="Actually click submit buttons.")
    return parser.parse_args()


def attach_to_chrome(debugger_address: str) -> WebDriver:
    options = Options()
    options.add_experimental_option("debuggerAddress", debugger_address)
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)
    driver.set_page_load_timeout(60)
    return driver


def human_scroll(driver: WebDriver, min_pause: float = 0.4, max_pause: float = 1.8) -> None:
    """Scroll through the page gradually for realistic QA coverage of lazy-loaded comment UIs."""
    gradual_scroll(
        driver,
        min_step_px=150,
        max_step_px=350,
        min_pause_seconds=min_pause,
        max_pause_seconds=max_pause,
    )


def generate_unique_comment(iteration: int, used_comments: set[str]) -> str:
    """Build natural-looking, unique local QA text from reusable components."""
    return build_unique_comment(
        iteration=iteration,
        used_comments=used_comments,
        opening_phrases=OPENING_REMARKS,
        subject_phrases=SUBJECT_PRAISE,
        ending_phrases=CONCLUDING_THOUGHTS,
        joiners=BRIDGE_PHRASES,
        punctuation=PUNCTUATION_ENDINGS,
        add_suffix=True,
    )


def submit_comment(driver: WebDriver, wait_seconds: int, live_posting: bool) -> None:
    submit_button = find_in_page_or_iframe(driver, SUBMIT_BUTTON_LOCATORS, wait_seconds)
    logging.info("Submit button found in %s", submit_button.context)
    if live_posting:
        submit_button.element.click()
        logging.info("Submit clicked.")
    else:
        logging.info("Dry run: submit button was found but not clicked.")


def post_one_comment(driver: WebDriver, url: str, comment: str, args: argparse.Namespace) -> bool:
    logging.info("Opening %s", url)
    driver.switch_to.default_content()
    driver.get(url)
    time.sleep(random.uniform(1.5, 4.0))

    human_scroll(driver)

    comment_box = find_in_page_or_iframe(driver, COMMENT_BOX_LOCATORS, args.wait_seconds)
    logging.info("Comment box found in %s using %s", comment_box.context, comment_box.locator.label)
    clear_existing_text(comment_box.element)
    slow_type(comment_box.element, comment)
    submit_comment(driver, args.wait_seconds, args.confirm_live_posting)
    return True


def cooldown(min_seconds: int, max_seconds: int, enabled: bool) -> None:
    if not enabled:
        return
    wait_time = random.randint(min_seconds, max_seconds)
    logging.info("Cooldown for %s seconds before next submission.", wait_time)
    time.sleep(wait_time)


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    urls = load_lines(args.urls_file, chapter_urls)
    comments = load_lines(args.comments_file, comments_pool)
    if args.min_cooldown > args.max_cooldown:
        raise ValueError("--min-cooldown must be <= --max-cooldown")

    if not args.confirm_live_posting:
        logging.warning("Dry-run mode is active. Add --confirm-live-posting to click submit.")

    driver = attach_to_chrome(args.debugger_address)
    logging.info("Attached to Chrome at %s", args.debugger_address)

    posted = 0
    failures = 0
    used_comments: set[str] = set()
    total_iterations = args.max_comments or len(comments)
    try:
        for comment_number in range(args.start_index, args.start_index + total_iterations):
            if args.max_comments and posted >= args.max_comments:
                logging.info("Reached --max-comments=%s; stopping.", args.max_comments)
                break

            url = urls[comment_number % len(urls)]
            if args.comment_mode == "generated":
                comment = generate_unique_comment(comment_number, used_comments)
            else:
                comment = comments[comment_number % len(comments)]

            try:
                if post_one_comment(driver, url, comment, args):
                    posted += 1
                    logging.info("Completed comment index %s. Successful flow count: %s", comment_number, posted)
                    cooldown(args.min_cooldown, args.max_cooldown, args.confirm_live_posting)
            except Exception as exc:
                failures += 1
                logging.exception("Failed comment index %s on %s: %s", comment_number, url, exc)
                driver.switch_to.default_content()
                time.sleep(random.uniform(5, 15))
                continue
    finally:
        logging.info("Run finished. Successful flows: %s. Failures: %s.", posted, failures)
        # Do not quit Chrome: it is the user's manually opened authenticated session.

    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
