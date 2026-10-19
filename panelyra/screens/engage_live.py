"""Engage live screen."""
from __future__ import annotations

def render(*, Path, parse_bot_progress, read_log, render_live_progress_region, render_page_header, render_setup_stepper, st):
    runtime = st.session_state.bot_runtimes["comment"]
    log_path = runtime.get("log_path")
    log_text = read_log(Path(log_path), int(st.session_state.console_max_chars)) if log_path else "No comment run started yet."
    progress = parse_bot_progress(log_text)
    render_page_header("Engage", "Live run", ["Animated progress", "Verified submissions", "Mac controls"])
    render_live_progress_region("comment")
    with st.expander("Diagnostics", expanded=bool(progress["stop_reason"])):
        st.code(log_text, language="text")
