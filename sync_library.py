"""Validated, read-only Original library export for the tablet companion."""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import zipfile
from PIL import Image
from pathlib import Path


IMAGE_TYPES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"}
_lock = threading.RLock()


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def _valid_image(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size < 64:
        return False
    with path.open("rb") as source:
        head = source.read(16)
    magic = bool(
        head.startswith(b"\xff\xd8\xff") or head.startswith(b"\x89PNG\r\n\x1a\n")
        or head[:4] == b"RIFF" and head[8:12] == b"WEBP"
        or head[:6] in (b"GIF87a", b"GIF89a")
        or head[4:12] in (b"ftypavif", b"ftypavis")
    )
    if not magic: return False
    try:
        from panelyra.services.library_store import decode_image
        decode_image(path.read_bytes())
        return True
    except (OSError, ValueError): return False


def _valid_cbz(path: Path) -> bool:
    from panelyra.services.library_store import validate_archive
    try:
        return bool(validate_archive(path))
    except (OSError, ValueError, zipfile.BadZipFile, RuntimeError):
        return False


def _valid_image_bytes(head: bytes) -> bool:
    return bool(head.startswith(b"\xff\xd8\xff") or head.startswith(b"\x89PNG\r\n\x1a\n")
                or head[:4] == b"RIFF" and head[8:12] == b"WEBP"
                or head[:6] in (b"GIF87a", b"GIF89a")
                or head[4:12] in (b"ftypavif", b"ftypavis"))


def _sort_key(path: Path) -> tuple:
    return tuple((0, int(part)) if part.isdigit() else (1, part.lower()) for part in re.split(r'(\d+)', path.name))


def chapter_identity(value: str) -> str:
    from decimal import Decimal
    match = re.fullmatch(r'(?:chapter[-_ ]*)?(\d+(?:\.\d+)?)', value, re.I)
    return format(Decimal(match.group(1)).normalize(), 'f') if match else value


class OriginalLibrary:
    def __init__(self, app_dir: Path):
        self.root = app_dir / "manhwa"
        self.cache = app_dir / "data" / "tablet_companion" / "sync_cache"
        self.covers = app_dir / "data" / "reader_covers"
        self._files: dict[tuple[str, str], Path] = {}
        self._cover_files: dict[str, Path] = {}
        self._validated: dict[str, tuple[tuple, str]] = {}
        self._catalog: dict = {}
        self._catalog_signature: tuple = ()

    def verified_digest(self, file: Path) -> str:
        stat = file.stat()
        signature = (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        cached = self._validated.get(str(file))
        if cached and cached[0] == signature:
            return cached[1]
        if file.suffix == '.cbz' and not _valid_cbz(file):
            raise ValueError('CBZ integrity check failed')
        digest = _digest(file)
        self._validated[str(file)] = signature, digest
        return digest

    def catalog(self) -> dict:
        from panelyra.services.reading_catalog import catalog
        chapters, errors, files, covers = [], [], {}, {}
        with _lock:
            for folder, entry in catalog(self.root.parent).items():
                sid = entry['series_id']
                for label, chapter in entry['editions'].get('Original', {}).items():
                    if not chapter['healthy'] or not chapter['verified']:
                        errors.append({'series': entry['title'], 'chapter': label, 'error': chapter['error']})
                        continue
                    ready = chapter['artifact']
                    number = chapter_identity(label)
                    if sid not in covers:
                        supplied = next((self.covers/(folder+s) for s in ('.cover','.jpg','.png','.webp') if (self.covers/(folder+s)).is_file()), None)
                        covers[sid] = supplied if supplied and _valid_image(supplied) else (chapter['images'][0] if chapter['images'] else self._first_page(ready))
                    files[(sid, number)] = ready
                    # Legacy in-process callers can still resolve a selected folder.
                    files[(folder, number)] = ready
                    covers[folder] = covers[sid]
                    chapters.append({'series_key': sid, 'library_key': folder, 'series_title': entry['title'], 'chapter': number,
                        'legacy_series_keys': [folder] if re.fullmatch(r'[a-zA-Z0-9._-]+',folder) else [],
                        'chapter_id': chapter['id'], 'manifest_version': 4, 'edition': 'Original',
                        'language': entry['metadata'].get('language','und'), 'title': chapter['title'],
                        'size': ready.stat().st_size, 'sha256': chapter['checksum'],
                        'source_url': str(chapter['metadata'].get('url') or ''),
                        'download_url': f'/api/v2/library/chapters/{sid}/{number}',
                        'cover_url': f'/api/v2/library/covers/{sid}',
                        'cover_version': self.verified_digest(covers[sid]),
                        'cover_provenance': 'supplied' if covers[sid].parent==self.covers else 'thumbnail'})
            self._files, self._cover_files = files, covers
            self._catalog = {'version':4, 'chapters':chapters, 'errors':errors}
            return self._catalog

    def _package(self, series: str, chapter: str, images: list[Path]) -> Path:
        target = self.cache / series / chapter / "chapter-pages.cbz"
        marker = target.with_suffix(".manifest.json")
        signature = [[p.name, p.stat().st_size, p.stat().st_mtime_ns] for p in images]
        try:
            if target.is_file() and json.loads(marker.read_text()) == signature:
                self.verified_digest(target)
                return target
        except (OSError, ValueError):
            pass
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".cbz.partial")
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_STORED) as archive:
            for image in images:
                archive.write(image, image.name)
        if not _valid_cbz(temporary):
            temporary.unlink(missing_ok=True)
            raise ValueError("Packaged CBZ failed validation")
        os.replace(temporary, target)
        marker.write_text(json.dumps(signature), encoding="utf-8")
        return target

    def _first_page(self, cbz: Path) -> Path:
        target = self.cache / "covers" / f"{cbz.parent.parent.name}-{cbz.parent.name}.cover"
        target.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(cbz) as archive:
            name = sorted((name for name in archive.namelist() if not name.endswith('/')), key=lambda n: _sort_key(Path(n)))[0]
            target.write_bytes(archive.read(name))
        return target

    def chapter_file(self, series: str, chapter: str) -> Path | None:
        return self._files.get((series, chapter))

    def cover_file(self, series: str) -> Path | None:
        return self._cover_files.get(series)
