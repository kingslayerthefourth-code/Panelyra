#!/usr/bin/env python3
"""Regression checks for reviewed manhwa cleaning."""

from __future__ import annotations

import json
import sys
import tempfile
import zipfile
from pathlib import Path

from PIL import Image


APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

from panelyra.services.manhwa_cleaner import clean_chapter, detect_boundary_promos, numbered_images


def write_image(path: Path, size: tuple[int, int], color: tuple[int, int, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path)


def main() -> int:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        app_dir = root / "Panelyra"
        series = app_dir / "manhwa" / "money-keeps-piling-up"
        chapter = series / "15"
        images = chapter / "images"
        write_image(images / "001.webp", (100, 900), (6, 20, 22))
        write_image(images / "002.webp", (100, 300), (240, 240, 240))
        write_image(images / "003.webp", (100, 850), (7, 30, 32))
        (chapter / "metadata.json").write_text('{"title":"Chapter 15"}\n', encoding="utf-8")
        (series / "manifest.json").write_text(
            json.dumps(
                [
                    {
                        "title": "Money Keeps Piling Up Chapter 15",
                        "url": "https://example.test/money-keeps-piling-up-chapter-15",
                        "images_dir": "manhwa/money-keeps-piling-up/15/images",
                        "image_count": 3,
                        "archive_file": "",
                    }
                ],
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        original_size = Image.open(images / "001.webp").size
        suggestion = detect_boundary_promos(images / "001.webp", images / "003.webp")
        assert suggestion.confidence == "suggested"
        assert suggestion.first_top > 0
        assert suggestion.last_bottom > 0

        result = clean_chapter(
            series,
            app_dir / "manhwa_clean",
            "15",
            first_top=100,
            last_bottom=120,
            app_dir=app_dir,
        )

        assert Image.open(images / "001.webp").size == original_size
        clean_images = numbered_images(result.chapter_dir / "images")
        assert len(clean_images) == 3
        assert Image.open(clean_images[0]).size == (100, 800)
        assert Image.open(clean_images[-1]).size == (100, 730)
        assert result.archive_file.is_file()
        with zipfile.ZipFile(result.archive_file) as archive:
            assert archive.testzip() is None
            assert archive.namelist() == ["001.webp", "002.webp", "003.webp"]

        metadata = json.loads((result.chapter_dir / "metadata.json").read_text(encoding="utf-8"))
        assert metadata["clean_copy"] is True
        assert metadata["promo_crop"]["first_image_top_pixels"] == 100

        manifest = json.loads((result.destination / "manifest.json").read_text(encoding="utf-8"))
        assert manifest[0]["images_dir"] == "manhwa_clean/money-keeps-piling-up-clean/15/images"
        assert manifest[0]["archive_file"] == "manhwa_clean/money-keeps-piling-up-clean/15/chapter-pages.cbz"

        try:
            clean_chapter(series, app_dir / "manhwa_clean", "15", first_top=900, app_dir=app_dir)
            raise AssertionError("Invalid full-image crop was accepted.")
        except ValueError:
            pass

    print("Manhwa cleaner regression checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
