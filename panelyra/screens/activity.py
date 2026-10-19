"""Activity screen."""
from __future__ import annotations

def render(*, display_path, json, list_run_records, redact_private_paths, render_page_header, restore_run_configuration, set_page, st, time, uuid):
    render_page_header(
        "Activity",
        "Search, inspect, export, and retry durable records from every Panelyra workspace.",
        ["Searchable records", "JSON export", "Retry setup"],
    )
    records = list_run_records(limit=100)
    filter_columns = st.columns([2, 1, 1])
    history_search = filter_columns[0].text_input(
        "Search",
        key="run_history_search",
        placeholder="Run ID, stage, URL, or stop reason",
    ).strip().lower()
    bot_filter = filter_columns[1].selectbox(
        "Workspace",
        options=["All", "Engage", "Capture", "Studio (retired)"],
    )
    status_filter = filter_columns[2].selectbox(
        "Status",
        options=["All", "running", "completed", "partial", "failed", "stopped"],
    )
    bot_values = {"Engage": "comment", "Capture": "download", "Studio (retired)": "word_art"}
    if bot_filter != "All":
        records = [record for record in records if record.get("bot_type") == bot_values[bot_filter]]
    if status_filter != "All":
        records = [record for record in records if str(record.get("status", "")).lower() == status_filter]
    if history_search:
        records = [
            record
            for record in records
            if history_search in json.dumps(record, ensure_ascii=False).lower()
        ]
    st.download_button(
        "Export visible run report",
        data=json.dumps(records, indent=2, ensure_ascii=False),
        file_name=f"panelyra-run-report-{time.strftime('%Y%m%d-%H%M%S')}.json",
        mime="application/json",
        disabled=not records,
        width="stretch",
    )
    if not records:
        st.info("No matching runs. Downloads, Capture and Engage runs appear here.")
    else:
        for record in records:
            with st.expander(
                f"{record.get('bot_type', 'bot').replace('_', ' ').title()} · {record.get('status', 'unknown')} · {record.get('run_id', '')}",
                expanded=False,
            ):
                cols = st.columns(4)
                cols[0].metric("Stage", record.get("stage", "unknown"))
                cols[1].metric("Progress", f"{record.get('progress_current', 0)} / {record.get('progress_total', 0)}")
                cols[2].metric("PID", record.get("pid", 0) or "n/a")
                cols[3].metric("Updated", time.strftime("%Y-%m-%d %H:%M", time.localtime(record.get("updated_at", 0))))
                if record.get("stop_reason"):
                    st.error(str(record["stop_reason"]))
                st.code(redact_private_paths(json.dumps(record.get("settings", {}), indent=2)), language="json")
                if record.get("log_path"):
                    st.caption(display_path(record["log_path"]))
                run_id = str(record.get("run_id", uuid.uuid4().hex))
                history_actions = st.columns(1)
                if history_actions[0].button(
                    "Retry from saved configuration",
                    key=f"retry_{run_id}",
                disabled=record.get("bot_type") not in {"comment", "download"},
                    width="stretch",
                ):
                    restore_run_configuration(record)
                    st.rerun()
