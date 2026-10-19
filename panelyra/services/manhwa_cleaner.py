"""Create reviewed clean copies of local manhwa chapters."""

from __future__ import annotations

import json
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image


IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


@dataclass(frozen=True)
class CleaningSuggestion:
    first_top: int = 0
    last_bottom: int = 0
    confidence: str = "manual"
    reason: str = "No obvious promo boundary detected."


@dataclass(frozen=True)
class CleaningResult:
    destination: Path
    chapter_dir: Path
    archive_file: Path
    report_file: Path
    first_before: tuple[int, int]
    first_after: tuple[int, int]
    last_before: tuple[int, int]
    last_after: tuple[int, int]


def chapter_sort_key(path: Path) -> tuple[int, int | str, str]:
    if path.stem.isdigit():
        return (0, int(path.stem), path.name.lower())
    return (1, path.stem.lower(), path.name.lower())


def numbered_images(images_dir: Path) -> list[Path]:
    if not images_dir.is_dir():
        return []
    return sorted(
        (path for path in images_dir.iterdir() if path.suffix.lower() in IMAGE_SUFFIXES),
        key=chapter_sort_key,
    )


def detect_boundary_promos(first_image: Path, last_image: Path) -> CleaningSuggestion:
    """Suggest conservative boundary crops; Reader always requires review."""
    first_top = 0
    last_bottom = 0
    reasons: list[str] = []

    with Image.open(first_image) as first:
        width, height = first.size
        probe_height = min(height, max(1, int(width * 0.9)))
        probe = first.crop((0, 0, width, probe_height)).convert("L")
        dark_pixels = sum(1 for pixel in probe.getdata() if pixel < 80)
        if height > width * 4 and dark_pixels / max(1, probe.width * probe.height) > 0.35:
            first_top = probe_height
            reasons.append("dark promotional header on the first page")

    with Image.open(last_image) as last:
        width, height = last.size
        probe_height = min(height, max(1, int(width * 1.4)))
        probe = last.crop((0, height - probe_height, width, height)).convert("L")
        dark_pixels = sum(1 for pixel in probe.getdata() if pixel < 80)
        if height > width * 4 and dark_pixels / max(1, probe.width * probe.height) > 0.22:
            last_bottom = probe_height
            reasons.append("dark promotional footer on the last page")

    if first_top or last_bottom:
        return CleaningSuggestion(first_top, last_bottom, "suggested", "; ".join(reasons))
    return CleaningSuggestion()


def validate_crop(first_image: Path, last_image: Path, first_top: int, last_bottom: int) -> None:
    with Image.open(first_image) as first:
        if first_top < 0 or first_top >= first.height:
            raise ValueError("First-page crop must leave visible story content.")
    with Image.open(last_image) as last:
        if last_bottom < 0 or last_bottom >= last.height:
            raise ValueError("Last-page crop must leave visible story content.")


def _crop_vertical(path: Path, *, top: int = 0, bottom: int = 0) -> tuple[int, int]:
    with Image.open(path) as image:
        width, height = image.size
        cropped = image.crop((0, top, width, height - bottom))
        save_options: dict[str, Any] = {}
        if path.suffix.lower() == ".webp":
            save_options = {"format": "WEBP", "lossless": True, "quality": 100}
        cropped.save(path, **save_options)
        return cropped.size


def _rebuild_cbz(chapter_dir: Path, images: list[Path]) -> Path:
    archive = chapter_dir / "chapter-pages.cbz"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for image in images:
            bundle.write(image, arcname=image.name)
    return archive


def _load_json(path: Path) -> Any:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def update_clean_manifest(source_series: Path, clean_series: Path, chapter: str, app_dir: Path) -> None:
    source_manifest = source_series / "manifest.json"
    clean_manifest = clean_series / "manifest.json"
    records = _load_json(clean_manifest) or _load_json(source_manifest) or []
    if not isinstance(records, list):
        return

    found = False
    rel_chapter = (clean_series / chapter).relative_to(app_dir)
    for item in records:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title", ""))
        url = str(item.get("url", ""))
        images_dir = str(item.get("images_dir", ""))
        if (
            url.rstrip("/").endswith(f"chapter-{chapter}")
            or f"Chapter {chapter}" in title
            or images_dir.rstrip("/").endswith(f"/{chapter}/images")
        ):
            item["image_urls_file"] = str(rel_chapter / "image_urls.txt")
            item["images_dir"] = str(rel_chapter / "images")
            item["archive_file"] = str(rel_chapter / "chapter-pages.cbz")
            item["image_count"] = len(numbered_images(clean_series / chapter / "images"))
            found = True

    if found:
        _write_json(clean_manifest, records)


def clean_chapter(
    source_series: Path,
    output_root: Path,
    chapter: str,
    *,
    first_top: int = 0,
    last_bottom: int = 0,
    app_dir: Path | None = None,
) -> CleaningResult:
    source_chapter = source_series / chapter
    source_images = numbered_images(source_chapter / "images")
    if not source_images:
        raise FileNotFoundError(f"No readable chapter images found for {source_series.name} chapter {chapter}.")
    validate_crop(source_images[0], source_images[-1], first_top, last_bottom)

    app_root = app_dir or source_series.parent.parent
    clean_series = output_root / f"{source_series.name.removesuffix('-clean')}-clean"
    destination_chapter = clean_series / chapter

    output_root.mkdir(parents=True, exist_ok=True)
    clean_series.mkdir(parents=True, exist_ok=True)
    if source_series.joinpath("manifest.json").is_file() and not clean_series.joinpath("manifest.json").is_file():
        shutil.copy2(source_series / "manifest.json", clean_series / "manifest.json")

    with tempfile.TemporaryDirectory(prefix=f".{source_series.name}-{chapter}-", dir=output_root) as temp_dir:
        staged = Path(temp_dir) / chapter
        shutil.copytree(source_chapter, staged, ignore=shutil.ignore_patterns(".DS_Store"))
        images = numbered_images(staged / "images")

        with Image.open(images[0]) as first:
            first_before = first.size
        with Image.open(images[-1]) as last:
            last_before = last.size

        first_after = _crop_vertical(images[0], top=first_top)
        last_after = _crop_vertical(images[-1], bottom=last_bottom)
        archive = _rebuild_cbz(staged, images)

        rel_chapter = (clean_series / chapter).relative_to(app_root)
        metadata_path = staged / "metadata.json"
        metadata = _load_json(metadata_path)
        if not isinstance(metadata, dict):
            metadata = {}
        metadata.update(
            {
                "images_dir": str(rel_chapter / "images"),
                "archive_file": str(rel_chapter / "chapter-pages.cbz"),
                "image_count": len(images),
                "clean_copy": True,
                "clean_source": source_series.name,
                "promo_crop": {
                    "first_image_top_pixels": first_top,
                    "last_image_bottom_pixels": last_bottom,
                    "last_image_removed_ranges": [],
                    "story_pages_removed": 0,
                },
                "cleaned_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        _write_json(metadata_path, metadata)

        if destination_chapter.exists():
            shutil.rmtree(destination_chapter)
        staged.replace(destination_chapter)

    update_clean_manifest(source_series, clean_series, chapter, app_root)
    report_file = clean_series / "cleaning-report.json"
    report = _load_json(report_file)
    if not isinstance(report, dict):
        report = {
            "source": str(source_series.relative_to(app_root)),
            "destination": str(clean_series.relative_to(app_root)),
            "chapters": {},
        }
    chapters = report.setdefault("chapters", {})
    if isinstance(chapters, dict):
        chapters[chapter] = {
            "page_count": len(source_images),
            "first_page": {
                "file": source_images[0].name,
                "before": list(first_before),
                "after": list(first_after),
                "top_crop_pixels": first_top,
            },
            "last_page": {
                "file": source_images[-1].name,
                "before": list(last_before),
                "after": list(last_after),
                "bottom_crop_pixels": last_bottom,
            },
            "archive": str((clean_series / chapter / "chapter-pages.cbz").relative_to(app_root)),
            "cleaned_at": datetime.now(timezone.utc).isoformat(),
        }
    report["updated_at"] = datetime.now(timezone.utc).isoformat()
    _write_json(report_file, report)

    return CleaningResult(
        destination=clean_series,
        chapter_dir=clean_series / chapter,
        archive_file=clean_series / chapter / "chapter-pages.cbz",
        report_file=report_file,
        first_before=first_before,
        first_after=first_after,
        last_before=last_before,
        last_after=last_after,
    )
