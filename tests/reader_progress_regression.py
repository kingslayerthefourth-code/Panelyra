#!/usr/bin/env python3
"""Regression checks for atomic, edition-aware Reader progress."""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from panelyra.services.reader_progress import ReaderProgressStore


with tempfile.TemporaryDirectory() as directory:
    path = Path(directory) / "reader_progress.json"
    store = ReaderProgressStore(path)
    first = store.update({"series": "fog-land", "chapter": "10.5", "edition": "Original", "panel": 4, "total_panels": 10, "percent": 40, "offset": 900, "mode": "comfort", "updated_at": 10})
    assert first.panel == 4 and first.percent == 40
    stale = store.update({"series": "fog-land", "chapter": "10.5", "edition": "Original", "panel": 1, "total_panels": 10, "updated_at": 5})
    assert stale.panel == 4
    clean = store.update({"series": "fog-land-clean", "chapter": "10.5", "edition": "Clean", "panel": 2, "total_panels": 5, "percent": 40, "updated_at": 12})
    assert clean.series == "fog-land"
    assert store.mark("fog-land", "10.5", "Clean", True).completed
    assert not path.exists()  # Legacy JSON is never overwritten by the fresh index.
    assert store.database.is_file()
    assert not list(path.parent.glob("*.tmp"))

print("Reader progress regression checks passed.")
