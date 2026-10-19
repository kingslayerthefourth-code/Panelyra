#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tablet_companion import CompanionState


def main() -> int:
    with tempfile.TemporaryDirectory() as directory:
        app = Path(directory)
        (app / "manhwa_ocr_downloader.py").write_text("", encoding="utf-8")
        state = CompanionState(app_dir=app)
        assert not state.authorized("Bearer wrong")
        token = state.pair(state.pairing_secret, "Huawei MatePad")
        assert token and state.authorized(f"Bearer {token}")
        assert state.device_name == "Huawei MatePad"
        try:
            state.create_job({"start_url": "file:///private/book", "chapter_count": 1})
        except ValueError as exc:
            assert "HTTP" in str(exc)
        else:
            raise AssertionError("Unsafe URL scheme accepted")
        try:
            state.create_job({"start_url": "https://reader.test/chapter-1", "chapter_count": 10001})
        except ValueError as exc:
            assert "between 1 and 10000" in str(exc)
        else:
            raise AssertionError("Chapter limit was not enforced")
        state.unpair()
        assert not state.authorized(f"Bearer {token}")
        saved = json.loads((app / "data/tablet_companion/state.json").read_text(encoding="utf-8"))
        assert saved["device_token"] == ""
    print("Tablet companion regression checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
