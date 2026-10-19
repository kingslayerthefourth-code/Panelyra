"""Storage screen."""
from __future__ import annotations

def render(*, format_bytes, prune_generated_files, render_page_header, save_local_preferences, st, storage_report):
    render_page_header(
        "Storage",
        "Inspect generated artifacts, apply retention policies, and preserve protected content.",
        ["Usage report", "Guarded cleanup", "Legacy compatibility"],
    )
    report = storage_report()
    total_bytes = sum(int(item["bytes"]) for item in report)
    total_files = sum(int(item["files"]) for item in report)
    storage_metrics = st.columns(3)
    storage_metrics[0].metric("Managed storage", format_bytes(total_bytes))
    storage_metrics[1].metric("Managed files", total_files)
    storage_metrics[2].metric("Retention", f"{int(st.session_state.retention_days)} days")


    st.markdown("### Storage Areas")
    st.dataframe(
        [
            {
                "Area": item["label"],
                "Size": item["size"],
                "Files": item["files"],
                "Automatic cleanup": "Allowed" if item["clearable"] else "Protected",
                "Path": item["path"],
            }
            for item in report
        ],
        width="stretch",
        hide_index=True,
    )
    with st.container(border=True):
        st.markdown("#### Retention Policy")
        retention_days = st.number_input(
            "Keep generated logs and records for at least",
            min_value=1,
            max_value=3650,
            step=1,
            key="retention_days",
            help="Cleanup removes only files at least this old from an explicitly selected generated area.",
        )
        cleanup_options = {
            str(item["label"]): str(item["key"])
            for item in report
            if bool(item["clearable"])
        }
        cleanup_label = st.selectbox("Generated area", options=list(cleanup_options))
        cleanup_confirmation = st.text_input(
            "Type CLEAN to confirm",
            key="storage_cleanup_confirmation",
            placeholder="CLEAN",
        )
        if st.button(
            "Apply retention cleanup",
            disabled=cleanup_confirmation != "CLEAN",
            width="stretch",
        ):
            removed_files, removed_bytes = prune_generated_files(
                cleanup_options[cleanup_label],
                int(retention_days),
            )
            save_local_preferences(
                {
                    "onboarding_complete": bool(st.session_state.onboarding_complete),
                    "authorization_confirmed": bool(st.session_state.authorization_confirmed),
                    "retention_days": int(retention_days),
                }
            )
            st.success(f"Removed {removed_files} file(s), releasing {format_bytes(removed_bytes)}.")
            st.rerun()

    st.info(
        "Capture downloads, Studio assets, history, read markers, and legacy OCR output are protected. "
        "Panelyra never deletes those areas automatically."
    )
