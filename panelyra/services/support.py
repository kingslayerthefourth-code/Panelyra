"""Editable local support-ticket storage with legacy JSONL compatibility."""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from history_utils import read_jsonl_records


TICKET_STATUSES = ("open", "investigating", "waiting", "resolved", "closed")
TICKET_SEVERITIES = ("low", "medium", "high", "critical")


def write_tickets(path: Path, tickets: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for ticket in tickets:
            handle.write(json.dumps(ticket, ensure_ascii=False, default=str) + "\n")
    os.replace(temporary, path)


def read_tickets(path: Path) -> list[dict[str, Any]]:
    tickets: list[dict[str, Any]] = []
    for raw in read_jsonl_records(path):
        ticket = dict(raw)
        ticket.setdefault("status", "open")
        ticket.setdefault("severity", "medium")
        ticket.setdefault("category", "general")
        ticket.setdefault("workspace", "")
        ticket.setdefault("updated_at", ticket.get("timestamp", ""))
        ticket.setdefault("internal_note", "")
        ticket.setdefault("resolution", "")
        ticket.setdefault("user_reply", "")
        ticket.setdefault("evidence", [])
        tickets.append(ticket)
    return tickets


def create_ticket(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    ticket = {
        "id": str(payload.get("id") or f"SUP-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"),
        "timestamp": now,
        "updated_at": now,
        "status": str(payload.get("status") or "open"),
        "severity": str(payload.get("severity") or "medium"),
        "category": str(payload.get("category") or "workflow"),
        "workspace": str(payload.get("workspace") or ""),
        "page": str(payload.get("page") or ""),
        "bot_status": str(payload.get("bot_status") or ""),
        "request": str(payload.get("request") or "").strip(),
        "reproduction_steps": str(payload.get("reproduction_steps") or "").strip(),
        "user_reply": str(payload.get("user_reply") or "").strip(),
        "internal_note": str(payload.get("internal_note") or "").strip(),
        "resolution": str(payload.get("resolution") or "").strip(),
        "evidence": payload.get("evidence") if isinstance(payload.get("evidence"), list) else [],
        "latest_log_findings": (
            payload.get("latest_log_findings")
            if isinstance(payload.get("latest_log_findings"), list)
            else []
        ),
    }
    tickets = read_tickets(path)
    tickets.append(ticket)
    write_tickets(path, tickets)
    return ticket


def update_ticket(path: Path, ticket_id: str, changes: dict[str, Any]) -> dict[str, Any] | None:
    tickets = read_tickets(path)
    updated: dict[str, Any] | None = None
    for ticket in tickets:
        if str(ticket.get("id", "")) != ticket_id:
            continue
        for key in (
            "status",
            "severity",
            "category",
            "workspace",
            "reproduction_steps",
            "user_reply",
            "internal_note",
            "resolution",
        ):
            if key in changes:
                ticket[key] = changes[key]
        ticket["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        updated = ticket
        break
    if updated is not None:
        write_tickets(path, tickets)
    return updated
