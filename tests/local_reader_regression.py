#!/usr/bin/env python3
"""Regression checks for the loopback-only immersive Reader service."""

from __future__ import annotations

import sys
import io
import json
from PIL import Image
from urllib.parse import urlencode
import tempfile
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen


APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

from panelyra.services.local_reader import LocalReaderService


stream = io.BytesIO()
Image.new("RGB", (400, 1200), "teal").save(stream, "PNG")
PNG = stream.getvalue()


def write_chapter(root: Path, series: str, chapter: str) -> None:
    images = root / series / chapter / "images"
    images.mkdir(parents=True)
    (images / "001.png").write_bytes(PNG)
    (images / "002.png").write_bytes(PNG)
    (images / "003.png").write_bytes(PNG)
    (images.parent/'metadata.json').write_text(json.dumps({'image_count':3,'series_title':series}))


def main() -> int:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        write_chapter(root / "manhwa", "fog-land", "01")
        write_chapter(root / "manhwa_clean", "fog-land-clean", "01")
        service = LocalReaderService(root)
        chapter = service.chapter("Original", "fog-land", "01")
        assert chapter is not None
        assert "Open Lyra" not in service._reader_html(chapter)
        lyra_html = service._reader_html(chapter, "private-token")
        assert "Open Lyra" not in lyra_html and "private-token" not in lyra_html
        assert "/api/lyra/chat" not in lyra_html
        assert "/assets/common.js" in lyra_html
        service.start()
        try:
            original_url = service.reader_url("Original", "fog-land", "01")
            original_html = urlopen(original_url, timeout=3).read().decode("utf-8")
            assert "\"view\": \"reader\"" in original_html
            assert "__SERIES" not in original_html
            manifest_url = service.base_url + "/api/manifest?" + urlencode(dict(series="fog-land", edition="Original", chapter="01", reader_token=service.reader_token))
            manifest = json.loads(urlopen(manifest_url).read())
            assert len(manifest["images"]) == 3
            assert manifest["images"][0]["height"] == 1200
            script = urlopen(service.base_url + "/assets/reader.js").read().decode()
            assert 'loading="${i<2?' in script
            assert "image_fraction" in script
            assert "/api/lyra" not in script.lower()
            image = urlopen(service.base_url+manifest['images'][0]['url']+'?reader_token='+service.reader_token, timeout=3).read()
            assert image == PNG

            clean_url = service.reader_url("Clean", "fog-land", "01")
            clean_html = urlopen(clean_url, timeout=3).read().decode("utf-8")
            assert '"edition": "Clean"' in clean_html
            clean = service.manifest('fog-land','Clean','01')
            clean_image = urlopen(service.base_url+clean['images'][0]['url']+'?reader_token='+service.reader_token).read()
            assert clean_image == PNG

            try:
                urlopen(f"{service.base_url}/media/Original/fog-land/01/../metadata.json?reader_token={service.reader_token}", timeout=3)
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
