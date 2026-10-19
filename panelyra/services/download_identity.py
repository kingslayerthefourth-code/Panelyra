"""Chapter identities and exact-content duplicate checks. No library mutation."""
from __future__ import annotations
import hashlib
import json
import re
import unicodedata
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


def chapter_key(value) -> str:
    try:
        number = Decimal(str(value))
        if not number.is_finite() or number < 0 or number > 100000:
            raise ValueError('Invalid chapter number')
        return format(number.normalize(), 'f')
    except (InvalidOperation, ValueError):
        raise ValueError('Use numeric chapter numbers, such as 1 or 1.5.')


def expand_chapters(value: str) -> list[str]:
    chapters = set()
    if not value.strip():
        raise ValueError('Choose chapters, for example 1,3,5-10.')
    for part in value.replace(' ', '').split(','):
        if re.fullmatch(r'\d+(?:\.\d+)?', part):
            chapters.add(chapter_key(part))
        elif re.fullmatch(r'\d+-\d+', part):
            start, end = map(int, part.split('-'))
            if end < start or end - start >= 10000 or end > 100000:
                raise ValueError('Use ascending ranges of at most 10,000 chapters.')
            chapters.update(str(n) for n in range(start, end + 1))
        else:
            raise ValueError('Use whole-number ranges; enter decimal chapters individually, such as 1.5,2.5.')
        if len(chapters) > 10000:
            raise ValueError('A single durable job supports up to 10,000 chapters.')
    return sorted(chapters, key=Decimal)


def title_key(value: str) -> str:
    value = re.sub(r'\s*\(Naver\s+\d+\)', '', value, flags=re.I)
    value = re.sub(r'(?:[-\s]+English|[-\s]+clean)$', '', value, flags=re.I)
    return ' '.join(re.findall(r'\w+', unicodedata.normalize('NFKC', value).casefold())).strip()


def source_key(url: str) -> str:
    from panelyra.services.library_store import canonical_source
    return canonical_source(url)


def aliases(url: str, title: str = '', slug: str = '') -> set[str]:
    path_slug = urlsplit(source_key(url)).path.rstrip('/').rsplit('/', 1)[-1]
    return {v for v in (title_key(title), title_key(slug), title_key(path_slug)) if v and not v.isdigit() and len(v) > 1}


def language_of(meta: dict, folder: str) -> str:
    explicit = str(meta.get('language') or meta.get('lang') or '').lower()
    if explicit:
        return explicit.split('-')[0]
    url = str(meta.get('series_url') or meta.get('url') or '')
    if 'english' in folder.casefold():
        return 'en'
    if 'naver.com' in url or folder.startswith('naver-'):
        return 'ko'
    from panelyra.services.title_search import SOURCES
    if urlsplit(url).hostname in {urlsplit(u).hostname for u in SOURCES.values()} or '/en/' in url:
        return 'en'
    return 'und'


def library_chapters(output_dir: Path):
    if not output_dir.exists():
        return
    for series in output_dir.iterdir():
        if not series.is_dir() or series.is_symlink() or series.name.startswith('.'):
            continue
        for chapter in series.iterdir():
            if not chapter.is_dir() or chapter.is_symlink() or not re.fullmatch(r'\d+(?:\.\d+)?', chapter.name):
                continue
            try:
                meta = json.loads((chapter / 'metadata.json').read_text())
            except (OSError, ValueError):
                meta = {}
            if not isinstance(meta, dict):
                meta = {}
            images = [p for p in (chapter / 'images').glob('*') if p.is_file() and not p.is_symlink() and p.suffix.lower() in {'.png','.jpg','.jpeg','.webp','.avif','.gif'} and p.stat().st_size]
            expected = meta.get('image_count') or meta.get('saved_image_count') or len(images)
            archive = chapter / 'chapter-pages.cbz'
            archive_info = None
            if not images and archive.is_file() and not archive.is_symlink():
                info = archive.stat()
                archive_info = _archive_info(str(archive), info.st_size, info.st_mtime_ns)
            from panelyra.services.reading_catalog import image_size
            complete = bool(meta) and meta.get('identity_verified') is not False and isinstance(expected, int) and expected > 0 and (bool(archive_info) and archive_info[0] == expected or bool(images) and len(images) == expected and all(all(image_size(p)) for p in images))
            yield {'path':chapter,'number':chapter_key(chapter.name),'meta':meta,'images':images,'archive_info':archive_info,'complete':complete,'language':language_of(meta,series.name),'aliases':aliases(meta.get('series_url',''),meta.get('series_title',''),series.name),'source':source_key(meta.get('series_url',''))}


def existing_chapters(output_dir: Path, series_url: str, title: str = '', language: str = 'en', slug: str = '') -> dict:
    from panelyra.services.library_store import LibraryStore, connect
    store = LibraryStore(output_dir.parent)
    store.scan()
    with connect(store.path) as db:
        rows = db.execute('''SELECT c.number,c.locator FROM chapters c JOIN sources s ON s.series_id=c.series_id
            WHERE s.identity=? AND s.language IN (?, 'und') AND c.edition='Original' AND c.status='Ready' ''',
            (source_key(series_url),language)).fetchall()
    return {r['number']: str(output_dir.parent/r['locator']) for r in rows if (output_dir.parent/r['locator']).is_dir()}


@lru_cache(maxsize=16000)
def _file_hash(path: str, size: int, modified: int) -> bytes:
    return hashlib.sha256(Path(path).read_bytes()).digest()


def page_fingerprint(pages) -> str:
    return hashlib.sha256(b''.join(hashlib.sha256(content).digest() for _, content in pages)).hexdigest()


@lru_cache(maxsize=512)
def _archive_info(path: str, size: int, modified: int):
    from panelyra.automation.manga_backend import archive_pages
    try:
        pages = archive_pages(Path(path))
        return (len(pages), page_fingerprint(pages))
    except Exception:
        return None


def duplicate_content(output_dir: Path, pages) -> Path | None:
    fingerprint = page_fingerprint(pages)
    for row in library_chapters(output_dir):
        if row['archive_info'] == (len(pages), fingerprint):
            return row['path']
        if len(row['images']) != len(pages):
            continue
        hashes = []
        for p in row['images']:
            info = p.stat()
            hashes.append(_file_hash(str(p), info.st_size, info.st_mtime_ns))
        if hashlib.sha256(b''.join(hashes)).hexdigest() == fingerprint:
            return row['path']
    return None
