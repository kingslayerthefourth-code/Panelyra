"""Incremental, query-specific grounding for Lyra."""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable


TEXT_LIMIT = 48_000
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


@dataclass(frozen=True)
class KnowledgeDocument:
    doc_id: str
    kind: str
    title: str
    text: str
    source: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PageContext:
    page: str
    view: str = ""
    workspace: str = ""
    series: str = ""
    edition: str = ""
    chapter: str = ""
    panel: int = 0
    attached_run: str = ""


@dataclass(frozen=True)
class PanelyraSnapshot:
    comment_count: int
    reader_series_count: int
    run_count: int
    studio_asset_count: int
    documents: tuple[KnowledgeDocument, ...]


@dataclass(frozen=True)
class GroundingBundle:
    relevant: bool
    page_context: PageContext
    summary: dict[str, Any]
    records: tuple[dict[str, Any], ...]
    source_labels: tuple[str, ...]
    prompt_text: str


PROCESS_DOCS = (
    ("Dashboard", "Dashboard shows active runs, recent outcomes, Reader continuation, metrics, and launches Engage, Capture, Studio, and Reader."),
    ("Engage", "Engage visits chapter pages, extracts chapter context, generates a style-aware comment, fills the comment editor, submits only after the user starts the run, verifies the posted comment, optionally clicks Say thanks, and writes every result to comment_history.jsonl. Configure sets URL, chapter limit, routing, speed, selectors, content/OCR and research. Live shows progress, pause/resume/stop, stage, ETA and diagnostics. Intelligence summarizes exact generated comments and failures."),
    ("Capture", "Capture opens a chapter or series, detects story-region images, follows the selected routing, and writes chapter images plus optional CBZ and OCR text. Configure controls URL, chapter limit, reader layout, output folder, output formats, performance and selectors. Live reports pages and saved images. Library lists local Capture outputs."),
    ("Studio", "Studio accepts source media and text, builds a local word-art preview, then may upload or submit only when the user explicitly starts that workflow. Studio keeps source uploads and generated previews separate. Its Setup, Live and Library pages expose configuration, progress and outputs."),
    ("Reader", "Reader catalogs local Original and Clean editions. Original is never overwritten by cleaning. Clean exists only when a separate cleaned chapter folder was produced. The immersive reader serves local images with zero gaps, chapter navigation, width modes and resume position stored in browser local storage."),
    ("Run History", "Run History stores durable Engage, Capture and Studio run receipts including configuration, progress, stage, status, stop reason, output and log path. Retrying restores configuration but never starts automation automatically."),
    ("Settings", "Settings controls dashboard density, progress motion, notifications, comment intelligence, Lyra's local model and privacy, explicit web search, presets, selector profiles, storage and advanced behavior."),
    ("Help", "Help documents every major field and button. Chrome is shared by Engage and Capture, so one browser workflow owns the authenticated profile at a time. Login challenges require user interaction. Selector failures usually mean a page layout changed."),
    ("Safety", "Lyra may explain verified data and offer at most one confirmed configuration, selector restoration or retry-preparation fix. Lyra cannot start or stop automation, submit comments, download chapters, delete data, publish, restart services or edit source code."),
)


def _tokens(value: str) -> set[str]:
    values: set[str] = set()
    for part in re.findall(r"[a-z0-9]+", value.lower()):
        if len(part) <= 1:
            continue
        values.add(part)
        if len(part) > 3 and part.endswith("s"):
            values.add(part[:-1])
    return values


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace")[:TEXT_LIMIT])
    except (OSError, json.JSONDecodeError):
        return None


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as source:
            for line in source:
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    records.append(value)
    except OSError:
        pass
    return records


class PanelyraKnowledgeService:
    """Indexes structured text and refreshes only sources whose signatures changed."""

    def __init__(self, app_dir: Path) -> None:
        self.app_dir = app_dir.resolve()
        self._cache: dict[str, tuple[tuple[Any, ...], Any]] = {}

    def _cached(self, key: str, signature: tuple[Any, ...], loader: Callable[[], Any]) -> Any:
        cached = self._cache.get(key)
        if cached and cached[0] == signature:
            return cached[1]
        value = loader()
        self._cache[key] = (signature, value)
        return value

    @staticmethod
    def _signature(paths: list[Path]) -> tuple[Any, ...]:
        result: list[Any] = []
        for path in paths:
            try:
                stat = path.stat()
                result.extend((str(path), stat.st_mtime_ns, stat.st_size))
            except OSError:
                result.extend((str(path), 0, 0))
        return tuple(result)

    def comments(self) -> list[dict[str, Any]]:
        path = self.app_dir / "data/history/comment_history.jsonl"
        return self._cached("comments", self._signature([path]), lambda: _read_jsonl(path))

    def runs(self) -> list[dict[str, Any]]:
        files = sorted((self.app_dir / "runs").glob("*.json"))
        def load() -> list[dict[str, Any]]:
            values = [value for value in (_read_json(path) for path in files) if isinstance(value, dict)]
            return sorted(values, key=lambda item: str(item.get("updated_at") or item.get("started_at") or ""), reverse=True)
        return self._cached("runs", self._signature(files), load)

    def reader_catalog(self) -> list[dict[str, Any]]:
        roots = (("Original", self.app_dir / "manhwa"), ("Clean", self.app_dir / "manhwa_clean"))
        dirs: list[Path] = []
        for _, root in roots:
            if root.is_dir():
                dirs.extend(path for path in root.iterdir() if path.is_dir())
                for series in [path for path in root.iterdir() if path.is_dir()]:
                    dirs.extend(path for path in series.iterdir() if path.is_dir())
                    dirs.extend(series.glob("manifest*.json"))
        def load() -> list[dict[str, Any]]:
            catalog: list[dict[str, Any]] = []
            for edition, root in roots:
                if not root.is_dir():
                    continue
                for series in sorted((path for path in root.iterdir() if path.is_dir()), key=lambda p: p.name.lower()):
                    for chapter in sorted((path for path in series.iterdir() if path.is_dir()), key=lambda p: p.name.lower()):
                        images_dir = chapter / "images"
                        images = sum(1 for item in images_dir.iterdir() if item.is_file() and item.suffix.lower() in IMAGE_SUFFIXES) if images_dir.is_dir() else 0
                        metadata = _read_json(chapter / "metadata.json") or {}
                        catalog.append({
                            "series": series.name.removesuffix("-clean"), "stored_series": series.name,
                            "title": str(metadata.get("series_title") or metadata.get("title") or series.name.replace("-", " ").title()),
                            "edition": edition, "chapter": chapter.name, "image_count": images,
                            "cbz": any(chapter.glob("*.cbz")), "ocr": any(chapter.glob("*.txt")),
                            "manifest": any(chapter.glob("manifest*.json")) or any(series.glob("manifest*.json")),
                            "cleaning_report": any(chapter.glob("*clean*report*.json")),
                        })
            return catalog
        return self._cached("reader", self._signature(dirs), load)

    def studio_assets(self) -> list[dict[str, Any]]:
        root = self.app_dir / "uploads"
        files = sorted(path for path in root.rglob("*") if path.is_file()) if root.is_dir() else []
        return self._cached("studio", self._signature(files), lambda: [
            {"name": path.name, "relative_path": str(path.relative_to(self.app_dir)), "bytes": path.stat().st_size, "modified": path.stat().st_mtime}
            for path in files
        ])

    def settings(self) -> dict[str, Any]:
        paths = [self.app_dir / "config" / name for name in ("preferences.json", "presets.json", "selector_profiles.json")]
        return self._cached("settings", self._signature(paths), lambda: {
            path.stem: (_read_json(path) or {}) for path in paths
        })

    def support(self) -> list[dict[str, Any]]:
        path = self.app_dir / "data/history/owner_requests.jsonl"
        return self._cached("support", self._signature([path]), lambda: _read_jsonl(path))

    def snapshot(self) -> PanelyraSnapshot:
        comments, reader, runs, studio = self.comments(), self.reader_catalog(), self.runs(), self.studio_assets()
        documents = tuple(KnowledgeDocument(f"process:{title.lower()}", "process", title, text, "Panelyra process guide") for title, text in PROCESS_DOCS)
        return PanelyraSnapshot(len(comments), len({item["series"] for item in reader}), len(runs), len(studio), documents)

    @staticmethod
    def is_panelyra_question(query: str, page: PageContext) -> bool:
        terms = {"panelyra", "lyra", "engage", "capture", "studio", "reader", "library", "chapter", "clean", "original", "comment", "run", "history", "setting", "page", "book", "selector", "browser", "download", "output"}
        query_tokens = _tokens(query)
        greeting_tokens = {"hi", "hey", "hello", "yo", "sup", "morning", "afternoon", "evening"}
        if query_tokens and query_tokens <= greeting_tokens:
            return False
        followup = len(query_tokens) <= 8 and bool(query_tokens & {"it", "that", "this", "why", "there", "them"})
        contextual_request = any(
            phrase in query.lower()
            for phrase in ("what should i do", "what do i do", "what next", "on this screen", "where am i")
        )
        return bool(query_tokens & terms) or followup or contextual_request

    def retrieve(self, query: str, page: PageContext, *, max_chars: int = 6_500, max_records: int = 12) -> GroundingBundle:
        if not self.is_panelyra_question(query, page):
            return GroundingBundle(False, page, {}, (), (), "")
        comments, reader, runs, studio = self.comments(), self.reader_catalog(), self.runs(), self.studio_assets()
        query_lower = query.lower()
        tokens = _tokens(query + " " + page.page + " " + page.workspace + " " + page.series + " " + page.chapter)
        style_stats: dict[str, dict[str, int]] = {}
        for style in {str(item.get("style") or "Unknown") for item in comments}:
            rows = [item for item in comments if str(item.get("style") or "Unknown") == style]
            style_stats[style] = dict(Counter(str(item.get("status") or "unknown") for item in rows))
        edition_map: dict[str, dict[str, list[str]]] = {}
        for item in reader:
            edition_map.setdefault(item["series"], {}).setdefault(item["edition"], []).append(item["chapter"])
        settings_raw = self.settings()
        preference_values = settings_raw.get("preferences", {}) if isinstance(settings_raw.get("preferences"), dict) else {}
        safe_preferences = {
            str(key): value
            for key, value in preference_values.items()
            if isinstance(value, (str, int, float, bool, type(None)))
        }
        summary = {
            "identity": "Lyra is Panelyra's built-in assistant and speaks in first person about verified app data.",
            "current_context": asdict(page),
            "comments": {"total": len(comments), "statuses": dict(Counter(str(item.get("status") or "unknown") for item in comments)), "styles": style_stats},
            "reader": {"series": edition_map, "chapter_records": len(reader)},
            "runs": {"total": len(runs), "statuses": dict(Counter(str(item.get("status") or "unknown") for item in runs)), "latest_by_type": {}},
            "studio": {"assets": studio},
            "settings": {
                "preferences": safe_preferences,
                "preset_names": sorted((settings_raw.get("presets") or {}).keys()) if isinstance(settings_raw.get("presets"), dict) else [],
                "selector_profile_names": sorted((settings_raw.get("selector_profiles") or {}).keys()) if isinstance(settings_raw.get("selector_profiles"), dict) else [],
            },
            "support_requests": {
                "count": len(self.support()),
                "recent": self.support()[-5:],
            },
        }
        for bot_type in ("comment", "download", "word_art"):
            latest = next((item for item in runs if str(item.get("bot_type")) == bot_type), None)
            if latest:
                summary["runs"]["latest_by_type"][bot_type] = {
                    key: latest.get(key)
                    for key in (
                        "run_id", "bot_type", "status", "stage", "message", "stop_reason",
                        "started_at", "updated_at", "finished_at", "progress_current",
                        "progress_total", "log_path",
                    )
                    if key in latest
                }
        if not any(term in query_lower for term in ("style", "fail", "failure")):
            summary["comments"].pop("styles", None)
        if not any(term in query_lower for term in ("run", "happened", "engage", "capture", "studio", "failed", "stopped")):
            summary["runs"].pop("latest_by_type", None)
        if "studio" not in query_lower:
            summary["studio"] = {"asset_count": len(studio)}
        if not any(term in query_lower for term in ("setting", "preference", "preset", "selector", "configure", "configuration")):
            summary["settings"] = {"available": True}
        if not any(term in query_lower for term in ("support", "ticket", "request")):
            summary["support_requests"] = {"count": len(self.support())}
        scored: list[tuple[int, str, dict[str, Any]]] = []
        for index, item in enumerate(comments):
            haystack = json.dumps(item, ensure_ascii=False).lower()
            score = len(tokens & _tokens(haystack)) * 3 + (4 if "comment" in query_lower else 0)
            if score:
                scored.append((score, f"comment:{index}", {"kind": "comment", **item}))
        for index, item in enumerate(reader):
            haystack = json.dumps(item, ensure_ascii=False).lower()
            score = len(tokens & _tokens(haystack)) * 4 + (5 if any(word in query_lower for word in ("chapter", "book", "library", "clean", "original")) else 0)
            if page.series and item["series"] == page.series:
                score += 8
            if score:
                scored.append((score, f"reader:{index}", {"kind": "reader_chapter", **item}))
        for index, item in enumerate(runs):
            haystack = json.dumps(item, ensure_ascii=False).lower()
            score = len(tokens & _tokens(haystack)) * 3 + (5 if "run" in query_lower or "happened" in query_lower else 0)
            if score:
                scored.append((score, f"run:{index}", {"kind": "run", **item}))
        for index, (title, text) in enumerate(PROCESS_DOCS):
            score = len(tokens & _tokens(title + " " + text)) * 3 + (5 if title.lower() in query_lower else 0)
            if score:
                scored.append((score, f"process:{index}", {"kind": "process", "title": title, "text": text}))
        scored.sort(key=lambda row: (-row[0], row[1]))
        records: list[dict[str, Any]] = []
        used = len(json.dumps({"summary": summary, "relevant_exact_records": []}, ensure_ascii=False))
        for _, _, record in scored:
            encoded = json.dumps(record, ensure_ascii=False)
            if len(records) >= max_records or used + len(encoded) > max_chars:
                continue
            records.append(record)
            used += len(encoded)
        labels: list[str] = []
        if any(term in query_lower for term in ("comment", "style", "keyword", "submitted")):
            labels.append(f"{len(comments)} comment records")
        if any(term in query_lower for term in ("reader", "library", "book", "chapter", "clean", "original")):
            labels.append("Reader catalog")
        if any(term in query_lower for term in ("run", "happened", "capture", "engage", "failed", "stopped")):
            labels.append(f"{len(runs)} run records")
        if "studio" in query_lower and studio:
            labels.append("Studio assets")
        if any(term in query_lower for term in ("how", "page", "next", "control", "workflow", "setting")):
            labels.append("Panelyra process guide")
        if not labels:
            labels.append("Current Panelyra page")
        prompt_text = json.dumps(
            {"summary": summary, "relevant_exact_records": records},
            ensure_ascii=False,
            indent=2,
        )
        if len(prompt_text) > max_chars:
            # Preserve valid JSON even if unusually large metadata leaves no record budget.
            prompt_text = json.dumps(
                {"summary": summary, "relevant_exact_records": []},
                ensure_ascii=False,
                separators=(",", ":"),
            )
        return GroundingBundle(True, page, summary, tuple(records), tuple(labels), prompt_text)
