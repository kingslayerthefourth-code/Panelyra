"""Capture screen."""
from __future__ import annotations

def render(*, MANHWA_CONTAINER_SELECTOR, MANHWA_IMAGE_SELECTOR, OCR_DOWNLOAD_DIR, Path, apply_ocr_download_defaults, display_path, load_selector_profiles, preflight_url, render_page_header, running, set_page, st, start_manhwa_ocr_bot):
    download_runtime = st.session_state.bot_runtimes["download"]
    apply_ocr_download_defaults()
    capture_profiles = load_selector_profiles()
    if st.session_state.ocr_selector_profile not in capture_profiles:
        st.session_state.ocr_selector_profile = next(iter(capture_profiles))
    capture_profile_values = capture_profiles[st.session_state.ocr_selector_profile]
    for field_name, profile_key in (
        ("ocr_next_chapter_selector", "next_link"),
        ("ocr_next_page_selector", "next_page"),
        ("ocr_page_counter_selector", "page_counter"),
        ("ocr_chapter_list_selector", "chapter_list"),
    ):
        if field_name not in st.session_state:
            st.session_state[field_name] = capture_profile_values.get(profile_key, "")
    render_page_header("Capture", "Save chapter images, archives, and optional OCR.")
    st.markdown("### Choose the source")
    source_columns = st.columns([3, 1])
    source_columns[0].text_input(
        "Starting URL",
        key="ocr_start_url",
        placeholder="https://staging.example.com/chapter-1",
        help="Paste the first chapter page. Capture follows the reader using the layout selected below.",
    )
    source_columns[1].number_input(
        "Maximum chapters",
        min_value=1,
        max_value=10000,
        step=1,
        key="ocr_max_chapters",
    )
    st.caption("OUTPUTS")
    outputs = st.columns(3)
    outputs[0].checkbox("Save chapter images", key="ocr_save_images")
    outputs[1].checkbox("Create CBZ archive", key="ocr_create_archive")
    outputs[2].checkbox("Create OCR transcript", key="ocr_save_text")
    with st.expander("Advanced · layout, selectors and performance"):
        st.selectbox(
            "Reader layout",
            options=["auto", "continuous", "next-chapter", "paginated", "mixed"],
            key="ocr_reader_layout",
            format_func=lambda value: {
                "auto": "Auto detect",
                "continuous": "Continuous vertical pages",
                "next-chapter": "Next chapter control",
                "paginated": "Next page reader",
                "mixed": "Pages plus next chapter",
            }[value],
        )
        st.selectbox("Capture performance", ["Balanced", "Rapid", "Reliable"], key="ocr_performance_profile")
        st.text_input("Custom output folder", key="ocr_custom_output_dir", placeholder="Default: manhwa")
        st.slider("OCR confidence", min_value=0.05, max_value=0.95, step=0.05, key="ocr_download_confidence")
        profile_columns = st.columns([3, 1])
        profile_columns[0].selectbox(
            "Selector profile",
            options=list(capture_profiles),
            key="ocr_selector_profile",
        )
        if profile_columns[1].button("Apply profile", key="capture_apply_profile", width="stretch"):
            values = capture_profiles[st.session_state.ocr_selector_profile]
            st.session_state.ocr_next_chapter_selector = values.get("next_link", "")
            st.session_state.ocr_next_page_selector = values.get("next_page", "")
            st.session_state.ocr_page_counter_selector = values.get("page_counter", "")
            st.session_state.ocr_chapter_list_selector = values.get("chapter_list", "")
            st.rerun()
        advanced_columns = st.columns(2)
        advanced_columns[0].text_input("Chapter container", key="ocr_container_selector")
        advanced_columns[1].text_input("Chapter images", key="ocr_download_image_selector")
        advanced_columns[0].text_input("Next chapter", key="ocr_next_chapter_selector")
        advanced_columns[1].text_input("Next page", key="ocr_next_page_selector")
        advanced_columns[0].text_input("Page counter", key="ocr_page_counter_selector")
        advanced_columns[1].text_input("Chapter list", key="ocr_chapter_list_selector")
        advanced_columns[0].number_input(
            "Minimum scroll delay",
            min_value=0.0,
            max_value=5.0,
            step=0.01,
            key="ocr_scroll_min_delay",
        )
        advanced_columns[1].number_input(
            "Maximum scroll delay",
            min_value=0.0,
            max_value=5.0,
            step=0.01,
            key="ocr_scroll_max_delay",
        )
        st.number_input(
            "Image fetch timeout",
            min_value=1.0,
            max_value=120.0,
            step=1.0,
            key="ocr_image_fetch_timeout",
        )
    effective_ocr_output_dir = str(
        Path(str(st.session_state.ocr_custom_output_dir).strip() or str(OCR_DOWNLOAD_DIR)).expanduser()
    )
    effective_ocr_container_selector = (
        str(st.session_state.ocr_container_selector).strip() or MANHWA_CONTAINER_SELECTOR
    )
    effective_ocr_image_selector = (
        str(st.session_state.ocr_download_image_selector).strip() or MANHWA_IMAGE_SELECTOR
    )
    capture_blockers = preflight_url(str(st.session_state.ocr_start_url))
    if float(st.session_state.ocr_scroll_max_delay) < float(st.session_state.ocr_scroll_min_delay):
        capture_blockers.append("fix the scan timing")
    if int(st.session_state.ocr_max_chapters) > 1 and not str(st.session_state.ocr_next_chapter_selector).strip():
        capture_blockers.append("add a next-chapter selector")
    if st.session_state.ocr_reader_layout in {"paginated", "mixed"} and not str(
        st.session_state.ocr_next_page_selector
    ).strip():
        capture_blockers.append("add a next-page selector")

    selected_outputs = [label for enabled, label in ((st.session_state.ocr_save_images, "images"), (st.session_state.ocr_create_archive, "CBZ"), (st.session_state.ocr_save_text, "OCR")) if enabled]
    if not selected_outputs:
        capture_blockers.append("choose at least one output")
    st.caption(f"Up to {st.session_state.ocr_max_chapters} chapter(s) · {', '.join(selected_outputs) or 'No outputs selected'} · {display_path(effective_ocr_output_dir)}")
    st.caption("Capture opens Chrome. Use browser review when you need to log in or check the chapter first.")
    if capture_blockers and st.session_state.ocr_start_url:
        st.warning("Before starting: " + "; ".join(capture_blockers))
    review_navigation = st.columns([1, 1, 2])
    def launch_capture(wait_for_inspection: bool) -> None:
        st.session_state.bot_process, st.session_state.log_path, st.session_state.start_signal_path = start_manhwa_ocr_bot(
            str(st.session_state.ocr_start_url).strip(),
            int(st.session_state.ocr_max_chapters),
            wait_for_inspection,
            effective_ocr_output_dir,
            effective_ocr_container_selector,
            effective_ocr_image_selector,
            float(st.session_state.ocr_download_confidence),
            float(st.session_state.ocr_scroll_min_delay),
            float(st.session_state.ocr_scroll_max_delay),
            float(st.session_state.ocr_image_fetch_timeout),
            bool(st.session_state.ocr_save_images),
            bool(st.session_state.ocr_create_archive),
            bool(st.session_state.ocr_save_text),
            str(st.session_state.ocr_reader_layout),
            str(st.session_state.ocr_next_chapter_selector).strip(),
            str(st.session_state.ocr_next_page_selector).strip(),
            str(st.session_state.ocr_page_counter_selector).strip(),
            str(st.session_state.ocr_chapter_list_selector).strip(),
            str(st.session_state.ocr_performance_profile),
        )
        set_page("Capture Live")
        st.rerun()

    inspect_clicked = review_navigation[0].button(
        "Browser review",
        icon=":material/open_in_browser:",
        key="capture_prepare",
        disabled=running or bool(capture_blockers),
        width="stretch",
    )
    direct_clicked = review_navigation[1].button(
        "Start Capture",
        icon=":material/download:",
        key="capture_start",
        type="primary",
        disabled=running or bool(capture_blockers),
        width="stretch",
    )
    if inspect_clicked:
        launch_capture(True)
    if direct_clicked:
        launch_capture(False)
