"""Activity screen."""
from __future__ import annotations

def download_run_records(root=None):
    """Present genuine durable Downloads jobs alongside legacy run receipts."""
    from pathlib import Path
    from panelyra.automation.download_queue import read_queue, SUCCESS
    root = Path(root) if root is not None else Path(__file__).resolve().parents[2]
    records = []
    for job in read_queue(root)['jobs']:
        chapters = job.get('chapters', {})
        records.append({
            'bot_type': 'manga_download', 'run_id': job['id'],
            'title': job.get('title') or 'Chapter download',
            'status': job['status'],
            'stage': 'Ready in Library' if job['status'] == 'completed' else job['status'].replace('_', ' ').title(),
            'progress_current': sum(c.get('status') in SUCCESS for c in chapters.values()),
            'progress_total': len(chapters), 'updated_at': job.get('updated_at', 0),
            'settings': {'series': job.get('title', ''), 'source': job.get('source', ''),
                         'series_url': job.get('series_url', ''), 'chapters': ', '.join(chapters)},
            'stop_reason': '; '.join(dict.fromkeys(c['error'] for c in chapters.values() if c.get('error'))),
        })
    return records

def render(*, display_path, json, list_run_records, redact_private_paths, render_page_header, restore_run_configuration, set_page, st, time, uuid):
    render_page_header(
        "Activity",
        "Search, inspect, export, and retry durable records from every Panelyra workspace.",
        ["Searchable records", "JSON export", "Retry setup"],
    )
    records = list_run_records(limit=100)
    try:
        records = sorted(records + download_run_records(), key=lambda r: r.get('updated_at', 0), reverse=True)[:100]
    except (OSError, ValueError) as exc:
        st.warning(f'Download receipts are unavailable: {exc}')
    filter_columns = st.columns([2, 1, 1])
    history_search = filter_columns[0].text_input(
        "Search",
        key="run_history_search",
        placeholder="Run ID, stage, URL, or stop reason",
    ).strip().lower()
    bot_filter = filter_columns[1].selectbox(
        "Workspace",
        options=["All", "Downloads", "Engage", "Capture", "Studio (retired)"],
    )
    status_filter = filter_columns[2].selectbox(
        "Status",
        options=["All", "running", "completed", "partial", "failed", "stopped"],
    )
    bot_values = {"Downloads": "manga_download", "Engage": "comment", "Capture": "download", "Studio (retired)": "word_art"}
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
            label = 'Downloads' if record.get('bot_type') == 'manga_download' else record.get('bot_type', 'bot').replace('_', ' ').title()
            title = f" · {record['title']}" if record.get('title') else ''
            with st.expander(
                f"{label}{title} · {record.get('status', 'unknown')} · {record.get('run_id', '')}",
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
                if record.get('bot_type') == 'manga_download':
                    if st.button('Open download queue', key=f'queue_receipt_{run_id}', width='stretch'):
                        st.session_state.manga_download_tab = 'Queue'
                        set_page('Downloads')
                        st.rerun()
                    continue
                history_actions = st.columns(1)
                if history_actions[0].button(
                    "Retry from saved configuration",
                    key=f"retry_{run_id}",
                disabled=record.get("bot_type") not in {"comment", "download"},
                    width="stretch",
                ):
                    restore_run_configuration(record)
                    st.rerun()
