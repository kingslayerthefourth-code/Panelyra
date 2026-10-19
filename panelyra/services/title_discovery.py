"""Volatile, thread-safe title lookups. Workers never access Streamlit state."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass
from threading import Event, RLock, Thread
from time import monotonic

_DETAILS = ThreadPoolExecutor(max_workers=6, thread_name_prefix="panelyra-title-details")


@dataclass(frozen=True)
class SearchSnapshot:
    results: list[dict]
    notices: dict[str, str]
    checked: int
    total: int
    finished: bool
    details_pending: bool
    error: str


class TitleSearchTask:
    """A session owns its task; revisiting a query reuses its recent results."""

    def __init__(self, query: str, sources: list[str], search):
        self.query = query
        self.sources = list(sources)
        self.created_at = monotonic()
        self._lock = RLock()
        self._finished = Event()
        self._cancelled = Event()
        self._rows: list[dict] = []
        self._notices: dict[str, str] = {}
        self._checked = 0
        self._error = ""
        self._details: dict[str, dict] = {}
        self._details_waiting: set[str] = set()
        self._details_requested: set[str] = set()
        Thread(target=self._run, args=(search,), daemon=True, name="panelyra-title-search").start()

    def _publish(self, rows, notices, done, total):
        with self._lock:
            self._rows = deepcopy(rows)
            self._notices = dict(notices)
            self._checked = done

    def _run(self, search):
        try:
            rows, notices = search(
                self.query, self.sources, on_results=self._publish,
                include_counts=False, background=True, cancelled=self._cancelled.is_set,
            )
            self._publish(rows, notices, len(self.sources), len(self.sources))
        except Exception:
            # Technical details remain internal; any already-discovered books
            # remain usable if one worker fails unexpectedly.
            with self._lock:
                self._error = "Search could not finish. Please try again."
        finally:
            self._finished.set()

    def cancel(self):
        self._cancelled.set()

    def wait(self, timeout=0.02):
        """Allow an immediate cached/mocked response without a visual flash."""
        return self._finished.wait(timeout)

    def matches(self, query, sources):
        with self._lock:
            return (
                self.query.casefold() == query.casefold()
                and set(self.sources) == set(sources)
                and monotonic() - self.created_at < 300
                and not (self._finished.is_set() and not self._rows)
            )

    def snapshot(self) -> SearchSnapshot:
        with self._lock:
            rows = deepcopy(self._rows)
            for row in rows:
                row.update(self._details.get(row.get("url", ""), {}))
            return SearchSnapshot(
                rows, dict(self._notices), self._checked, len(self.sources),
                self._finished.is_set(), bool(self._details_waiting), self._error,
            )

    def request_details(self, rows):
        """Only count chapters for visible cards, keeping large searches fast."""
        for row in rows:
            url = row.get("url", "")
            with self._lock:
                if not url or url in self._details_requested or "chapter_count" in row:
                    continue
                self._details_requested.add(url)
                self._details_waiting.add(url)
            _DETAILS.submit(self._count, dict(row))

    def _count(self, row):
        from panelyra.services.chapter_counts import get_chapter_count
        try:
            details = get_chapter_count(row)
        except Exception:
            details = {"chapter_count": None, "chapter_count_kind": "unknown"}
        with self._lock:
            self._details[row["url"]] = details
            self._details_waiting.discard(row["url"])


class ChapterCatalogTask:
    """Load the selected book's chapter list without delaying its form."""

    def __init__(self, url, source, refresh=False):
        self.url = url
        self.source = source
        self.created_at = monotonic()
        self.stage = "Connecting to the source…"
        from panelyra.services.chapter_availability import get_catalog
        self._future = _DETAILS.submit(get_catalog, url, source, refresh=refresh, on_progress=self._progress)

    def _progress(self, stage):
        self.stage = stage

    def snapshot(self):
        if not self._future.done():
            return None
        try:
            return deepcopy(self._future.result())
        except Exception:
            return {"chapters": {}, "complete": False, "notice": "Chapter lookup could not finish. Refresh to try again, or enter an explicit chapter range."}
