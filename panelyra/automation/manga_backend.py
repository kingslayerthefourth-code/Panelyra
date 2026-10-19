"""Optional external downloader. Stages archives; never mutates the legacy engine."""
from __future__ import annotations

import hashlib
import io
import platform
import os
import signal
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
import zipfile
from pathlib import Path

import requests
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
VERSION = "1.9.0"
SOURCE = "https://github.com/elboletaire/manga-downloader"
INSTALL = ROOT / "vendor" / "manga-downloader" / VERSION
BINARY = INSTALL / "manga-downloader"
DIGESTS = {
    "arm64": "2e2380c93041c90a507b64b4cde420f28402187e1b5d79bdc665dcd3710f5169",
    "x86_64": "f965c272f00ad8ac45194af8ab92f1f71c9166fc2fcbdadd6d6458fe2cec9861",
}


def install_backend() -> Path:
    """Install only the pinned, checksum-verified official macOS release."""
    if BINARY.is_file():
        return BINARY
    machine = platform.machine()
    if platform.system() != "Darwin" or machine not in DIGESTS:
        raise ValueError("This installer currently supports Apple Silicon and Intel Macs only.")
    arch = "arm64" if machine == "arm64" else "amd64"
    url = f"{SOURCE}/releases/download/v{VERSION}/manga-downloader-v{VERSION}-darwin-{arch}.tar.gz"
    response = requests.get(url, timeout=120)
    response.raise_for_status()
    if hashlib.sha256(response.content).hexdigest() != DIGESTS[machine]:
        raise ValueError("Release checksum mismatch; nothing was installed.")
    INSTALL.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=INSTALL.parent) as temp:
        staged = Path(temp)
        with tarfile.open(fileobj=io.BytesIO(response.content), mode="r:gz") as archive:
            for member in archive.getmembers():
                name = Path(member.name).name
                if member.isfile() and name in {"manga-downloader", "LICENSE", "README.md"}:
                    if member.size > 150_000_000:
                        raise ValueError("Unexpected release member size.")
                    stream = archive.extractfile(member)
                    if stream:
                        (staged / name).write_bytes(stream.read())
        executable = staged / "manga-downloader"
        if not executable.is_file():
            raise ValueError("Official release did not contain the expected executable.")
        executable.chmod(0o755)
        INSTALL.mkdir(exist_ok=True)
        for item in staged.iterdir():
            shutil.copy2(item, INSTALL / item.name)
    return BINARY


def build_command(series_url: str, chapters: str, output: Path, *, browser_consent: bool) -> list[str]:
    from urllib.parse import urlsplit
    parsed = urlsplit(series_url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Enter a public series landing-page URL, without credentials.")
    if not browser_consent:
        raise ValueError("This backend may open Chrome; explicit permission is required.")
    chapters = chapters.replace(" ", "")
    from panelyra.services.download_identity import expand_chapters
    expand_chapters(chapters)
    return [str(BINARY), series_url.strip(), chapters, "--output-dir", str(output),
            "--filename-template", "{{.Number}}{{if gt .Version 1}}-v{{.Version}}{{end}}",
            "--format", "cbz", "--convert-images", "none", "--concurrency", "1",
            "--concurrency-pages", "4", "--retry", "2", "--language", "en"]


def archive_pages(archive: Path) -> list[tuple[str, bytes]]:
    from panelyra.services.library_store import validate_archive
    return validate_archive(archive, strict_sequence=True)


def run_staged(series_url: str, chapters: str, staging: Path, *, browser_consent: bool) -> int:
    from automation_runtime import acquire_browser_lock, release_browser_lock
    from manhwa_ocr_downloader import DOWNLOAD_BROWSER_LOCK
    from panelyra.automation.quiet_updates import updater_lock
    command = build_command(series_url, chapters, staging, browser_consent=browser_consent)
    if not BINARY.is_file():
        raise ValueError("Install the optional backend first.")
    handles = []
    process = None
    owner_lease=None;watcher=None
    previous_handler = signal.getsignal(signal.SIGTERM)
    def stop_requested(signum, frame):
        signal.signal(signal.SIGTERM,signal.SIG_IGN)
        raise SystemExit(128 + signum)
    try:
        signal.signal(signal.SIGTERM, stop_requested)
        engine_root=os.environ.get('PANELYRA_ENGINE_ROOT','')
        if engine_root:
            from panelyra.services.worker_runtime import worker_lease, watch_owner
            owner_lease=worker_lease(Path(engine_root),'backend');owner_lease.__enter__()
            watcher=watch_owner(Path(engine_root),lambda:os.kill(os.getpid(),signal.SIGTERM))
        with updater_lock():
            handles.append(acquire_browser_lock("manga-downloader"))
            handles.append(acquire_browser_lock("manga-downloader", DOWNLOAD_BROWSER_LOCK))
            staging.mkdir(parents=True, exist_ok=True)
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, start_new_session=True)
            while process.poll() is None:
                changed = max((p.stat().st_mtime for p in staging.rglob('*') if p.is_file()), default=time.time())
                if time.time()-changed > 900:
                    raise TimeoutError('No source progress for 15 minutes. Completed work was retained.')
                time.sleep(.3)
            code = process.returncode
            # Exit zero is NOT proof of success: upstream sometimes skips failed chapters.
            return code
    finally:
        if watcher:watcher.set()
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=5)
            except ProcessLookupError:
                pass
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
        signal.signal(signal.SIGTERM, previous_handler)
        for handle in reversed(handles):
            release_browser_lock(handle)
        if owner_lease:owner_lease.__exit__(None,None,None)


def import_reviewed_archive(archive: Path, series_url: str, chapter_url: str,
                            series_slug: str, *, output_dir: Path = ROOT / "manhwa",
                            history_file: Path | None = None, title: str = "",
                            verified_number: str | None = None) -> Path:
    """User-approved import; source mapping is explicit because upstream emits no metadata."""
    import os
    import time
    from urllib.parse import urlsplit
    from automation_runtime import atomic_write_json
    from panelyra.automation.quiet_updates import chapter_folder, updater_lock, _write_manifest
    from panelyra.automation.series_updates import (
        chapter_number, canonical_history_url, record_downloaded_chapter, DEFAULT_HISTORY_FILE,
    )
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", series_slug):
        raise ValueError("Choose a lowercase library folder name, for example my-series.")
    number = chapter_number(chapter_url)
    if verified_number is not None:
        from panelyra.services.download_identity import chapter_key
        number = float(chapter_key(verified_number))
    if number is None or not re.fullmatch(r"\d+(?:\.\d+)?", archive.stem) or float(archive.stem) != number:
        raise ValueError("The chapter URL number must match the archive. Alternate versions require manual review.")
    source, chapter = urlsplit(series_url), urlsplit(chapter_url)
    if chapter.scheme not in {"http", "https"} or not chapter.hostname or chapter.hostname != source.hostname or chapter.username or chapter.password:
        raise ValueError("Confirm a chapter URL on the same source site.")
    chapter_url = canonical_history_url(chapter_url)
    pages = archive_pages(archive)
    with updater_lock():
        from panelyra.services.download_identity import existing_chapters, duplicate_content, chapter_key, page_fingerprint
        if chapter_key(number) in existing_chapters(output_dir, series_url, title, slug=series_slug):
            raise ValueError("This chapter is already in Library, including under another folder name.")
        # Content hashes identify versions, not books. Identical bytes in distinct
        # chapters are allowed; identity-scoped checks above prevent repeat imports.
        output_dir.mkdir(parents=True, exist_ok=True)
        series_dir = output_dir / series_slug
        if series_dir.is_symlink():
            raise ValueError("A linked library folder cannot be imported into.")
        series_dir.mkdir(exist_ok=True)
        destination = series_dir / chapter_folder(number, chapter_url)
        if destination.exists() or destination.is_symlink():
            raise ValueError("This chapter already exists; nothing was overwritten.")
        with tempfile.TemporaryDirectory(prefix=".manga-import-", dir=series_dir) as temp:
            staged = Path(temp) / "chapter"
            images = staged / "images"
            images.mkdir(parents=True)
            for name, content in pages:
                (images / name).write_bytes(content)
            shutil.copy2(archive, staged / "chapter-pages.cbz")
            metadata = {
                "index": 1, "series_title": title or series_slug.replace("-", " ").title(),
                "language": "en", "page_fingerprint": page_fingerprint(pages),
                "series_slug": series_slug, "series_dir": str(series_dir),
                "series_url": series_url, "chapter_folder": destination.name,
                "url": chapter_url, "title": f"Chapter {number:g}",
                "image_count": len(pages), "saved_image_count": len(pages),
                "images_dir": str(destination / "images"),
                "archive_file": str(destination / "chapter-pages.cbz"),
                "saved_at_epoch": int(time.time()), "reader_layout": "manga-downloader",
                "character_count": 0,
            }
            atomic_write_json(staged / "metadata.json", metadata)
            from panelyra.services.library_store import LibraryStore
            LibraryStore(output_dir.parent).publish(staged, destination)
            try:
                _write_manifest(series_dir, dict(metadata, status="ok", error="", text_file=""))
            except Exception:
                os.rename(destination, series_dir / f".failed-import-{time.time_ns()}")
                raise
            record_downloaded_chapter(chapter_url, history_file or DEFAULT_HISTORY_FILE)
        return destination


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--series-url", required=True)
    parser.add_argument("--chapters", required=True)
    parser.add_argument("--staging", type=Path, required=True)
    parser.add_argument("--allow-browser", action="store_true")
    args = parser.parse_args()
    from automation_runtime import BrowserBusyError
    try:
        code=run_staged(args.series_url, args.chapters, args.staging, browser_consent=args.allow_browser)
    except BrowserBusyError:
        code=75  # Temporary local resource contention, not a failed source.
    raise SystemExit(code)
