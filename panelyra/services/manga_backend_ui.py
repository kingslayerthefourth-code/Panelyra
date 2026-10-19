"""Dedicated title-search and download workspace, with reviewed Library imports."""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit


def _read_request(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _library_name(request: dict) -> str:
    title = request.get("title") or urlsplit(request.get("series_url", "")).path.rstrip("/").split("/")[-1]
    return re.sub(r"[^a-z0-9]+", "-", str(title).lower()).strip("-") or "my-series"


def chapter_evidence(staging: Path, request: dict) -> dict:
    """Compare explicit requested chapter identities with archive names, not exit status."""
    from decimal import Decimal, InvalidOperation
    def number(value):
        return str(Decimal(value).normalize())
    expected = set()
    try:
        for part in str(request.get("chapters", "")).replace(" ", "").split(","):
            ends = part.split("-")
            if len(ends) == 1:
                expected.add(number(ends[0]))
            elif len(ends) == 2 and all(re.fullmatch(r"\d+", end) for end in ends) and 0 <= int(ends[1])-int(ends[0]) <= 50:
                expected.update(number(str(n)) for n in range(int(ends[0]), int(ends[1])+1))
            else:
                expected = None
                break
    except (InvalidOperation, ValueError):
        expected = None
    found = set()
    for path in staging.glob("*.cbz"):
        match = re.fullmatch(r"(\d+(?:\.\d+)?)(?:-v\d+)?", path.stem)
        if match and path.stat().st_size:
            found.add(number(match[1]))
    missing = sorted(expected-found, key=Decimal) if expected is not None else None
    return {"requested": expected, "found": found, "missing": missing}


def evidence_text(staging: Path, request: dict) -> str:
    evidence = chapter_evidence(staging, request)
    if evidence["requested"] is None:
        return f"{len(evidence['found'])} chapter archive(s) found · requested {request.get('chapters', '?')} · range completion unverified"
    matched = len(evidence["found"] & evidence["requested"])
    text = f"{matched} of {len(evidence['requested'])} requested chapter archives found"
    if evidence["missing"]:
        text += " · missing: " + ", ".join(evidence["missing"])
    return text + " · contents require review"


def _start_download(series_url: str, chapters: str, title: str = "", cover: str = "") -> None:
    import streamlit as st
    from panelyra.automation.manga_backend import ROOT, build_command
    from panelyra.automation.download_queue import enqueue, ensure_worker, job_dir
    selected = st.session_state.get("manga_selected_series") or {}
    source = selected.get("source", "") if selected.get("url") == series_url else ""
    build_command(series_url, chapters, ROOT, browser_consent=True)
    lookup = st.session_state.get("manga_catalog_task")
    catalog = lookup.snapshot() if lookup and lookup.url == series_url else None
    # Never perform a blocking source lookup on the Download click. The list
    # above owns discovery; explicit ranges remain usable if it is unavailable.
    job = enqueue(ROOT, series_url, chapters, title, source, catalog or {}, cover=cover)
    st.session_state.manga_latest_job = job["id"]
    st.session_state.manga_review_run = job_dir(ROOT, job) / "request.json"
    ensure_worker(ROOT)
    st.session_state.manga_queued_notice = f"{title or 'Your series'} · chapters {chapters} added to Queue."


def _render_status() -> None:
    import streamlit as st
    process = st.session_state.get("manga_process")
    if process is None:
        return
    running = process.poll() is None
    @st.fragment(run_every=2 if running else None)
    def status():
        code = process.poll()
        staging = Path(st.session_state.manga_staging)
        if code is None:
            with st.container(border=True):
                st.markdown("**Downloading chapters…**")
                st.caption(evidence_text(staging, _read_request(staging / "request.json")))
                if st.button("Stop download", key="manga_stop"):
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    st.info("Stopping download…")
        else:
            if code == 0:
                evidence = chapter_evidence(staging, _read_request(staging / "request.json"))
                if evidence["missing"] is None or evidence["missing"]:
                    st.warning("Download process ended; not all requested chapters are confirmed.")
                else:
                    st.info("Requested archive names are present. Review their contents in My downloads before adding them to Library.")
                st.caption(evidence_text(staging, _read_request(staging / "request.json")))
            else:
                st.warning("Download stopped or failed. Check My downloads for any completed chapters and details.")
            if running:
                st.rerun()
    status()


def _render_downloads() -> None:
    import streamlit as st
    from panelyra.automation.manga_backend import ROOT, import_reviewed_archive
    base = ROOT / "data" / "manga_backend"
    runs = sorted(base.glob("run-*/request.json"), key=lambda p: p.stat().st_mtime, reverse=True) if base.exists() else []
    if not runs:
        st.info("Your downloads will appear here. Find a title to get started.")
        return
    def label(path):
        request = _read_request(path)
        date = datetime.fromtimestamp(path.stat().st_mtime).strftime("%d %b, %H:%M")
        return f"{request.get('title') or _library_name(request).replace('-', ' ').title()} · chapters {request.get('chapters', '?')} · {date}"
    if st.session_state.get("manga_review_run") not in runs:
        st.session_state.manga_review_run = runs[0]
    selected = st.selectbox("Download", runs, format_func=label, key="manga_review_run")
    staging = selected.parent
    request = _read_request(selected)
    process = st.session_state.get("manga_process")
    running = process is not None and process.poll() is None
    with st.expander("Download details"):
        st.write(request.get("series_url", ""))
        log = staging / "download.log"
        if log.is_file():
            with log.open("rb") as handle:
                handle.seek(max(0, log.stat().st_size - 16000))
                st.code(handle.read().decode("utf-8", errors="replace"), language="text")
    if request.get("cover"):
        preview, summary = st.columns([1, 4], gap="medium", vertical_alignment="center")
        with preview:
            from panelyra.services.title_search import render_cover
            render_cover(st, request, compact=True)
        with summary:
            st.markdown(f"**{request.get('title') or _library_name(request).replace('-', ' ').title()}**")
            st.caption(f"Requested chapters {request.get('chapters', '?')} · {request.get('source') or 'Saved source'}")
    if running and str(staging) == st.session_state.get("manga_staging"):
        st.info("This download is still running. Chapters will be available for review when it finishes.")
        return
    from panelyra.automation.download_queue import read_queue
    try:
        queue_job = next((j for j in read_queue(ROOT)["jobs"] if j["id"] == request.get("queue_job_id")), None)
    except (ValueError, OSError) as exc:
        st.error(str(exc))
        return
    if queue_job and queue_job["status"] in {"running", "queued", "interrupted"}:
        st.info("Check Queue for progress. Review this batch when it has finished or been cancelled.")
        return
    archives = sorted(staging.glob("*.cbz"))
    if not archives:
        st.info("No completed chapters found. Check Download details for the reason.")
        return
    st.caption(evidence_text(staging, request))
    archive = st.selectbox("Chapter", archives, format_func=lambda p: f"Chapter {p.stem}", key=f"manga_archive_{staging.name}")
    st.caption("Confirm the source chapter before adding it to your library.")
    identity = f"{staging.name}_{archive.stem}"
    with st.form(f"manga_import_{identity}"):
        url = st.text_input("Original chapter URL", value=request.get("chapter_urls", {}).get(archive.stem, ""), placeholder="Paste the source page for this chapter")
        with st.expander("Library folder"):
            slug = st.text_input("Folder name", value=_library_name(request))
        approved = st.checkbox("I checked the download details and confirmed this chapter and series")
        submitted = st.form_submit_button("Add to Library", type="primary")
    if submitted:
        if not approved:
            st.error("Confirm the chapter and series before adding it to Library.")
        else:
            try:
                known_url = request.get("chapter_urls", {}).get(archive.stem)
                destination = import_reviewed_archive(archive, request["series_url"], url.strip(), slug.strip(), title=request.get("title", ""), verified_number=archive.stem if known_url and url.strip() == known_url else None)
                st.success(f"Chapter {destination.name} added to Library.")
            except Exception as exc:
                st.error(f"Not imported: {exc}")


def render_manga_backend() -> None:
    import streamlit as st
    @st.fragment
    def workspace():
        with st.container(key="manga_download_workspace"):
            _render_manga_backend()
    workspace()


def _render_manga_backend() -> None:
    import streamlit as st
    from panelyra.automation.manga_backend import BINARY, VERSION, install_backend
    from panelyra.services.title_search import _inject_download_style
    _inject_download_style(st)
    for key in ("manga_result_source", "manga_results_page", "manga_search_query", "manga_search_sources", "manga_chapters", "manga_source_mode"):
        if key in st.session_state:
            st.session_state[key] = st.session_state[key]
    _render_status()
    if st.session_state.get("manga_queued_notice"):
        st.success(st.session_state.pop("manga_queued_notice"))
    browse, queue, downloads = st.tabs(["Find & download", "Queue", "My downloads"], default=st.session_state.get("manga_download_tab", "Find & download"))
    with browse:
        if not BINARY.is_file():
            st.info("Set up the downloader once to save chapters from supported sources.")
            if st.button("Set up downloader", key="manga_install", type="primary"):
                try:
                    with st.spinner("Setting up downloader…"):
                        install_backend()
                    st.rerun()
                except Exception as exc:
                    st.error(f"Setup failed: {exc}")
        mode = st.radio("Find a series", ["Search titles", "Paste link"], horizontal=True, key="manga_source_mode", label_visibility="collapsed")
        if mode == "Paste link":
            from panelyra.services.source_catalog import render_source_catalog
            render_source_catalog()
        selected = st.session_state.get("manga_selected_series") if mode == "Search titles" else None
        title_task = st.session_state.get("manga_title_task")
        if selected and title_task:
            selected = next((row for row in title_task.snapshot().results if row.get("url") == selected["url"]), selected)
            st.session_state.manga_selected_series = selected
        if mode == "Search titles" and selected is None:
            from panelyra.services.title_search import render_title_search
            render_title_search()
        else:
            with st.container(border=True, key="manga_selected_title"):
                if selected:
                    cover_column, heading, change = st.columns([1, 3.4, 1], gap="medium", vertical_alignment="center")
                    from panelyra.services.title_search import render_cover
                    with cover_column:
                        render_cover(st, selected, compact=True)
                    heading.subheader(selected["title"])
                    from panelyra.services.chapter_counts import chapter_count_label
                    count = chapter_count_label(selected) if selected.get("chapter_count") is not None else ""
                    heading.caption(selected["source"] + (" · " + count if count else ""))
                    if change.button("Change title", key="manga_change_title", width="stretch"):
                        st.session_state.pop("manga_selected_series", None)
                        st.session_state.manga_download_tab = "Find & download"
                        st.rerun()
                    st.link_button("View source", selected["url"])
                    _render_availability(selected)

                with st.form("manga_download_form"):
                    series_url = selected["url"] if selected else st.text_input("Series URL", key="manga_series_url", placeholder="Paste the series page link")
                    chapters = st.text_input("Chapters", placeholder="1, 3, 5-1000", key="manga_chapters", help="Up to 10,000 chapters in a resumable queue. Verified chapters enter Reader automatically; ambiguous archives stay in Review.")
                    st.caption("Queueing may open Chrome. Saved and queued chapters are skipped. Downloads continue when you close this page.")
                    process = st.session_state.get("manga_process")
                    running = process is not None and process.poll() is None
                    clicked = st.form_submit_button("Download chapters", type="primary", disabled=running or not BINARY.is_file(), width="stretch", key="manga_download")
                if clicked:
                    try:
                        arguments = (
                            series_url.strip(),
                            chapters.strip(),
                            selected["title"] if selected else "",
                        )
                        cover = selected.get("cover", "") if selected else ""
                        with st.spinner("Adding chapters to your queue…"):
                            _start_download(*arguments, cover=cover) if cover else _start_download(*arguments)
                        st.success(st.session_state.pop("manga_queued_notice", "Chapters queued."))
                    except Exception as exc:
                        st.error(str(exc))
                _render_current_download()
        st.caption(f"Powered by manga-downloader {VERSION} · availability depends on the source.")
    with queue:
        _render_queue()
    with downloads:
        _render_downloads()


def _render_availability(selected):
    import streamlit as st
    from panelyra.automation.manga_backend import ROOT
    from panelyra.services.title_discovery import ChapterCatalogTask
    from panelyra.services.download_identity import existing_chapters, library_chapters, aliases
    lookup = st.session_state.get("manga_catalog_task")
    if lookup is None or lookup.url != selected["url"]:
        lookup = ChapterCatalogTask(selected["url"], selected.get("source", ""))
        st.session_state.manga_catalog_task = lookup

    @st.fragment(run_every=1 if lookup.snapshot() is None else None)
    def availability():
        with st.container(border=True):
            catalog = lookup.snapshot()
            if catalog is None:
                from time import monotonic
                from panelyra.services.waiting_feedback import duration, note
                elapsed = monotonic() - lookup.created_at
                st.markdown("**Finding chapters…**")
                st.status(lookup.stage, state="running")
                st.caption(f"{duration(elapsed)} elapsed · {selected.get('source') or 'Source'} · time remaining: estimating…")
                st.caption(note(elapsed))
                st.caption("The full discovered list will appear here together. You can also enter a range below.")
                return
            if st.button("Refresh availability", key="manga_refresh_availability"):
                st.session_state.manga_catalog_task = ChapterCatalogTask(selected["url"], selected.get("source", ""), refresh=True)
                st.rerun(scope="app")
            saved = existing_chapters(ROOT / "manhwa", selected["url"], selected["title"])
            st.caption(f"{len(saved)} English chapters confirmed in Library. A public link does not guarantee downloadable images; downloads verify the archive.")
            keys = aliases(selected["url"], selected["title"])
            uncertain = {row["number"] for row in library_chapters(ROOT / "manhwa") if row["complete"] and row["language"] == "und" and keys & row["aliases"]}
            if uncertain:
                st.warning(f"{len(uncertain)} matching Library chapters have no confirmed language. Check those before downloading; they are not automatically skipped.")
            if catalog.get("notice"):
                st.info(catalog["notice"])
            rows = [{"Chapter": n, "Availability": "In Library" if n in saved else c["status"].replace("_", " ").capitalize()} for n, c in catalog.get("chapters", {}).items()]
            if rows:
                st.progress(1.0, text=f"{len(rows)} chapters found · {len(saved)} already in Library")
                st.dataframe(rows, hide_index=True, width="stretch")
            if not catalog.get("complete"):
                st.caption("The chapter list may be partial. Unlisted chapters remain unverified.")
    availability()


def _render_current_download():
    """Keep the latest receipt and live progress next to the Download button."""
    import streamlit as st
    from panelyra.automation.manga_backend import ROOT
    from panelyra.automation import download_queue as q
    from panelyra.services.waiting_feedback import queue_feedback, duration, note
    @st.fragment(run_every=1)
    def current_download():
        identity = st.session_state.get("manga_latest_job")
        if not identity:
            return
        try:
            state = q.read_queue(ROOT)
            job = next((j for j in state["jobs"] if j["id"] == identity), None)
            if job is None:
                return
            from panelyra.engine import policy
            globally_paused = policy(ROOT)
            feedback = queue_feedback(job, paused=state["paused"] or globally_paused, running=q.worker_running(ROOT))
            with st.container(border=True):
                st.markdown(f"**{feedback['label']}**")
                counts = f"{feedback['ready']} / {feedback['total']} chapters ready or already saved"
                if feedback['elsewhere']:
                    counts += f" · {feedback['elsewhere']} queued in another job"
                st.progress(feedback["fraction"], text=counts)
                st.caption("Find chapters → Receive pages → Check archive → Ready in Reader")
                if feedback["active"]:
                    if globally_paused:
                        st.info("Background work is paused in Settings. Resume it there when you want these chapters to start.")
                    elif state["paused"]:
                        st.info("Queue paused. Open Queue → Resume queue to continue.")
                    else:
                        eta = f"about {duration(feedback['eta'])} left" if feedback["eta"] is not None else "time remaining: estimating…"
                        st.caption(f"{duration(feedback['elapsed'])} on this chapter · {eta}")
                        st.caption(note(feedback["elapsed"]))
                if job["status"] in {"failed", "partial", "cancelled"}:
                    st.warning("Completed chapters are kept. Open Queue for details or to retry the unfinished ones.")
        except (ValueError, OSError) as exc:
            st.error(f"Could not read download progress: {exc}")
    current_download()


def _render_queue():
    import streamlit as st
    from panelyra.automation.manga_backend import ROOT
    from panelyra.automation import download_queue as q
    @st.fragment(run_every=3)
    def panel():
        try:
            state = q.read_queue(ROOT)
            jobs = state["jobs"]
            if not jobs:
                st.info("Your queued chapters will appear here.")
                return
            running = q.worker_running(ROOT)
            from panelyra.engine import policy
            from panelyra.services.waiting_feedback import queue_feedback, duration, note
            globally_paused = policy(ROOT)
            active = any(job["status"] in q.ACTIVE for job in jobs)
            if active:
                st.caption("Queue paused" if state["paused"] else "Downloading in the background" if running else "Ready to resume")
                left, right = st.columns(2)
                if left.button("Pause after current chapter", disabled=state["paused"], key="queue_pause"):
                    q.set_paused(ROOT, True); st.rerun(scope="fragment")
                if right.button("Resume queue", disabled=running and not state["paused"], key="queue_resume", type="primary"):
                    q.set_paused(ROOT, False); q.ensure_worker(ROOT); st.rerun(scope="fragment")
                if globally_paused:
                    st.info("Background work is paused in Settings. Resume it there to start queued downloads.")
            else:
                st.caption("Your latest downloads")
            for job in reversed(jobs):
                with st.container(border=True):
                    if job.get("cover"):
                        cover_column, job_summary = st.columns([1, 4], gap="medium", vertical_alignment="center")
                        with cover_column:
                            from panelyra.services.title_search import render_cover
                            render_cover(st, job, compact=True)
                        with job_summary:
                            st.markdown(f"**{job.get('title') or _library_name(job)}**")
                            st.caption(f"{job.get('source') or 'Saved source'} · {job['status'].capitalize()}")
                    else:
                        st.markdown(f"**{job.get('title') or _library_name(job)}** · {job['status'].capitalize()}")
                    chapters = job["chapters"]
                    complete = sum(c["status"] in q.SUCCESS | {"already_queued"} for c in chapters.values())
                    st.progress(complete / max(1, len(chapters)), text=f"{complete} of {len(chapters)} chapters completed or skipped")
                    feedback = queue_feedback(job, paused=state['paused'] or globally_paused, running=running)
                    st.caption(feedback['label'])
                    if feedback['active'] and not (state['paused'] or globally_paused):
                        eta = f"about {duration(feedback['eta'])} left" if feedback['eta'] is not None else "time remaining: estimating…"
                        st.caption(f"{duration(feedback['elapsed'])} on this chapter · {eta}")
                        st.caption(note(feedback['elapsed']))
                    if job["status"] in {"failed", "partial"}:
                        remaining = sum(c["status"] in {"failed", "pending", "cancelled"} for c in chapters.values())
                        st.caption(f"{remaining} chapters need another try. Completed chapters are kept.")
                    with st.expander("Chapter details"):
                        st.dataframe([{"Chapter": n, "Status": c["status"].replace("_", " "), "Attempts": c["attempts"], "Details": c.get("error", "")} for n,c in chapters.items()], hide_index=True, width="stretch")
                    if job["status"] in q.ACTIVE:
                        if st.button("Cancel job", key="cancel_" + job["id"]):
                            q.cancel_job(ROOT, job["id"]); st.rerun(scope="fragment")
                    elif job["status"] in {"failed", "partial", "cancelled"}:
                        if st.button("Retry incomplete chapters", key="retry_" + job["id"]):
                            q.retry_job(ROOT, job["id"]); q.ensure_worker(ROOT); st.rerun(scope="fragment")
        except (ValueError, OSError) as exc:
            st.error(str(exc))
    panel()
