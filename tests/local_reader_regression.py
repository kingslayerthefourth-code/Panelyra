#!/usr/bin/env python3
"""Regression checks for the loopback-only immersive Reader service."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen


APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

from panelyra.services.local_reader import LocalReaderService


PNG = b"\x89PNG\r\n\x1a\n" + b"fixture" * 80


def write_chapter(root: Path, series: str, chapter: str) -> None:
    images = root / series / chapter / "images"
    images.mkdir(parents=True)
    (images / "001.png").write_bytes(PNG)
    (images / "002.png").write_bytes(PNG)
    (images / "003.png").write_bytes(PNG)


def main() -> int:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        write_chapter(root / "manhwa", "fog-land", "01")
        write_chapter(root / "manhwa_clean", "fog-land-clean", "01")
        service = LocalReaderService(root).start()
        try:
            original_url = service.reader_url("Original", "fog-land", "01")
            original_html = urlopen(original_url, timeout=3).read().decode("utf-8")
            assert "Panel 1" in original_html
            assert "__SERIES" not in original_html
            assert 'loading="lazy"' in original_html
            image = urlopen(f"{service.base_url}/media/Original/fog-land/01/001.png", timeout=3).read()
            assert image == PNG

            clean_url = service.reader_url("Clean", "fog-land", "01")
            clean_html = urlopen(clean_url, timeout=3).read().decode("utf-8")
            assert "fog-land-clean" in clean_html

            try:
                urlopen(f"{service.base_url}/media/Original/fog-land/01/../metadata.json", timeout=3)
                raise AssertionError("Reader served a path outside the image allowlist.")
            except HTTPError as error:
                assert error.code == 404
        finally:
            assert service._server is not None
            service._server.shutdown()
            service._server.server_close()
    print("Local Reader regression checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
