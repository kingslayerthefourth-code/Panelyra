#!/usr/bin/env python3
"""Regression checks for standalone series update wrappers."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path


APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

from panelyra.automation.series_updates import (
    canonical_history_url,
    download_backward_from_madara_prev,
    download_library_latest_chapters_once,
    download_missing_series_chapters_once,
    extract_chapter_links,
    find_madara_previous_url,
    latest_chapter_links_only,
    library_series_sources,
    load_download_history,
    parser,
    save_download_history,
)


class FakeResponse:
    def __init__(self, text: str) -> None:
        self.text = text

    def raise_for_status(self) -> None:
        return None


class FakeSession:
    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages

    def get(self, url: str, timeout: float, headers: dict[str, str]) -> FakeResponse:
        return FakeResponse(self.pages[canonical_history_url(url)])


class Downloaded:
    status = "ok"


def main() -> int:
    html = """
    <a href="/series-title-chapter-10/">Ten</a>
    <a href="https://kayn.example/series-title-chapter-2/">Two</a>
    <a href="/series-title-chapter-11.5/">Eleven point five</a>
    <a href="/not-a-chapter/">Ignore</a>
    <a href="/series-title-chapter-1/">One</a>
    """
    assert extract_chapter_links("https://kayn.example/manga/series-title/", html) == [
        "https://kayn.example/series-title-chapter-1",
        "https://kayn.example/series-title-chapter-2",
        "https://kayn.example/series-title-chapter-10",
        "https://kayn.example/series-title-chapter-11.5",
    ]

    assert find_madara_previous_url(
        "https://thunder.example/title-chapter-16/",
        '<a class="btn prev_page" href="/title-chapter-15/">Prev</a>',
    ) == "https://thunder.example/title-chapter-15"
    assert find_madara_previous_url(
        "https://thunder.example/title-chapter-16/",
        '<a class="prev_page" href="https://thunder.example/title-chapter-15/">Prev</a>',
    ) == "https://thunder.example/title-chapter-15"

    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        history_file = root / "downloaded_chapters.json"
        save_download_history(["https://EXAMPLE.test/title-chapter-1/?utm=1"], history_file)
        assert load_download_history(history_file) == {"https://example.test/title-chapter-1"}

        called: list[str] = []

        def fake_download(**kwargs: object) -> list[Downloaded]:
            called.append(str(kwargs["start_url"]))
            assert kwargs["max_chapters"] == 1
            assert kwargs["reader_layout"] == "auto"
            assert kwargs["save_raw_images"] is True
            assert kwargs["create_archive"] is True
            assert kwargs["save_ocr"] is False
            return [Downloaded()]

        session = FakeSession(
            {
                "https://kayn.example/manga/series": html,
                "https://thunder.example/title-chapter-16": '<a class="prev_page" href="/title-chapter-15/">Prev</a>',
                "https://thunder.example/title-chapter-15": '<a class="prev_page" href="/title-chapter-14/">Prev</a>',
            }
        )
        outcomes = download_missing_series_chapters_once(
            ["https://kayn.example/manga/series"],
            history_file=history_file,
            output_dir=root / "manhwa",
            download_func=fake_download,
            session=session,
        )
        assert [item.url for item in outcomes] == [
            "https://kayn.example/series-title-chapter-1",
            "https://kayn.example/series-title-chapter-2",
            "https://kayn.example/series-title-chapter-10",
            "https://kayn.example/series-title-chapter-11.5",
        ]
        assert all(item.recorded for item in outcomes)
        assert "https://kayn.example/series-title-chapter-10" in load_download_history(history_file)

        outcomes = download_backward_from_madara_prev(
            "https://thunder.example/title-chapter-16/",
            2,
            history_file=history_file,
            output_dir=root / "manhwa",
            download_func=fake_download,
            session=session,
            delay_seconds=(0, 0),
        )
        assert [item.url for item in outcomes] == [
            "https://thunder.example/title-chapter-16",
            "https://thunder.example/title-chapter-15",
        ]
        assert "https://thunder.example/title-chapter-15" in load_download_history(history_file)

        def failing_download(**kwargs: object) -> list[object]:
            return []

        failed = download_backward_from_madara_prev(
            "https://thunder.example/title-chapter-16/",
            1,
            history_file=history_file,
            output_dir=root / "manhwa",
            download_func=failing_download,
            session=session,
            delay_seconds=(0, 0),
        )
        assert failed[0].recorded is False

        library_root = root / "manhwa"
        first_series = library_root / "asura-title" / "10"
        first_series.mkdir(parents=True)
        (first_series / "metadata.json").write_text(
            json.dumps(
                {
                    "url": "https://asurascans.com/asura-title-chapter-10/",
                    "title": "Asura Title Chapter 10",
                }
            ),
            encoding="utf-8",
        )
        second_series = library_root / "thunder-title" / "03"
        second_series.mkdir(parents=True)
        (second_series / "metadata.json").write_text(
            json.dumps(
                {
                    "url": "https://en-thunderscans.com/thunder-title-chapter-3/",
                    "title": "Thunder Title Chapter 3",
                }
            ),
            encoding="utf-8",
        )
        ignored_series = library_root / "other-title" / "99"
        ignored_series.mkdir(parents=True)
        (ignored_series / "metadata.json").write_text(
            json.dumps({"url": "https://other.example/other-title-chapter-99/"}),
            encoding="utf-8",
        )
        sources = library_series_sources(library_root)
        assert sorted(sources) == ["asura-title", "other-title", "thunder-title"]
        assert sources["asura-title"]["series_urls"] == ["https://asurascans.com"]
        assert latest_chapter_links_only(
            [
                "https://asurascans.com/asura-title-chapter-9",
                "https://asurascans.com/asura-title-chapter-10",
                "https://asurascans.com/asura-title-chapter-11",
            ],
            ["https://asurascans.com/asura-title-chapter-10"],
        ) == ["https://asurascans.com/asura-title-chapter-11"]

        library_session = FakeSession(
            {
                "https://asurascans.com": """
                    <a href="/asura-title-chapter-9/">Old</a>
                    <a href="/asura-title-chapter-10/">Current</a>
                    <a href="/asura-title-chapter-11/">Latest</a>
                """,
                "https://en-thunderscans.com": """
                    <a href="/thunder-title-chapter-2/">Old</a>
                    <a href="/thunder-title-chapter-3/">Current</a>
                    <a href="/thunder-title-chapter-4/">Latest</a>
                """,
                "https://other.example": """
                    <a href="/other-title-chapter-99/">Current</a>
                    <a href="/other-title-chapter-100/">Latest</a>
                """,
            }
        )
        history_file = root / "library_downloaded_chapters.json"
        library_outcomes = download_library_latest_chapters_once(
            library_root=library_root,
            history_file=history_file,
            output_dir=root / "manhwa",
            download_func=fake_download,
            session=library_session,
        )
        assert [item.url for item in library_outcomes] == [
            "https://asurascans.com/asura-title-chapter-11",
            "https://other.example/other-title-chapter-100",
            "https://en-thunderscans.com/thunder-title-chapter-4",
        ]
        assert all(item.recorded for item in library_outcomes)

    parsed = parser().parse_args(["--series-url", "https://example.test/manga/title", "--once"])
    assert parsed.series_url == ["https://example.test/manga/title"]
    parsed_backward = parser().parse_args(["backward", "--start-url", "https://example.test/title-chapter-4", "--count", "3"])
    assert parsed_backward.command == "backward"
    print("Series update wrapper regression checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
