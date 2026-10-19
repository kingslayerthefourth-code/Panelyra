"""Capture live screen."""
from __future__ import annotations

def render(*, Path, cancel_prepared_capture, inspect_prepared_capture_browser, parse_ocr_progress, read_log, render_live_progress_region, render_page_header, render_setup_stepper, st):
    runtime = st.session_state.bot_runtimes["download"]
    log_path = runtime.get("log_path")
    log_text = read_log(Path(log_path), int(st.session_state.console_max_chars)) if log_path else "No download run started yet."
    progress = parse_ocr_progress(log_text)
    render_page_header("Capture", "Live run", ["Animated progress", "Reader pages", "Original images"])
    if st.session_state.lyra_capture_prepared:
        if not st.session_state.lyra_capture_browser_snapshot:
            inspect_prepared_capture_browser()
        snapshot = dict(st.session_state.lyra_capture_browser_snapshot)
        with st.container(border=True, key="lyra_capture_review"):
            st.markdown("### Browser review required")
            st.write(
                "This prepared run is paused. Check the chapter in Chrome, "
                "then use **Begin Capture** below."
            )
            review_columns = st.columns(3)
            review_columns[0].metric("Status", str(st.session_state.lyra_capture_review_status).title())
            review_columns[1].metric("Chapters", int(st.session_state.ocr_max_chapters))
            outputs = [
                label for enabled, label in (
                    (st.session_state.ocr_save_images, "Images"),
                    (st.session_state.ocr_create_archive, "CBZ"),
                    (st.session_state.ocr_save_text, "OCR"),
                ) if enabled
            ]
            review_columns[2].metric("Outputs", ", ".join(outputs) or "Metadata")
            st.code(str(st.session_state.ocr_start_url), language="text")
            if snapshot.get("available"):
                st.caption(
                    f"Chrome: {snapshot.get('title', 'Untitled page')} · {snapshot.get('url', '')}"
                )
            for warning in snapshot.get("warnings", []):
                st.warning(str(warning))
            if snapshot.get("ready_to_begin"):
                st.success("The Capture browser is connected and matches the requested site.")
            review_actions = st.columns(2)
            if review_actions[0].button(
                "Recheck Browser",
                key="lyra_capture_recheck",
                width="stretch",
            ):
                inspect_prepared_capture_browser()
                st.rerun()
            if review_actions[1].button(
                "Cancel Prepared Capture",
                key="lyra_capture_cancel",
                width="stretch",
            ):
                cancel_prepared_capture()
                st.rerun()
    render_live_progress_region("download")
    with st.expander("Diagnostics", expanded=bool(progress["failures"])):
        st.code(log_text, language="text")
