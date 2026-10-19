"""Local foundation: transactional identities, ordered artifacts and recoverable index.

The original library is input, never a disposable cache. SQLite is rebuildable;
downloads become readable only after bounded full decoding and a committed index.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import sqlite3
import stat
import time
import uuid
import zipfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from PIL import Image

IMAGE_SUFFIXES = {'.jpg', '.jpeg', '.png', '.webp', '.gif', '.avif'}
MAX_PAGE_BYTES = 50_000_000
MAX_CHAPTER_BYTES = 1_000_000_000
MAX_PAGES = 2000
MAX_PIXELS = 32_000_000
NAMESPACE = uuid.UUID('3e32d8ba-af96-42d6-9b68-5b4df928d026')


def natural_key(value):
    return tuple((1, int(s)) if s.isdigit() else (0, s.casefold())
                 for s in re.split(r'(\d+)', str(value)))


def canonical_source(url):
    """Provider identity, not a display title, episode or tracking query."""
    p = urlsplit(str(url).strip())
    if not p.hostname:
        return ''
    host = p.hostname.lower()
    path = p.path.rstrip('/')
    pairs = dict(parse_qsl(p.query))
    if host == 'comic.naver.com':
        title = pairs.get('titleId')
        return f'https://{host}/webtoon/list?titleId={title}' if title else ''
    ids = [(k, v) for k, v in pairs.items() if k.lower() in {'title_no', 'series_id', 'manga_id', 'titleid'}]
    path = re.sub(r'[-_/](?:chapter|episode|ep)[-_ /]?\d+(?:[.-]\d+)?/?$', '', path, flags=re.I)
    if '/comics/' in path:
        path = re.sub(r'-[a-f0-9]{8}$', '', path)
    return urlunsplit(('https', host, path, urlencode(sorted(ids)), ''))


def stable_id(*parts):
    return uuid.uuid5(NAMESPACE, '\x1f'.join(map(str, parts))).hex


class _Connection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


def connect(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), timeout=30, factory=_Connection)
    path.chmod(0o600)
    version = db.execute('PRAGMA user_version').fetchone()[0]
    if version > 4:
        db.close()
        raise ValueError('This library was created by a newer Panelyra. Restore a compatible app; the database was preserved.')
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA busy_timeout=30000')
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA synchronous=FULL')
    db.execute('PRAGMA foreign_keys=ON')
    db.executescript('''
    CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS series(
        id TEXT PRIMARY KEY, title TEXT NOT NULL, language TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS locators(
        locator TEXT PRIMARY KEY, series_id TEXT NOT NULL REFERENCES series(id));
    CREATE TABLE IF NOT EXISTS sources(
        identity TEXT NOT NULL, language TEXT NOT NULL,
        series_id TEXT NOT NULL REFERENCES series(id),
        PRIMARY KEY(identity,language));
    CREATE TABLE IF NOT EXISTS chapters(
        id TEXT PRIMARY KEY, series_id TEXT NOT NULL REFERENCES series(id),
        edition TEXT NOT NULL, number TEXT NOT NULL, locator TEXT NOT NULL UNIQUE,
        status TEXT NOT NULL, error TEXT NOT NULL DEFAULT '', signature TEXT NOT NULL,
        artifact TEXT NOT NULL DEFAULT '', checksum TEXT NOT NULL DEFAULT '',
        pages TEXT NOT NULL DEFAULT '[]', metadata TEXT NOT NULL DEFAULT '{}',
        updated REAL NOT NULL, UNIQUE(series_id,edition,number));
    CREATE TABLE IF NOT EXISTS progress(
        key TEXT PRIMARY KEY, payload TEXT NOT NULL, updated REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS jobs(
        id TEXT PRIMARY KEY, payload TEXT NOT NULL, updated REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS commits(
        id TEXT PRIMARY KEY, destination TEXT NOT NULL, staged TEXT NOT NULL,
        status TEXT NOT NULL, updated REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS events(
        sequence INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
        payload TEXT NOT NULL, created REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS discovery_jobs(
        id TEXT PRIMARY KEY, payload TEXT NOT NULL, created REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS discovery_results(
        series TEXT PRIMARY KEY, payload TEXT NOT NULL, updated REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS transfers(
        id TEXT PRIMARY KEY, transport TEXT NOT NULL, target_pin TEXT NOT NULL,
        created REAL NOT NULL, updated REAL NOT NULL, status TEXT NOT NULL,
        total_bytes INTEGER NOT NULL, received_bytes INTEGER NOT NULL DEFAULT 0,
        bytes_per_second INTEGER NOT NULL DEFAULT 0, eta_seconds INTEGER NOT NULL DEFAULT 0,
        error TEXT NOT NULL DEFAULT '');
    CREATE TABLE IF NOT EXISTS transfer_chapters(
        transfer_id TEXT NOT NULL REFERENCES transfers(id) ON DELETE CASCADE,
        series_key TEXT NOT NULL, chapter TEXT NOT NULL, position INTEGER NOT NULL,
        manifest TEXT NOT NULL, file_path TEXT NOT NULL, cover_path TEXT NOT NULL,
        acknowledged INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(transfer_id,series_key,chapter), UNIQUE(transfer_id,position));
    CREATE TABLE IF NOT EXISTS migration_issues(
        scope TEXT NOT NULL, origin TEXT NOT NULL, error TEXT NOT NULL,
        created REAL NOT NULL, PRIMARY KEY(scope,origin));
    CREATE TABLE IF NOT EXISTS capture_runs(
        id TEXT PRIMARY KEY, record TEXT NOT NULL, command TEXT NOT NULL DEFAULT '[]',
        checkpoint TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL, updated REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS capture_chapters(
        run_id TEXT NOT NULL REFERENCES capture_runs(id), position INTEGER NOT NULL,
        url TEXT NOT NULL, state TEXT NOT NULL, artifact TEXT NOT NULL DEFAULT '',
        updated REAL NOT NULL, PRIMARY KEY(run_id,position));
    CREATE INDEX IF NOT EXISTS capture_run_status ON capture_runs(status,updated);
    ''')
    if version < 4:db.execute('PRAGMA user_version=4')
    return db


@contextmanager
def transaction(path):
    db = connect(path)
    try:
        db.execute('BEGIN IMMEDIATE')
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def db_path(root):
    return Path(root)/'data'/'foundation.sqlite3'


def event(db, kind, payload):
    db.execute('INSERT INTO events(kind,payload,created) VALUES(?,?,?)',
               (kind, json.dumps(payload, ensure_ascii=False), time.time()))


def decode_image(content):
    if len(content) > MAX_PAGE_BYTES:
        raise ValueError('Page exceeds the 50 MB encoded limit.')
    with Image.open(io.BytesIO(content)) as im:
        w, h = im.size
        if min(w, h) < 1 or max(w, h) > 100000 or w*h > MAX_PIXELS:
            raise ValueError('Page exceeds the 32 megapixel decode limit.')
        # verify() checks structure, not JPEG pixel completeness. load() is required.
        im.load()
        return w, h


def validate_archive(path, *, strict_sequence=False):
    """Return ordered, fully decoded pages; reject unsafe/ambiguous ZIP entries."""
    pages, seen, numbers = [], set(), set()
    with zipfile.ZipFile(path) as archive:
        members = archive.infolist()
        if not members or len(members) > MAX_PAGES or sum(m.file_size for m in members) > MAX_CHAPTER_BYTES:
            raise ValueError('Empty or oversized chapter archive.')
        for member in sorted(members, key=lambda m: natural_key(m.filename)):
            name = member.filename
            pure = PurePosixPath(name)
            if (member.is_dir() or pure.is_absolute() or '..' in pure.parts or '\\' in name
                    or len(pure.parts) != 1 or pure.suffix.lower() not in IMAGE_SUFFIXES
                    or stat.S_ISLNK(member.external_attr >> 16)
                    or member.flag_bits & 1 or name.casefold() in seen
                    or member.file_size > MAX_PAGE_BYTES):
                raise ValueError(f'Unsafe, duplicate or unsupported archive entry: {name}')
            seen.add(name.casefold())
            if strict_sequence:
                if not pure.stem.isdigit() or int(pure.stem) in numbers:
                    raise ValueError('Duplicate or unnumbered archive pages.')
                numbers.add(int(pure.stem))
            content = archive.read(member)  # also verifies CRC
            decode_image(content)
            pages.append((name, content))
        if strict_sequence and numbers != set(range(len(pages))):
            raise ValueError('The archive has missing pages (expected 0 through N-1).')
    return pages


def atomic_json(path, value):
    path = Path(path)
    temp = path.with_name('.'+path.name+'.'+uuid.uuid4().hex+'.tmp')
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with temp.open('w') as f:
            json.dump(value, f, ensure_ascii=False)
            f.flush(); os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def _prepared_matches(path, pages):
    try:
        with zipfile.ZipFile(path) as z:
            if z.namelist()!=[name for name,_ in pages]:return False
            for name,content in pages:
                info=z.getinfo(name)
                if info.file_size!=len(content):return False
                digest=hashlib.sha256()
                with z.open(name) as stream:
                    for block in iter(lambda:stream.read(1024*1024),b''):digest.update(block)
                if digest.digest()!=hashlib.sha256(content).digest():return False
        return True
    except (OSError,ValueError,zipfile.BadZipFile):return False


def _write_prepared(path, pages):
    with zipfile.ZipFile(path,'w',compression=zipfile.ZIP_STORED) as z:
        for name,data in pages:
            z.writestr(zipfile.ZipInfo(name,(1980,1,1,0,0,0)),data)


def materialize(root, artifact, checksum):
    """Disposable page cache with a persisted exact mapping; never deletes books."""
    cache = Path(root)/'data'/'page_cache'/checksum
    marker = cache/'pages.json'
    try:
        mapping = json.loads(marker.read_text())
        files = [cache/p['name'] for p in mapping]
        if all(p.is_file() and p.stat().st_size == m['bytes'] for p, m in zip(files, mapping)):
            os.utime(marker, None)
            return files, [tuple(m['dimensions']) for m in mapping]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    cache.mkdir(parents=True, exist_ok=True)
    mapping = []
    for index, (name, content) in enumerate(validate_archive(artifact)):
        filename = f'{index:05d}'+Path(name).suffix.lower()
        target = cache/filename
        temp = target.with_suffix(target.suffix+'.tmp-'+uuid.uuid4().hex)
        temp.write_bytes(content)
        os.replace(temp, target)
        mapping.append({'name': filename, 'bytes': len(content), 'dimensions': decode_image(content)})
    atomic_json(marker, mapping)
    evict_cache(Path(root)/'data'/'page_cache', checksum)
    return [cache/m['name'] for m in mapping], [tuple(m['dimensions']) for m in mapping]


def evict_cache(base, keep='', budget=512_000_000):
    import shutil
    folders = []
    for folder in base.iterdir() if base.is_dir() else []:
        if folder.is_dir() and not folder.is_symlink() and re.fullmatch('[a-f0-9]{64}', folder.name):
            files = list(folder.iterdir())
            folders.append((max((p.stat().st_mtime for p in files), default=0), folder, sum(p.stat().st_size for p in files if p.is_file())))
    total = sum(size for _, _, size in folders)
    for _, folder, size in sorted(folders):
        if total <= budget:break
        if folder.name != keep:
            shutil.rmtree(folder);total -= size


def cached_paths(root, artifact, checksum, mapping):
    cache = Path(root)/'data'/'page_cache'/checksum
    files = [cache/(f'{i:05d}'+Path(p['name']).suffix.lower()) for i,p in enumerate(mapping)]
    # Only the thumbnail is needed in the library. Opening a title materializes
    # that chapter, not every downloaded book in the catalog.
    if files and not files[0].is_file():
        cache.mkdir(parents=True,exist_ok=True)
        with zipfile.ZipFile(artifact) as z:
            content = z.read(mapping[0]['name'])
        temp = files[0].with_name('.'+uuid.uuid4().hex+'.tmp')
        temp.write_bytes(content);os.replace(temp,files[0])
    return files, [tuple(p['dimensions']) for p in mapping]


class LibraryStore:
    def __init__(self, root, storage_root=None):
        self.root = Path(root).resolve()
        self.storage_root = Path(storage_root).resolve() if storage_root else self.root
        self.path = db_path(self.storage_root)

    def _identity(self, db, locator, meta, folder):
        from panelyra.services.download_identity import language_of
        language = language_of(meta, folder)
        source = canonical_source(meta.get('series_url') or meta.get('url') or '')
        row = db.execute('SELECT series_id FROM locators WHERE locator=?', (locator,)).fetchone()
        if row:
            if source:
                db.execute('INSERT OR IGNORE INTO sources VALUES(?,?,?)', (source,language,row[0]))
            return row[0]
        mapped = db.execute('SELECT series_id FROM sources WHERE identity=? AND language=?', (source, language)).fetchone() if source else None
        identity = mapped[0] if mapped else stable_id(source or locator, language)
        db.execute('INSERT OR IGNORE INTO series VALUES(?,?,?)',
                   (identity, str(meta.get('series_title') or folder.replace('-', ' ').title()), language))
        db.execute('INSERT INTO locators VALUES(?,?)', (locator, identity))
        if source:
            db.execute('INSERT OR IGNORE INTO sources VALUES(?,?,?)', (source, language, identity))
        return identity

    def index_chapter(self, directory, edition, folder, signature_cache=None):
        from panelyra.services.download_identity import chapter_key
        directory = Path(directory).resolve()
        locator = str(directory.relative_to(self.root))
        meta_path = directory/'metadata.json'
        try:
            if meta_path.is_symlink():raise ValueError('Linked metadata is not trusted.')
            raw = meta_path.read_bytes()
            meta = json.loads(raw)
            if not isinstance(meta, dict):
                raise ValueError('Invalid metadata object.')
        except (OSError, ValueError) as exc:
            raw, meta = b'', {}
        images = sorted((p for p in (directory/'images').glob('*') if p.is_file() and not p.is_symlink() and p.suffix.lower() in IMAGE_SUFFIXES), key=lambda p: natural_key(p.name))
        archives = sorted(p for p in directory.glob('*.cbz') if p.is_file() and not p.is_symlink())
        if not images and not archives:
            return
        files = [archives[0]] if archives else images
        signature = hashlib.sha256(raw+json.dumps([(p.name, p.stat().st_size, p.stat().st_mtime_ns, p.stat().st_ctime_ns) for p in files]).encode()).hexdigest()
        def reusable(old):
            if not old or old['signature']!=signature:return False
            if old['status']!='Ready':return True
            try:
                info=Path(old['artifact']).stat()
                return json.loads(old['metadata']).get('_artifact_signature')==[info.st_size,info.st_mtime_ns,info.st_ctime_ns]
            except (OSError,ValueError):return False
        if signature_cache is not None:
            if reusable(signature_cache.get(locator)):
                return
        else:
            with connect(self.path) as db:
                old = db.execute('SELECT signature,status,artifact,metadata FROM chapters WHERE locator=?', (locator,)).fetchone()
                if reusable(old):
                    return
        artifact, checksum, mapping, error = '', '', [], ''
        state = 'Ready'
        try:
            number = chapter_key(directory.name)
            expected = meta.get('saved_image_count') or meta.get('image_count')
            if not meta or not isinstance(expected, int) or expected < 1 or meta.get('identity_verified') is False:
                raise LookupError('Needs confirmation: completion metadata or chapter identity is missing.')
            if archives:
                pages = validate_archive(archives[0])
                artifact = str(archives[0])
            else:
                if sum(p.stat().st_size for p in images) > MAX_CHAPTER_BYTES or len(images) > MAX_PAGES:
                    raise ValueError('Chapter exceeds resource limits.')
                pages = [(p.name, p.read_bytes()) for p in images]
                for _, data in pages:
                    decode_image(data)
            if len(pages) != expected:
                raise ValueError(f'Expected {expected} pages, found {len(pages)}.')
            if not artifact:
                # Prepare without mutating source files; publication of the artifact is atomic.
                pages = [(f'{i:05d}'+Path(name).suffix.lower(), data) for i,(name,data) in enumerate(pages)]
                key = hashlib.sha256(b''.join(name.encode()+hashlib.sha256(b).digest() for name, b in pages)).hexdigest()
                artifact_path = self.storage_root/'data'/'artifacts'/(key+'.cbz')
                artifact_path.parent.mkdir(parents=True, exist_ok=True)
                if not artifact_path.exists():
                    temp = artifact_path.with_name('.'+uuid.uuid4().hex+'.cbz')
                    _write_prepared(temp,pages)
                    with temp.open('rb') as f:
                        os.fsync(f.fileno())
                    os.replace(temp, artifact_path)
                elif not _prepared_matches(artifact_path,pages):
                    # A damaged prepared copy is disposable; originals stay intact.
                    temp=artifact_path.with_name('.'+uuid.uuid4().hex+'.cbz')
                    _write_prepared(temp,pages);os.replace(temp,artifact_path)
                artifact = str(artifact_path)
            checksum = hashlib.sha256(Path(artifact).read_bytes()).hexdigest()
            info=Path(artifact).stat()
            meta['_artifact_signature']=[info.st_size,info.st_mtime_ns,info.st_ctime_ns]
            mapping = [{'name': name, 'dimensions': decode_image(data)} for name, data in pages]
        except LookupError as exc:
            number, state, error = directory.name, 'Needs confirmation', str(exc)
        except Exception as exc:
            number, state, error = directory.name, 'Damaged', str(exc)[:500]
        with transaction(self.path) as db:
            series_locator = str(directory.parent.relative_to(self.root)).replace('manhwa_clean/', 'manhwa/').removesuffix('-clean')
            sid = self._identity(db, series_locator, meta, folder)
            cid = stable_id(sid, edition, number)
            collision = db.execute('SELECT locator FROM chapters WHERE id=?', (cid,)).fetchone()
            if collision and collision[0] != locator:
                # Do not silently collapse alternate downloads. Preserve and report both.
                cid, state, error = stable_id(cid, locator), 'Needs confirmation', 'Alternate copy of the same chapter identity.'
                number = number+'@'+stable_id(locator)[:8]
            db.execute('''INSERT INTO chapters VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(locator) DO UPDATE SET status=excluded.status,error=excluded.error,
                signature=excluded.signature,artifact=excluded.artifact,checksum=excluded.checksum,
                pages=excluded.pages,metadata=excluded.metadata,updated=excluded.updated''',
                (cid, sid, edition, number, locator, state, error, signature, artifact, checksum,
                 json.dumps(mapping), json.dumps(meta), time.time()))
            event(db, 'indexed', {'chapter': cid, 'state': state, 'reason': error})

    def scan(self):
        with connect(self.path) as db:
            signatures = {row['locator']:row for row in db.execute('SELECT locator,signature,status,artifact,metadata FROM chapters').fetchall()}
        for edition, base in [('Original', self.root/'manhwa'), ('Clean', self.root/'manhwa_clean')]:
            if not base.is_dir():
                continue
            for folder in sorted(base.iterdir(), key=lambda p: natural_key(p.name)):
                if not folder.is_dir() or folder.is_symlink() or folder.name.startswith('.'):
                    continue
                for directory in sorted(folder.iterdir(), key=lambda p: natural_key(p.name)):
                    if directory.is_dir() and not directory.is_symlink() and not directory.name.startswith('.') and not (directory/'images').is_symlink():
                        self.index_chapter(directory, edition, folder.name, signatures)

    def catalog(self):
        self.scan()
        result = {}
        with connect(self.path) as db:
            rows = db.execute('SELECT c.*,s.title,s.language FROM chapters c JOIN series s ON s.id=c.series_id ORDER BY c.locator').fetchall()
        for row in rows:
            directory = self.root/row['locator']
            # Never delete an index record or a tablet book because an original is offline.
            if not directory.is_dir():
                continue
            key = directory.parent.name.removesuffix('-clean')
            meta = json.loads(row['metadata'])
            ready = row['status'] == 'Ready'
            images, dimensions = [], []
            if ready:
                # Image-folder sources need no extraction. CBZ-only artifacts use cache on open.
                images, dimensions = cached_paths(self.storage_root, row['artifact'], row['checksum'], json.loads(row['pages']))
            entry = result.setdefault(key, {'series_key': key, 'series_id': row['series_id'], 'title': row['title'], 'editions': {}, 'metadata': {}})
            if meta.get('series_title'):
                entry['title'] = meta['series_title']
            for field in ('synopsis', 'description', 'author', 'genres', 'status', 'language', 'series_url'):
                if meta.get(field):
                    entry['metadata'].setdefault(field, meta[field])
            entry['editions'].setdefault(row['edition'], {})[directory.name] = {
                'id': row['id'], 'series_id': row['series_id'], 'images': images, 'dimensions': dimensions,
                'title': str(meta.get('title') or f'Chapter {directory.name}'), 'chapter_dir': directory,
                'metadata': meta, 'number': float(row['number']) if '@' not in row['number'] and re.fullmatch(r'\d+(?:\.\d+)?', row['number']) else None,
                'verified': ready, 'healthy': ready, 'status': row['status'], 'error': row['error'],
                'artifact': Path(row['artifact']) if row['artifact'] else None, 'checksum': row['checksum']}
        return result

    def readable_chapter(self, edition, series, label):
        """Resolve one chapter for media requests; do not rescan every book/page."""
        from panelyra.services.download_identity import chapter_key
        if edition not in {'Original','Clean'} or any(Path(str(v)).name!=v or v in {'.','..',''} for v in (series,label)):
            return None
        try:number=chapter_key(label)
        except ValueError:return None
        locator='manhwa/'+series.removesuffix('-clean')
        def lookup():
            with connect(self.path) as db:
                return db.execute('''SELECT c.* FROM chapters c JOIN locators l ON l.series_id=c.series_id
                    WHERE l.locator=? AND c.edition=? AND c.number=?''',(locator,edition,number)).fetchone()
        row=lookup()
        if not row:
            self.scan();row=lookup()
        if not row:return None
        directory=self.root/row['locator']
        if not directory.is_dir():return None
        self.index_chapter(directory,edition,directory.parent.name)
        row=lookup()
        if row['status']!='Ready':return None
        files,dimensions=materialize(self.storage_root,row['artifact'],row['checksum'])
        meta=json.loads(row['metadata'])
        return {'chapter_dir':directory,'images':files,'dimensions':dimensions,
                'title':str(meta.get('title') or f'Chapter {directory.name}'),
                'artifact':row['artifact'],'checksum':row['checksum']}

    def publish(self, staged, destination, fault=None):
        """Durable intent + atomic directory rename + restart-recoverable indexing."""
        staged, destination = Path(staged).resolve(), Path(destination).resolve()
        if destination.exists() or destination.is_symlink():
            raise ValueError('Chapter already exists; no original was overwritten.')
        identity = uuid.uuid4().hex
        with transaction(self.path) as db:
            db.execute('INSERT INTO commits VALUES(?,?,?,?,?)', (identity, str(destination), str(staged), 'prepared', time.time()))
        if fault:
            fault('prepared')
        os.rename(staged, destination)
        if fault:
            fault('renamed')
        self.index_chapter(destination, 'Original', destination.parent.name)
        with transaction(self.path) as db:
            db.execute("UPDATE commits SET status='committed',updated=? WHERE id=?", (time.time(), identity))
        return destination

    def recover(self):
        with connect(self.path) as db:
            pending = db.execute("SELECT * FROM commits WHERE status='prepared'").fetchall()
        for row in pending:
            destination, staged = Path(row['destination']), Path(row['staged'])
            if destination.is_dir():
                self.index_chapter(destination, 'Original', destination.parent.name)
                state = 'committed'
            elif staged.is_dir():
                # Still staged is safe to retry; never publish without caller validation.
                state = 'interrupted'
            else:
                state = 'failed'
            with transaction(self.path) as db:
                db.execute('UPDATE commits SET status=?,updated=? WHERE id=?', (state, time.time(), row['id']))
