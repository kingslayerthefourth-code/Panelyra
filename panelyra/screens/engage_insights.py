"""Engage insights screen."""
from __future__ import annotations

def render(*, COMMENT_STYLES, Counter, HISTORY_FILE, history_records, render_jsonl_metric_grid, render_page_header, render_panel, st, style_recommendation):
    records = history_records
    render_page_header(
        "Engage Insights",
        "Review generated comment history, balance style variety, and inspect repeated keywords.",
        [f"{len(COMMENT_STYLES)} styles", "History analytics", "Research source tracking"],
    )
    st.markdown("### JSONL Metrics")
    st.divider()
    with st.container(border=True):
        render_jsonl_metric_grid(records, columns=4)
        st.info(style_recommendation(records))

    if records:
        style_counts = Counter(str(record.get("style", "unknown")) for record in records)
        research_counts = Counter(str(record.get("research_source", "chapter-only") or "chapter-only") for record in records)
        keyword_counts = Counter(str(record.get("keyword", "")).lower() for record in records if str(record.get("keyword", "")).strip())
        st.markdown("### Analysis Charts")
        st.divider()
        with st.container(border=True):
            chart_cols = st.columns(3)
            chart_cols[0].bar_chart([{"style": style, "count": count} for style, count in style_counts.items()], x="style", y="count")
            chart_cols[1].bar_chart([{"source": source, "count": count} for source, count in research_counts.items()], x="source", y="count")
            chart_cols[2].bar_chart(
                [{"keyword": keyword, "count": count} for keyword, count in keyword_counts.most_common(int(st.session_state.top_keywords))],
                x="keyword",
                y="count",
            )

        recent_rows = [
            {
                "chapter": record.get("chapter_index"),
                "style": record.get("style"),
                "keyword": record.get("keyword"),
                "emoji": "yes" if record.get("emoji_used") else "no",
                "reflection": "yes" if record.get("reflection_used") else "no",
                "research": record.get("research_source") or "chapter-only",
                "comment": record.get("comment"),
            }
            for record in records[-int(st.session_state.history_rows):]
        ]
        st.markdown("### Recent Records")
        st.divider()
        with st.container(border=True):
            st.dataframe(recent_rows, width="stretch", hide_index=True)
    else:
        render_panel("No History Yet", f"History will appear after a run writes records to {HISTORY_FILE.name}.")
