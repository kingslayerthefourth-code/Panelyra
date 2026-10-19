"""Pure presentation helpers for Panelyra's macOS progress surfaces.

The functions in this module deliberately do not import Streamlit or inspect the
filesystem.  Runtime data is normalized by the app and passed in as a
``RunProgressModel``; this module only derives display state and renders escaped
HTML.  Keeping the boundary this small makes the component straightforward to
unit test and prevents UI work from changing automation behavior.
"""

from __future__ import annotations

import html
import math
import re
from dataclasses import dataclass, field
from typing import Mapping, Optional, Sequence, Tuple, Union


_STATE_NAMES = {
    "preparing",
    "running",
    "paused",
    "waiting",
    "completed",
    "stopped",
    "failed",
}

_HEALTH_NAMES = {
    "healthy",
    "slowing",
    "stalled",
    "reconnecting",
    "paused",
    "waiting",
    "complete",
    "stopped",
    "failed",
}

_WORKSPACE_NAMES = {"engage", "capture", "studio"}

_STATE_LABELS = {
    "preparing": "Preparing",
    "running": "Running",
    "paused": "Paused",
    "waiting": "Waiting for you",
    "completed": "Completed",
    "stopped": "Stopped",
    "failed": "Failed",
}

_HEALTH_LABELS = {
    "healthy": "Live",
    "slowing": "Slowing",
    "stalled": "No recent update",
    "reconnecting": "Reconnecting",
    "paused": "Paused",
    "waiting": "Needs attention",
    "complete": "Complete",
    "stopped": "Stopped",
    "failed": "Failed",
}

_ACTIVITY_LINE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})\s+"
    r"(?P<time>\d{2}:\d{2}:\d{2})\s+"
    r"(?P<level>DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+"
    r"(?P<message>.+?)\s*$",
    re.IGNORECASE,
)

_CAPTURE_PROGRESS = re.compile(
    r"Reader\s+media\s+progress:\s*page\s+(?P<page>\d+)\s*/\s*"
    r"(?P<pages>\d+|\?)\s*,\s*(?P<images>\d+)\s+unique\s+image(?:\(s\)|s)?\.?",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ActivityItem:
    """One trustworthy, timestamped line in the recent-activity list."""

    time_label: str
    message: str
    severity: str = "info"
    kind: str = ""


@dataclass(frozen=True)
class StageItem:
    """A step in the small run timeline."""

    label: str
    status: str = "pending"


@dataclass
class RunProgressModel:
    """All display data required by a full or compact progress surface.

    Counts are optional on purpose.  Unknown totals stay indeterminate rather
    than inventing progress.  ``history`` accepts either fractions (0..1) or
    percentages (0..100).
    """

    component_id: str
    workspace: str
    title: str
    state: str = "preparing"
    health: str = "healthy"
    current: Optional[float] = None
    total: Optional[float] = None
    previous_current: Optional[float] = None
    stage: str = "Preparing your run"
    elapsed: str = "—"
    eta: str = "Calculating"
    last_updated: str = "Just now"
    status_label: str = ""
    subprogress_label: str = ""
    subprogress_current: Optional[float] = None
    subprogress_total: Optional[float] = None
    stages: Sequence[StageItem] = field(default_factory=tuple)
    activities: Sequence[ActivityItem] = field(default_factory=tuple)
    history: Sequence[float] = field(default_factory=tuple)
    detail_items: Sequence[Tuple[str, str]] = field(default_factory=tuple)
    notice: str = ""
    receipt_title: str = ""
    receipt_body: str = ""
    receipt_items: Sequence[Tuple[str, str]] = field(default_factory=tuple)
    aria_label: str = ""
    announcement: str = ""
    motion_phase: float = 0.0

    @property
    def is_indeterminate(self) -> bool:
        return not _has_known_total(self.current, self.total)

    @property
    def progress_fraction(self) -> Optional[float]:
        return _fraction(self.current, self.total)

    @property
    def previous_fraction(self) -> Optional[float]:
        if not _has_known_total(self.previous_current, self.total):
            return self.progress_fraction
        return _fraction(self.previous_current, self.total)


def resolve_progress_state(
    paused: bool = False,
    waiting: bool = False,
    running: bool = False,
    status: str = "",
    stage: str = "",
    return_code: Optional[int] = None,
) -> str:
    """Resolve runtime hints into one stable visual state.

    Explicit pause and user-wait signals take precedence over transient process
    fields.  This matters when a live child process remains alive while it is
    paused or waiting for the user.  A durable ``partial`` result is presented
    as stopped, never as a successful completion.
    """

    normalized_status = _normalized_token(status)
    normalized_stage = _normalized_token(stage)

    if paused:
        return "paused"

    waiting_markers = (
        "waiting-for-user",
        "waiting for user",
        "waiting for you",
        "needs-user",
        "needs user",
        "user-action",
        "user action",
        "manual-step",
        "manual step",
    )
    if waiting or any(marker in normalized_status for marker in waiting_markers):
        return "waiting"
    if any(marker in normalized_stage for marker in waiting_markers):
        return "waiting"

    # Durable terminal status wins over stale stage text such as
    # ``opening-browser`` left behind by a child process that has exited.
    if normalized_status in {"completed", "complete", "succeeded", "success"}:
        return "completed"
    if normalized_status in {
        "partial",
        "stopped",
        "cancelled",
        "canceled",
        "terminated",
        "interrupted",
    }:
        return "stopped"
    if normalized_status in {"failed", "failure", "error", "crashed"}:
        return "failed"

    preparing_statuses = {
        "created",
        "queued",
        "starting",
        "preparing",
        "process-started",
        "launching",
        "opening-browser",
    }
    preparing_markers = (
        "preparing",
        "starting",
        "launching",
        "opening browser",
        "opening-browser",
    )
    if normalized_status in preparing_statuses:
        return "preparing"
    if any(marker in normalized_stage for marker in preparing_markers):
        return "preparing"

    if running or normalized_status in {"running", "active", "resumed"}:
        return "running"

    if return_code is not None:
        return "completed" if return_code == 0 else "failed"
    return "preparing"


def resolve_health(
    state: str,
    last_update_age: Optional[float],
    refresh_seconds: float = 2.0,
    stage: str = "",
    reconnecting: bool = False,
) -> str:
    """Return a conservative freshness label for a progress surface.

    Slowing and stalled are only possible while actively preparing or running.
    Image saving and OCR can legitimately be quiet for longer, so those stages
    use a five-minute stalled threshold.  Paused and waiting runs never appear
    stalled.
    """

    normalized_state = state if state in _STATE_NAMES else "preparing"
    terminal_health = {
        "completed": "complete",
        "stopped": "stopped",
        "failed": "failed",
        "paused": "paused",
        "waiting": "waiting",
    }
    if normalized_state in terminal_health:
        return terminal_health[normalized_state]

    if reconnecting:
        return "reconnecting"
    if last_update_age is None:
        return "healthy"

    try:
        age = max(0.0, float(last_update_age))
        cadence = max(0.25, float(refresh_seconds))
    except (TypeError, ValueError):
        return "healthy"

    slowing_after = max(30.0, cadence * 4.0)
    stalled_after = max(120.0, cadence * 15.0)
    normalized_stage = _normalized_token(stage)
    if any(word in normalized_stage for word in ("ocr", "saving images", "save images")):
        stalled_after = max(stalled_after, 300.0)

    if age >= stalled_after:
        return "stalled"
    if age >= slowing_after:
        return "slowing"
    return "healthy"


def build_stage_items(
    workspace: str,
    state: str,
    stage_text: str = "",
    markers: Optional[Union[Mapping[str, bool], Sequence[str]]] = None,
) -> Tuple[StageItem, ...]:
    """Build a compact workspace-specific stage timeline.

    The timeline only reflects the current durable stage and supplied markers;
    it never advances based on elapsed time.
    """

    workspace_name = _workspace_name(workspace)
    marker_text = _marker_text(markers)
    haystack = " ".join(part for part in (_normalized_token(stage_text), marker_text) if part)

    if workspace_name == "capture":
        labels = ("Prepare", "Open reader", "Scan pages", "Save images", "Finish")
        stage_terms = (
            ("prepare", "start", "launch"),
            ("browser", "reader", "login", "open"),
            ("scan", "page", "media progress", "inspect"),
            ("saving images", "saved images", "save batch", "downloading", "downloaded", "writing files", "output folder"),
            ("finish", "complete", "done"),
        )
    elif workspace_name == "studio":
        review_mode = any(
            term in marker_text
            for term in (
                "review-mode",
                "review mode",
                "review_mode",
                "skip-submit",
                "skip submit",
                "skip_submit",
            )
        )
        final_label = "Ready" if review_mode else "Save"
        labels = ("Prepare", "Open Studio", "Upload", "Edit", final_label)
        stage_terms = (
            ("prepare", "start", "launch"),
            ("browser", "studio", "login", "open"),
            ("upload", "image", "asset"),
            ("edit", "editor", "chapter", "fill"),
            ("save", "submit", "review", "ready", "complete", "done"),
        )
    else:
        labels = ("Prepare", "Open browser", "Scan", "Submit", "Verify")
        stage_terms = (
            ("prepare", "start", "launch"),
            ("browser", "login", "open"),
            ("scan", "chapter", "find", "inspect"),
            ("submit", "comment", "send"),
            ("verify", "confirm", "complete", "done"),
        )

    current_index = 0
    for index, terms in enumerate(stage_terms):
        if any(term in haystack for term in terms):
            current_index = max(current_index, index)

    normalized_state = state if state in _STATE_NAMES else "preparing"
    items = []
    for index, label in enumerate(labels):
        if normalized_state == "completed":
            item_status = "complete"
        elif index < current_index:
            item_status = "complete"
        elif index > current_index:
            item_status = "pending"
        elif normalized_state == "failed":
            item_status = "failed"
        elif normalized_state == "paused":
            item_status = "paused"
        elif normalized_state == "waiting":
            item_status = "waiting"
        elif normalized_state == "stopped":
            item_status = "stopped"
        else:
            item_status = "active"
        items.append(StageItem(label=label, status=item_status))
    return tuple(items)


def extract_activity_items(
    log_text: str,
    workspace: str,
    limit: int = 3,
) -> Tuple[ActivityItem, ...]:
    """Extract recent trustworthy activity from timestamped runtime log lines.

    Banners, trace continuations, and unstructured output are ignored.  Capture's
    actual reader-media line is recognized explicitly, including its ``page``
    word, so real page and image counts are preserved.
    """

    if not log_text or limit <= 0:
        return tuple()

    workspace_name = _workspace_name(workspace)
    items = []
    for raw_line in str(log_text).splitlines():
        match = _ACTIVITY_LINE.match(raw_line.strip())
        if not match:
            continue

        level = match.group("level").lower()
        message = match.group("message").strip()
        kind = ""

        if workspace_name == "capture":
            capture_match = _CAPTURE_PROGRESS.search(message)
            if capture_match:
                page = int(capture_match.group("page"))
                pages = capture_match.group("pages")
                images = int(capture_match.group("images"))
                image_word = "image" if images == 1 else "images"
                message = "Page {0} of {1} · {2} unique {3}".format(
                    page, pages, images, image_word
                )
                kind = "capture-progress"

        severity = _activity_severity(level, message)
        items.append(
            ActivityItem(
                time_label=match.group("time"),
                message=message,
                severity=severity,
                kind=kind,
            )
        )

    return tuple(items[-int(limit) :])


def build_progress_html(
    model: RunProgressModel,
    motion_mode: str = "Subtle",
    compact: bool = False,
    show_history: bool = True,
) -> str:
    """Render a safely escaped progress card suitable for ``st.html``.

    No script or interactive action is emitted.  Real controls remain native
    Streamlit widgets next to the component, preserving keyboard and VoiceOver
    behavior on macOS.
    """

    component_id = _safe_identifier(model.component_id)
    workspace = _workspace_name(model.workspace)
    state = model.state if model.state in _STATE_NAMES else "preparing"
    health = model.health if model.health in _HEALTH_NAMES else "healthy"
    motion = _motion_name(motion_mode)

    fraction = model.progress_fraction
    previous_fraction = model.previous_fraction
    indeterminate = fraction is None
    current_fraction = 0.0 if fraction is None else fraction
    old_fraction = current_fraction if previous_fraction is None else previous_fraction
    changed = abs(current_fraction - old_fraction) > 0.00001
    phase = _finite_number(model.motion_phase, 0.0)
    if phase > 0:
        phase = -phase

    style = (
        "--p-from:{0:.6f};--p-to:{1:.6f};"
        "--p-from-left:{2:.3f}%;--p-to-left:{3:.3f}%;--p-phase:{4:.3f}s;".format(
            old_fraction,
            current_fraction,
            old_fraction * 100.0,
            current_fraction * 100.0,
            phase,
        )
    )
    card_classes = [
        "panelyra-progress",
        "p-workspace-{0}".format(workspace),
        "p-state-{0}".format(state),
        "p-health-{0}".format(health),
        "p-motion-{0}".format(motion),
    ]
    if compact:
        card_classes.append("p-compact")
    if indeterminate:
        card_classes.append("p-is-indeterminate")
    if changed:
        card_classes.append("p-has-update")

    title = _escape(model.title or "Run progress")
    stage = _escape(model.stage or _STATE_LABELS[state])
    state_label = _escape(model.status_label or _STATE_LABELS[state])
    health_label = _escape(_HEALTH_LABELS[health])
    percentage_label = "—" if indeterminate else "{0:.0f}%".format(current_fraction * 100.0)

    aria_name = model.aria_label or "{0} progress".format(model.title or workspace.title())
    description_text = _progress_description(model, state, indeterminate)
    aria_values = ""
    if not indeterminate:
        aria_values = (
            ' aria-valuemin="0" aria-valuemax="100" aria-valuenow="{0:.0f}"'.format(
                current_fraction * 100.0
            )
        )

    stages_html = _render_stages(model.stages)
    subprogress_html = _render_subprogress(model, component_id)
    stats_html = _render_stats(model, indeterminate)
    activity_html = _render_activities(model.activities)
    details_html = _render_details(model.detail_items)
    history_html = _render_history(model.history) if show_history else ""
    notice_html = _render_notice(model.notice, state)
    receipt_html = _render_receipt(model)
    announcement_html = ""
    if model.announcement:
        announcement_html = (
            '<p class="p-visually-hidden" role="status" aria-live="polite">{0}</p>'.format(
                _escape(model.announcement)
            )
        )

    compact_attr = "true" if compact else "false"
    html_parts = [
        '<section class="{classes}" style="{style}" data-compact="{compact}" '
        'aria-labelledby="{cid}-label">'.format(
            classes=" ".join(card_classes),
            style=style,
            compact=compact_attr,
            cid=component_id,
        ),
        '<header class="p-header">',
        '<div class="p-heading">',
        '<span class="p-kicker">{0}</span>'.format(_escape(workspace.title())),
        '<h3 id="{0}-label">{1}</h3>'.format(component_id, title),
        '</div>',
        '<div class="p-status-cluster">',
        '<span class="p-health"><span class="p-health-dot" aria-hidden="true"></span>{0}</span>'.format(
            health_label
        ),
        '<span class="p-state">{0}</span>'.format(state_label),
        '<strong class="p-percent" aria-hidden="true">{0}</strong>'.format(percentage_label),
        '</div>',
        '</header>',
        '<div class="p-stage-row">',
        '<p class="p-stage">{0}</p>'.format(stage),
        '<p class="p-updated">Updated {0}</p>'.format(_escape(model.last_updated or "just now")),
        '</div>',
        stages_html,
        '<div class="p-track" role="progressbar" aria-label="{aria}" '
        'aria-describedby="{cid}-description"{values}>'.format(
            aria=_escape(aria_name), cid=component_id, values=aria_values
        ),
        '<div class="p-clip"><div class="p-fill"><span class="p-flow" aria-hidden="true"></span></div></div>',
        '<span class="p-runner" aria-hidden="true"></span>',
        '</div>',
        '<p id="{0}-description" class="p-visually-hidden">{1}</p>'.format(
            component_id, _escape(description_text)
        ),
        subprogress_html,
        stats_html,
        activity_html,
        details_html,
        history_html,
        notice_html,
        receipt_html,
        announcement_html,
        '</section>',
    ]
    return "".join(html_parts)


def _render_stages(stages: Sequence[StageItem]) -> str:
    if not stages:
        return ""
    items = []
    for stage in stages:
        status = stage.status if stage.status in {
            "complete",
            "active",
            "pending",
            "failed",
            "paused",
            "waiting",
            "stopped",
        } else "pending"
        symbol = "✓" if status == "complete" else ""
        items.append(
            '<li class="p-step p-step-{status}">'
            '<span class="p-step-dot" aria-hidden="true">{symbol}</span>'
            '<span class="p-step-label">{label}</span>'
            '</li>'.format(status=status, symbol=symbol, label=_escape(stage.label))
        )
    return '<ol class="p-steps" aria-label="Run stages">{0}</ol>'.format("".join(items))


def _render_subprogress(model: RunProgressModel, component_id: str) -> str:
    if not model.subprogress_label:
        return ""
    fraction = _fraction(model.subprogress_current, model.subprogress_total)
    label = _escape(model.subprogress_label)
    if fraction is None:
        value_label = "—"
        value_attrs = ""
        style = "--p-sub-to:0;"
        indeterminate_class = " p-sub-indeterminate"
        description = "{0}, total not yet known".format(model.subprogress_label)
    else:
        value_label = "{0:.0f}%".format(fraction * 100.0)
        value_attrs = ' aria-valuemin="0" aria-valuemax="100" aria-valuenow="{0:.0f}"'.format(
            fraction * 100.0
        )
        style = "--p-sub-to:{0:.6f};".format(fraction)
        indeterminate_class = ""
        description = "{0}, {1}".format(model.subprogress_label, value_label)
    return (
        '<div class="p-subprogress{indeterminate}" style="{style}">'
        '<div class="p-subheading"><span>{label}</span><strong>{value}</strong></div>'
        '<div class="p-subtrack" role="progressbar" aria-label="{description}"{attrs}>'
        '<span class="p-subfill"></span></div></div>'
    ).format(
        indeterminate=indeterminate_class,
        style=style,
        label=label,
        value=value_label,
        description=_escape(description),
        attrs=value_attrs,
        cid=component_id,
    )


def _render_stats(model: RunProgressModel, indeterminate: bool) -> str:
    if indeterminate:
        completed = _format_number(model.current) if model.current is not None else "—"
        remaining = "—"
    else:
        completed = _format_number(model.current)
        remaining_value = max(0.0, float(model.total or 0.0) - float(model.current or 0.0))
        remaining = _format_number(remaining_value)

    stats = (
        ("Completed", completed),
        ("Remaining", remaining),
        ("Elapsed", model.elapsed or "—"),
        ("ETA", model.eta or "Calculating"),
    )
    return '<dl class="p-stats">{0}</dl>'.format(
        "".join(
            '<div class="p-stat"><dt>{0}</dt><dd>{1}</dd></div>'.format(
                _escape(label), _escape(value)
            )
            for label, value in stats
        )
    )


def _render_activities(activities: Sequence[ActivityItem]) -> str:
    if not activities:
        return ""
    rows = []
    for item in activities:
        severity = item.severity if item.severity in {"info", "success", "warning", "error"} else "info"
        rows.append(
            '<li class="p-activity p-activity-{severity}">'
            '<time>{time}</time><span class="p-activity-mark" aria-hidden="true"></span>'
            '<span>{message}</span></li>'.format(
                severity=severity,
                time=_escape(item.time_label),
                message=_escape(item.message),
            )
        )
    return (
        '<section class="p-activity-section" aria-label="Recent activity">'
        '<h4>Recent activity</h4><ol class="p-activity-list">{0}</ol></section>'
    ).format("".join(rows))


def _render_details(detail_items: Sequence[Tuple[str, str]]) -> str:
    if not detail_items:
        return ""
    return '<dl class="p-details">{0}</dl>'.format(
        "".join(
            '<div><dt>{0}</dt><dd>{1}</dd></div>'.format(_escape(label), _escape(value))
            for label, value in detail_items
        )
    )


def _render_history(history: Sequence[float]) -> str:
    points = _history_points(history)
    if len(points) < 2:
        return ""
    svg_points = " ".join("{0:.2f},{1:.2f}".format(x, y) for x, y in points)
    return (
        '<div class="p-history">'
        '<span>Run pace</span>'
        '<svg viewBox="0 0 180 32" preserveAspectRatio="none" aria-hidden="true" focusable="false">'
        '<path class="p-history-baseline" d="M0 31 H180"></path>'
        '<polyline points="{0}"></polyline>'
        '</svg></div>'
    ).format(svg_points)


def _render_notice(notice: str, state: str) -> str:
    if not notice:
        return ""
    return '<aside class="p-notice p-notice-{0}">{1}</aside>'.format(
        state, _escape(notice)
    )


def _render_receipt(model: RunProgressModel) -> str:
    if not (model.receipt_title or model.receipt_body or model.receipt_items):
        return ""
    title = model.receipt_title or "Run summary"
    body = '<p>{0}</p>'.format(_escape(model.receipt_body)) if model.receipt_body else ""
    items = ""
    if model.receipt_items:
        items = '<dl>{0}</dl>'.format(
            "".join(
                '<div><dt>{0}</dt><dd>{1}</dd></div>'.format(
                    _escape(label), _escape(value)
                )
                for label, value in model.receipt_items
            )
        )
    return (
        '<section class="p-receipt" aria-label="Run receipt">'
        '<span class="p-receipt-icon" aria-hidden="true">✓</span>'
        '<div><h4>{0}</h4>{1}{2}</div></section>'
    ).format(_escape(title), body, items)


def _progress_description(model: RunProgressModel, state: str, indeterminate: bool) -> str:
    parts = [_STATE_LABELS[state]]
    if model.stage:
        parts.append(model.stage)
    if indeterminate:
        if model.current is not None:
            parts.append("{0} completed; total not yet known".format(_format_number(model.current)))
        else:
            parts.append("total not yet known")
    else:
        fraction = model.progress_fraction or 0.0
        parts.append("{0:.0f} percent complete".format(fraction * 100.0))
        parts.append(
            "{0} of {1}".format(_format_number(model.current), _format_number(model.total))
        )
    if model.eta:
        parts.append("ETA {0}".format(model.eta))
    return ". ".join(parts)


def _history_points(history: Sequence[float]) -> Tuple[Tuple[float, float], ...]:
    values = []
    for raw_value in history:
        value = _finite_number(raw_value, None)
        if value is None:
            continue
        if value > 1.0:
            value /= 100.0
        values.append(min(1.0, max(0.0, value)))
    if len(values) < 2:
        return tuple()
    if len(values) > 36:
        values = values[-36:]
    x_step = 180.0 / float(len(values) - 1)
    return tuple(
        (index * x_step, 30.0 - (value * 27.0))
        for index, value in enumerate(values)
    )


def _activity_severity(level: str, message: str) -> str:
    if level in {"error", "critical"}:
        return "error"
    if level == "warning":
        return "warning"
    lowered = message.lower()
    if any(word in lowered for word in ("completed", "verified", "saved", "success")):
        return "success"
    return "info"


def _marker_text(markers: Optional[Union[Mapping[str, bool], Sequence[str]]]) -> str:
    if not markers:
        return ""
    if isinstance(markers, Mapping):
        values = [str(key) for key, enabled in markers.items() if enabled]
    elif isinstance(markers, str):
        values = [markers]
    else:
        values = [str(value) for value in markers]
    return " ".join(_normalized_token(value) for value in values)


def _workspace_name(workspace: str) -> str:
    normalized = _normalized_token(workspace)
    for candidate in _WORKSPACE_NAMES:
        if candidate in normalized:
            return candidate
    return "engage"


def _motion_name(value: str) -> str:
    normalized = _normalized_token(value)
    if normalized == "full":
        return "full"
    if normalized in {"off", "none", "disabled"}:
        return "off"
    return "subtle"


def _normalized_token(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _safe_identifier(value: object) -> str:
    identifier = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(value or "progress")).strip("-")
    if not identifier:
        identifier = "progress"
    if identifier[0].isdigit():
        identifier = "p-{0}".format(identifier)
    return identifier[:80]


def _escape(value: object) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _finite_number(value: object, fallback: Optional[float]) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    return number if math.isfinite(number) else fallback


def _has_known_total(current: object, total: object) -> bool:
    current_number = _finite_number(current, None)
    total_number = _finite_number(total, None)
    return current_number is not None and total_number is not None and total_number > 0.0


def _fraction(current: object, total: object) -> Optional[float]:
    if not _has_known_total(current, total):
        return None
    value = float(current) / float(total)
    return min(1.0, max(0.0, value))


def _format_number(value: object) -> str:
    number = _finite_number(value, None)
    if number is None:
        return "—"
    if abs(number - round(number)) < 0.000001:
        return "{0:,}".format(int(round(number)))
    return "{0:,.1f}".format(number).rstrip("0").rstrip(".")


PROGRESS_CSS = r"""
<style>
.panelyra-progress {
    --p-surface: #1A1C23;
    --p-surface-raised: #1F2128;
    --p-track: #090B10;
    --p-border: rgba(255, 255, 255, 0.085);
    --p-text: #F5F7FA;
    --p-secondary: #A7B0BE;
    --p-muted: #6B7280;
    --p-start: #00B4D8;
    --p-end: #9B8AFB;
    --p-semantic: #00B4D8;
    position: relative;
    isolation: isolate;
    width: 100%;
    box-sizing: border-box;
    overflow: hidden;
    padding: 18px;
    border: 1px solid var(--p-border);
    border-radius: 16px;
    color: var(--p-text);
    background:
        radial-gradient(110% 130% at 100% 0%, color-mix(in srgb, var(--p-semantic) 10%, transparent), transparent 58%),
        linear-gradient(145deg, rgba(31, 33, 40, 0.98), rgba(20, 22, 28, 0.98));
    box-shadow: 0 14px 34px rgba(0, 0, 0, 0.22), inset 0 1px rgba(255, 255, 255, 0.025);
    font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Helvetica Neue", sans-serif;
    -webkit-font-smoothing: antialiased;
}

.panelyra-progress *,
.panelyra-progress *::before,
.panelyra-progress *::after { box-sizing: border-box; }

.panelyra-progress.p-workspace-capture { --p-start: #00B4D8; --p-end: #3B82F6; }
.panelyra-progress.p-workspace-studio { --p-start: #9B8AFB; --p-end: #FB7185; }
.panelyra-progress.p-state-preparing { --p-start: #60A5FA; --p-end: #A78BFA; --p-semantic: #60A5FA; }
.panelyra-progress.p-state-paused,
.panelyra-progress.p-state-waiting { --p-start: #FBBF24; --p-end: #F59E0B; --p-semantic: #FBBF24; }
.panelyra-progress.p-state-completed { --p-start: #34D399; --p-end: #2EC4B6; --p-semantic: #34D399; }
.panelyra-progress.p-state-failed { --p-start: #FB7185; --p-end: #F43F5E; --p-semantic: #FB7185; }
.panelyra-progress.p-state-stopped { --p-start: #94A3B8; --p-end: #64748B; --p-semantic: #94A3B8; }

.panelyra-progress .p-header,
.panelyra-progress .p-stage-row,
.panelyra-progress .p-subheading,
.panelyra-progress .p-history {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
}

.panelyra-progress .p-header { align-items: flex-start; }
.panelyra-progress .p-heading { min-width: 0; }
.panelyra-progress .p-kicker {
    display: block;
    margin-bottom: 3px;
    color: var(--p-secondary);
    font-size: 10px;
    font-weight: 750;
    letter-spacing: 0.12em;
    line-height: 1.2;
    text-transform: uppercase;
}
.panelyra-progress h3,
.panelyra-progress h4,
.panelyra-progress p,
.panelyra-progress dl,
.panelyra-progress dd,
.panelyra-progress ol { margin: 0; }
.panelyra-progress h3 {
    overflow: hidden;
    color: var(--p-text);
    font-size: 16px;
    font-weight: 720;
    letter-spacing: -0.015em;
    line-height: 1.25;
    text-overflow: ellipsis;
    white-space: nowrap;
}
.panelyra-progress .p-status-cluster {
    display: flex;
    flex: 0 0 auto;
    align-items: center;
    gap: 7px;
}
.panelyra-progress .p-health,
.panelyra-progress .p-state {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    min-height: 24px;
    padding: 4px 8px;
    border: 1px solid rgba(255, 255, 255, 0.07);
    border-radius: 999px;
    color: var(--p-secondary);
    background: rgba(255, 255, 255, 0.035);
    font-size: 11px;
    font-weight: 650;
    line-height: 1;
    white-space: nowrap;
}
.panelyra-progress .p-state {
    color: color-mix(in srgb, var(--p-semantic) 84%, white);
    background: color-mix(in srgb, var(--p-semantic) 9%, transparent);
    border-color: color-mix(in srgb, var(--p-semantic) 22%, transparent);
}
.panelyra-progress .p-health-dot {
    width: 6px;
    height: 6px;
    flex: 0 0 6px;
    border-radius: 50%;
    background: #34D399;
    box-shadow: 0 0 9px rgba(52, 211, 153, 0.72);
}
.panelyra-progress.p-health-slowing .p-health-dot { background: #FBBF24; box-shadow: 0 0 9px rgba(251, 191, 36, 0.62); }
.panelyra-progress.p-health-stalled .p-health-dot,
.panelyra-progress.p-health-failed .p-health-dot { background: #FB7185; box-shadow: 0 0 9px rgba(251, 113, 133, 0.62); }
.panelyra-progress.p-health-reconnecting .p-health-dot { background: #60A5FA; box-shadow: 0 0 9px rgba(96, 165, 250, 0.65); }
.panelyra-progress.p-health-paused .p-health-dot,
.panelyra-progress.p-health-waiting .p-health-dot { background: #FBBF24; box-shadow: none; }
.panelyra-progress.p-health-complete .p-health-dot { background: #34D399; box-shadow: none; }
.panelyra-progress.p-health-stopped .p-health-dot { background: #94A3B8; box-shadow: none; }
.panelyra-progress .p-percent {
    min-width: 48px;
    color: var(--p-text);
    font-size: 18px;
    font-variant-numeric: tabular-nums;
    letter-spacing: -0.025em;
    text-align: right;
}
.panelyra-progress .p-stage-row { margin-top: 12px; }
.panelyra-progress .p-stage {
    overflow: hidden;
    color: var(--p-text);
    font-size: 13px;
    font-weight: 620;
    text-overflow: ellipsis;
    white-space: nowrap;
}
.panelyra-progress .p-updated {
    color: var(--p-muted);
    font-size: 11px;
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
}

.panelyra-progress .p-steps {
    display: grid;
    grid-template-columns: repeat(5, minmax(0, 1fr));
    padding: 0;
    margin: 16px 0 13px;
    list-style: none;
}
.panelyra-progress .p-step {
    position: relative;
    display: grid;
    justify-items: center;
    gap: 5px;
    min-width: 0;
    color: var(--p-muted);
    font-size: 10px;
    line-height: 1.2;
    text-align: center;
}
.panelyra-progress .p-step::before {
    position: absolute;
    z-index: -1;
    top: 5px;
    right: 50%;
    width: 100%;
    height: 1px;
    content: "";
    background: rgba(148, 163, 184, 0.18);
}
.panelyra-progress .p-step:first-child::before { display: none; }
.panelyra-progress .p-step-dot {
    display: grid;
    width: 11px;
    height: 11px;
    place-items: center;
    border: 1px solid rgba(148, 163, 184, 0.33);
    border-radius: 50%;
    color: #08110E;
    background: #1F2128;
    font-size: 8px;
    font-weight: 850;
}
.panelyra-progress .p-step-complete { color: var(--p-secondary); }
.panelyra-progress .p-step-complete::before { background: color-mix(in srgb, var(--p-semantic) 46%, transparent); }
.panelyra-progress .p-step-complete .p-step-dot { border-color: #34D399; background: #34D399; }
.panelyra-progress .p-step-active,
.panelyra-progress .p-step-paused,
.panelyra-progress .p-step-waiting,
.panelyra-progress .p-step-failed,
.panelyra-progress .p-step-stopped { color: var(--p-text); }
.panelyra-progress .p-step-active .p-step-dot,
.panelyra-progress .p-step-paused .p-step-dot,
.panelyra-progress .p-step-waiting .p-step-dot,
.panelyra-progress .p-step-failed .p-step-dot,
.panelyra-progress .p-step-stopped .p-step-dot {
    border-color: var(--p-semantic);
    background: var(--p-semantic);
    box-shadow: 0 0 0 4px color-mix(in srgb, var(--p-semantic) 12%, transparent);
}

.panelyra-progress .p-track {
    position: relative;
    height: 8px;
    margin: 2px 0 0;
    overflow: visible;
    border-radius: 999px;
    outline: none;
}
.panelyra-progress .p-clip {
    position: absolute;
    inset: 0;
    overflow: hidden;
    border: 1px solid rgba(255, 255, 255, 0.045);
    border-radius: inherit;
    background: var(--p-track);
    box-shadow: inset 0 1px 3px rgba(0, 0, 0, 0.62);
}
.panelyra-progress .p-fill {
    position: absolute;
    inset: 0;
    overflow: hidden;
    border-radius: inherit;
    background: linear-gradient(90deg, var(--p-start), var(--p-end));
    box-shadow: inset 0 0 8px rgba(255, 255, 255, 0.18);
    transform: scaleX(var(--p-to));
    transform-origin: left center;
    will-change: transform;
}
.panelyra-progress .p-flow {
    position: absolute;
    inset: -2px -55%;
    opacity: 0.58;
    background: linear-gradient(105deg, transparent 28%, rgba(255, 255, 255, 0.6) 48%, transparent 67%);
    transform: translate3d(-28%, 0, 0);
    will-change: transform;
}
.panelyra-progress .p-runner {
    position: absolute;
    z-index: 2;
    top: 50%;
    left: var(--p-to-left);
    width: 7px;
    height: 7px;
    border-radius: 50%;
    opacity: 0.92;
    background: #FFFFFF;
    box-shadow: 0 0 5px #FFFFFF, 0 0 12px var(--p-end), 0 0 22px color-mix(in srgb, var(--p-end) 60%, transparent);
    transform: translate3d(-50%, -50%, 0);
    pointer-events: none;
}
.panelyra-progress[style*="--p-to:0.000000"] .p-runner,
.panelyra-progress.p-state-paused .p-runner,
.panelyra-progress.p-state-waiting .p-runner,
.panelyra-progress.p-state-stopped .p-runner,
.panelyra-progress.p-state-failed .p-runner { opacity: 0; }

.panelyra-progress.p-is-indeterminate .p-fill {
    right: auto;
    width: 42%;
    transform: translate3d(-112%, 0, 0);
}
.panelyra-progress.p-is-indeterminate .p-runner { display: none; }

.panelyra-progress.p-motion-full.p-has-update:not(.p-is-indeterminate) .p-fill,
.panelyra-progress.p-motion-subtle.p-has-update:not(.p-is-indeterminate) .p-fill {
    animation: p-progress-update 560ms cubic-bezier(.22, .78, .24, 1) both;
}
.panelyra-progress.p-motion-full.p-has-update:not(.p-is-indeterminate) .p-runner {
    animation: p-runner-update 720ms cubic-bezier(.22, .78, .24, 1) both;
}
.panelyra-progress.p-motion-subtle.p-has-update:not(.p-is-indeterminate) .p-runner {
    animation: p-runner-subtle 560ms ease-out both;
}
.panelyra-progress.p-motion-full.p-state-running .p-flow,
.panelyra-progress.p-motion-full.p-state-preparing .p-flow {
    animation: p-flow 3.2s linear infinite;
    animation-delay: var(--p-phase);
}
.panelyra-progress.p-motion-subtle.p-state-running .p-flow,
.panelyra-progress.p-motion-subtle.p-state-preparing .p-flow {
    opacity: 0.26;
    animation: p-flow 6.4s linear infinite;
    animation-delay: var(--p-phase);
}
.panelyra-progress.p-motion-full.p-is-indeterminate .p-fill,
.panelyra-progress.p-motion-subtle.p-is-indeterminate .p-fill {
    animation: p-indeterminate 1.65s cubic-bezier(.4, 0, .2, 1) infinite;
    animation-delay: var(--p-phase);
}
.panelyra-progress.p-motion-subtle.p-is-indeterminate .p-fill { animation-duration: 2.2s; }
.panelyra-progress.p-motion-full.p-state-completed .p-flow {
    animation: p-completion-sweep 720ms ease-out both;
}
.panelyra-progress.p-motion-full.p-state-failed {
    animation: p-failed-emphasis 260ms ease-out both;
}
.panelyra-progress.p-motion-off .p-flow { display: none; }
.panelyra-progress.p-motion-off .p-fill,
.panelyra-progress.p-motion-off .p-runner,
.panelyra-progress.p-motion-off.p-is-indeterminate .p-fill { animation: none; transition: none; }
.panelyra-progress.p-motion-off.p-is-indeterminate .p-fill {
    right: 28%;
    left: 28%;
    width: auto;
    opacity: 0.68;
    transform: none;
}
.panelyra-progress.p-state-paused .p-fill,
.panelyra-progress.p-state-paused .p-runner,
.panelyra-progress.p-state-waiting .p-fill,
.panelyra-progress.p-state-waiting .p-runner,
.panelyra-progress.p-state-stopped .p-fill,
.panelyra-progress.p-state-stopped .p-runner,
.panelyra-progress.p-state-failed .p-fill,
.panelyra-progress.p-state-failed .p-runner {
    animation: none !important;
}
.panelyra-progress.p-is-indeterminate.p-state-completed .p-fill,
.panelyra-progress.p-state-paused .p-subfill,
.panelyra-progress.p-state-waiting .p-subfill,
.panelyra-progress.p-state-completed .p-subfill,
.panelyra-progress.p-state-stopped .p-subfill,
.panelyra-progress.p-state-failed .p-subfill { animation: none !important; }
.panelyra-progress.p-is-indeterminate.p-state-paused .p-fill,
.panelyra-progress.p-is-indeterminate.p-state-waiting .p-fill,
.panelyra-progress.p-is-indeterminate.p-state-completed .p-fill,
.panelyra-progress.p-is-indeterminate.p-state-stopped .p-fill,
.panelyra-progress.p-is-indeterminate.p-state-failed .p-fill {
    right: 28%;
    left: 28%;
    width: auto;
    opacity: .68;
    transform: none;
}

@keyframes p-progress-update {
    from { transform: scaleX(var(--p-from)); }
    to { transform: scaleX(var(--p-to)); }
}
@keyframes p-runner-update {
    0% { left: var(--p-from-left); opacity: 0.4; transform: translate3d(-50%, -50%, 0) scale(.7); }
    74% { opacity: 1; transform: translate3d(-50%, -50%, 0) scale(1.3); }
    100% { left: var(--p-to-left); opacity: .92; transform: translate3d(-50%, -50%, 0) scale(1); }
}
@keyframes p-runner-subtle {
    0% { left: var(--p-from-left); opacity: .35; }
    100% { left: var(--p-to-left); opacity: .82; }
}
@keyframes p-flow {
    from { transform: translate3d(-28%, 0, 0); }
    to { transform: translate3d(28%, 0, 0); }
}
@keyframes p-indeterminate {
    0% { transform: translate3d(-112%, 0, 0); }
    62% { transform: translate3d(150%, 0, 0); }
    100% { transform: translate3d(340%, 0, 0); }
}
@keyframes p-completion-sweep {
    from { transform: translate3d(-50%, 0, 0); opacity: .72; }
    to { transform: translate3d(50%, 0, 0); opacity: 0; }
}
@keyframes p-failed-emphasis {
    0%, 100% { transform: translate3d(0, 0, 0); }
    45% { transform: translate3d(2px, 0, 0); }
}

.panelyra-progress .p-subprogress { margin-top: 11px; }
.panelyra-progress .p-subheading {
    margin-bottom: 5px;
    color: var(--p-secondary);
    font-size: 10px;
}
.panelyra-progress .p-subheading strong { color: var(--p-text); font-variant-numeric: tabular-nums; }
.panelyra-progress .p-subtrack {
    position: relative;
    height: 3px;
    overflow: hidden;
    border-radius: 999px;
    background: rgba(9, 11, 16, 0.88);
}
.panelyra-progress .p-subfill {
    position: absolute;
    inset: 0;
    border-radius: inherit;
    background: color-mix(in srgb, var(--p-semantic) 70%, #FFFFFF);
    transform: scaleX(var(--p-sub-to));
    transform-origin: left center;
}
.panelyra-progress .p-sub-indeterminate .p-subfill {
    width: 36%;
    transform: translate3d(92%, 0, 0);
    opacity: .7;
}
.panelyra-progress.p-motion-full .p-sub-indeterminate .p-subfill,
.panelyra-progress.p-motion-subtle .p-sub-indeterminate .p-subfill {
    animation: p-sub-indeterminate 2s ease-in-out infinite;
}
@keyframes p-sub-indeterminate {
    0%, 100% { transform: translate3d(-100%, 0, 0); }
    55% { transform: translate3d(280%, 0, 0); }
}

.panelyra-progress .p-stats {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 8px;
    margin-top: 14px;
}
.panelyra-progress .p-stat {
    min-width: 0;
    padding: 9px 10px;
    border: 1px solid rgba(255, 255, 255, 0.055);
    border-radius: 10px;
    background: rgba(255, 255, 255, 0.025);
}
.panelyra-progress .p-stat dt,
.panelyra-progress .p-details dt,
.panelyra-progress .p-receipt dt {
    color: var(--p-muted);
    font-size: 9px;
    font-weight: 720;
    letter-spacing: .055em;
    text-transform: uppercase;
}
.panelyra-progress .p-stat dd {
    overflow: hidden;
    margin-top: 3px;
    color: var(--p-text);
    font-size: 12px;
    font-weight: 660;
    font-variant-numeric: tabular-nums;
    text-overflow: ellipsis;
    white-space: nowrap;
}
.panelyra-progress .p-activity-section { margin-top: 14px; }
.panelyra-progress .p-activity-section h4,
.panelyra-progress .p-history > span {
    color: var(--p-secondary);
    font-size: 10px;
    font-weight: 720;
    letter-spacing: .04em;
    text-transform: uppercase;
}
.panelyra-progress .p-activity-list {
    display: grid;
    gap: 6px;
    padding: 0;
    margin-top: 7px;
    list-style: none;
}
.panelyra-progress .p-activity {
    display: grid;
    grid-template-columns: 56px 7px minmax(0, 1fr);
    align-items: baseline;
    gap: 7px;
    color: var(--p-secondary);
    font-size: 11px;
    line-height: 1.35;
}
.panelyra-progress .p-activity time { color: var(--p-muted); font-variant-numeric: tabular-nums; }
.panelyra-progress .p-activity-mark {
    width: 5px;
    height: 5px;
    border-radius: 50%;
    background: #60A5FA;
}
.panelyra-progress .p-activity-success .p-activity-mark { background: #34D399; }
.panelyra-progress .p-activity-warning .p-activity-mark { background: #FBBF24; }
.panelyra-progress .p-activity-error .p-activity-mark { background: #FB7185; }

.panelyra-progress .p-details {
    display: flex;
    flex-wrap: wrap;
    gap: 7px 18px;
    margin-top: 13px;
    padding-top: 12px;
    border-top: 1px solid rgba(255, 255, 255, 0.055);
}
.panelyra-progress .p-details div { display: flex; gap: 6px; align-items: baseline; }
.panelyra-progress .p-details dd,
.panelyra-progress .p-receipt dd { color: var(--p-secondary); font-size: 11px; }
.panelyra-progress .p-history {
    margin-top: 12px;
    padding-top: 10px;
    border-top: 1px solid rgba(255, 255, 255, 0.055);
}
.panelyra-progress .p-history svg { width: min(180px, 48%); height: 32px; overflow: visible; }
.panelyra-progress .p-history polyline {
    fill: none;
    stroke: var(--p-semantic);
    stroke-linecap: round;
    stroke-linejoin: round;
    stroke-width: 2;
    vector-effect: non-scaling-stroke;
}
.panelyra-progress .p-history-baseline { fill: none; stroke: rgba(148, 163, 184, .16); stroke-width: 1; }
.panelyra-progress .p-notice {
    margin-top: 13px;
    padding: 10px 12px;
    border: 1px solid color-mix(in srgb, var(--p-semantic) 24%, transparent);
    border-radius: 10px;
    color: color-mix(in srgb, var(--p-semantic) 74%, white);
    background: color-mix(in srgb, var(--p-semantic) 8%, transparent);
    font-size: 11px;
    line-height: 1.4;
}
.panelyra-progress .p-receipt {
    display: grid;
    grid-template-columns: 30px minmax(0, 1fr);
    gap: 10px;
    margin-top: 14px;
    padding: 12px;
    border: 1px solid rgba(52, 211, 153, .22);
    border-radius: 12px;
    background: rgba(52, 211, 153, .065);
}
.panelyra-progress .p-receipt-icon {
    display: grid;
    width: 28px;
    height: 28px;
    place-items: center;
    border-radius: 50%;
    color: #07120F;
    background: #34D399;
    font-size: 14px;
    font-weight: 850;
}
.panelyra-progress .p-receipt h4 { color: var(--p-text); font-size: 12px; }
.panelyra-progress .p-receipt p { margin-top: 3px; color: var(--p-secondary); font-size: 11px; line-height: 1.4; }
.panelyra-progress .p-receipt dl { display: flex; flex-wrap: wrap; gap: 6px 16px; margin-top: 8px; }
.panelyra-progress .p-receipt dl div { display: flex; align-items: baseline; gap: 5px; }

.panelyra-progress.p-compact {
    padding: 12px 13px;
    border-radius: 13px;
}
.panelyra-progress.p-compact .p-kicker,
.panelyra-progress.p-compact .p-health,
.panelyra-progress.p-compact .p-steps,
.panelyra-progress.p-compact .p-updated,
.panelyra-progress.p-compact .p-subprogress,
.panelyra-progress.p-compact .p-stats,
.panelyra-progress.p-compact .p-activity-section,
.panelyra-progress.p-compact .p-details,
.panelyra-progress.p-compact .p-history,
.panelyra-progress.p-compact .p-notice,
.panelyra-progress.p-compact .p-receipt { display: none; }
.panelyra-progress.p-compact .p-header { align-items: center; }
.panelyra-progress.p-compact h3 { font-size: 13px; }
.panelyra-progress.p-compact .p-state { min-height: 21px; padding: 3px 7px; font-size: 10px; }
.panelyra-progress.p-compact .p-percent { min-width: 42px; font-size: 14px; }
.panelyra-progress.p-compact .p-stage-row { margin-top: 7px; }
.panelyra-progress.p-compact .p-stage { color: var(--p-secondary); font-size: 10px; }
.panelyra-progress.p-compact .p-track { height: 4px; margin-top: 8px; }
.panelyra-progress.p-compact .p-runner { width: 5px; height: 5px; }

.panelyra-progress .p-visually-hidden {
    position: absolute !important;
    width: 1px !important;
    height: 1px !important;
    padding: 0 !important;
    margin: -1px !important;
    overflow: hidden !important;
    clip: rect(0, 0, 0, 0) !important;
    white-space: nowrap !important;
    border: 0 !important;
}

@media (max-width: 760px) {
    .panelyra-progress { padding: 15px; border-radius: 14px; }
    .panelyra-progress .p-header { display: grid; grid-template-columns: minmax(0, 1fr); }
    .panelyra-progress .p-status-cluster { width: 100%; }
    .panelyra-progress .p-percent { margin-left: auto; }
    .panelyra-progress .p-health { display: none; }
    .panelyra-progress .p-step-label { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; width: 100%; }
    .panelyra-progress .p-stats { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    .panelyra-progress .p-activity { grid-template-columns: 48px 6px minmax(0, 1fr); gap: 6px; }
    .panelyra-progress.p-compact .p-header { display: flex; }
}

@media (prefers-reduced-motion: reduce) {
    .panelyra-progress,
    .panelyra-progress *,
    .panelyra-progress *::before,
    .panelyra-progress *::after {
        scroll-behavior: auto !important;
        animation: none !important;
        transition: none !important;
    }
    .panelyra-progress.p-is-indeterminate .p-fill {
        right: 28%;
        left: 28%;
        width: auto;
        opacity: .68;
        transform: none;
    }
    .panelyra-progress .p-flow { display: none; }
}
</style>
"""


__all__ = [
    "ActivityItem",
    "StageItem",
    "RunProgressModel",
    "PROGRESS_CSS",
    "resolve_progress_state",
    "resolve_health",
    "build_stage_items",
    "extract_activity_items",
    "build_progress_html",
]
