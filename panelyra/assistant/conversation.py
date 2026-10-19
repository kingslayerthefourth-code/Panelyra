"""Durable, thread-safe conversations shared by every Lyra surface."""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from panelyra.services.paths import CONFIG_DIR

DEFAULT_CONVERSATIONS_FILE = CONFIG_DIR / "lyra_conversations.json"


def _thread_title(messages: list[dict[str, Any]]) -> str:
    first = next(
        (str(item.get("content", "")).strip() for item in messages if item.get("role") == "user"),
        "",
    )
    if not first:
        return "New conversation"
    title = re.sub(r"\s+", " ", first).strip(" .!?:;,-")
    return title[:46].rstrip() + ("…" if len(title) > 46 else "")


@dataclass
class _Conversation:
    title: str = "New conversation"
    messages: list[dict[str, Any]] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    last_opened_at: float = field(default_factory=time.time)
    unread: bool = False
    context: dict[str, Any] = field(default_factory=dict)
    model: str = ""


class ConversationStore:
    """Local conversation threads with atomic persistence and token compatibility."""

    def __init__(
        self,
        *,
        storage_path: Path | None = DEFAULT_CONVERSATIONS_FILE,
        ttl_seconds: int = 0,
        max_messages: int = 120,
        persistence_enabled: bool = True,
    ) -> None:
        self.storage_path = storage_path
        self.ttl_seconds = max(0, ttl_seconds)
        self.max_messages = max_messages
        self.persistence_enabled = persistence_enabled
        self._items: dict[str, _Conversation] = {}
        self._lock = threading.RLock()
        self._load()

    def create_thread(
        self,
        *,
        title: str = "New conversation",
        context: dict[str, Any] | None = None,
        model: str = "",
    ) -> str:
        token = secrets.token_urlsafe(24)
        with self._lock:
            self._expire_locked()
            self._items[token] = _Conversation(
                title=title.strip() or "New conversation",
                context=dict(context or {}),
                model=model,
            )
            self._save_locked()
        return token

    def new_token(self) -> str:
        return self.create_thread()

    def list_threads(self) -> list[dict[str, Any]]:
        with self._lock:
            self._expire_locked()
            return [
                {"token": token, **asdict(item), "message_count": len(item.messages)}
                for token, item in sorted(
                    self._items.items(), key=lambda pair: pair[1].updated_at, reverse=True
                )
            ]

    def select_thread(self, token: str, *, mark_read: bool = True) -> dict[str, Any] | None:
        with self._lock:
            self._expire_locked()
            item = self._items.get(token)
            if item is None:
                return None
            item.last_opened_at = time.time()
            if mark_read:
                item.unread = False
            self._save_locked()
            return {"token": token, **asdict(item)}

    def valid(self, token: str) -> bool:
        with self._lock:
            self._expire_locked()
            return bool(token) and token in self._items

    def messages(self, token: str) -> list[dict[str, Any]]:
        with self._lock:
            self._expire_locked()
            item = self._items.get(token)
            return [dict(message) for message in item.messages] if item else []

    def replace(self, token: str, messages: list[dict[str, Any]]) -> None:
        with self._lock:
            self._expire_locked()
            item = self._items.setdefault(token, _Conversation())
            item.messages = [dict(message) for message in messages[-self.max_messages :]]
            if item.title == "New conversation":
                item.title = _thread_title(item.messages)
            item.updated_at = time.time()
            self._save_locked()

    def append(self, token: str, message: dict[str, Any], *, unread: bool = False) -> None:
        with self._lock:
            self._expire_locked()
            item = self._items.setdefault(token, _Conversation())
            item.messages.append(dict(message))
            del item.messages[:-self.max_messages]
            if item.title == "New conversation":
                item.title = _thread_title(item.messages)
            item.updated_at = time.time()
            item.unread = unread
            self._save_locked()

    def update_message(self, token: str, message_id: str, **changes: Any) -> bool:
        with self._lock:
            item = self._items.get(token)
            if item is None:
                return False
            for message in item.messages:
                if str(message.get("id", "")) == message_id:
                    message.update(changes)
                    item.updated_at = time.time()
                    self._save_locked()
                    return True
            return False

    def rename_thread(self, token: str, title: str) -> bool:
        with self._lock:
            item = self._items.get(token)
            title = re.sub(r"\s+", " ", title).strip()
            if item is None or not title:
                return False
            item.title = title[:80]
            item.updated_at = time.time()
            self._save_locked()
            return True

    def set_context(self, token: str, context: dict[str, Any], *, model: str = "") -> None:
        with self._lock:
            item = self._items.setdefault(token, _Conversation())
            item.context = dict(context)
            if model:
                item.model = model
            item.updated_at = time.time()
            self._save_locked()

    def mark_unread(self, token: str, unread: bool = True) -> None:
        with self._lock:
            item = self._items.get(token)
            if item:
                item.unread = unread
                self._save_locked()

    def delete_thread(self, token: str) -> bool:
        with self._lock:
            removed = self._items.pop(token, None) is not None
            if removed:
                self._save_locked()
            return removed

    def clear(self, token: str) -> None:
        with self._lock:
            item = self._items.get(token)
            if item is None:
                self._items[token] = _Conversation()
            else:
                item.messages = []
                item.title = "New conversation"
                item.unread = False
                item.updated_at = time.time()
            self._save_locked()

    def clear_all_threads(self) -> None:
        with self._lock:
            self._items.clear()
            self._save_locked()

    def set_persistence_enabled(self, enabled: bool) -> None:
        with self._lock:
            self.persistence_enabled = bool(enabled)
            if enabled:
                self._save_locked()

    def _expire_locked(self) -> None:
        if not self.ttl_seconds:
            return
        cutoff = time.time() - self.ttl_seconds
        for token in [key for key, item in self._items.items() if item.updated_at < cutoff]:
            self._items.pop(token, None)

    def _load(self) -> None:
        if not self.persistence_enabled or self.storage_path is None or not self.storage_path.exists():
            return
        try:
            payload = json.loads(self.storage_path.read_text(encoding="utf-8"))
            threads = payload.get("threads", {}) if isinstance(payload, dict) else {}
            for token, raw in threads.items():
                if isinstance(raw, dict):
                    allowed = {key: raw[key] for key in _Conversation.__dataclass_fields__ if key in raw}
                    self._items[str(token)] = _Conversation(**allowed)
            self._expire_locked()
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            self._items = {}

    def _save_locked(self) -> None:
        if not self.persistence_enabled or self.storage_path is None:
            return
        temporary = self.storage_path.with_name(f".{self.storage_path.name}.{uuid.uuid4().hex}.tmp")
        try:
            self.storage_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": 1,
                "threads": {token: asdict(item) for token, item in self._items.items()},
            }
            temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
            os.replace(temporary, self.storage_path)
        except OSError:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


CONVERSATIONS = ConversationStore()
