#!/usr/bin/env python3
"""Regression coverage for Lyra's durable local conversation threads."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

from panelyra.assistant.conversation import ConversationStore


def main() -> int:
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "lyra.json"
        store = ConversationStore(storage_path=path)
        first = store.create_thread(context={"page": "Capture"}, model="llama3.2:3b")
        assert store.valid(first)
        store.append(first, {"id": "u1", "role": "user", "content": "Why did Capture stop?"})
        store.append(first, {"id": "a1", "role": "assistant", "content": "Checking."}, unread=True)
        assert store.list_threads()[0]["title"] == "Why did Capture stop"
        assert store.list_threads()[0]["unread"]
        assert store.update_message(first, "a1", content="Chrome disconnected.", status="partial")
        assert store.messages(first)[-1]["status"] == "partial"
        selected = store.select_thread(first)
        assert selected and not selected["unread"]
        assert store.rename_thread(first, "Capture diagnosis")

        second = store.create_thread(title="Reader help")
        assert [item["token"] for item in store.list_threads()] == [second, first]
        reloaded = ConversationStore(storage_path=path)
        assert reloaded.messages(first)[-1]["content"] == "Chrome disconnected."
        assert reloaded.list_threads()[1]["title"] == "Capture diagnosis"
        assert reloaded.delete_thread(second)
        reloaded.clear_all_threads()
        assert reloaded.list_threads() == []
        assert json.loads(path.read_text(encoding="utf-8"))["threads"] == {}

    print("Conversation store regression checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
