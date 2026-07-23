#!/usr/bin/env python3
"""Fast regressions for downloader URL, filtering, and chapter selection logic."""

from __future__ import annotations

import sys
import tempfile
import zipfile
import json
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

import manhwa_ocr_downloader as downloader
from automation_runtime import normalize_web_url, preflight_url


class FakeSeriesDriver:
    def __init__(self) -> None:
        self.current_url = "https://example.test/content/title/chapter-20?page=13"

    def execute_script(self, script: str, *args: object) -> object:
        if "document.querySelectorAll(\"a[href]\")" in script:
            return [
                {"href": "https://example.test/content/title/chapter-19", "label": "Vol. 1 Ch. 19"},
                {"href": "https://example.test/content/title/chapter-20", "label": "Vol. 1 Ch. 20"},
                {"href": "https://example.test/content/title/chapter-21", "label": "Vol. 1 Ch. 21"},
            ]
        return ""


class FakeMediaOnlyDriver:
    current_url = "https://reader.other-example.test/read/volume-1/chapter-7"

    def execute_script(self, script: str, *args: object) -> object:
        if "return images.length > 0" in script:
            return True
        return ""


def main() -> int:
    assert normalize_web_url(" reader.other-example.test/chapter-7 ") == (
        "https://reader.other-example.test/chapter-7"
    )
    assert not preflight_url("reader.other-example.test/chapter-7")
    series_title = downloader.clean_series_title(
        "Since I'm Immune to Corruption, I Start with a Disaster-Class Demon Sword "
        "Chapter 1 - Thunderscans EN"
    )
    assert series_title == (
        "Since I'm Immune to Corruption, I Start with a Disaster-Class Demon Sword"
    )
    series_slug = downloader.slugify(series_title, "manhwa")
    assert series_slug == (
        "since-im-immune-to-corruption-i-start-with-a-disaster-class-demon-sword"
    )
    assert downloader.chapter_folder_name(
        f"{series_title} Chapter 27.5",
        1,
    ) == "27.5"
    assert downloader.chapter_folder_name(
        f"{series_title} Chapter 2",
        2,
    ) == "02"
    assert downloader.chapter_folder_name(
        series_title,
        3,
        segment_min_y=1200,
    ) == "03"
    assert downloader.url_with_reader_page(
        "https://example.test/content/title/id?page=13&mode=reader", 2
    ) == "https://example.test/content/title/id?page=2&mode=reader"
    assert downloader.chapter_number_from_text("Read Title - Vol. 1 Ch. 20") == 20
    assert downloader.PROMOTIONAL_PATTERN.search("https://cdn.test/discord-banner.png")
    assert not downloader.PROMOTIONAL_PATTERN.search("https://cdn.test/chapter-20-page-01.webp")
    assert not downloader.is_promotional_asset_url(
        "https://scanlation-reader.test/assets/chapters/page-001.webp"
    )
    assert downloader.is_promotional_asset_url(
        "https://cdn.reader.test/assets/discord-banner.webp"
    )
    assert downloader.image_extension_from_payload(
        "https://cdn.reader.test/page?id=1",
        b"\x89PNG\r\n\x1a\n" + b"x" * 600,
    ) == ".png"
    assert downloader.is_image_payload(b"RIFF" + b"x" * 4 + b"WEBP" + b"x" * 600)
    assert downloader.bot.series_url_from_chapter_url(
        "https://reader.other-example.test/manga/series-name/chapter-7"
    ) == "https://reader.other-example.test/manga/series-name"

    with tempfile.TemporaryDirectory() as library_tmpdir:
        output_root = Path(library_tmpdir)
        legacy_dir = output_root / (
            "01-since-i-m-immune-to-corruption-i-start-with-a-disaster-class-"
            "demon-sword-chapter-1-thunder"
        )
        legacy_dir.mkdir()
        legacy_metadata = {
            "index": 1,
            "url": (
                "https://reader.test/since-im-immune-to-corruption-i-start-with-a-"
                "disaster-class-demon-sword-chapter-1/"
            ),
            "title": f"{series_title} Chapter 1 - Reader",
            "images_dir": str(legacy_dir / "images"),
        }
        (legacy_dir / "metadata.json").write_text(
            json.dumps(legacy_metadata),
            encoding="utf-8",
        )
        (output_root / "manifest.json").write_text(
            json.dumps(
                [
                    {
                        "url": legacy_metadata["url"],
                        "status": "ok",
                        "text_file": str(legacy_dir / "ocr.txt"),
                    }
                ]
            ),
            encoding="utf-8",
        )
        series_dir = downloader.resolve_series_output_dir(
            output_root,
            series_title,
            series_slug,
        )
        assert series_dir == output_root / series_slug
        assert not legacy_dir.exists()
        assert (series_dir / "01" / "metadata.json").is_file()
        migrated_metadata = json.loads(
            (series_dir / "01" / "metadata.json").read_text(encoding="utf-8")
        )
        assert migrated_metadata["series_title"] == series_title
        assert migrated_metadata["chapter_folder"] == "01"
        migrated_manifest = json.loads(
            (series_dir / "manifest.json").read_text(encoding="utf-8")
        )
        assert migrated_manifest[0]["text_file"].startswith(str(series_dir / "01"))
        assert downloader.resolve_series_output_dir(
            output_root,
            series_title,
            series_slug,
        ) == series_dir
        assert json.loads((output_root / "manifest.json").read_text(encoding="utf-8")) == []

    with tempfile.TemporaryDirectory() as custom_tmpdir:
        output_root = Path(custom_tmpdir)
        custom_series_dir = output_root / "my-existing-library-folder"
        chapter_dir = custom_series_dir / "01"
        chapter_dir.mkdir(parents=True)
        (chapter_dir / "metadata.json").write_text(
            json.dumps({"series_title": series_title, "title": f"{series_title} Chapter 1"}),
            encoding="utf-8",
        )
        assert downloader.resolve_series_output_dir(
            output_root,
            series_title,
            series_slug,
        ) == custom_series_dir

    media_driver = FakeMediaOnlyDriver()
    original_continue = downloader.bot.continue_reading_url_or_click
    original_first_link = downloader.bot.first_matching_link_url
    try:
        downloader.bot.continue_reading_url_or_click = lambda active: (_ for _ in ()).throw(
            AssertionError("Media-only chapter should not invoke series navigation")
        )
        downloader.bot.first_matching_link_url = lambda active, selector, url: (_ for _ in ()).throw(
            AssertionError("Media-only chapter should not search for another chapter")
        )
        assert downloader.maybe_open_capture_chapter(
            media_driver,
            media_driver.current_url,
            "img",
        ) == media_driver.current_url
    finally:
        downloader.bot.continue_reading_url_or_click = original_continue
        downloader.bot.first_matching_link_url = original_first_link


    driver = FakeSeriesDriver()
    original_loader = downloader.bot.load_url_with_retries
    try:
        downloader.bot.load_url_with_retries = lambda active, url, label: True
        next_url = downloader.next_chapter_from_series(
            driver,
            "https://example.test/content/title/chapter-20?page=13",
            "Read Title - Vol. 1 Ch. 20",
        )
    finally:
        downloader.bot.load_url_with_retries = original_loader
    assert next_url == "https://example.test/content/title/chapter-21"
    with tempfile.TemporaryDirectory() as raw_tmpdir:
        chapter_dir = Path(raw_tmpdir)
        images_dir = chapter_dir / "images"
        images_dir.mkdir()
        (images_dir / "001.jpeg").write_bytes(b"page-one")
        (images_dir / "002.jpeg").write_bytes(b"page-two")
        archive = downloader.create_chapter_archive(images_dir, chapter_dir)
        with zipfile.ZipFile(archive) as opened:
            assert opened.namelist() == ["001.jpeg", "002.jpeg"]
    print("Downloader regression checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
