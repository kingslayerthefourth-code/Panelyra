"""Storage reporting and guarded cleanup for generated Panelyra artifacts."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from pathlib import Path

from panelyra.services.health import format_bytes
from panelyra.services.paths import (
    DOWNLOADS_DIR,
    LEGACY_DOWNLOAD_DIR,
    PANELYRA_LOG_DIR,
    PROJECT_ROOT,
    RUNS_DIR,
    UPLOADS_DIR,
)


@dataclass(frozen=True)
class StorageArea:
    key: str
    label: str
    path: Path
    clearable: bool
    description: str


STORAGE_AREAS = (
    StorageArea("downloads", "Capture downloads", DOWNLOADS_DIR, False, "Chapter images, CBZ files, and OCR output."),
    StorageArea("studio", "Studio renders", UPLOADS_DIR, False, "Uploaded source images and generated Word Art."),
    StorageArea("runs", "Run records", RUNS_DIR, True, "Process records and checkpoints."),
    StorageArea("logs", "Temporary logs", PANELYRA_LOG_DIR, True, "Bounded automation console logs."),
    StorageArea("reports", "Test reports", PROJECT_ROOT / "outputs/test-reports", True, "Generated regression reports."),
    StorageArea("legacy_ocr", "Legacy OCR output", LEGACY_DOWNLOAD_DIR, False, "Older output retained for compatibility."),
)


def directory_size(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    total = 0
    for item in path.rglob("*"):
        if not item.is_file() or item.is_symlink():
            continue
        try:
            total += item.stat().st_size
        except OSError:
            continue
    return total


def file_count(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return 1
    return sum(1 for item in path.rglob("*") if item.is_file() and not item.is_symlink())


def storage_report() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for area in STORAGE_AREAS:
        size = directory_size(area.path)
        rows.append(
            {
                **asdict(area),
                "path": str(area.path),
                "bytes": size,
                "size": format_bytes(size),
                "files": file_count(area.path),
            }
        )
    return rows


def prune_generated_files(area_key: str, retention_days: int) -> tuple[int, int]:
    area = next((item for item in STORAGE_AREAS if item.key == area_key), None)
    if area is None or not area.clearable:
        raise ValueError("This storage area cannot be cleaned automatically.")
    resolved_root = area.path.resolve()
    if resolved_root == PROJECT_ROOT.resolve() or PROJECT_ROOT.resolve() not in resolved_root.parents:
        if area.key != "logs":
            raise ValueError("Refusing to clean a path outside the generated artifact boundary.")
    if not area.path.exists():
        return 0, 0

    cutoff = time.time() - max(0, retention_days) * 86400
    removed_files = 0
    removed_bytes = 0
    for item in sorted(area.path.rglob("*"), reverse=True):
        if item.is_symlink():
            continue
        if item.is_file():
            try:
                stat = item.stat()
                if stat.st_mtime <= cutoff:
                    removed_bytes += stat.st_size
                    item.unlink()
                    removed_files += 1
            except OSError:
                continue
        elif item.is_dir():
            try:
                item.rmdir()
            except OSError:
                continue
    return removed_files, removed_bytes
