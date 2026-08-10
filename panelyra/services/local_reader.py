"""Local-only immersive reader for Panelyra chapter image folders."""

from __future__ import annotations

import html
import json
import mimetypes
import threading
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, quote, unquote, urlencode, urlsplit


IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
DEFAULT_READER_PORT = 8572


def _sort_key(path: Path) -> tuple[int, int | str, str]:
    if path.stem.isdigit():
        return (0, int(path.stem), path.name.lower())
    return (1, path.stem.lower(), path.name.lower())


def _safe_child(root: Path, *parts: str) -> Optional[Path]:
    if any(not part or Path(part).name != part for part in parts):
        return None
    try:
        candidate = root.joinpath(*parts).resolve()
        candidate.relative_to(root.resolve())
        return candidate
    except (OSError, ValueError):
        return None


@dataclass(frozen=True)
class ReaderChapter:
    edition: str
    series: str
    chapter: str
    title: str
    images: tuple[str, ...]


class LocalReaderService:
    """A small loopback-only server with a strict managed-folder allowlist."""

    def __init__(self, app_dir: Path) -> None:
        self.app_dir = app_dir.resolve()
        self.roots = {
            "Original": self.app_dir / "manhwa",
            "Clean": self.app_dir / "manhwa_clean",
        }
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def base_url(self) -> str:
        if self._server is None:
            raise RuntimeError("Local Reader service has not started.")
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> "LocalReaderService":
        if self._server is not None:
            return self
        service = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                service._handle(self)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        try:
            self._server = ThreadingHTTPServer(("127.0.0.1", DEFAULT_READER_PORT), Handler)
        except OSError:
            self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="panelyra-local-reader",
            daemon=True,
        )
        self._thread.start()
        return self

    def reader_url(self, edition: str, series: str, chapter: str) -> str:
        query = urlencode({"edition": edition, "series": series, "chapter": chapter})
        return f"{self.base_url}/reader?{query}"

    def chapter(self, edition: str, series: str, chapter: str) -> Optional[ReaderChapter]:
        root = self.roots.get(edition)
        if root is None:
            return None
        chapter_dir = _safe_child(root, series, chapter)
        if (chapter_dir is None or not chapter_dir.is_dir()) and edition == "Clean" and not series.endswith("-clean"):
            chapter_dir = _safe_child(root, f"{series}-clean", chapter)
        if chapter_dir is None:
            return None
        images_dir = chapter_dir / "images"
        if not images_dir.is_dir():
            return None
        images = tuple(
            image.name
            for image in sorted(images_dir.iterdir(), key=_sort_key)
            if image.is_file() and image.suffix.lower() in IMAGE_SUFFIXES
        )
        if not images:
            return None
        title = f"{series.replace('-', ' ').title()} · Chapter {chapter}"
        metadata_path = chapter_dir / "metadata.json"
        if metadata_path.is_file():
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                title = str(metadata.get("title") or title).strip()
            except (OSError, json.JSONDecodeError):
                pass
        return ReaderChapter(edition, chapter_dir.parent.name, chapter, title, images)

    def _chapter_options(self, edition: str, series: str) -> list[str]:
        root = self.roots.get(edition)
        series_dir = _safe_child(root, series) if root else None
        if series_dir is None or not series_dir.is_dir():
            return []
        return [
            item.name
            for item in sorted(series_dir.iterdir(), key=_sort_key)
            if item.is_dir() and self.chapter(edition, series, item.name)
        ]

    def _handle(self, handler: BaseHTTPRequestHandler) -> None:
        request = urlsplit(handler.path)
        if request.path == "/reader":
            query = parse_qs(request.query)
            chapter = self.chapter(
                query.get("edition", [""])[0],
                query.get("series", [""])[0],
                query.get("chapter", [""])[0],
            )
            if chapter is None:
                self._send_text(handler, HTTPStatus.NOT_FOUND, "Chapter not found.")
                return
            self._send_html(handler, self._reader_html(chapter))
            return
        if request.path.startswith("/media/"):
            parts = [part for part in request.path.split("/") if part]
            if len(parts) != 5:
                self._send_text(handler, HTTPStatus.NOT_FOUND, "Media not found.")
                return
            _, edition, series, chapter, filename = (unquote(part) for part in parts)
            root = self.roots.get(edition)
            image_path = _safe_child(root, series, chapter, "images", filename) if root else None
            if image_path is None or not image_path.is_file() or image_path.suffix.lower() not in IMAGE_SUFFIXES:
                self._send_text(handler, HTTPStatus.NOT_FOUND, "Media not found.")
                return
            self._send_file(handler, image_path)
            return
        self._send_text(handler, HTTPStatus.NOT_FOUND, "Not found.")

    @staticmethod
    def _send_text(handler: BaseHTTPRequestHandler, status: HTTPStatus, value: str) -> None:
        payload = value.encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "text/plain; charset=utf-8")
        handler.send_header("Content-Length", str(len(payload)))
        handler.end_headers()
        handler.wfile.write(payload)

    @staticmethod
    def _send_html(handler: BaseHTTPRequestHandler, value: str) -> None:
        payload = value.encode("utf-8")
        handler.send_response(HTTPStatus.OK)
        handler.send_header("Content-Type", "text/html; charset=utf-8")
        handler.send_header("Content-Length", str(len(payload)))
        handler.end_headers()
        handler.wfile.write(payload)

    @staticmethod
    def _send_file(handler: BaseHTTPRequestHandler, path: Path) -> None:
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        try:
            size = path.stat().st_size
            handler.send_response(HTTPStatus.OK)
            handler.send_header("Content-Type", content_type)
            handler.send_header("Content-Length", str(size))
            handler.send_header("Cache-Control", "private, max-age=3600")
            handler.end_headers()
            with path.open("rb") as source:
                while chunk := source.read(128 * 1024):
                    handler.wfile.write(chunk)
        except OSError:
            handler.close_connection = True

    def _reader_html(self, chapter: ReaderChapter) -> str:
        chapters = self._chapter_options(chapter.edition, chapter.series)
        current_index = chapters.index(chapter.chapter)
        previous_chapter = chapters[current_index - 1] if current_index else ""
        next_chapter = chapters[current_index + 1] if current_index + 1 < len(chapters) else ""
        base_query = {"edition": chapter.edition, "series": chapter.series}
        options = "".join(
            f'<option value="{html.escape(item)}" {"selected" if item == chapter.chapter else ""}>Chapter {html.escape(item)}</option>'
            for item in chapters
        )
        def reader_href(target: str) -> str:
            return "/reader?" + urlencode({**base_query, "chapter": target}) if target else "#"
        images = "\n".join(
            (
                '<figure class="panel" data-panel="{index}"><img src="{src}" alt="Chapter {chapter_number}, panel {index}" '
                'loading="{loading}" decoding="async"></figure>'
            ).format(
                index=index,
                chapter_number=html.escape(chapter.chapter),
                loading="eager" if index <= 2 else "lazy",
                src="/media/" + "/".join(
                    quote(part) for part in (chapter.edition, chapter.series, chapter.chapter, filename)
                ),
            )
            for index, filename in enumerate(chapter.images, start=1)
        )
        payload = {
            "key": f"panelyra-reader:{chapter.edition}:{chapter.series}:{chapter.chapter}",
            "total": len(chapter.images),
            "previous": reader_href(previous_chapter),
            "next": reader_href(next_chapter),
        }
        return _READER_TEMPLATE.replace("__TITLE__", html.escape(chapter.title)).replace(
            "__EDITION__", html.escape(chapter.edition)
        ).replace("__CHAPTER__", html.escape(chapter.chapter)).replace(
            "__OPTIONS__", options
        ).replace("__PREVIOUS__", html.escape(reader_href(previous_chapter))).replace(
            "__NEXT__", html.escape(reader_href(next_chapter))).replace(
            "__IMAGES__", images
        ).replace("__PAYLOAD__", json.dumps(payload)).replace(
            "__SERIES_JSON__", json.dumps(chapter.series)
        )


_SERVICE: Optional[LocalReaderService] = None
_SERVICE_LOCK = threading.Lock()


def start_local_reader_server(app_dir: Path) -> LocalReaderService:
    global _SERVICE
    with _SERVICE_LOCK:
        if _SERVICE is None:
            _SERVICE = LocalReaderService(app_dir).start()
        return _SERVICE


_READER_TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__ · Panelyra</title><style>
:root{color-scheme:dark;--bg:#050507;--panel:#101116;--line:#282a33;--text:#f4f5f7;--muted:#a6a9b3;--accent:#3ed0c3}*{box-sizing:border-box}html{background:var(--bg)}body{margin:0;background:var(--bg);color:var(--text);font:14px ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}.reader-bar{position:fixed;z-index:3;inset:0 0 auto;display:flex;align-items:center;gap:10px;padding:12px 18px;background:rgba(7,8,11,.88);border-bottom:1px solid var(--line);backdrop-filter:blur(18px);transition:transform .22s ease,opacity .22s ease}.reader-bar.hidden{transform:translateY(-110%);opacity:0}.title{min-width:0;flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-weight:650}.meta{color:var(--muted);font-size:12px}.control,select{height:34px;border:1px solid var(--line);border-radius:7px;background:#171820;color:var(--text);padding:0 10px;text-decoration:none}.control:hover{border-color:#575b68}.reader{padding:64px 0 40px;display:flex;align-items:center;flex-direction:column;gap:0;min-height:100vh}.panel{margin:0;padding:0;line-height:0;display:block}.panel img{display:block;height:auto;max-width:100%;margin:0}.fit .panel{width:min(100%,1120px)}.fit .panel img{width:100%}.comfort .panel{width:min(100%,900px)}.comfort .panel img{width:100%}.original .panel{width:auto;max-width:100%}.original .panel img{width:auto}.progress{position:fixed;z-index:3;bottom:16px;left:50%;transform:translateX(-50%);display:flex;align-items:center;gap:9px;padding:8px 11px;border:1px solid var(--line);border-radius:9px;background:rgba(16,17,22,.9);backdrop-filter:blur(16px);color:var(--muted);font-size:12px;transition:opacity .2s ease}.progress.hidden{opacity:0;pointer-events:none}.mode{font-size:12px}.mode.active{border-color:var(--accent);color:var(--accent)}.jump{width:58px}@media(max-width:700px){.reader-bar{padding:10px;gap:6px}.meta{display:none}.reader{padding-top:54px}.control,select{height:32px;padding:0 8px}.title{font-size:13px}.fit .panel,.comfort .panel{width:100%}.reader-bar .nav-label{display:none}}
</style></head><body><header class="reader-bar" id="bar"><a class="control" href="javascript:history.back()">Library</a><div class="title">__TITLE__</div><span class="meta">__EDITION__ · Chapter __CHAPTER__</span><a class="control nav-label" href="__PREVIOUS__">Previous</a><select id="chapter">__OPTIONS__</select><a class="control nav-label" href="__NEXT__">Next</a></header><main id="reader" class="reader fit">__IMAGES__</main><footer class="progress" id="progress"><span id="position">Panel 1</span><input id="jump" class="control jump" type="number" min="1"><button class="control mode active" data-mode="fit">Fit</button><button class="control mode" data-mode="comfort">Comfort</button><button class="control mode" data-mode="original">Original</button></footer><script>
const state=__PAYLOAD__, reader=document.querySelector('#reader'),bar=document.querySelector('#bar'),progress=document.querySelector('#progress'),position=document.querySelector('#position'),jump=document.querySelector('#jump'),panels=[...document.querySelectorAll('.panel')],series=__SERIES_JSON__;let current=1,lastSave=0;const stored=JSON.parse(localStorage.getItem(state.key)||'null');const widthKey=state.key+':width';function save(){localStorage.setItem(state.key,JSON.stringify({panel:current,offset:scrollY}));lastSave=Date.now()}function setMode(mode){reader.className='reader '+mode;localStorage.setItem(widthKey,mode);document.querySelectorAll('.mode').forEach(button=>button.classList.toggle('active',button.dataset.mode===mode))}setMode(localStorage.getItem(widthKey)||'fit');jump.max=state.total;jump.value=1;document.querySelector('#chapter').addEventListener('change',event=>{location.href='/reader?'+new URLSearchParams({edition:'__EDITION__',series,chapter:event.target.value})});document.querySelectorAll('.mode').forEach(button=>button.addEventListener('click',()=>setMode(button.dataset.mode)));jump.addEventListener('change',()=>{const target=panels[Math.max(0,Math.min(state.total,Number(jump.value)||1)-1)];target?.scrollIntoView({block:'start'})});const observer=new IntersectionObserver(entries=>{let best=null;for(const entry of entries){if(entry.isIntersecting&&(!best||entry.intersectionRatio>best.intersectionRatio))best=entry}if(best){current=Number(best.target.dataset.panel);position.textContent='Panel '+current+' of '+state.total;jump.value=current;if(Date.now()-lastSave>1000)save()}},{threshold:[.35,.55,.75]});panels.forEach(panel=>observer.observe(panel));let idle;addEventListener('mousemove',()=>{bar.classList.remove('hidden');progress.classList.remove('hidden');clearTimeout(idle);idle=setTimeout(()=>{bar.classList.add('hidden');progress.classList.add('hidden')},1800)});addEventListener('keydown',event=>{if(event.key==='j'||event.key==='ArrowDown'||event.key==='PageDown')scrollBy({top:innerHeight*.82,behavior:'smooth'});if(event.key==='k'||event.key==='ArrowUp'||event.key==='PageUp')scrollBy({top:-innerHeight*.82,behavior:'smooth'});if(event.key==='g'){event.preventDefault();jump.focus();jump.select()}if(event.key==='f')document.documentElement.requestFullscreen?.()});addEventListener('beforeunload',save);if(stored?.offset)setTimeout(()=>scrollTo(0,stored.offset),120);
</script></body></html>"""
