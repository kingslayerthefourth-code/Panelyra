"""Atomic, local-only reading progress shared by Panelyra surfaces."""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ReaderProgress:
    series: str
    chapter: str
    edition: str = "Original"
    panel: int = 1
    total_panels: int = 1
    percent: float = 0.0
    image_id: str = ""
    image_fraction: float = 0.0
    offset: int = 0
    mode: str = "fit"
    completed: bool = False
    updated_at: float = 0.0

    @property
    def key(self) -> str:
        return progress_key(self.series, self.chapter, self.edition)


def progress_key(series: str, chapter: str, edition: str) -> str:
    from panelyra.services.download_identity import chapter_key
    try:
        chapter = chapter_key(chapter)
    except ValueError:
        pass
    return f"{series.removesuffix('-clean')}::{chapter}::{edition}"


def _safe_record(payload: Any) -> ReaderProgress | None:
    if not isinstance(payload, dict):
        return None
    try:
        series = str(payload.get("series", "")).removesuffix("-clean")
        chapter = str(payload.get("chapter", ""))
        from panelyra.services.download_identity import chapter_key
        try:
            chapter = chapter_key(chapter)
        except ValueError:
            pass
        edition = str(payload.get("edition", "Original"))
        if not series or not chapter or edition not in {"Original", "Clean"}:
            return None
        total = max(1, int(payload.get("total_panels", 1) or 1))
        panel = max(1, min(total, int(payload.get("panel", 1) or 1)))
        percent = max(0.0, min(100.0, float(payload.get("percent", 0.0) or 0.0)))
        mode = str(payload.get("mode", "fit"))
        if mode not in {"fit", "comfort", "original", "single", "double", "webtoon"}:
            mode = "fit"
        return ReaderProgress(
            series=series,
            chapter=chapter,
            edition=edition,
            panel=panel,
            total_panels=total,
            percent=percent,
            image_id=str(payload.get("image_id", ""))[:255],
            image_fraction=max(0.0, min(1.0, float(payload.get("image_fraction", 0) or 0))),
            offset=max(0, int(payload.get("offset", 0) or 0)),
            mode=mode,
            completed=bool(payload.get("completed", False)),
            updated_at=max(0.0, float(payload.get("updated_at", 0.0) or 0.0)),
        )
    except (TypeError, ValueError):
        return None


_LOCKS: dict[str, threading.RLock] = {}


class ReaderProgressStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        # Fresh index by request. Legacy JSON remains untouched for rollback.
        self.database = path.parent / ('foundation.sqlite3' if path.name == 'reader_progress.json' else path.name+'.sqlite3')
        self._lock = _LOCKS.setdefault(str(path.resolve()), threading.RLock())

    def _key(self, series, chapter, edition):
        from panelyra.services.library_store import connect
        with connect(self.database) as db:
            row = db.execute('SELECT series_id FROM locators WHERE locator=?', ('manhwa/'+series.removesuffix('-clean'),)).fetchone()
        return progress_key(row[0] if row else series, chapter, edition)

    def _load(self) -> dict[str, dict[str, Any]]:
        from panelyra.services.library_store import connect
        with connect(self.database) as db:
            return {row['key']: json.loads(row['payload']) for row in db.execute('SELECT key,payload FROM progress')}

    def _save(self, records: dict[str, dict[str, Any]]) -> None:
        from panelyra.services.library_store import transaction
        with transaction(self.database) as db:
            for key, value in records.items():
                db.execute('INSERT INTO progress VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET payload=excluded.payload,updated=excluded.updated WHERE excluded.updated>=progress.updated',
                           (key, json.dumps(value), value.get('updated_at', 0)))

    def all(self) -> list[ReaderProgress]:
        with self._lock:
            return [record for value in self._load().values() if (record := _safe_record(value))]

    def get(self, series: str, chapter: str, edition: str) -> ReaderProgress | None:
        with self._lock:
            return _safe_record(self._load().get(self._key(series, chapter, edition)))

    def update(self, payload: dict[str, Any]) -> ReaderProgress:
        incoming = _safe_record({**payload, "updated_at": payload.get("updated_at") or time.time()})
        if incoming is None:
            raise ValueError("Invalid Reader progress payload.")
        with self._lock:
            from panelyra.services.library_store import transaction
            key = self._key(incoming.series,incoming.chapter,incoming.edition)
            with transaction(self.database) as db:
                row = db.execute('SELECT payload FROM progress WHERE key=?', (key,)).fetchone()
                current = _safe_record(json.loads(row[0])) if row else None
                if current and current.updated_at > incoming.updated_at:
                    return current
                # Scrolling updates position, not the separately set completed flag.
                if 'completed' not in payload and current:
                    incoming = _safe_record({**asdict(incoming), 'completed': current.completed})
                db.execute('INSERT INTO progress VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET payload=excluded.payload,updated=excluded.updated',
                           (key, json.dumps(asdict(incoming)), incoming.updated_at))
        return incoming

    def mark(self, series: str, chapter: str, edition: str, completed: bool) -> ReaderProgress:
        from panelyra.services.library_store import transaction
        key = self._key(series,chapter,edition)
        with transaction(self.database) as db:
            row = db.execute('SELECT payload FROM progress WHERE key=?',(key,)).fetchone()
            current = _safe_record(json.loads(row[0])) if row else ReaderProgress(series,chapter,edition)
            record = _safe_record({**asdict(current),'completed':completed,'updated_at':time.time()})
            db.execute('INSERT INTO progress VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET payload=excluded.payload,updated=excluded.updated',
                       (key,json.dumps(asdict(record)),record.updated_at))
        return record

    def latest_for_series(self, series: str) -> ReaderProgress | None:
        matches = [item for item in self.all() if item.series == series.removesuffix("-clean")]
        return max(matches, key=lambda item: item.updated_at, default=None)

    def series_summary(self, series: str) -> dict[str, Any]:
        matches = [item for item in self.all() if item.series == series.removesuffix("-clean")]
        latest = max(matches, key=lambda item: item.updated_at, default=None)
        return {
            "latest": asdict(latest) if latest else None,
            "completed_chapters": sorted({item.chapter for item in matches if item.completed}),
            "in_progress_chapters": sorted({item.chapter for item in matches if not item.completed and item.percent > 0}),
        }
