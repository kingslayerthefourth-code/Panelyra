#!/usr/bin/env python3
"""Regression coverage for Panelyra Copilot grounding and controlled actions."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

from panelyra.assistant import (
    CopilotContext,
    WorkspaceRun,
    build_model_prompt,
    classify_mode,
    create_local_reply,
    extract_log_events,
    needs_external_research,
)
from panelyra.assistant.copilot import retrieve_knowledge
from panelyra.services.support import create_ticket, read_tickets, update_ticket


def fixture_context(events=()) -> CopilotContext:
    return CopilotContext(
        current_page="Capture",
        current_view="Live",
        browser_owner={"bot_type": "Panelyra Capture", "pid": 123},
        runs={
            "engage": WorkspaceRun(workspace="engage"),
            "capture": WorkspaceRun(
                workspace="capture",
                status="failed",
                stage="opening-browser",
                message="Chrome startup failed",
                progress_current=0,
                progress_total=5,
                log_path="/tmp/capture.log",
                settings={"start_url": "https://reader.test/chapter-1", "max_chapters": 5},
                events=tuple(events),
            ),
            "studio": WorkspaceRun(workspace="studio"),
        },
        settings={
            "capture": {
                "url": "https://reader.test/chapter-1",
                "max_chapters": 5,
                "routing": "auto",
            }
        },
        outputs={"capture_chapters": 2, "capture_images": 40},
        tickets=(),
    )


def main() -> int:
    assert classify_mode("Why did Capture fail?", "Auto") == "Diagnose"
    assert classify_mode("Configure this URL for five chapters", "Auto") == "Configure"
    assert classify_mode("Draft a reply to this user", "Auto") == "Support"
    assert classify_mode("What does Capture save?", "Auto") == "Ask"
    assert classify_mode("Anything", "Support") == "Support"

    events = extract_log_events(
        "2026-07-23 ERROR Chrome is already in use by Panelyra Capture.\n"
        "2026-07-23 INFO Starting Chrome.\n"
    )
    assert events
    assert events[-1].category == "browser_busy"
    context = fixture_context(events)

    diagnosis = create_local_reply("Why did the downloader fail?", "Auto", context)
    assert diagnosis.mode == "Diagnose"
    assert diagnosis.confidence == "high"
    assert "shared Chrome profile" in diagnosis.text
    assert any(action.kind == "prepare_retry" for action in diagnosis.actions)
    assert any(action.kind == "inspect_browser" for action in diagnosis.actions)
    assert any(action.kind == "validate_configuration" for action in diagnosis.actions)

    configuration = create_local_reply(
        "Configure Capture for https://reader.test/series/chapter-4 with 7 chapters and continuous routing",
        "Configure",
        context,
    )
    assert configuration.mode == "Configure"
    apply_action = next(action for action in configuration.actions if action.kind == "apply_configuration")
    assert any(action.kind == "validate_configuration" for action in configuration.actions)
    assert apply_action.requires_confirmation
    assert apply_action.payload["values"]["max_chapters"] == 7
    assert apply_action.payload["values"]["routing"] == "infinite-scroll"

    support = create_local_reply(
        "Write a reply to a user who says the downloader crashed",
        "Support",
        context,
    )
    assert "Reply draft" in support.text
    assert any(action.kind == "create_support_ticket" for action in support.actions)

    knowledge = retrieve_knowledge("Where are CBZ chapter images saved?", "Capture")
    assert knowledge[0]["id"] == "capture"
    model_prompt = build_model_prompt("Why did it fail?", diagnosis, context)
    assert "Chrome is already in use" in model_prompt
    assert "Do not claim an action was executed" in model_prompt
    assert not needs_external_research("How does Capture work?", "Ask")
    assert needs_external_research("Search the web for the latest reader format", "Ask")
    assert not needs_external_research("Search the web", "Diagnose")

    with tempfile.TemporaryDirectory() as tmpdir:
        ticket_path = Path(tmpdir) / "tickets.jsonl"
        ticket = create_ticket(
            ticket_path,
            {
                "request": "Capture failed",
                "workspace": "capture",
                "severity": "high",
                "user_reply": "We are investigating.",
            },
        )
        assert read_tickets(ticket_path)[0]["status"] == "open"
        updated = update_ticket(
            ticket_path,
            str(ticket["id"]),
            {"status": "resolved", "resolution": "Chrome ownership fixed."},
        )
        assert updated
        assert read_tickets(ticket_path)[0]["resolution"] == "Chrome ownership fixed."

    print("Assistant regression checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
