"""Shared helpers for Selenium-based QA comment automation."""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Sequence

from selenium.common.exceptions import (
    ElementClickInterceptedException,
    JavascriptException,
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


@dataclass(frozen=True)
class Locator:
    by: str
    value: str
    label: str


@dataclass(frozen=True)
class LocatedElement:
    element: WebElement
    locator: Locator
    context: str


def load_lines(path: Optional[Path], fallback: list[str]) -> list[str]:
    if path is None:
        return fallback

    lines = [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if not lines:
        raise ValueError(f"No usable lines found in {path}")
    return lines


def wait_for_clickable(driver: WebDriver, locator: Locator, wait_seconds: int) -> WebElement:
    wait = WebDriverWait(
        driver,
        wait_seconds,
        ignored_exceptions=(StaleElementReferenceException, ElementClickInterceptedException),
    )
    return wait.until(EC.element_to_be_clickable((locator.by, locator.value)))


def find_with_locators(driver: WebDriver, locators: Iterable[Locator], wait_seconds: int) -> tuple[WebElement, Locator]:
    last_error: Optional[Exception] = None
    for locator in locators:
        try:
            element = wait_for_clickable(driver, locator, wait_seconds)
            logging.info("Found element using %s", locator.label)
            return element, locator
        except (TimeoutException, NoSuchElementException, StaleElementReferenceException) as exc:
            last_error = exc
    raise TimeoutException(f"No matching element found. Last error: {last_error}")


def find_in_page_or_iframe(driver: WebDriver, locators: Iterable[Locator], wait_seconds: int) -> LocatedElement:
    driver.switch_to.default_content()
    try:
        element, locator = find_with_locators(driver, locators, wait_seconds)
        return LocatedElement(element=element, locator=locator, context="top document")
    except TimeoutException:
        logging.info("Element not found in top document; checking iframes.")

    frames = driver.find_elements(By.TAG_NAME, "iframe")
    for index, frame in enumerate(frames):
        driver.switch_to.default_content()
        try:
            driver.switch_to.frame(frame)
            element, locator = find_with_locators(driver, locators, max(3, wait_seconds // 2))
            return LocatedElement(element=element, locator=locator, context=f"iframe[{index}]")
        except (TimeoutException, NoSuchElementException, StaleElementReferenceException, WebDriverException):
            continue

    driver.switch_to.default_content()
    raise TimeoutException(f"No matching element found in top document or {len(frames)} iframe(s).")


def gradual_scroll(
    driver: WebDriver,
    *,
    min_step_px: int,
    max_step_px: int,
    min_pause_seconds: float,
    max_pause_seconds: float,
) -> None:
    try:
        page_height = int(
            driver.execute_script("return Math.max(document.body.scrollHeight, document.documentElement.scrollHeight);")
        )
        viewport_height = int(driver.execute_script("return window.innerHeight;"))
        current_y = int(driver.execute_script("return window.scrollY;"))
    except (JavascriptException, TypeError, ValueError):
        logging.warning("Could not inspect page dimensions; skipping scroll.")
        return

    target_y = max(0, page_height - viewport_height - random.randint(80, 240))
    while current_y < target_y:
        step = random.randint(min_step_px, max_step_px)
        driver.execute_script("window.scrollBy({top: arguments[0], left: 0, behavior: 'smooth'});", step)
        time.sleep(random.uniform(min_pause_seconds, max_pause_seconds))
        current_y = int(driver.execute_script("return window.scrollY;"))


def clear_existing_text(element: WebElement) -> None:
    try:
        element.send_keys(Keys.COMMAND, "a")
        element.send_keys(Keys.BACKSPACE)
    except WebDriverException:
        logging.debug("Could not clear element with keyboard shortcut; continuing.")


def slow_type(element: WebElement, text: str, min_delay: float = 0.05, max_delay: float = 0.25) -> None:
    driver = element.parent
    try:
        driver.execute_script("arguments[0].scrollIntoView({block: 'center', inline: 'nearest'});", element)
        time.sleep(0.15)
    except WebDriverException:
        logging.debug("Could not center element before typing; continuing.")

    try:
        element.click()
    except WebDriverException as exc:
        logging.info("Normal editor click failed; focusing with JavaScript fallback: %s", exc)
        driver.execute_script("arguments[0].focus();", element)

    for char in text:
        if ord(char) > 0xFFFF:
            logging.info("Skipping non-BMP character unsupported by ChromeDriver send_keys.")
            continue
        try:
            element.send_keys(char)
        except WebDriverException:
            driver.execute_script("arguments[0].focus();", element)
            element.send_keys(char)
        time.sleep(random.uniform(min_delay, max_delay))


def maybe_sentence_case(text: str) -> str:
    if not text:
        return text

    style = random.choice(("original", "lower", "sentence"))
    if style == "lower":
        return text.lower()
    if style == "sentence":
        return text[0].upper() + text[1:]
    return text


def build_comment(
    *,
    opening_phrases: Sequence[str],
    subject_phrases: Sequence[str],
    ending_phrases: Sequence[str],
    joiners: Sequence[str],
    punctuation: Sequence[str],
) -> str:
    opening = random.choice(opening_phrases)
    subject = random.choice(subject_phrases)
    ending = random.choice(ending_phrases)
    style = random.choice(("comma", "periods", "dash", "joiner"))

    if style == "comma":
        text = f"{opening}, {subject}, {ending}"
    elif style == "periods":
        text = f"{opening}. {maybe_sentence_case(subject)}. {maybe_sentence_case(ending)}"
    elif style == "dash":
        text = f"{opening} - {subject}; {ending}"
    else:
        text = f"{opening}, {subject} {random.choice(joiners)} {ending}"

    text = maybe_sentence_case(text).rstrip(".! ")
    return f"{text}{random.choice(punctuation)}"


def generate_unique_comment(
    *,
    iteration: int,
    used_comments: set[str],
    opening_phrases: Sequence[str],
    subject_phrases: Sequence[str],
    ending_phrases: Sequence[str],
    joiners: Sequence[str],
    punctuation: Sequence[str],
    add_suffix: bool = False,
) -> str:
    for _ in range(100):
        text = build_comment(
            opening_phrases=opening_phrases,
            subject_phrases=subject_phrases,
            ending_phrases=ending_phrases,
            joiners=joiners,
            punctuation=punctuation,
        )
        if add_suffix:
            suffix_style = random.choice(("none", "short", "bracketed"))
            if suffix_style == "short":
                text = f"{text} #{iteration + 1}"
            elif suffix_style == "bracketed":
                text = f"{text} [{iteration + 1}]"

        if text not in used_comments:
            used_comments.add(text)
            return text

    fallback = f"{text} qa-{iteration + 1}-{int(time.time())}"
    used_comments.add(fallback)
    return fallback
