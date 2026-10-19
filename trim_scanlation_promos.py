#!/usr/bin/env python3
"""Create a separate manhwa copy with boundary scanlation promos cropped."""

from __future__ import annotations

import argparse
import json
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_SOURCE = (
    PROJECT_ROOT
    / "manhwa"
    / "since-im-immune-to-corruption-i-start-with-a-disaster-class-demon-sword"
)
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "manhwa_clean"
SMALL_LAST_PAGE_PROMO_MAX_FRACTION = 0.08


@dataclass(frozen=True)
class BoundaryCrop:
    top: int = 0
    bottom: int = 0
    last_image_remove_ranges: tuple[tuple[int, int], ...] = ()
    force_bottom: bool = False
    source_hint: str = ""


# These values crop only the promotional bands visible at chapter boundaries.
# They deliberately retain the story portions of the same long-strip images.
TITLE_CROP_PRESET: dict[str, BoundaryCrop] = {
    "01": BoundaryCrop(
        top=1_650,
        bottom=830,
        source_hint="en-thunderscans.com",
    ),
    "02": BoundaryCrop(
        top=760,
        bottom=486,
        last_image_remove_ranges=((7_000, 7_320),),
        force_bottom=True,
        source_hint="novatoon.com",
    ),
    "03": BoundaryCrop(
        top=760,
        bottom=486,
        last_image_remove_ranges=((2_400, 2_720),),
        force_bottom=True,
        source_hint="novatoon.com",
    ),
    "04": BoundaryCrop(
        top=760,
        bottom=486,
        last_image_remove_ranges=((6_500, 6_840),),
        force_bottom=True,
        source_hint="novatoon.com",
    ),
    "05": BoundaryCrop(
        top=760,
        bottom=486,
        last_image_remove_ranges=((4_980, 5_280),),
        force_bottom=True,
        source_hint="novatoon.com",
    ),
    "06": BoundaryCrop(
        top=760,
        bottom=486,
        last_image_remove_ranges=((1_850, 2_150),),
        force_bottom=True,
        source_hint="novatoon.com",
    ),
}


def numbered_images(images_dir: Path) -> list[Path]:
    supported = {".jpg", ".jpeg", ".png", ".webp"}
    images = [path for path in images_dir.iterdir() if path.suffix.lower() in supported]
    return sorted(
        images,
        key=lambda path: (
            int(path.stem) if path.stem.isdigit() else 10**9,
            path.name.lower(),
        ),
    )


def crop_boundary_image(
    path: Path,
    *,
    top: int = 0,
    bottom: int = 0,
    remove_ranges: tuple[tuple[int, int], ...] = (),
) -> None:
    if top <= 0 and bottom <= 0 and not remove_ranges:
        return

    with Image.open(path) as image:
        width, height = image.size
        removals = [(0, max(0, top)), (max(0, height - bottom), height)]
        removals.extend(remove_ranges)
        normalized = sorted(
            (max(0, start), min(height, end))
            for start, end in removals
            if end > start
        )
        merged: list[tuple[int, int]] = []
        for start, end in normalized:
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))

        kept: list[tuple[int, int]] = []
        cursor = 0
        for start, end in merged:
            if start > cursor:
                kept.append((cursor, start))
            cursor = max(cursor, end)
        if cursor < height:
            kept.append((cursor, height))
        output_height = sum(end - start for start, end in kept)
        if output_height <= 0:
            raise ValueError(f"Configured cleaning removes the entire image: {path}")

        cropped = Image.new(image.mode, (width, output_height))
        output_y = 0
        for start, end in kept:
            segment = image.crop((0, start, width, end))
            cropped.paste(segment, (0, output_y))
            output_y += end - start
        save_options = {}
        if path.suffix.lower() == ".webp":
            save_options = {"format": "WEBP", "lossless": True, "quality": 100}
        cropped.save(path, **save_options)


def apply_last_page_policy(
    path: Path,
    requested_bottom: int,
    *,
    force: bool = False,
) -> tuple[int, str]:
    """Keep small footer ads; crop only substantial closing promotions."""
    if requested_bottom <= 0:
        return 0, "no_promotion_configured"
    if force:
        return requested_bottom, "source_verified_promotion_cropped"

    with Image.open(path) as image:
        promo_fraction = requested_bottom / image.height

    if promo_fraction <= SMALL_LAST_PAGE_PROMO_MAX_FRACTION:
        return 0, "small_footer_preserved"
    return requested_bottom, "large_promotion_cropped"


def rebuild_cbz(chapter_dir: Path, images: list[Path]) -> Path:
    archive = chapter_dir / "chapter-pages.cbz"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for image in images:
            bundle.write(image, arcname=image.name)
    return archive


def update_metadata(
    chapter_dir: Path,
    crop: BoundaryCrop,
    output_title_dir: Path,
    images: list[Path],
) -> None:
    metadata_path = chapter_dir / "metadata.json"
    metadata: dict[str, object] = {}
    if metadata_path.exists():
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            metadata = {}

    relative_chapter = (output_title_dir / chapter_dir.name).relative_to(PROJECT_ROOT)
    metadata.update(
        {
            "images_dir": str(relative_chapter / "images"),
            "archive_file": str(relative_chapter / "chapter-pages.cbz"),
            "image_count": len(images),
            "clean_copy": True,
            "clean_source": str(output_title_dir.name.removesuffix("-clean")),
            "promo_crop": {
                "first_image_top_pixels": crop.top,
                "last_image_bottom_pixels": crop.bottom,
                "last_image_removed_ranges": [
                    {"start": start, "end": end}
                    for start, end in crop.last_image_remove_ranges
                ],
                "story_pages_removed": 0,
            },
            "cleaned_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    metadata_path.write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def chapter_source_url(chapter_dir: Path) -> str:
    payload = {}
    metadata_path = chapter_dir / "metadata.json"
    if metadata_path.exists():
        try:
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            payload = {}
    return str(payload.get("url", "")).strip() if isinstance(payload, dict) else ""


def update_clean_manifest(staged: Path, destination: Path) -> None:
    manifest_path = staged / "manifest.json"
    if not manifest_path.exists():
        return
    try:
        records = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    if not isinstance(records, list):
        return

    url_to_chapter = {
        chapter_source_url(chapter_dir).rstrip("/"): chapter_dir.name
        for chapter_dir in staged.iterdir()
        if chapter_dir.is_dir() and chapter_source_url(chapter_dir)
    }
    cleaned_records: list[dict[str, object]] = []
    for item in records:
        if not isinstance(item, dict):
            continue
        chapter_name = url_to_chapter.get(str(item.get("url", "")).rstrip("/"))
        if chapter_name:
            relative_chapter = (destination / chapter_name).relative_to(PROJECT_ROOT)
            item.update(
                {
                    "text_file": str(relative_chapter / "ocr.txt"),
                    "image_urls_file": str(relative_chapter / "image_urls.txt"),
                    "images_dir": str(relative_chapter / "images"),
                    "archive_file": str(relative_chapter / "chapter-pages.cbz"),
                }
            )
        cleaned_records.append(item)
    manifest_path.write_text(
        json.dumps(cleaned_records, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def clean_title(
    source: Path,
    output_root: Path,
    preset: dict[str, BoundaryCrop],
) -> Path:
    if not source.is_dir():
        raise FileNotFoundError(f"Source title folder not found: {source}")

    output_root.mkdir(parents=True, exist_ok=True)
    destination = output_root / f"{source.name}-clean"

    with tempfile.TemporaryDirectory(prefix=f".{source.name}-", dir=output_root) as temp:
        staged = Path(temp) / destination.name
        shutil.copytree(source, staged, ignore=shutil.ignore_patterns(".DS_Store"))

        report: dict[str, object] = {
            "source": str(source.relative_to(PROJECT_ROOT)),
            "destination": str(destination.relative_to(PROJECT_ROOT)),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "chapters": {},
        }

        chapter_dirs = sorted(path for path in staged.iterdir() if path.is_dir())
        for chapter_dir in chapter_dirs:
            images = numbered_images(chapter_dir / "images")
            if not images:
                continue

            crop = preset.get(chapter_dir.name, BoundaryCrop())
            source_url = chapter_source_url(chapter_dir)
            if crop.source_hint and crop.source_hint.lower() not in source_url.lower():
                crop = BoundaryCrop()
            applied_bottom, last_page_decision = apply_last_page_policy(
                images[-1],
                crop.bottom,
                force=crop.force_bottom,
            )
            applied_crop = BoundaryCrop(
                top=crop.top,
                bottom=applied_bottom,
                last_image_remove_ranges=crop.last_image_remove_ranges,
                force_bottom=crop.force_bottom,
                source_hint=crop.source_hint,
            )
            crop_boundary_image(images[0], top=crop.top)
            crop_boundary_image(
                images[-1],
                bottom=applied_bottom,
                remove_ranges=crop.last_image_remove_ranges,
            )
            rebuild_cbz(chapter_dir, images)
            update_metadata(chapter_dir, applied_crop, destination, images)

            report["chapters"][chapter_dir.name] = {
                "page_count": len(images),
                "first_page_top_crop_pixels": crop.top,
                "last_page_requested_crop_pixels": crop.bottom,
                "last_page_applied_crop_pixels": applied_bottom,
                "last_page_decision": last_page_decision,
                "last_page_removed_ranges": [
                    {"start": start, "end": end}
                    for start, end in crop.last_image_remove_ranges
                ],
                "archive": str(
                    (destination / chapter_dir.name / "chapter-pages.cbz").relative_to(
                        PROJECT_ROOT
                    )
                ),
            }

        update_clean_manifest(staged, destination)
        (staged / "cleaning-report.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

        if destination.exists():
            shutil.rmtree(destination)
        staged.replace(destination)

    return destination


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create a separate copy with scanlation promo bands cropped."
    )
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    destination = clean_title(
        args.source.expanduser().resolve(),
        args.output_root.expanduser().resolve(),
        TITLE_CROP_PRESET,
    )
    print(f"Clean copy created: {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
