#!/usr/bin/env python3
"""Deterministic reader-layout and cross-site routing regressions."""

from __future__ import annotations

import sys
from pathlib import Path


APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

import bot as engage
import manhwa_ocr_downloader as downloader
from reader_routing import (
    ReaderSelectors,
    ReaderSnapshot,
    canonical_chapter_url,
    chapter_number_from_text,
    classify_reader_layout,
    next_chapter_from_visible_list,
    parse_page_counter_text,
)


def snapshot(**changes: object) -> ReaderSnapshot:
    values: dict[str, object] = {
        "url": "https://reader.test/title/chapter-4",
        "title": "Chapter 4",
        "page_current": 0,
        "page_total": 0,
        "image_signature": "page-a",
        "image_count": 12,
        "visible_comment_editors": 1,
        "has_next_page": False,
        "has_next_chapter": False,
        "scroll_height": 9000,
        "viewport_height": 900,
    }
    values.update(changes)
    return ReaderSnapshot(**values)  # type: ignore[arg-type]


class ChapterListDriver:
    current_url = "https://reader.test/title/chapter-4"
    title = "Series - Chapter 4"

    def execute_script(self, script: str, *args: object) -> object:
        return [
            {"href": "https://reader.test/title/chapter-3", "label": "Chapter 3"},
            {"href": "https://reader.test/title/chapter-4", "label": "Chapter 4"},
            {"href": "https://reader.test/title/chapter-5", "label": "Chapter 5"},
            {"href": "https://reader.test/title/chapter-6", "label": "Chapter 6"},
        ]


def main() -> int:
    assert "next chapter" in engage.NEXT_LINK_SELECTOR.lower()
    assert "aria-label*='next' i" not in engage.NEXT_LINK_SELECTOR.lower()
    assert "a[rel='next']" not in {
        item.strip() for item in engage.NEXT_LINK_SELECTOR.lower().split(",")
    }
    for comic_mode in ("comic", "manga", "manhwa", "manhua"):
        assert engage.normalize_content_mode(comic_mode) == "manhwa"
    assert engage.normalize_content_mode("novel") == "novel"
    assert not engage.supports_url_idempotency("continuous")
    assert engage.supports_url_idempotency("paginated")
    assert engage.supports_url_idempotency("next-chapter")

    assert parse_page_counter_text("14 / 14") == (14, 14)
    assert parse_page_counter_text("Page 3 of 22") == (3, 22)
    assert parse_page_counter_text("not a counter") == (0, 0)

    assert classify_reader_layout(snapshot(visible_comment_editors=3), "auto") == "continuous"
    assert classify_reader_layout(snapshot(chapter_marker_count=3), "auto") == "continuous"
    assert classify_reader_layout(
        snapshot(page_current=1, page_total=12, has_next_page=True),
        "auto",
    ) == "paginated"
    assert classify_reader_layout(
        snapshot(page_current=1, page_total=12, has_next_page=True, has_next_chapter=True),
        "auto",
    ) == "mixed"
    assert classify_reader_layout(snapshot(has_next_chapter=True), "auto") == "next-chapter"
    assert classify_reader_layout(
        snapshot(page_current=14, page_total=14, image_count=14, has_next_chapter=True),
        "auto",
    ) == "next-chapter"
    assert classify_reader_layout(
        snapshot(page_current=14, page_total=14, image_count=14),
        "auto",
    ) == "continuous"
    assert classify_reader_layout(snapshot(), "paginated") == "paginated"

    assert canonical_chapter_url(
        "https://reader.test/title/chapter-4?page=13&mode=reader#comments"
    ) == "https://reader.test/title/chapter-4?mode=reader"
    assert chapter_number_from_text("Episode 27.5") == 27.5
    assert downloader.page_query_state("https://reader.test/chapter-4?p=7") == ("p", 7)
    assert downloader.url_with_page_value(
        "https://reader.test/chapter-4?page=13&mode=reader",
        "page",
        14,
    ) == "https://reader.test/chapter-4?page=14&mode=reader"

    selectors = ReaderSelectors(
        next_chapter=".next-chapter",
        next_page=".next-page",
        page_counter=".page-counter",
        comment_editor="textarea",
        chapter_list=".chapter-list a",
    )
    assert next_chapter_from_visible_list(
        ChapterListDriver(),
        selectors,
        ChapterListDriver.current_url,
        ChapterListDriver.title,
    ) == "https://reader.test/title/chapter-5"

    print("Reader routing regression checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
