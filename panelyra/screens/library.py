"""Library screen."""
from __future__ import annotations

def apply_clean_transition(st):
    """Move the review queue before Streamlit instantiates its bound widgets."""
    transition = st.session_state.pop('_reader_clean_transition', None)
    if transition is not None:
        st.session_state.reader_clean_batch = transition['queue']
        if transition.get('chapter'):
            st.session_state.reader_chapter = transition['chapter']

def render(*, APP_DIR, PANELYRA_LOG_DIR, Path, READER_PROGRESS_FILE, ReaderProgressStore, _chapter_integrity, _chapter_number, _reader_cover_data, html, is_process_running, json, local_reader_service, reader_catalog, render_cleaner_review, render_page_header, set_page, st, start_clean_review, subprocess, sys):
    from urllib.parse import urlencode
    from tablet_ui import render_chapter_delivery
    # Apply cleaner transitions before the chapter/queue widgets are created.
    # Streamlit rejects assigning a widget-backed key later in the same run.
    apply_clean_transition(st)
    restored_query = st.query_params.get('library_search')
    if restored_query is not None and st.session_state.get('_restored_library_query') != restored_query:
        st.session_state.library_search = restored_query
        st.session_state._restored_library_query = restored_query

    render_page_header("Library", "Your saved chapters, ready when you are.")
    for runtime_type, runtime in st.session_state.bot_runtimes.items():
        if is_process_running(runtime.get("process")):
            label = {"download": "Capture", "comment": "Engage", "word_art": "Studio (retired)"}.get(runtime_type, runtime_type)
            st.info(f"{label} is running.")
            if runtime_type in {"download", "comment"} and st.button(f"View {label} progress", key=f"library_live_{runtime_type}"):
                set_page(f"{label} Live")
                st.rerun()
    catalog = reader_catalog()
    if not catalog:
        st.info("Your library is empty. Find a title in Downloads or save a chapter with Capture.")
        left, right = st.columns(2)
        if left.button("Find a title", type="primary"):
            set_page("Downloads"); st.rerun()
        if right.button("Capture a chapter"):
            set_page("Capture Setup"); st.rerun()
        return
    progress_store = ReaderProgressStore(READER_PROGRESS_FILE)
    def reader_url(series, edition, chapter):
        return local_reader_service().reader_url(edition, series, chapter, lyra_token="")
    # Skip deleted chapters and stale editions when choosing a resume target.
    from panelyra.services.download_identity import chapter_key
    resume_labels = {(s,e,chapter_key(n)):n for s,entry in catalog.items() for e,chapters in entry['editions'].items() for n,c in chapters.items() if c['healthy'] and c['verified']}
    valid_progress = [item for item in progress_store.all() if (item.series,item.edition,item.chapter) in resume_labels and item.percent > 0 and not item.completed]
    latest = max(valid_progress, key=lambda item: item.updated_at, default=None)
    if latest:
        with st.container(border=True, key="library-resume"):
            left, right = st.columns([3, 1])
            left.caption("CONTINUE READING")
            left.subheader(str(catalog[latest.series]["title"]))
            left.caption(f"Chapter {latest.chapter} · {latest.edition} · {latest.percent:.0f}%")
            right.link_button("Continue reading", reader_url(latest.series, latest.edition, resume_labels[(latest.series,latest.edition,latest.chapter)]), type="primary", width="stretch")
    st.link_button("Chapter updates · check all series", local_reader_service().base_url + "/updates?" + urlencode({"reader_token": local_reader_service().reader_token}))
    with st.container():
        query = st.text_input("Search your library", placeholder="Search by series title", key="library_search").strip().casefold()
        visible = [key for key in catalog if query in str(catalog[key]["title"]).casefold()]
        visible.sort(key=lambda key: str(catalog[key]["title"]).casefold())
        st.caption(f"{len(visible)} of {len(catalog)} series")
        cards=[]
        for key in visible:
            entry=catalog[key]
            count=len({n for chapters in entry["editions"].values() for n,c in chapters.items() if c["healthy"] and c["verified"]})
            review=len({n for chapters in entry["editions"].values() for n,c in chapters.items() if not c["healthy"] or not c["verified"]})
            count_label=f"{count} saved chapters"+(f" · {review} need review" if review else "")
            cover=_reader_cover_data(entry)
            selected=key == st.session_state.reader_series
            href=local_reader_service().series_url(key) + "&" + urlencode({"library_search": query})
            art=f'<img src="{cover}" alt="" loading="lazy">' if cover else '<span class="book-placeholder" aria-hidden="true">P</span>'
            cards.append(f'<a id="book-{html.escape(key, quote=True)}" class="library-book {"selected" if selected else ""}" href="{html.escape(href)}" target="_self" aria-label="Select {html.escape(str(entry["title"]), quote=True)}">{art}<strong>{html.escape(str(entry["title"]))}</strong><small>{count_label}</small></a>')
        if cards:
            st.markdown('<div class="library-grid">'+''.join(cards)+'</div>', unsafe_allow_html=True)
        else:
            st.info("No saved series matches that title. Try a shorter search.")
    from streamlit.components.v1 import html as component_html
    component_html((Path(__file__).resolve().parents[1] / "web/library_return.html").read_text(), height=0)
    if st.query_params.get("manage") != "1":
        if st.session_state.reader_series in catalog and not st.session_state.get("_series_link_shown"):
            st.link_button("Open selected series", local_reader_service().series_url(st.session_state.reader_series))
        return
    st.caption("Chapter file management · return to the series page to read")
    if st.session_state.reader_series in catalog:
        st.link_button("Back to series", local_reader_service().series_url(st.session_state.reader_series))
    series=st.session_state.reader_series
    if series not in catalog:
        if series: st.info("That series is no longer available. Select a saved book above.")
        return
    if query and series not in visible:
        return
    entry=catalog[series]
    st.divider()
    st.subheader(str(entry["title"]))
    editions=entry["editions"]
    options=[name for name in ("Original", "Clean") if name in editions]
    if st.session_state.reader_edition not in options: st.session_state.reader_edition=options[0]
    if len(options)>1:
        st.radio("Edition", options, horizontal=True, key="reader_edition", format_func=lambda name: "Trimmed" if name=="Clean" else name)
    else:
        st.caption("Trimmed edition" if options[0]=="Clean" else "Original edition")
    edition=st.session_state.reader_edition
    chapters=editions[edition]
    ordered=sorted(chapters, key=_chapter_number)
    if st.session_state.reader_chapter not in chapters: st.session_state.reader_chapter=ordered[0]
    chapter_id=st.selectbox("Chapter", ordered, key="reader_chapter", format_func=lambda key:f"Chapter {key} · {len(chapters[key]['images'])} pages")
    chapter=chapters[chapter_id]
    ok, integrity=_chapter_integrity(chapter) if chapter["images"] else (False,"No readable images saved for this chapter.")
    if not ok: st.warning(integrity)
    else: st.link_button("Read chapter", reader_url(series,edition,chapter_id), type="primary", width="stretch")
    with st.expander("Chapter actions · files, trimming and tablet"):
        chapter_dir=Path(chapter["chapter_dir"])
        files=sorted([p for p in chapter_dir.iterdir() if p.is_file() and p.suffix.lower() in {".cbz", ".txt", ".json"}])
        st.caption(f"{len(chapter['images'])} image files · {len(files)} supporting files")
        if files:
            chosen=st.selectbox("Chapter file",files,format_func=lambda p:p.name,key=f"chapter_file_{series}_{edition}_{chapter_id}")
            # Read only the chosen file; do not load every archive on each library render.
            if st.checkbox("Prepare file download",key=f"prepare_file_{series}_{edition}_{chapter_id}"):
                try:
                    st.download_button("Download file",chosen.read_bytes(),file_name=chosen.name)
                except OSError:
                    st.warning("This file is no longer available. Refresh the library.")
        if chapter["images"]:
            with st.expander("Image files"):
                image_file=st.selectbox("Page image",chapter["images"],format_func=lambda p:p.name,key=f"page_file_{series}_{edition}_{chapter_id}")
                if chapter.get('artifact') and not image_file.is_file():
                    from panelyra.services.library_store import materialize
                    materialize(APP_DIR,chapter['artifact'],chapter['checksum'])
                try: st.download_button("Download page image",image_file.read_bytes(),file_name=image_file.name)
                except OSError: st.warning("This image is no longer available.")
        original=editions.get("Original",{}).get(chapter_id)
        if st.button("Trim page edges",disabled=not original or not original.get("images"),help="Review and crop margins. Original files are preserved."):
            st.session_state.library_trim=True
            start_clean_review(series,chapter_id,Path(original["chapter_dir"]))
            st.rerun()
        st.caption("Trimming crops margins only. It does not remove Korean text or translate pages.")
        render_chapter_delivery(APP_DIR, series, chapter_id, available=bool(original and original.get("images")))
    if st.session_state.get("library_trim"):
        st.markdown("### Trim page edges")
        if st.button("Close trimming", key="close_library_trim"):
            st.session_state.library_trim=False; st.rerun()
        if editions.get("Original"):
            # Separate picker widget keys from the main Library selection widgets.
            render_cleaner_review({series:entry})
        else: st.info("An Original chapter is required to trim page edges.")
