"""Pure intent, knowledge, diagnostics, and action planning for Panelyra."""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any


ASSISTANT_MODES = ("Auto", "Ask", "Diagnose", "Configure", "Support")
WORKSPACE_KEYS = ("engage", "capture", "studio")


@dataclass(frozen=True)
class LogEvent:
    category: str
    severity: str
    message: str
    evidence: str


@dataclass(frozen=True)
class WorkspaceRun:
    workspace: str
    status: str = "idle"
    stage: str = ""
    message: str = ""
    progress_current: int = 0
    progress_total: int = 0
    log_path: str = ""
    settings: dict[str, Any] = field(default_factory=dict)
    events: tuple[LogEvent, ...] = ()


@dataclass(frozen=True)
class CopilotContext:
    current_page: str
    current_view: str
    browser_owner: dict[str, Any]
    runs: dict[str, WorkspaceRun]
    settings: dict[str, Any]
    outputs: dict[str, Any]
    recent_comments: tuple[str, ...] = ()
    tickets: tuple[dict[str, Any], ...] = ()
    browser_snapshot: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "current_page": self.current_page,
            "current_view": self.current_view,
            "browser_owner": self.browser_owner,
            "runs": {
                key: {
                    **asdict(value),
                    "events": [asdict(event) for event in value.events],
                }
                for key, value in self.runs.items()
            },
            "settings": self.settings,
            "outputs": self.outputs,
            "recent_comments": list(self.recent_comments),
            "ticket_count": len(self.tickets),
            "open_ticket_count": sum(
                1 for ticket in self.tickets if str(ticket.get("status", "open")) == "open"
            ),
            "browser_snapshot": self.browser_snapshot,
        }


@dataclass(frozen=True)
class AssistantEvidence:
    label: str
    value: str
    source: str
    severity: str = "info"


@dataclass(frozen=True)
class AssistantAction:
    action_id: str
    label: str
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    description: str = ""
    requires_confirmation: bool = False


@dataclass(frozen=True)
class AssistantReply:
    text: str
    mode: str
    confidence: str
    evidence: tuple[AssistantEvidence, ...] = ()
    actions: tuple[AssistantAction, ...] = ()

    def to_message(self) -> dict[str, Any]:
        return {
            "id": f"assistant-{uuid.uuid4().hex[:10]}",
            "role": "assistant",
            "content": self.text,
            "mode": self.mode,
            "confidence": self.confidence,
            "evidence": [asdict(item) for item in self.evidence],
            "actions": [asdict(item) for item in self.actions],
        }


KNOWLEDGE_ENTRIES = (
    {
        "id": "dashboard",
        "title": "Dashboard",
        "pages": ("Dashboard",),
        "keywords": ("dashboard", "home", "workspace", "start"),
        "body": (
            "Dashboard is the workspace launcher and status overview. Start Comment Testing opens "
            "Engage, Start Chapter Capture opens Capture, and Start Creative Studio opens Studio."
        ),
    },
    {
        "id": "engage",
        "title": "Engage workflow",
        "pages": ("Engage",),
        "keywords": ("engage", "comment", "comments", "submit", "thanks", "chapter"),
        "body": (
            "Engage generates contextual comments, submits them, verifies the result, optionally clicks "
            "Say thanks, and records chapter history. Configure the URL, routing, speed, selectors, and "
            "chapter limit before opening Chrome."
        ),
    },
    {
        "id": "capture",
        "title": "Capture workflow",
        "pages": ("Capture",),
        "keywords": ("capture", "download", "images", "cbz", "ocr", "archive", "manhwa"),
        "body": (
            "Capture detects story images, optionally downloads raw pages, creates CBZ archives, runs OCR, "
            "and stores each series in one title folder with numbered chapter folders such as 01 and 02."
        ),
    },
    {
        "id": "studio",
        "title": "Studio workflow",
        "pages": ("Studio",),
        "keywords": ("studio", "word", "art", "image", "upload", "editor"),
        "body": (
            "Studio renders a local word-art image, opens the configured editor, uploads the image, fills "
            "the text area, and either leaves the draft for review or activates the configured save control."
        ),
    },
    {
        "id": "browser_prepare",
        "title": "Open browser and prepare",
        "pages": ("Engage", "Capture", "Studio"),
        "keywords": ("browser", "login", "prepare", "pick", "chapter", "chrome"),
        "body": (
            "Use Open browser and prepare when login or manual navigation is required. Panelyra opens Chrome "
            "and pauses. Navigate to the exact target, return to Panelyra, and continue from the workspace Live view."
        ),
    },
    {
        "id": "browser_lock",
        "title": "Shared Chrome ownership",
        "pages": ("Engage", "Capture", "Studio"),
        "keywords": ("busy", "chrome", "browser", "locked", "already", "running"),
        "body": (
            "Engage, Capture, and Studio share one authenticated Chrome profile. Only one browser workflow "
            "can run at a time. Finish or stop the active workflow before starting another."
        ),
    },
    {
        "id": "reader_routing",
        "title": "Reader routing",
        "pages": ("Engage", "Capture"),
        "keywords": ("routing", "continuous", "next", "paginated", "infinite", "reader"),
        "body": (
            "Auto detect supports continuous readers, next-chapter links, paginated readers, and mixed layouts. "
            "Use a specific routing mode only when automatic detection chooses the wrong structure."
        ),
    },
    {
        "id": "selectors",
        "title": "Selector profiles",
        "pages": ("Engage", "Capture", "Studio"),
        "keywords": ("selector", "css", "element", "button", "textarea", "not found"),
        "body": (
            "Selector profiles map page controls such as comment editor, Submit, Next Page, Next Chapter, "
            "chapter list, confirmation, upload, and save. A selector failure normally means the target layout changed."
        ),
    },
    {
        "id": "live",
        "title": "Live rooms",
        "pages": ("Engage", "Capture", "Studio"),
        "keywords": ("live", "log", "progress", "pause", "stop", "status"),
        "body": (
            "Each workspace Live view contains only that workflow's progress, run controls, structured status, "
            "and diagnostics. Use it to pause, resume, stop, or inspect a failure."
        ),
    },
    {
        "id": "support",
        "title": "Support workflow",
        "pages": ("Assistant", "Settings"),
        "keywords": ("support", "ticket", "user", "reply", "owner", "incident"),
        "body": (
            "Support mode drafts a user-facing response from local evidence and can create a tracked local ticket. "
            "Tickets record severity, workspace, reproduction details, status, internal notes, and resolution."
        ),
    },
)


LOG_EVENT_RULES = (
    (
        "browser_busy",
        "error",
        re.compile(r"chrome is already in use|session not created|chrome instance exited", re.I),
        "The shared Chrome profile is busy or Chrome failed during startup.",
    ),
    (
        "challenge",
        "warning",
        re.compile(r"cloudflare|verify you are human|checking your browser|challenge", re.I),
        "The target is showing an interactive browser challenge.",
    ),
    (
        "selector",
        "error",
        re.compile(r"selector|no matching element|element.*not found|could not find", re.I),
        "A configured page control could not be located.",
    ),
    (
        "timeout",
        "warning",
        re.compile(r"timeout|timed out|readyState=complete", re.I),
        "A page, network request, or element exceeded its wait window.",
    ),
    (
        "media_missing",
        "error",
        re.compile(r"no chapter page media|no.*image.*detected|media was not detected", re.I),
        "Capture could not identify story-region images.",
    ),
    (
        "submission",
        "error",
        re.compile(r"submit.*failed|submission.*failed|confirmation.*not", re.I),
        "Engage could not verify that the comment was submitted.",
    ),
    (
        "completed",
        "info",
        re.compile(r"run complete|finished\. chapters written|automation complete", re.I),
        "The workflow reported successful completion.",
    ),
)


PLAYBOOKS = {
    "browser_busy": (
        "Another workflow owns the shared Chrome profile. Wait for it to finish or stop it from its Live view, "
        "then prepare a retry. Do not launch a second Chrome profile process."
    ),
    "challenge": (
        "Open the browser manually, complete the login or verification challenge, navigate to the exact target, "
        "then continue the paused workflow."
    ),
    "selector": (
        "Open the target page, identify the failed control, apply the closest selector profile, and validate the "
        "specific selector before retrying."
    ),
    "timeout": (
        "Confirm the page is reachable and logged in, then retry with balanced timing. If the same stage times out, "
        "inspect the target control and network state."
    ),
    "media_missing": (
        "Confirm the URL is an actual reader chapter, not a series listing. Then inspect the reader DOM and update "
        "the chapter image or content-container selector."
    ),
    "submission": (
        "Check that the editor contains the intended text, verify the Submit selector, and confirm the site exposes "
        "a success marker or posted comment before retrying."
    ),
}


def tokenize(value: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", value.lower())
        if len(token) > 2
    }


def extract_log_events(log_text: str, limit: int = 12) -> tuple[LogEvent, ...]:
    events: list[LogEvent] = []
    seen: set[tuple[str, str]] = set()
    for line in log_text.splitlines():
        compact = " ".join(line.strip().split())
        if not compact:
            continue
        for category, severity, pattern, message in LOG_EVENT_RULES:
            if not pattern.search(compact):
                continue
            key = (category, compact[-240:])
            if key in seen:
                break
            seen.add(key)
            events.append(
                LogEvent(
                    category=category,
                    severity=severity,
                    message=message,
                    evidence=compact[-500:],
                )
            )
            break
    return tuple(events[-limit:])


def retrieve_knowledge(prompt: str, current_page: str, limit: int = 3) -> list[dict[str, Any]]:
    prompt_tokens = tokenize(prompt)
    scored: list[tuple[int, dict[str, Any]]] = []
    for entry in KNOWLEDGE_ENTRIES:
        entry_tokens = tokenize(
            " ".join(
                (
                    str(entry["title"]),
                    str(entry["body"]),
                    " ".join(entry["keywords"]),
                )
            )
        )
        overlap = len(prompt_tokens & entry_tokens)
        page_bonus = 2 if any(current_page.startswith(page) for page in entry["pages"]) else 0
        score = overlap * 3 + page_bonus
        if score:
            scored.append((score, entry))
    scored.sort(key=lambda item: item[0], reverse=True)
    if not scored:
        return [entry for entry in KNOWLEDGE_ENTRIES if entry["id"] == "dashboard"]
    return [entry for _, entry in scored[:limit]]


def classify_mode(prompt: str, selected_mode: str = "Auto") -> str:
    if selected_mode in ASSISTANT_MODES and selected_mode != "Auto":
        return selected_mode
    normalized = prompt.lower()
    if any(term in normalized for term in ("reply to", "user says", "support", "ticket", "customer", "owner")):
        return "Support"
    if any(
        term in normalized
        for term in ("failed", "error", "stuck", "broken", "not working", "diagnose", "why did", "problem")
    ):
        return "Diagnose"
    if any(
        term in normalized
        for term in ("configure", "set up", "setup", "prepare", "use this url", "chapters", "speed", "routing")
    ):
        return "Configure"
    return "Ask"


def workspace_from_prompt(prompt: str, current_page: str) -> str:
    normalized = prompt.lower()
    aliases = {
        "engage": ("engage", "comment", "submit", "thanks"),
        "capture": ("capture", "download", "cbz", "ocr", "archive", "images"),
        "studio": ("studio", "word art", "upload", "editor"),
    }
    for workspace, terms in aliases.items():
        if any(term in normalized for term in terms):
            return workspace
    lowered_page = current_page.lower()
    for workspace in WORKSPACE_KEYS:
        if lowered_page.startswith(workspace):
            return workspace
    return "engage"


def navigation_action(workspace: str, view: str = "Live") -> AssistantAction:
    page = workspace.title()
    return AssistantAction(
        action_id=f"navigate-{workspace}-{view.lower()}",
        label=f"Open {page} {view}",
        kind="navigate",
        payload={"page": page, "view": view},
        description=f"Navigate to the {page} {view.lower()} view.",
    )


def diagnose_reply(prompt: str, context: CopilotContext) -> AssistantReply:
    workspace = workspace_from_prompt(prompt, context.current_page)
    run = context.runs.get(workspace, WorkspaceRun(workspace=workspace))
    evidence = [
        AssistantEvidence("Workspace", workspace.title(), "Current request"),
        AssistantEvidence("Run status", run.status or "idle", "Run record"),
    ]
    if run.stage:
        evidence.append(AssistantEvidence("Stage", run.stage, "Run record"))
    if run.log_path:
        evidence.append(AssistantEvidence("Log", run.log_path, "Runtime"))
    if context.browser_owner:
        evidence.append(
            AssistantEvidence(
                "Browser owner",
                str(context.browser_owner.get("bot_type", "Panelyra workflow")),
                "Browser lock",
                "warning",
            )
        )

    actionable_event = next(
        (event for event in reversed(run.events) if event.category in PLAYBOOKS),
        None,
    )
    if actionable_event:
        evidence.append(
            AssistantEvidence(
                "Latest finding",
                actionable_event.evidence,
                "Workspace log",
                actionable_event.severity,
            )
        )
        diagnosis = actionable_event.message
        next_step = PLAYBOOKS[actionable_event.category]
        confidence = "high"
    elif run.status in {"failed", "stopped"}:
        diagnosis = run.message or "The latest run ended without a recognized diagnostic event."
        next_step = (
            "Open the workspace Live view, inspect the final log stage, and prepare a retry only after the "
            "target URL, login state, and selector profile are confirmed."
        )
        confidence = "medium"
    elif run.status in {"running", "starting"}:
        diagnosis = f"The {workspace.title()} workflow is currently {run.status} at {run.stage or 'startup'}."
        next_step = "Monitor its Live view. Stop it only if progress and the log remain unchanged."
        confidence = "high"
    else:
        diagnosis = f"No active {workspace.title()} run is available to diagnose."
        next_step = "Start or select a run, then ask again so the assistant can inspect its structured record and log."
        confidence = "medium"

    actions = [
        navigation_action(workspace, "Live"),
        AssistantAction(
            action_id=f"validate-current-{workspace}",
            label="Validate current setup",
            kind="validate_configuration",
            payload={"workspace": workspace, "values": {}},
            description="Check the saved URL and required selectors without changing them.",
        ),
        AssistantAction(
            action_id=f"prepare-retry-{workspace}",
            label="Prepare retry",
            kind="prepare_retry",
            payload={"workspace": workspace},
            description="Restore the latest run settings without starting the workflow.",
            requires_confirmation=True,
        ),
        AssistantAction(
            action_id="inspect-browser",
            label="Inspect browser page",
            kind="inspect_browser",
            payload={"include_screenshot": True},
            description="Read the active Chrome tab DOM summary and save a local screenshot.",
            requires_confirmation=True,
        ),
    ]
    return AssistantReply(
        text=(
            f"**{workspace.title()} diagnosis**\n\n"
            f"{diagnosis}\n\n"
            f"**Recommended next step**\n\n{next_step}"
        ),
        mode="Diagnose",
        confidence=confidence,
        evidence=tuple(evidence),
        actions=tuple(actions),
    )


def parse_configuration(prompt: str, workspace: str) -> dict[str, Any]:
    url_match = re.search(r"https?://[^\s)>\]]+", prompt)
    chapter_match = re.search(
        r"(?:max(?:imum)?\s*)?(\d{1,3})\s*chapters?",
        prompt,
        flags=re.I,
    )
    normalized = prompt.lower()
    speed = next(
        (candidate for candidate in ("Maximum", "Performance", "Balanced", "Gentle") if candidate.lower() in normalized),
        "",
    )
    routing = ""
    for term, value in (
        ("continuous", "infinite-scroll"),
        ("infinite", "infinite-scroll"),
        ("next chapter", "next-link"),
        ("paginated", "paginated"),
        ("next page", "paginated"),
        ("auto", "auto"),
    ):
        if term in normalized:
            routing = value
            break

    values: dict[str, Any] = {}
    if url_match:
        values["url"] = url_match.group(0).rstrip(".,")
    if chapter_match:
        values["max_chapters"] = max(1, min(20, int(chapter_match.group(1))))
    if speed and workspace == "engage":
        values["speed"] = speed
    if routing:
        values["routing"] = routing
    return values


def configure_reply(prompt: str, context: CopilotContext) -> AssistantReply:
    workspace = workspace_from_prompt(prompt, context.current_page)
    values = parse_configuration(prompt, workspace)
    lines = [f"**{workspace.title()} configuration proposal**", ""]
    if values:
        labels = {
            "url": "Target URL",
            "max_chapters": "Maximum chapters",
            "speed": "Speed",
            "routing": "Reader routing",
        }
        lines.extend(f"- {labels[key]}: `{value}`" for key, value in values.items())
    else:
        lines.append(
            "I could not extract a URL or workflow settings from the request. Include the target URL, "
            "chapter limit, and any routing or speed preference."
        )
    lines.extend(
        (
            "",
            "Applying this proposal only fills the workspace form. It does not launch Chrome or start a run.",
        )
    )
    actions = [
        AssistantAction(
            action_id=f"validate-config-{workspace}",
            label="Validate proposal",
            kind="validate_configuration",
            payload={"workspace": workspace, "values": values},
            description="Check the proposed URL and required selector fields.",
        ),
        AssistantAction(
            action_id=f"apply-config-{workspace}",
            label="Apply configuration",
            kind="apply_configuration",
            payload={"workspace": workspace, "values": values},
            description="Fill the matching workspace setup fields.",
            requires_confirmation=True,
        ),
        navigation_action(workspace, "Configure"),
    ]
    return AssistantReply(
        text="\n".join(lines),
        mode="Configure",
        confidence="high" if values else "low",
        evidence=(
            AssistantEvidence("Current page", context.current_page, "Application state"),
            AssistantEvidence("Parsed fields", str(len(values)), "Request parser"),
        ),
        actions=tuple(actions),
    )


def support_reply(prompt: str, context: CopilotContext) -> AssistantReply:
    workspace = workspace_from_prompt(prompt, context.current_page)
    run = context.runs.get(workspace, WorkspaceRun(workspace=workspace))
    event = next((event for event in reversed(run.events) if event.severity != "info"), None)
    issue = event.message if event else (run.message or "The reported issue is being investigated.")
    user_message = (
        "Thanks for reporting this. We checked the latest "
        f"{workspace.title()} status and found: {issue} "
        "No successful action will be claimed until the workflow is verified. "
        "Please keep the target page available while we review the run details, and we will update you after the next check."
    )
    severity = "high" if event and event.severity == "error" else "medium"
    evidence = [
        AssistantEvidence("Workspace", workspace.title(), "Request and current page"),
        AssistantEvidence("Run status", run.status, "Run record"),
    ]
    if event:
        evidence.append(
            AssistantEvidence("Incident evidence", event.evidence, "Workspace log", event.severity)
        )
    return AssistantReply(
        text=f"**Reply draft**\n\n```text\n{user_message}\n```\n\nReview the wording before sending it.",
        mode="Support",
        confidence="high" if event else "medium",
        evidence=tuple(evidence),
        actions=(
            AssistantAction(
                action_id=f"create-ticket-{uuid.uuid4().hex[:8]}",
                label="Create support ticket",
                kind="create_support_ticket",
                payload={
                    "workspace": workspace,
                    "severity": severity,
                    "request": prompt,
                    "user_reply": user_message,
                    "evidence": [asdict(item) for item in evidence],
                },
                description="Create a tracked local support ticket with this evidence and draft.",
                requires_confirmation=True,
            ),
            navigation_action(workspace, "Live"),
        ),
    )


def ask_reply(prompt: str, context: CopilotContext) -> AssistantReply:
    entries = retrieve_knowledge(prompt, context.current_page)
    answer = "\n\n".join(f"**{entry['title']}**\n\n{entry['body']}" for entry in entries)
    current_workspace = workspace_from_prompt(prompt, context.current_page)
    run = context.runs.get(current_workspace)
    evidence = [
        AssistantEvidence("Current page", context.current_page, "Application state"),
        *[
            AssistantEvidence(entry["title"], entry["id"], "Panelyra knowledge base")
            for entry in entries
        ],
    ]
    if run:
        evidence.append(
            AssistantEvidence(
                f"{current_workspace.title()} state",
                run.status,
                "Structured run record",
            )
        )
    return AssistantReply(
        text=answer,
        mode="Ask",
        confidence="high",
        evidence=tuple(evidence),
        actions=(navigation_action(current_workspace, "Configure"),),
    )


def create_local_reply(
    prompt: str,
    selected_mode: str,
    context: CopilotContext,
) -> AssistantReply:
    mode = classify_mode(prompt, selected_mode)
    if mode == "Diagnose":
        return diagnose_reply(prompt, context)
    if mode == "Configure":
        return configure_reply(prompt, context)
    if mode == "Support":
        return support_reply(prompt, context)
    return ask_reply(prompt, context)


def build_model_prompt(
    prompt: str,
    reply: AssistantReply,
    context: CopilotContext,
) -> str:
    payload = {
        "request": prompt,
        "assistant_mode": reply.mode,
        "deterministic_baseline": reply.text,
        "confidence": reply.confidence,
        "evidence": [asdict(item) for item in reply.evidence],
        "proposed_actions": [asdict(item) for item in reply.actions],
        "application_context": context.to_dict(),
    }
    return (
        "Use the grounded Panelyra context below to improve the deterministic baseline. "
        "Answer the user's request directly. Keep all factual claims tied to the supplied evidence. "
        "Do not claim an action was executed. Do not invent logs, browser state, files, submissions, "
        "downloads, support updates, or web findings. Keep action recommendations consistent with the "
        "provided proposed actions. For Support mode, return a polished reply the operator can review. "
        "For Diagnose mode, state the likely cause, evidence, and next step.\n\n"
        f"{json.dumps(payload, indent=2, ensure_ascii=False)}"
    )


def needs_external_research(prompt: str, mode: str) -> bool:
    if mode != "Ask":
        return False
    normalized = prompt.lower()
    return any(
        term in normalized
        for term in ("search the web", "search online", "internet research", "latest", "current version")
    )
