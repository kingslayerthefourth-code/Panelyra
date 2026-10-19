#!/usr/bin/env python3
"""Pure regressions for Panelyra's macOS progress presentation helpers."""

from __future__ import annotations

import re
import sys
from pathlib import Path


APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

from panelyra.progress_ui import (  # noqa: E402
    ActivityItem,
    PROGRESS_CSS,
    RunProgressModel,
    build_progress_html,
    extract_activity_items,
    resolve_health,
    resolve_progress_state,
)


def make_model(**overrides: object) -> RunProgressModel:
    values = {
        "component_id": "engage-live-progress",
        "workspace": "engage",
        "title": "Engage run",
        "state": "running",
        "health": "healthy",
        "current": 2,
        "total": 8,
        "previous_current": 1,
        "stage": "Submitting chapter 2",
        "elapsed": "1m 12s",
        "eta": "About 3m",
        "last_updated": "just now",
    }
    values.update(overrides)
    return RunProgressModel(**values)


def assert_state_resolution() -> None:
    expected = {
        "preparing": resolve_progress_state(status="created"),
        "running": resolve_progress_state(running=True, status="running"),
        "paused": resolve_progress_state(paused=True, status="running"),
        "waiting": resolve_progress_state(waiting=True, status="running"),
        "completed": resolve_progress_state(status="completed"),
        "stopped": resolve_progress_state(status="stopped"),
        "failed": resolve_progress_state(status="failed"),
    }
    assert expected == {
        "preparing": "preparing",
        "running": "running",
        "paused": "paused",
        "waiting": "waiting",
        "completed": "completed",
        "stopped": "stopped",
        "failed": "failed",
    }

    # A durable partial run is stopped early, never shown as completed.
    assert resolve_progress_state(status="partial", return_code=0) == "stopped"

    # Pause and user-attention signals outrank stale process/terminal hints.
    assert resolve_progress_state(paused=True, waiting=True, status="failed") == "paused"
    assert resolve_progress_state(waiting=True, status="completed") == "waiting"
    assert resolve_progress_state(
        running=True,
        status="running",
        stage="Waiting for user confirmation",
    ) == "waiting"

    # Durable terminal state outranks an old startup stage.
    assert resolve_progress_state(status="failed", stage="opening-browser") == "failed"
    assert resolve_progress_state(status="completed", stage="opening browser") == "completed"
    assert resolve_progress_state(status="", return_code=0) == "completed"
    assert resolve_progress_state(status="", return_code=9) == "failed"


def assert_health_resolution() -> None:
    assert resolve_health("running", 0, refresh_seconds=2) == "healthy"
    assert resolve_health("running", 29.99, refresh_seconds=2) == "healthy"
    assert resolve_health("running", 30, refresh_seconds=2) == "slowing"
    assert resolve_health("running", 119.99, refresh_seconds=2) == "slowing"
    assert resolve_health("running", 120, refresh_seconds=2) == "stalled"

    # A deliberately slow refresh cadence scales both freshness thresholds.
    assert resolve_health("running", 39, refresh_seconds=10) == "healthy"
    assert resolve_health("running", 40, refresh_seconds=10) == "slowing"
    assert resolve_health("running", 149, refresh_seconds=10) == "slowing"
    assert resolve_health("running", 150, refresh_seconds=10) == "stalled"

    # OCR/image writes may be quiet for five minutes without a false stall.
    assert resolve_health("running", 299.99, stage="Saving images") == "slowing"
    assert resolve_health("running", 300, stage="OCR pages") == "stalled"

    assert resolve_health("paused", 10_000) == "paused"
    assert resolve_health("waiting", 10_000) == "waiting"
    assert resolve_health("completed", 10_000) == "complete"
    assert resolve_health("stopped", 10_000) == "stopped"
    assert resolve_health("failed", 10_000) == "failed"
    assert resolve_health("running", 1, reconnecting=True) == "reconnecting"


def assert_capture_activity() -> None:
    log_text = "\n".join(
        (
            "======== CAPTURE ========",
            "2026-08-17 21:01:02 INFO Reader media progress: page 3/12, 7 unique image(s).",
            "Traceback continuation that must not appear",
            "2026-08-17 21:01:03 INFO Reader media progress: page 4/?, 8 unique image(s).",
            "2026-08-17 21:01:04 WARNING Saving images is taking longer than expected",
        )
    )
    activities = extract_activity_items(log_text, "capture", limit=3)
    assert len(activities) == 3
    assert activities[0].message == "Page 3 of 12 · 7 unique images"
    assert activities[0].kind == "capture-progress"
    assert activities[1].message == "Page 4 of ? · 8 unique images"
    assert activities[1].kind == "capture-progress"
    assert activities[2].severity == "warning"
    assert all("Traceback" not in activity.message for activity in activities)

    limited = extract_activity_items(log_text, "Capture", limit=1)
    assert len(limited) == 1
    assert limited[0].time_label == "21:01:04"


def assert_escaping_and_aria() -> None:
    hostile = '<script>alert("x")</script><img src=x onerror="boom"> & done'
    model = make_model(
        component_id='7 bad id" onclick="boom',
        title=hostile,
        stage=hostile,
        aria_label='Run "unsafe" & progress',
        activities=(ActivityItem("10:20:30", hostile),),
        detail_items=((hostile, hostile),),
        notice=hostile,
        receipt_title=hostile,
        receipt_body=hostile,
        receipt_items=((hostile, hostile),),
    )
    rendered = build_progress_html(model)
    assert "<script>" not in rendered
    assert "<img " not in rendered
    assert "onclick=\"boom" not in rendered
    assert "&lt;script&gt;" in rendered
    assert "&lt;img src=x onerror=&quot;boom&quot;&gt;" in rendered
    assert 'aria-label="Run &quot;unsafe&quot; &amp; progress"' in rendered
    assert 'id="p-7-bad-id-onclick-boom-label"' in rendered

    determinate = build_progress_html(make_model(current=2, total=8))
    assert 'role="progressbar"' in determinate
    assert 'aria-valuemin="0"' in determinate
    assert 'aria-valuemax="100"' in determinate
    assert 'aria-valuenow="25"' in determinate
    assert '<strong class="p-percent" aria-hidden="true">25%</strong>' in determinate
    assert "2 of 8" in determinate

    indeterminate = build_progress_html(make_model(current=4, total=None))
    assert "p-is-indeterminate" in indeterminate
    assert 'aria-valuenow="' not in indeterminate
    assert '<strong class="p-percent" aria-hidden="true">—</strong>' in indeterminate
    assert "4 completed; total not yet known" in indeterminate


def assert_motion_and_reduced_motion() -> None:
    assert "p-motion-full" in build_progress_html(make_model(), motion_mode="Full")
    assert "p-motion-subtle" in build_progress_html(make_model(), motion_mode="Subtle")
    assert "p-motion-off" in build_progress_html(make_model(), motion_mode="Off")
    assert "p-motion-off" in build_progress_html(make_model(), motion_mode="disabled")

    assert "@media (prefers-reduced-motion: reduce)" in PROGRESS_CSS
    reduced_block = PROGRESS_CSS.split("@media (prefers-reduced-motion: reduce)", 1)[1]
    assert "animation: none !important" in reduced_block
    assert "transition: none !important" in reduced_block
    assert ".panelyra-progress .p-flow { display: none; }" in reduced_block


def assert_compact_and_history() -> None:
    compact_model = make_model(
        receipt_title="Capture complete",
        receipt_body="Saved safely.",
        history=tuple(range(100)),
    )
    compact = build_progress_html(compact_model, compact=True)
    assert "panelyra-progress" in compact
    assert "p-compact" in compact
    assert 'data-compact="true"' in compact
    assert '<section class="p-receipt"' in compact
    assert re.search(
        r"\.panelyra-progress\.p-compact\s+\.p-track\s*\{\s*height:\s*4px;",
        PROGRESS_CSS,
    )
    # Compact cards retain receipt data in the markup but intentionally hide
    # the full receipt; the full Live surface remains the authoritative view.
    compact_hide_rule = re.search(
        r"\.panelyra-progress\.p-compact\s+\.p-kicker,(.*?)\{\s*display:\s*none;\s*\}",
        PROGRESS_CSS,
        flags=re.DOTALL,
    )
    assert compact_hide_rule
    assert ".panelyra-progress.p-compact .p-receipt" in compact_hide_rule.group(0)

    history_match = re.search(r'<polyline points="([^"]+)"></polyline>', compact)
    assert history_match
    # Rendering is intentionally bounded to the latest 36 samples.
    points = history_match.group(1).split()
    assert len(points) == 36
    assert points[0].startswith("0.00,")
    assert points[-1].startswith("180.00,")

    assert "p-history" not in build_progress_html(
        make_model(history=(0.1, 0.2, 0.3)),
        show_history=False,
    )
    assert "p-history" not in build_progress_html(make_model(history=(0.1,)))


def main() -> int:
    assert_state_resolution()
    assert_health_resolution()
    assert_capture_activity()
    assert_escaping_and_aria()
    assert_motion_and_reduced_motion()
    assert_compact_and_history()
    print("Progress UI regression checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
