#!/usr/bin/env python3
"""Regression checks for quiet, transactional library updates."""

from __future__ import annotations

import io
import json
import sys
import tempfile
import zipfile
from pathlib import Path

from PIL import Image


APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

from panelyra.automation.quiet_updates import (
    UpdateError,
    discover_library_sources,
    extract_chapter_links,
    extract_image_urls,
    load_json,
    quiet_download_chapter,
    record_failure,
    retry_failed_updates,
    run_quiet_library_once,
)
from panelyra.automation.series_updates import load_download_history


def png_bytes(color: str) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (96, 144), color).save(output, "PNG")
    return output.getvalue()


class Response:
    def __init__(self, *, text: str = "", content: bytes = b"", url: str, status: int = 200, content_type: str = "text/html") -> None:
        self.text = text
        self.content = content
        self.url = url
        self.status_code = status
        self.headers = {"Content-Type": content_type}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(str(self.status_code))


class Session:
    def __init__(self, responses: dict[str, Response]) -> None:
        self.responses = responses
        self.headers: dict[str, str] = {}

    def get(self, url: str, **_: object) -> Response:
        return self.responses[url.rstrip("/")]


def main() -> int:
    links = extract_chapter_links(
        "https://reader.example/manga/book/",
        '<a href="/manga/book-chapter-10/">10</a><a href="/manga/book-chapter-2/">2</a><a href="/news/2026">Ignore</a>',
    )
    assert links == ["https://reader.example/manga/book-chapter-2", "https://reader.example/manga/book-chapter-10"]

    image_html = """
    <img src="/placeholder.png" data-src="/pages/001.jpg">
    <img data-lazy-src="/pages/002.webp">
    <img srcset="/pages/003-small.jpg 400w, /pages/003.jpg 1000w">
    <script>window.pages=["https:\/\/cdn.example\/004.png"]</script>
    """
    assert extract_image_urls("https://reader.example/book-chapter-2", image_html) == [
        "https://reader.example/pages/001.jpg",
        "https://reader.example/pages/002.webp",
        "https://reader.example/pages/003.jpg",
        "https://cdn.example/004.png",
    ]
    novatoon_html = """
    <img src="https://novatoon.com/logo.png" alt="Novatoon">
    <img src="https://novatoon.com/wp-content/uploads/manga/book/0001.webp" alt="Book Chapter 2 - page 1">
    <img src="https://novatoon.com/wp-content/uploads/manga/book/0002.webp" alt="Book Chapter 2 - page 2">
    <img src="https://novatoon.com/wp-content/themes/manganova/assets/img/readerarea.svg">
    <img src="https://novatoon.com/wp-content/uploads/2026/cover.webp" class="post-image">
    """
    assert extract_image_urls("https://novatoon.com/book-chapter-2", novatoon_html) == [
        "https://novatoon.com/wp-content/uploads/manga/book/0001.webp",
        "https://novatoon.com/wp-content/uploads/manga/book/0002.webp",
    ]

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        library = root / "manhwa"
        local = library / "book" / "02"
        local.mkdir(parents=True)
        (local / "metadata.json").write_text(json.dumps({"url": "https://reader.example/manga/book-chapter-2"}), encoding="utf-8")
        discovered = discover_library_sources(library, root / "sources.json")
        assert discovered["book"]["highest_local"] == 2.0
        assert discovered["book"]["series_urls"] == ["https://reader.example/manga/book"]

        chapter_html = '<html><title>Book Chapter 3</title><img data-src="https://cdn.example/001.png"><img data-src="https://cdn.example/002.png"></html>'
        session = Session(
            {
                "https://reader.example/manga/book": Response(
                    text='<a href="/manga/book-chapter-2">2</a><a href="/manga/book-chapter-3">3</a>',
                    url="https://reader.example/manga/book",
                ),
                "https://reader.example/manga/book-chapter-3": Response(text=chapter_html, url="https://reader.example/manga/book-chapter-3"),
                "https://cdn.example/001.png": Response(content=png_bytes("red"), url="https://cdn.example/001.png", content_type="image/png"),
                "https://cdn.example/002.png": Response(content=png_bytes("blue"), url="https://cdn.example/002.png", content_type="image/png"),
            }
        )
        history = root / "downloaded.json"
        status = root / "status.json"
        failures = root / "failures.json"
        sources = root / "sources.json"
        results = run_quiet_library_once(
            library_root=library,
            history_file=history,
            status_file=status,
            failure_file=failures,
            source_file=sources,
            session=session,
            sleeper=lambda _: None,
            host_delay=(0, 0),
        )
        assert len(results) == 1 and results[0].status == "ok"
        chapter = library / "book" / "03"
        assert (chapter / "images" / "001.png").read_bytes() == png_bytes("red")
        assert json.loads((chapter / "metadata.json").read_text())["quiet_update"] is True
        with zipfile.ZipFile(chapter / "chapter-pages.cbz") as archive:
            assert archive.testzip() is None
            assert archive.namelist() == ["001.png", "002.png"]
        assert "https://reader.example/manga/book-chapter-3" in load_download_history(history)
        assert load_json(status, {})["successful"] == 1

        try:
            quiet_download_chapter(
                "https://reader.example/manga/book-chapter-4",
                series_slug="book",
                output_dir=library,
                session=Session({"https://reader.example/manga/book-chapter-4": Response(text="<html></html>", url="https://reader.example/manga/book-chapter-4")}),
                sleeper=lambda _: None,
            )
        except UpdateError as exc:
            assert exc.code == "no_images_found"
            record_failure(
                type(results[0])("https://reader.example/manga/book-chapter-4", "book", 4.0, "failed", error_code=exc.code, error=str(exc), recovery=exc.recovery),
                failures,
            )
        else:
            raise AssertionError("Missing-image page was accepted")
        failure_payload = load_json(failures, {})
        assert next(iter(failure_payload.values()))["reason_code"] == "no_images_found"
        assert "https://reader.example/manga/book-chapter-4" not in load_download_history(history)

        auth_result = type(results[0])(
            "https://locked.example/manga/book",
            "book",
            None,
            "failed",
            error_code="login_required",
            error="Login required.",
            recovery="browser",
        )
        auth_failures = root / "auth-failures.json"
        record_failure(auth_result, auth_failures)
        retried = retry_failed_updates(
            mode="browser",
            failure_file=auth_failures,
            history_file=history,
            output_dir=library,
        )
        auth_retry = next(item for item in retried if item.url == auth_result.url)
        assert auth_retry.status == "skipped"
        assert auth_retry.recovery == "none"

    print("Quiet update regression checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
