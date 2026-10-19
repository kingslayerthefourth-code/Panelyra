#!/usr/bin/env python3
"""Fast regressions for downloader URL, filtering, and chapter selection logic."""

from __future__ import annotations

import sys
import tempfile
import zipfile
import json
from pathlib import Path
from types import SimpleNamespace

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

import manhwa_ocr_downloader as downloader
from automation_runtime import normalize_web_url, preflight_url
from selenium.common.exceptions import WebDriverException


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


class BackwardOnlySeriesDriver(FakeSeriesDriver):
    def execute_script(self, script: str, *args: object) -> object:
        if "document.querySelectorAll(\"a[href]\")" in script:
            return [
                {"href": "https://example.test/content/title/chapter-18", "label": "Vol. 1 Ch. 18"},
                {"href": "https://example.test/content/title/chapter-19", "label": "Vol. 1 Ch. 19"},
                {"href": "https://example.test/content/title/chapter-20", "label": "Vol. 1 Ch. 20"},
            ]
        return ""


class FakeMediaOnlyDriver:
    current_url = "https://reader.other-example.test/read/volume-1/chapter-7"

    def execute_script(self, script: str, *args: object) -> object:
        if "return images.length > 0" in script:
            return True
        return ""


class FakeLazyReaderDriver:
    def execute_script(self, script: str, *args: object) -> object:
        return None


def main() -> int:
    assert downloader.DEFAULT_DOWNLOAD_CHROME_PROFILE == (
        Path.home() / "Library" / "Application Support" / "Panelyra" / "Chrome" / "CaptureProfile"
    )
    assert "Google" not in downloader.DEFAULT_DOWNLOAD_CHROME_PROFILE.parts
    original_argv = sys.argv
    try:
        sys.argv = ["manhwa_ocr_downloader.py", "--start-url", "https://example.test/chapter-1"]
        assert downloader.parse_args().chrome_profile_dir == downloader.DEFAULT_DOWNLOAD_CHROME_PROFILE
        with tempfile.TemporaryDirectory() as override_root:
            override = Path(override_root) / "custom-capture-profile"
            sys.argv += ["--chrome-profile-dir", str(override)]
            assert downloader.parse_args().chrome_profile_dir == override
    finally:
        sys.argv = original_argv
    assert downloader.DOWNLOAD_DEBUG_PORT == 9223
    assert downloader.DOWNLOAD_BROWSER_LOCK.name == "panelyra-capture-browser.lock"
    assert downloader.CAPTURE_PERFORMANCE_PROFILES["rapid"] == {
        "stable_rounds": 2,
        "max_rounds": 12,
        "download_workers": 4,
    }
    assert downloader.CAPTURE_PERFORMANCE_PROFILES["balanced"]["download_workers"] == 4
    assert downloader.CAPTURE_PERFORMANCE_PROFILES["reliable"]["stable_rounds"] == 6
    paginated_snapshot = SimpleNamespace(
        page_total=30,
        image_count=1,
        has_next_page=True,
        has_next_chapter=True,
        visible_comment_editors=0,
        scroll_height=4_000,
        viewport_height=1_000,
    )
    assert downloader.capture_reader_layout(
        paginated_snapshot,
        "next-chapter",
    ) == "mixed"
    vertical_snapshot = SimpleNamespace(
        page_total=30,
        image_count=30,
        has_next_page=False,
        has_next_chapter=True,
        visible_comment_editors=0,
        scroll_height=30_000,
        viewport_height=1_000,
    )
    assert downloader.capture_reader_layout(
        vertical_snapshot,
        "next-chapter",
    ) == "next-chapter"
    project_file = downloader.PROJECT_ROOT / "manhwa" / "series" / "01" / "metadata.json"
    assert downloader.portable_output_path(project_file) == "manhwa/series/01/metadata.json"
    assert downloader.portable_output_path(None) == ""
    shuffled_urls = [
        "https://cdn.test/chapter/0002_b.webp",
        "https://cdn.test/chapter/0001_a.webp",
        "https://cdn.test/chapter/0003_c.webp",
    ]
    assert downloader.normalize_panel_url_order(shuffled_urls) == [
        shuffled_urls[1],
        shuffled_urls[0],
        shuffled_urls[2],
    ]
    opaque_urls = ["https://cdn.test/a.webp", "https://cdn.test/b.webp"]
    assert downloader.normalize_panel_url_order(opaque_urls) == opaque_urls
    assert downloader.maximum_quality_image_url(
        "https://cdn.test/001_page.jpeg?width=700&type=jpeg&quality=50&token=abc"
    ) == "https://cdn.test/001_page.jpeg?token=abc"
    assert downloader.missing_sequential_panel_numbers(
        [
            "https://cdn.test/0001_a.webp",
            "https://cdn.test/0003_c.webp",
        ]
    ) == [2]
    assert not downloader.missing_sequential_panel_numbers(opaque_urls)

    lazy_snapshots = [
        ["https://cdn.test/0001_a.webp", "https://cdn.test/0002_b.webp"],
        [
            "https://cdn.test/0001_a.webp",
            "https://cdn.test/0002_b.webp",
            "https://cdn.test/0003_c.webp",
        ],
    ]
    lazy_snapshots.extend([lazy_snapshots[-1]] * 3)
    original_visible = downloader.visible_story_image_urls
    original_height = downloader.bot.document_height
    original_viewport = downloader.bot.viewport_height
    original_dismiss = downloader.bot.dismiss_page_interruptions
    original_sleep = downloader.time.sleep
    try:
        downloader.visible_story_image_urls = (
            lambda *args, **kwargs: lazy_snapshots.pop(0)
            if lazy_snapshots
            else [
                "https://cdn.test/0001_a.webp",
                "https://cdn.test/0002_b.webp",
                "https://cdn.test/0003_c.webp",
            ]
        )
        downloader.bot.document_height = lambda active: 10_000
        downloader.bot.viewport_height = lambda active: 1_000
        downloader.bot.dismiss_page_interruptions = lambda active, attempts=1: None
        downloader.time.sleep = lambda seconds: None
        stabilized = downloader.stabilize_story_image_urls(
            FakeLazyReaderDriver(),
            "img",
            0,
            0,
            ["https://cdn.test/0001_a.webp"],
            stable_rounds_required=2,
            max_rounds=8,
        )
    finally:
        downloader.visible_story_image_urls = original_visible
        downloader.bot.document_height = original_height
        downloader.bot.viewport_height = original_viewport
        downloader.bot.dismiss_page_interruptions = original_dismiss
        downloader.time.sleep = original_sleep
    assert stabilized == [
        "https://cdn.test/0001_a.webp",
        "https://cdn.test/0002_b.webp",
        "https://cdn.test/0003_c.webp",
    ]
    with tempfile.TemporaryDirectory() as order_tmpdir:
        order_root = Path(order_tmpdir)
        source_bytes = {
            "10.webp": b"RIFF-ten-WEBP-source-bytes",
            "2.webp": b"RIFF-two-WEBP-source-bytes",
            "1.webp": b"RIFF-one-WEBP-source-bytes",
        }
        for name, payload in source_bytes.items():
            (order_root / name).write_bytes(payload)
        assert [
            path.name for path in downloader.ordered_archive_images(order_root)
        ] == ["1.webp", "2.webp", "10.webp"]
        archive = downloader.create_chapter_archive(order_root, order_root)
        with zipfile.ZipFile(archive) as chapter_archive:
            assert chapter_archive.namelist() == ["1.webp", "2.webp", "10.webp"]
            for name, payload in source_bytes.items():
                assert chapter_archive.read(name) == payload

        captured_settings: dict[str, object] = {}
        original_extract = downloader.extract_chapter_content
        try:
            def fake_extract(driver: object, selector: str, settings: dict[str, object]) -> str:
                captured_settings.update(settings)
                return "local OCR"

            downloader.extract_chapter_content = fake_extract
            assert downloader.extract_saved_images_ocr(order_root, 0.4) == "local OCR"
        finally:
            downloader.extract_chapter_content = original_extract
        assert captured_settings["image_files"] == [
            str(order_root / "1.webp"),
            str(order_root / "2.webp"),
            str(order_root / "10.webp"),
        ]
        for name, payload in source_bytes.items():
            assert (order_root / name).read_bytes() == payload
    assert downloader.driver_session_lost(
        WebDriverException("no such window: target window already closed")
    )
    assert not downloader.driver_session_lost(WebDriverException("element click intercepted"))
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
    cross_site_title = "Immune to Corruption, I Wield a Cataclysmic Demon Sword"
    assert downloader.series_alias_match_score(series_title, cross_site_title) >= (
        downloader.SERIES_ALIAS_MATCH_THRESHOLD
    )
    assert downloader.series_alias_match_score(
        series_title,
        "Admission Is a Waste of Time",
    ) == 0
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
    assert downloader.next_numbered_chapter_url(
        "https://reader.test/series/fog-land/chapter-40"
    ) == "https://reader.test/series/fog-land/chapter-41"
    assert downloader.next_numbered_chapter_url(
        "https://reader.test/title/episode_07/?mode=reader"
    ) == "https://reader.test/title/episode_8/?mode=reader"
    assert downloader.next_numbered_chapter_url(
        "https://reader.test/title/volume-7"
    ) is None
    assert downloader.PROMOTIONAL_PATTERN.search("https://cdn.test/discord-banner.png")
    assert not downloader.PROMOTIONAL_PATTERN.search("https://cdn.test/chapter-20-page-01.webp")
    assert not downloader.is_promotional_asset_url(
        "https://scanlation-reader.test/assets/chapters/page-001.webp"
    )
    assert downloader.is_promotional_asset_url(
        "https://cdn.reader.test/assets/discord-banner.webp"
    )
    assert downloader.is_promotional_asset_url(
        "https://cdn.reader.test/placements/native-ad-728x90.webp"
    )
    assert downloader.is_promotional_asset_url(
        "https://pagead2.googlesyndication.com/pagead/imgad?id=fixture"
    )
    assert not downloader.is_promotional_asset_url(
        "https://static.reader.test/chapters/chapter-id/001.webp"
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

    with tempfile.TemporaryDirectory() as alias_tmpdir:
        output_root = Path(alias_tmpdir)
        canonical_series_dir = output_root / series_slug
        chapter_dir = canonical_series_dir / "01"
        chapter_dir.mkdir(parents=True)
        (chapter_dir / "metadata.json").write_text(
            json.dumps({"series_title": series_title, "title": f"{series_title} Chapter 1"}),
            encoding="utf-8",
        )
        cross_site_slug = downloader.slugify(cross_site_title, "manhwa")
        assert downloader.resolve_series_output_dir(
            output_root,
            cross_site_title,
            cross_site_slug,
        ) == canonical_series_dir
        assert not (output_root / cross_site_slug).exists()

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
    backward_driver = BackwardOnlySeriesDriver()
    original_loader = downloader.bot.load_url_with_retries
    try:
        downloader.bot.load_url_with_retries = lambda active, url, label: True
        next_url = downloader.next_chapter_from_series(
            backward_driver,
            "https://example.test/content/title/chapter-20?page=13",
            "Read Title - Vol. 1 Ch. 20",
        )
    finally:
        downloader.bot.load_url_with_retries = original_loader
    assert next_url is None
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
