"""Provider adapters around the unchanged Capture engine."""
from __future__ import annotations
import json
import logging
import os
import shutil
import tempfile
import time
import copy
from contextlib import nullcontext
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit
import requests
from bs4 import BeautifulSoup
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]


def naver_identity(url):
    parsed = urlsplit(url)
    query = parse_qs(parsed.query)
    if parsed.hostname != "comic.naver.com" or parsed.path != "/webtoon/detail":
        return None
    title, episode = query.get("titleId", [""])[0], query.get("no", [""])[0]
    if not title.isdigit() or not episode.isdigit() or int(episode) < 1:
        raise ValueError("Naver requires titleId and a positive episode number.")
    return title, int(episode)


def naver_story_images(soup, title_id, number):
    candidates = [image.get("src") or image.get("data-src")
                  for image in soup.select('#comic_view_area img, img[id^="content_image_"]')]
    return list(dict.fromkeys(p for p in candidates if isinstance(p, str)
                and urlsplit(p).hostname == "image-comic.pstatic.net"
                and urlsplit(p).path.startswith(f"/webtoon/{title_id}/{number}/")
                and "thumbnail" not in urlsplit(p).path.lower()))


def _capture_naver_episode(args, reporter=None):
    from automation_runtime import atomic_write_json
    from panelyra.automation.quiet_updates import download_images, build_cbz, updater_lock
    from panelyra.automation.series_updates import record_downloaded_chapter
    identity = naver_identity(args.start_url)
    title_id, first = identity
    output = Path(args.output_dir) / f"naver-{title_id}"
    output.mkdir(parents=True, exist_ok=True)
    results = []
    with updater_lock():
        for offset in range(args.max_chapters):
            number = first + offset
            url = "https://comic.naver.com/webtoon/detail?" + urlencode({"titleId": title_id, "no": number})
            destination = output / str(number).zfill(2)
            if reporter:
                reporter.update(status="running", stage="capture", progress_current=len(results), progress_total=args.max_chapters, message=f"Downloading episode {number}")
            if destination.exists():
                metadata = json.loads((destination / "metadata.json").read_text())
                files = list((destination / "images").glob("*"))
                if metadata.get("url") != url or not files or len(files) != metadata.get("image_count"):
                    raise ValueError(f"Existing episode {number} needs review.")
                for image in files:
                    with Image.open(image) as decoded:
                        decoded.load()
                manifest = output / "manifest.json"
                records = json.loads(manifest.read_text()) if manifest.exists() else []
                atomic_write_json(manifest, [r for r in records if r.get("url") != url] + [metadata])
                record_downloaded_chapter(url)
                results.append(metadata)
                continue
            response = requests.get(url, timeout=30)
            response.raise_for_status()
            if urlsplit(response.url).hostname != "comic.naver.com":
                raise ValueError(f"Episode {number} requires login or redirected away from Naver.")
            soup = BeautifulSoup(response.text, "html.parser")
            pages = naver_story_images(soup, title_id, number)
            if not pages:
                raise ValueError(f"Episode {number} has no verified public story images; it may require login or payment.")
            temporary = output / f'.receiving-{number}-naver'
            if temporary.is_symlink():
                raise ValueError('Capture staging must not be a symbolic link.')
            temporary.mkdir(exist_ok=True)
            with nullcontext(temporary) as temporary:
                staged = Path(temporary) / "chapter"
                image_dir = staged / "images"
                image_dir.mkdir(parents=True, exist_ok=True)
                def fetch(item):
                    index, page = item
                    with requests.Session() as session:
                        from panelyra.services.page_transfer import fetch_page
                        from manhwa_ocr_downloader import image_extension_from_payload
                        payload = fetch_page(session, page, {'Referer':url}, 45, Path(temporary)/'http-pages')
                    target = image_dir / f"{index:03d}{image_extension_from_payload(page,payload)}"
                    target.write_bytes(payload)
                    return target
                with ThreadPoolExecutor(max_workers=4) as pool:
                    files = list(pool.map(fetch, enumerate(pages, 1)))
                current_names = {file.name for file in files}
                for file in image_dir.iterdir():
                    if file.name not in current_names and file.is_file() and file.stem.isdigit():
                        file.unlink()  # Obsolete generated staging page, never a published original.
                if not args.skip_cbz:
                    build_cbz(staged, files)
                metadata = {"index": offset + 1, "url": url, "title": soup.title.get_text() if soup.title else f"Episode {number}",
                            "series_slug": output.name, "series_title": "Choi Kang High" if title_id == "845837" else (soup.title.get_text().split(" - ")[0] if soup.title else f"Naver {title_id}"),
                            "series_url": f"https://comic.naver.com/webtoon/list?titleId={title_id}",
                            "chapter_folder": destination.name, "image_count": len(files), "saved_image_count": len(files),
                            "images_dir": str(destination / "images"), "archive_file": str(destination / "chapter-pages.cbz") if not args.skip_cbz else "",
                            "saved_at_epoch": int(time.time()), "character_count": 0, "status": "ok", "error": ""}
                atomic_write_json(staged / "metadata.json", metadata)
                (staged / "image_urls.txt").write_text("\n".join(pages) + "\n")
                from panelyra.services.library_store import LibraryStore, decode_image
                for page in files:
                    decode_image(page.read_bytes())
                if Path(args.output_dir).name == 'manhwa':
                    LibraryStore(Path(args.output_dir).parent).publish(staged, destination)
                else:
                    os.rename(staged, destination)
            # Remove only this completed adapter-owned retry workspace, never raws.
            shutil.rmtree(temporary)
            manifest = output / "manifest.json"
            records = json.loads(manifest.read_text()) if manifest.exists() else []
            records = [r for r in records if r.get("url") != url] + [metadata]
            atomic_write_json(manifest, records)
            record_downloaded_chapter(url)
            results.append(metadata)
            logging.info("Verified episode %s: %s images", number, len(files))
            if reporter:
                reporter.update(progress_current=len(results), message=f"Saved {len(results)} of {args.max_chapters} episodes")
    if len(results) != args.max_chapters:
        raise ValueError(f"Only {len(results)} of {args.max_chapters} episodes completed.")
    if reporter:
        reporter.update(status="completed", stage="completed", progress_current=len(results), message=f"All {len(results)} episodes saved", stop_reason="")
    return results


def capture_naver(args, reporter=None):
    """Bounded per-episode retries; a failed episode never stops later ones."""
    from automation_runtime import atomic_write_json
    title_id, first = naver_identity(args.start_url)
    results, failures = [], []
    for offset in range(args.max_chapters):
        number = first + offset
        single = copy.copy(args)
        single.max_chapters = 1
        single.start_url = "https://comic.naver.com/webtoon/detail?" + urlencode({"titleId": title_id, "no": number})
        if reporter and reporter.capture:
            reporter.capture.checkpoint(reporter.record.run_id, 'scanning',
                                        {'approved': True, 'chapter_index': offset + 1, 'url': single.start_url})
        if reporter:
            reporter.update(status="running", stage="capture", progress_current=len(results),
                            progress_total=args.max_chapters, message=f"Checking episode {number}; {len(results)} saved")
        for attempt in range(3):
            try:
                results.extend(_capture_naver_episode(single))
                if reporter and reporter.capture:
                    reporter.capture.checkpoint(reporter.record.run_id, 'ready',
                                                {'chapter_index': offset + 1, 'url': single.start_url})
                break
            except Exception as exc:
                logging.warning("Episode %s attempt %s failed: %s", number, attempt + 1, exc)
                permanent = isinstance(exc, ValueError)
                if permanent or attempt == 2:
                    failures.append({"episode": number, "url": single.start_url, "error": str(exc), "attempts": attempt + 1})
                    break
                time.sleep(2 ** (attempt + 1))
        if reporter:
            reporter.update(progress_current=len(results), message=f"Checked {offset + 1}/{args.max_chapters}; saved {len(results)}; failed {len(failures)}")
        atomic_write_json(Path(args.output_dir) / f"naver-{title_id}" / "capture-report.json",
                          {"requested": args.max_chapters, "checked": offset + 1, "saved": len(results),
                           "failures": failures, "updated_at": int(time.time())})
        if offset + 1 < args.max_chapters:
            time.sleep(1)
    report = {"requested": args.max_chapters, "checked": args.max_chapters, "saved": len(results), "failures": failures, "updated_at": int(time.time())}
    atomic_write_json(Path(args.output_dir) / f"naver-{title_id}" / "capture-report.json", report)
    if reporter:
        reporter.update(status="failed" if failures else "completed", stage="partial" if failures else "completed",
                        progress_current=len(results), progress_total=args.max_chapters,
                        message=f"Saved {len(results)} of {args.max_chapters}; {len(failures)} failed",
                        stop_reason="episode_failures" if failures else "")
    if failures:
        raise ValueError(f"Saved {len(results)} of {args.max_chapters}; failed episodes: {[f['episode'] for f in failures]}")
    return results


def main():
    import manhwa_ocr_downloader as legacy
    from automation_runtime import reporter_from_environment
    args = legacy.parse_args()
    legacy.configure_logging()
    reporter = reporter_from_environment()
    try:
        if naver_identity(args.start_url) and args.skip_ocr and not args.start_signal_file:
            capture_naver(args, reporter)
            return 0
        code = legacy.main()
        if naver_identity(args.start_url) and args.skip_ocr and args.start_signal_file and code == 0:
            # Legacy owns browser review and waits for the user's start signal.
            # Only after it returns do we recover its unsupported Naver navigation.
            if reporter:
                reporter = reporter_from_environment()
                if reporter.record.progress_current < 1:
                    return code
            capture_naver(args, reporter)
            return 0
        if reporter:
            reporter = reporter_from_environment()
            if reporter.record.status == "completed" and reporter.record.progress_current < args.max_chapters:
                reporter.update(status="failed", stage="partial", stop_reason="chapter_navigation_stopped", message=f"Saved {reporter.record.progress_current} of {args.max_chapters}; chapter navigation stopped early")
                return 1
        return code
    except Exception as exc:
        logging.exception("Capture adapter failed: %s", exc)
        if reporter:
            reporter.update(status="failed", stage="partial", stop_reason=str(exc), message=str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
